"""Tests for Milestone M3: graph propagation, calibration, uses conflicts, and 4-stage escalation."""

from __future__ import annotations

import math

import pytest

from metacore.engine import MetaCoreEngine
from metacore.models import EdgeCreate, EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def test_goal_evaluation_stages_2_3_4_and_custom_thresholds(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    bag = repo.create_entity(EntityCreate(name="Körnerbrut", kind="worldline"))
    t0 = 1_795_000_000_000
    t_deadline = t0 + 20 * 86_400 * 1000

    # Predicted mass: 700 ± 40 g, predicted time: t0 + 18 d ± 1.0 d -> well inside goal bounds -> Stage 2
    pred = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="predicted",
            source="derived",
            property_name="mass",
            label="Erwartete Brut",
            value_num=700.0,
            value_sigma=40.0,
            value_unit="g",
            time_ms=t0 + 18 * 86_400 * 1000,
            sigma_seconds=1.0 * 86_400,
        )
    )
    goal = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="desired",
            source="user_goal",
            property_name="mass",
            label="Mindestens 600 g bis Tag 20",
            value_unit="g",
            target_min=600.0,
            target_latest_ms=t_deadline,
            threshold_ok=0.80,
            threshold_fail=0.20,
        )
    )
    ev2 = engine.evaluate_goal(goal.id)
    assert ev2.stage == 2
    assert ev2.status == "on_track"
    assert ev2.goal_probability >= 0.80
    assert ev2.broken_dimensions == []

    # Shift predicted time right onto deadline -> ~50% probability -> Stage 3
    repo.update_state(pred.id, time_ms=t_deadline, sigma_seconds=2.0 * 86_400)
    ev3 = engine.evaluate_goal(goal.id)
    assert ev3.stage == 3
    assert ev3.status == "at_risk"
    assert 0.20 <= ev3.goal_probability < 0.80

    # Drop mass below target and move time past deadline -> Stage 4 with exact broken dimensions
    repo.update_state(
        pred.id,
        value_num=300.0,
        value_sigma=20.0,
        time_ms=t_deadline + 10 * 86_400 * 1000,
        sigma_seconds=1.0 * 86_400,
    )
    ev4 = engine.evaluate_goal(goal.id)
    assert ev4.stage == 4
    assert ev4.status == "broken"
    assert ev4.goal_probability < 0.20
    assert "wie_viel" in ev4.broken_dimensions
    assert "wann" in ev4.broken_dimensions

    # Rescuing via widen_tolerance brings Stage 4 back to Stage 2
    fb_wide = engine.resolve_escalation(
        goal.id,
        "widen_tolerance",
        {"target_min": 250.0, "target_latest_ms": t_deadline + 15 * 86_400 * 1000},
    )
    assert fb_wide.goal_evaluations[0].stage == 2


def test_stage1_trigger_and_vorbedingung_guard(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    habitat = repo.create_entity(
        EntityCreate(name="Nordwiese", kind="worldline", category="pilz", source="derived")
    )
    weather = repo.create_entity(
        EntityCreate(name="Herbstwetter Nordwiese", kind="worldline", category="pilz")
    )

    # Register a seasonal template triggered by >= 35 mm rain
    tpl = repo.create_transformation(
        TransformationCreate(
            name="Schirmling-Herbstprognose",
            modality="template",
            driver="nature",
            formula={"trigger": {"property_name": "rain_mm", "category": "pilz", "min_value": 35.0}},
        )
    )

    fb_low = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="Regenmesser",
            property_name="rain_mm",
            label="Nieselregen 8 mm",
            value_num=8.0,
            value_sigma=1.0,
            value_unit="mm",
            time_ms=1_795_000_000_000,
        )
    )
    assert fb_low.triggered_template_ids == []

    fb_hit = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="Regenmesser",
            property_name="rain_mm",
            label="Herbstregen 40 mm",
            value_num=40.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=1_795_050_000_000,
        )
    )
    assert fb_hit.triggered_template_ids == [tpl.id]

    # Qualitative/environmental guard: soil temp >= +4 °C required for fruiting
    temp_ok = repo.create_state(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Boden +11 °C",
            value_num=11.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=1_795_050_000_000,
        )
    )
    tx_flush = repo.create_transformation(
        TransformationCreate(
            name="Fruktifikation Nordwiese",
            modality="planned",
            driver="nature",
            formula={
                "guards": [
                    {"entity_id": weather.id, "property_name": "temperature_c", "min_value": 4.0}
                ]
            },
        )
    )
    pred_flush = repo.create_state(
        StateCreate(
            entity_id=habitat.id,
            modality="predicted",
            source="derived",
            property_name="presence",
            label="Schirmlinge erwartet",
            value_num=3.0,
            value_sigma=1.0,
            value_unit="stk",
            lat=50.812,
            lon=12.914,
            sigma_meters=140.0,
            time_ms=1_796_000_000_000,
            sigma_seconds=5 * 86_400,
        )
    )
    goal_hab = repo.create_state(
        StateCreate(
            entity_id=habitat.id,
            modality="desired",
            source="user_goal",
            property_name="presence",
            label="Mindestens 1 Schirmling im Gebiet",
            target_min=1.0,
            lat=50.812,
            lon=12.914,
            target_radius_meters=300.0,
        )
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_flush.id, state_id=temp_ok.id, direction="in", role="triggers"
        )
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_flush.id, state_id=pred_flush.id, direction="out", role="produces"
        )
    )

    assert engine.evaluate_goal(goal_hab.id).stage == 2

    # Sudden frost -4 °C breaks vorbedingung -> Stage 4
    fb_frost = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Bodenfrost -4 °C",
            value_num=-4.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=1_795_100_000_000,
        )
    )
    assert fb_frost.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_frost.goal_evaluations[0].broken_dimensions


