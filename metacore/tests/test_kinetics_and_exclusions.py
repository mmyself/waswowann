"""Tests for Package 2: decay_step, cumulative_balance, and forbid_coexistence guards."""

from __future__ import annotations

import math

import pytest

from metacore.engine import MetaCoreEngine
from metacore.math_fusion import (
    GaussianValue,
    propagate_cumulative_balance,
    propagate_exponential_decay,
)
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def test_exponential_decay_and_cumulative_balance_math() -> None:
    # Half-life tau = 3600 / ln(2) => after 3600 s, value sits halfway between initial and equilibrium
    hl_s = 3600.0
    tau_s = hl_s / math.log(2.0)
    res = propagate_exponential_decay(
        initial=GaussianValue(80.0, 2.0, unit="%"),
        equilibrium=GaussianValue(40.0, 1.0, unit="%"),
        dt_seconds=hl_s,
        tau=GaussianValue(tau_s, tau_s * 0.1, source="ai_prior"),
        dt_sigma_seconds=60.0,
    )
    assert res.mean == pytest.approx(60.0, rel=1e-4)
    assert res.sigma > 1.0
    assert res.source == "ai_prior"
    assert res.unit == "%"

    # Temperature cooling from 60 °C toward 293.15 K (20 °C) over 1 tau -> ~34.715 °C
    cool = propagate_exponential_decay(
        initial=GaussianValue(60.0, 0.5, unit="°C"),
        equilibrium=GaussianValue(293.15, 0.2, unit="K"),
        dt_seconds=1800.0,
        tau=GaussianValue(1800.0, 100.0, source="derived"),
    )
    assert cool.unit == "°C"
    assert cool.mean == pytest.approx(20.0 + 40.0 * math.exp(-1.0), rel=1e-4)

    # Cumulative balance across compatible units with min/max clamps
    bal = propagate_cumulative_balance(
        flows=[
            (GaussianValue(2.0, 0.1, unit="l"), 1.0),
            (GaussianValue(500.0, 20.0, unit="ml"), -1.0),
        ],
        initial=GaussianValue(1.5, 0.05, unit="l"),
        min_clamp=0.0,
        max_clamp=5.0,
        target_unit="l",
    )
    assert bal.unit == "l"
    assert bal.mean == pytest.approx(3.0)