def test_instantiate_template_and_calibrate_ai_prior(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    charge = repo.create_entity(EntityCreate(name="Weizencharge 400 g", kind="worldline"))
    bag1 = repo.create_entity(
        EntityCreate(name="Beutel #1", kind="worldline", parent_entity_id=charge.id)
    )
    bag2 = repo.create_entity(
        EntityCreate(name="Beutel #2", kind="worldline", parent_entity_id=charge.id)
    )

    dry = repo.create_state(
        StateCreate(
            entity_id=charge.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label="400 g trockener Weizen",
            value_num=400.0,
            value_sigma=0.0,
            value_unit="g",
            time_ms=1_795_000_000_000,
        )
    )

    tpl = repo.create_transformation(
        TransformationCreate(
            name="Einweichen & Aufteilen",
            modality="template",
            driver="actor",
            duration_seconds=3 * 86_400,
            duration_sigma_seconds=3600.0,
            formula={
                "kind": "product_and_split",
                "output_property": "mass",
                "output_unit": "g",
                "factor": {"mean": 1.75, "sigma": 0.10, "source": "ai_prior"},
                "split_parts": 2,
                "split_sigma": 6.0,
            },
        )
    )

    fb_inst = engine.instantiate_template(
        tpl.id,
        input_state_ids=[dry.id],
        target_entity_id=charge.id,
        split_entities=[bag1.id, bag2.id],
    )
    assert len(fb_inst.created_state_ids) == 3
    assert len(fb_inst.open_ai_prior_state_ids) == 1

    pred_total = repo.get_state(fb_inst.created_state_ids[0])
    pred_b1 = repo.get_state(fb_inst.created_state_ids[1])
    assert pred_total is not None and pred_total.value_num == pytest.approx(700.0)
    assert pred_total.value_sigma == pytest.approx(40.0)
    assert pred_total.source == "ai_prior"
    assert pred_b1 is not None and pred_b1.value_num == pytest.approx(350.0)
    assert pred_b1.value_sigma == pytest.approx(math.hypot(20.0, 6.0))

    # Real measurement: drained mass weighed as 680 ± 10 g -> calibrates template factor toward 1.70
    fb_obs = engine.record_observation(
        StateCreate(
            entity_id=charge.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label="Abgetropft gewogen 680 g",
            value_num=680.0,
            value_sigma=10.0,
            value_unit="g",
            time_ms=1_795_000_000_000 + 3 * 86_400 * 1000,
        )
    )
    assert tpl.id in fb_obs.calibrated_template_ids
    assert fb_obs.open_ai_prior_state_ids == []

    cal_tpl = repo.get_transformation(tpl.id)
    assert cal_tpl is not None
    assert cal_tpl.formula["factor"]["source"] == "measured"
    assert 1.70 < cal_tpl.formula["factor"]["mean"] < 1.71
    assert cal_tpl.formula["factor"]["sigma"] < 0.03

    # Downstream split bag prediction shrunk automatically to 340 g ± ~7.81 g
    updated_b1 = repo.get_state(pred_b1.id)
    assert updated_b1 is not None and updated_b1.value_num == pytest.approx(340.0)
    assert updated_b1.value_sigma == pytest.approx(math.hypot(5.0, 6.0))


def test_choice_or_switch_uses_conflict_and_abort_release(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    gripper1 = repo.create_entity(EntityCreate(name="Greifer 1", kind="worldline", category="robotik"))
    glass = repo.create_entity(EntityCreate(name="Glas G1", kind="worldline", category="robotik"))
    door = repo.create_entity(EntityCreate(name="Flurtür T1", kind="worldline", category="robotik"))

    t_now = 1_795_000_000_000
    g_hold = repo.create_state(
        StateCreate(
            entity_id=gripper1.id,
            modality="observed",
            source="measured",
            property_name="occupancy",
            label="Greifer 1 hält Glas",
            value_text="belegt",
            time_ms=t_now,
        )
    )
    door_closed = repo.create_state(
        StateCreate(
            entity_id=door.id,
            modality="observed",
            source="measured",
            property_name="door_state",
            label="Tür T1 geschlossen",
            value_text="geschlossen",
            time_ms=t_now,
        )
    )

    # Two overlapping planned tasks competing for Greifer 1
    tx_deliver = repo.create_transformation(
        TransformationCreate(
            name="Glas zum Couchtisch bringen (Arm 2 öffnet Tür)",
            modality="planned",
            driver="actor",
            duration_seconds=84.0,
            duration_sigma_seconds=12.0,
            cost=1.0,
        )
    )
    tx_other = repo.create_transformation(
        TransformationCreate(
            name="Kaffeetasse greifen",
            modality="planned",
            driver="actor",
            duration_seconds=40.0,
            duration_sigma_seconds=5.0,
            meta={"scheduled_start_ms": t_now + 10_000},
        )
    )

    repo.create_edge(
        EdgeCreate(transformation_id=tx_deliver.id, state_id=g_hold.id, direction="in", role="uses")
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_other.id, state_id=g_hold.id, direction="in", role="uses")
    )

    # Second CHOICE_OR branch via Sideboard (slower, higher time sigma)
    tx_sideboard = repo.create_transformation(
        TransformationCreate(
            name="Glas auf Sideboard → Tür mit Greifer 1 öffnen",
            modality="planned",
            driver="actor",
            duration_seconds=106.0,
            duration_sigma_seconds=15.0,
            cost=2.5,
            formula={
                "guards": [
                    {
                        "entity_id": door.id,
                        "property_name": "door_state",
                        "forbidden_texts": ["verriegelt"],
                    }
                ]
            },
        )
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_sideboard.id, state_id=door_closed.id, direction="in", role="triggers"
        )
    )

    pred_arm2 = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="predicted",
            source="derived",
            property_name="location",
            label="Ankunft Arm 2 (+84 s)",
            location_label="Couchtisch",
            time_ms=t_now + 84_000,
            sigma_seconds=12.0,
        )
    )
    pred_side = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="predicted",
            source="derived",
            property_name="location",
            label="Ankunft Sideboard (+106 s)",
            location_label="Couchtisch",
            time_ms=t_now + 106_000,
            sigma_seconds=15.0,
        )
    )
    goal = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="desired",
            source="user_goal",
            property_name="location",
            label="Glas am Couchtisch in ≤ 120 s",
            location_label="Couchtisch",
            target_latest_ms=t_now + 120_000,
        )
    )

    e_arm2 = repo.create_edge(
        EdgeCreate(
            transformation_id=tx_deliver.id,
            state_id=pred_arm2.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="arm2",
            is_selected=True,
        )
    )
    e_side = repo.create_edge(
        EdgeCreate(
            transformation_id=tx_sideboard.id,
            state_id=pred_side.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="sideboard",
            is_selected=False,
        )
    )

    conflicts = engine.detect_uses_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].entity_id == gripper1.id

    ev = engine.evaluate_goal(goal.id)
    assert ev.stage == 2
    assert len(ev.alternative_branches) == 2

    # Switch branch from arm2 -> sideboard
    fb_sw = engine.resolve_escalation(goal.id, "switch_branch", {"branch_key": "sideboard"})
    assert fb_sw.goal_evaluations[0].predicted_state_id == pred_side.id
    assert repo.get_edge(e_arm2.id).outcome == "not_chosen"  # type: ignore[union-attr]
    assert repo.get_edge(e_side.id).outcome == "chosen"  # type: ignore[union-attr]

    # Abort goal -> cancels goal/planned txs and releases Greifer 1 `uses` conflict
    fb_abort = engine.resolve_escalation(goal.id, "abort_goal")
    assert fb_abort.uses_conflicts == []
    assert fb_abort.goal_evaluations[0].status == "cancelled"
    uses_edges = repo.list_edges(transformation_id=tx_deliver.id, role="uses")
    assert uses_edges[0].resource_released is True