def test_engine_decay_step_downstream_and_tau_autocalibration(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    pot = repo.create_entity(EntityCreate(name="Blumentopf_Ost", kind="worldline", category="pflanze"))
    t0 = 1_796_200_000_000

    moisture_0 = repo.create_state(
        StateCreate(
            entity_id=pot.id,
            modality="observed",
            source="measured",
            measurement_source="sensor_soil",
            property_name="soil_moisture",
            label="Frisch gegossen 80 %",
            value_num=80.0,
            value_sigma=1.5,
            value_unit="%",
            time_ms=t0,
        )
    )

    dry_tpl = repo.create_transformation(
        TransformationCreate(
            name="Abtrocknen im Topf",
            modality="template",
            driver="nature",
            duration_seconds=86_400.0,
            duration_sigma_seconds=1800.0,
            duration_source="measured",
            formula={
                "kind": "decay_step",
                "output_property": "soil_moisture",
                "output_unit": "%",
                "equilibrium": {"mean": 20.0, "sigma": 1.0, "unit": "%"},
                "tau_seconds": {"mean": 86_400.0, "sigma": 12_000.0, "source": "ai_prior"},
            },
        )
    )
    inst = engine.instantiate_template(
        dry_tpl.id,
        input_state_ids=[moisture_0.id],
        target_entity_id=pot.id,
        start_time_ms=t0,
    )
    pred_id = inst.created_state_ids[0]
    pred = repo.get_state(pred_id)
    assert pred is not None
    # At dt == tau: 20 + 60 * e^-1 = 42.0728 %
    assert pred.value_num == pytest.approx(20.0 + 60.0 * math.exp(-1.0), rel=1e-3)
    assert pred.source == "ai_prior"

    # Real sensor measurement shows faster drying: at 24 h moisture dropped to 31.0 %
    # Implied tau: -86400 / ln((31-20)/(80-20)) = 50929 s
    obs = engine.record_observation(
        StateCreate(
            entity_id=pot.id,
            modality="observed",
            source="measured",
            measurement_source="sensor_soil",
            property_name="soil_moisture",
            label="Nach 24 h 31 %",
            value_num=31.0,
            value_sigma=1.0,
            value_unit="%",
            time_ms=t0 + 86_400_000,
        )
    )
    assert dry_tpl.id in obs.calibrated_template_ids
    updated_tpl = repo.get_transformation(dry_tpl.id)
    assert updated_tpl is not None
    tau_cal = updated_tpl.formula["tau_seconds"]
    assert tau_cal["source"] == "measured"
    assert tau_cal["mean"] < 70_000.0


def test_engine_cumulative_balance_window_and_downstream(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    barrel = repo.create_entity(EntityCreate(name="Regentonne_Sued", kind="worldline", category="wasser"))
    t0 = 1_796_300_000_000

    init_level = repo.create_state(
        StateCreate(
            entity_id=barrel.id,
            modality="observed",
            source="measured",
            property_name="water_volume",
            label="Anfangsbestand 12 l",
            value_num=12.0,
            value_sigma=0.2,
            value_unit="l",
            time_ms=t0,
        )
    )
    # Inflow & outflow within a 6-hour rolling window
    repo.create_state(
        StateCreate(
            entity_id=barrel.id,
            modality="observed",
            source="measured",
            property_name="rain_inflow",
            label="Regen +4.5 l",
            value_num=4.5,
            value_sigma=0.2,
            value_unit="l",
            time_ms=t0 + 3_600_000,
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=barrel.id,
            modality="observed",
            source="measured",
            property_name="pump_outflow",
            label="Gießkanne -1500 ml",
            value_num=1500.0,
            value_sigma=50.0,
            value_unit="ml",
            time_ms=t0 + 7_200_000,
        )
    )
    # Old inflow outside 6 h window -> must be excluded
    repo.create_state(
        StateCreate(
            entity_id=barrel.id,
            modality="observed",
            source="measured",
            property_name="rain_inflow",
            label="Alter Regen +99 l",
            value_num=99.0,
            value_sigma=1.0,
            value_unit="l",
            time_ms=t0 - 10 * 3_600_000,
        )
    )

    bal_tpl = repo.create_transformation(
        TransformationCreate(
            name="Speicherbilanz 6h",
            modality="template",
            driver="nature",
            duration_seconds=3 * 3_600.0,
            duration_source="measured",
            formula={
                "kind": "cumulative_balance",
                "output_property": "water_volume",
                "output_unit": "l",
                "window_seconds": 6 * 3_600.0,
                "min_clamp": 0.0,
                "max_clamp": 50.0,
                "inflows": [{"property_name": "rain_inflow", "weight": 1.0}],
                "outflows": [{"property_name": "pump_outflow", "weight": 1.0}],
            },
        )
    )
    fb = engine.instantiate_template(
        bal_tpl.id,
        input_state_ids=[init_level.id],
        target_entity_id=barrel.id,
        start_time_ms=t0,
    )
    pred = repo.get_state(fb.created_state_ids[0])
    assert pred is not None
    # 12 l + 4.5 l - 1.5 l = 15.0 l (99 l from 10h earlier excluded)
    assert pred.value_unit == "l"
    assert pred.value_num == pytest.approx(15.0)


def test_forbid_coexistence_guard_and_spatial_proximity(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    pen = repo.create_entity(EntityCreate(name="Aussengehege", kind="worldline", category="gehege"))
    rabbit = repo.create_entity(EntityCreate(name="Häschen_Maja", kind="worldline", category="tier"))
    predator = repo.create_entity(EntityCreate(name="Fuchs_Alarm", kind="worldline", category="tier"))
    t0 = 1_796_400_000_000

    door = repo.create_state(
        StateCreate(
            entity_id=pen.id,
            modality="observed",
            source="measured",
            property_name="door_state",
            label="Gehegetür offen",
            value_text="open",
            distribution_kind="discrete",
            lat=51.0000,
            lon=13.0000,
            sigma_meters=3.0,
            time_ms=t0,
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            property_name="zone",
            label="Häschen im Garten",
            value_text="outside",
            distribution_kind="discrete",
            lat=51.0001,
            lon=13.0001,
            sigma_meters=4.0,
            time_ms=t0,
        )
    )
    # Far-away predator (> 500 m) does not violate 40 m spatial coexistence guard
    repo.create_state(
        StateCreate(
            entity_id=predator.id,
            modality="observed",
            source="measured",
            property_name="alert",
            label="Fuchs 800m entfernt",
            value_text="present",
            distribution_kind="discrete",
            lat=51.0080,
            lon=13.0000,
            sigma_meters=5.0,
            time_ms=t0,
        )
    )

    safe_tpl = repo.create_transformation(
        TransformationCreate(
            name="Freilauf-Freigabe",
            modality="template",
            driver="actor",
            duration_seconds=3600.0,
            duration_source="measured",
            formula={
                "kind": "duration_step",
                "output_property": "outdoor_access",
                "output_text": "freigegeben",
                "guards": [
                    {
                        "kind": "forbid_coexistence",
                        "same_location_radius_meters": 40.0,
                        "items": [
                            {"entity_id": rabbit.id, "property_name": "zone", "required_text": "outside"},
                            {"entity_id": predator.id, "property_name": "alert", "required_text": "present"},
                        ],
                    }
                ],
            },
        )
    )
    engine.instantiate_template(
        safe_tpl.id,
        input_state_ids=[door.id],
        target_entity_id=pen.id,
        start_time_ms=t0,
    )
    goal = repo.create_state(
        StateCreate(
            entity_id=pen.id,
            modality="desired",
            source="user_goal",
            property_name="outdoor_access",
            label="Freilauf sicher",
            target_value_text="freigegeben",
        )
    )
    ev_far = engine.evaluate_goal(goal.id)
    assert ev_far.stage == 2
    assert ev_far.dimension_probs["vorbedingung"] > 0.99

    # Now predator appears right next to the rabbit (< 15 m) -> Stage 4 escalation!
    fb_near = engine.record_observation(
        StateCreate(
            entity_id=predator.id,
            modality="observed",
            source="measured",
            property_name="alert",
            label="Fuchs direkt am Zaun",
            value_text="present",
            distribution_kind="discrete",
            lat=51.00012,
            lon=13.00012,
            sigma_meters=3.0,
            time_ms=t0 + 60_000,
        )
    )
    ev_near = next(e for e in fb_near.goal_evaluations if e.goal_id == goal.id)
    assert ev_near.stage == 4
    assert "vorbedingung" in ev_near.broken_dimensions
    assert ev_near.dimension_probs["vorbedingung"] < 0.1

    # Non-spatial 3-item exclusion: only 1 of 3 modes may be active at once
    e1 = repo.create_entity(EntityCreate(name="Mode_A", kind="worldline"))
    e2 = repo.create_entity(EntityCreate(name="Mode_B", kind="worldline"))
    e3 = repo.create_entity(EntityCreate(name="Mode_C", kind="worldline"))
    repo.create_state(
        StateCreate(
            entity_id=e1.id, modality="observed", source="measured",
            property_name="active", label="A on", value_text="yes", distribution_kind="discrete",
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=e2.id, modality="observed", source="measured",
            property_name="active", label="B off", value_text="no", distribution_kind="discrete",
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=e3.id, modality="observed", source="measured",
            property_name="active", label="C off", value_text="no", distribution_kind="discrete",
        )
    )
    p_single = engine._evaluate_condition(
        {
            "kind": "forbid_coexistence",
            "items": [
                {"entity_id": e1.id, "property_name": "active", "required_text": "yes"},
                {"entity_id": e2.id, "property_name": "active", "required_text": "yes"},
                {"entity_id": e3.id, "property_name": "active", "required_text": "yes"},
            ],
        },
        in_states=(),
    )
    assert p_single == pytest.approx(1.0)
    repo.create_state(
        StateCreate(
            entity_id=e2.id, modality="observed", source="measured",
            property_name="active", label="B on", value_text="yes", distribution_kind="discrete",
        )
    )
    p_two = engine._evaluate_condition(
        {
            "kind": "forbid_coexistence",
            "items": [
                {"entity_id": e1.id, "property_name": "active", "required_text": "yes"},
                {"entity_id": e2.id, "property_name": "active", "required_text": "yes"},
                {"entity_id": e3.id, "property_name": "active", "required_text": "yes"},
            ],
        },
        in_states=(),
    )
    assert p_two == pytest.approx(0.0)
