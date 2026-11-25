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


def test_multi_condition_windowed_triggers_and_or(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    weather = repo.create_entity(EntityCreate(name="Wetterstation", kind="worldline", category="pilz"))
    t0 = 1_795_000_000_000

    tpl_and = repo.create_transformation(
        TransformationCreate(
            name="Pilz Multi-Trigger (Regen + Abkühlung)",
            modality="template",
            driver="nature",
            formula={
                "triggers": {
                    "logic": "AND",
                    "category": "pilz",
                    "conditions": [
                        {
                            "entity_id": weather.id,
                            "property_name": "rain_mm",
                            "aggregation": "sum",
                            "window_seconds": 14 * 86_400,
                            "min_value": 35.0,
                        },
                        {
                            "entity_id": weather.id,
                            "property_name": "temperature_c",
                            "aggregation": "mean",
                            "window_seconds": 14 * 86_400,
                            "min_value": 8.0,
                            "max_value": 20.0,
                        },
                        {
                            "entity_id": weather.id,
                            "property_name": "temperature_c",
                            "aggregation": "delta",
                            "window_seconds": 14 * 86_400,
                            "max_value": -3.0,
                        },
                    ],
                }
            },
        )
    )
    tpl_or = repo.create_transformation(
        TransformationCreate(
            name="Pilz OR-Trigger (Starkregen oder hohe Luftfeuchte)",
            modality="template",
            driver="nature",
            formula={
                "triggers": {
                    "logic": "OR",
                    "category": "pilz",
                    "conditions": [
                        {"entity_id": weather.id, "property_name": "rain_mm", "min_value": 50.0},
                        {"entity_id": weather.id, "property_name": "humidity_pct", "min_value": 90.0},
                    ],
                }
            },
        )
    )

    # Warm spell without cooling (18 -> 19 °C): AND trigger must NOT fire even with 40 mm rain
    repo.create_state(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Temp -10 d (18 °C)",
            value_num=18.0,
            value_sigma=0.3,
            value_unit="°C",
            time_ms=t0 - 10 * 86_400_000,
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Temp -1 d (19 °C)",
            value_num=19.0,
            value_sigma=0.3,
            value_unit="°C",
            time_ms=t0 - 86_400_000,
        )
    )
    fb_no = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="rain_mm",
            label="Regen ohne Kältereiz (40 mm)",
            value_num=40.0,
            value_sigma=1.0,
            value_unit="mm",
            time_ms=t0,
        )
    )
    assert tpl_and.id not in fb_no.triggered_template_ids
    assert tpl_or.id not in fb_no.triggered_template_ids

    # Cooling arrives (-7 °C drop vs 18 °C baseline) + OR humidity >= 90%
    repo.create_state(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Abkühlung (11 °C)",
            value_num=11.0,
            value_sigma=0.3,
            value_unit="°C",
            time_ms=t0 + 86_400_000,
        )
    )
    fb_and = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="rain_mm",
            label="Folgeregen 10 mm",
            value_num=10.0,
            value_sigma=1.0,
            value_unit="mm",
            time_ms=t0 + 2 * 86_400_000,
        )
    )
    assert tpl_and.id in fb_and.triggered_template_ids

    fb_or = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="humidity_pct",
            label="Luftfeuchte 94 %",
            value_num=94.0,
            value_sigma=1.0,
            value_unit="%",
            time_ms=t0 + 2 * 86_400_000,
        )
    )
    assert tpl_or.id in fb_or.triggered_template_ids


def test_recipe_mix_weighted_sum_limiting_factor_ratios_and_modifiers(
    repo: MetaCoreRepository,
) -> None:
    engine = MetaCoreEngine(repo)
    kitchen = repo.create_entity(EntityCreate(name="Teigschüssel", kind="worldline", category="rezept"))
    t0 = 1_795_000_000_000

    # 1. Weighted sum + ratio guard + output & duration modifiers at cool temperature (14 °C)
    flour = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="flour_mass",
            label="Mehl 500 g",
            value_num=500.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    water = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="water_mass",
            label="Wasser 300 g",
            value_num=300.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    temp = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Raumtemperatur 14 °C",
            value_num=14.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t0,
        )
    )
    tpl_mix = repo.create_transformation(
        TransformationCreate(
            name="Brotteig ansetzen",
            modality="template",
            driver="actor",
            duration_seconds=3600.0,
            duration_sigma_seconds=120.0,
            duration_source="ai_prior",
            formula={
                "kind": "recipe_mix",
                "mode": "weighted_sum",
                "output_property": "dough_mass",
                "output_unit": "g",
                "ingredients": [
                    {"property_name": "flour_mass", "weight": 1.0, "role": "consumes"},
                    {"property_name": "water_mass", "weight": 1.0, "role": "consumes"},
                ],
                "input_roles": {"temperature_c": "triggers"},
                "ratio_guards": [
                    {
                        "kind": "ratio",
                        "numerator_entity_id": kitchen.id,
                        "numerator_property": "water_mass",
                        "denominator_entity_id": kitchen.id,
                        "denominator_property": "flour_mass",
                        "min_ratio": 0.55,
                        "max_ratio": 0.70,
                    }
                ],
                "modifiers": [
                    {
                        "entity_id": kitchen.id,
                        "property_name": "temperature_c",
                        "applies_to": "output",
                        "optimal_min": 18.0,
                        "optimal_max": 24.0,
                        "falloff_per_unit": 0.05,
                        "min_factor": 0.5,
                    },
                    {
                        "entity_id": kitchen.id,
                        "property_name": "temperature_c",
                        "applies_to": "duration",
                        "optimal_min": 18.0,
                        "optimal_max": 24.0,
                        "falloff_per_unit": 0.05,
                        "min_factor": 0.5,
                    },
                ],
            },
        )
    )
    fb_mix = engine.instantiate_template(
        tpl_mix.id,
        input_state_ids=[flour.id, water.id, temp.id],
        target_entity_id=kitchen.id,
        start_time_ms=t0,
    )
    pred_mix = repo.get_state(fb_mix.created_state_ids[0])
    assert pred_mix is not None
    # Raw sum = 800 g; temp=14 °C (4 units below 18 °C * 0.05 = 0.20 falloff -> factor 0.80) -> 640 g
    assert pred_mix.value_num == pytest.approx(640.0)
    # Duration scaled by 1 / 0.80 = 4500 s -> t0 + 4_500_000 ms
    assert pred_mix.time_ms == t0 + 4_500_000

    goal_dough = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="desired",
            source="user_goal",
            property_name="dough_mass",
            label="Teig >= 500 g mit passender Hydration",
            target_min=500.0,
        )
    )
    assert engine.evaluate_goal(goal_dough.id).stage == 2

    # Dry hydration ratio (200 / 500 = 0.40 < 0.55) breaks vorbedingung -> Stage 4
    fb_dry = engine.record_observation(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="water_mass",
            label="Nur 200 g Wasser",
            value_num=200.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=t0 + 1000,
        )
    )
    assert fb_dry.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_dry.goal_evaluations[0].broken_dimensions

    # 2. Limiting-factor mode detects bottleneck ingredient
    sugar = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="sugar_mass",
            label="Zucker 100 g",
            value_num=100.0,
            value_sigma=2.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    butter = repo.create_state(
        StateCreate(
            entity_id=kitchen.id,
            modality="observed",
            source="measured",
            property_name="butter_mass",
            label="Butter 200 g",
            value_num=200.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    tpl_limit = repo.create_transformation(
        TransformationCreate(
            name="Mürbeteig-Portionen (Limitierung)",
            modality="template",
            driver="actor",
            formula={
                "kind": "recipe_mix",
                "mode": "limiting_factor",
                "output_property": "portions",
                "output_unit": "stk",
                "unit_output_mean": 4.0,
                "ingredients": [
                    {"property_name": "flour_mass", "per_unit": 200.0, "role": "consumes"},  # 2.5 units
                    {"property_name": "butter_mass", "per_unit": 100.0, "role": "consumes"},  # 2.0 units (Engpass)
                    {"property_name": "sugar_mass", "per_unit": 25.0, "role": "consumes"},  # 4.0 units
                ],
            },
        )
    )
    fb_limit = engine.instantiate_template(
        tpl_limit.id,
        input_state_ids=[flour.id, butter.id, sugar.id],
        target_entity_id=kitchen.id,
        start_time_ms=t0,
    )
    pred_limit = repo.get_state(fb_limit.created_state_ids[0])
    assert pred_limit is not None
    assert pred_limit.value_num == pytest.approx(8.0)
    assert pred_limit.meta["limiting_ingredient"] == "butter_mass"


def test_live_duration_modifier_reapplies_unless_measured(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    fungus = repo.create_entity(EntityCreate(name="Hericium Test", kind="worldline", category="Pilz"))
    box = repo.create_entity(EntityCreate(name="Box Test", kind="worldline", category="umgebung"))
    t0 = 1_795_000_000_000

    pins = repo.create_state(
        StateCreate(
            entity_id=fungus.id,
            modality="observed",
            source="measured",
            property_name="growth_g",
            label="Primordien",
            value_num=10.0,
            value_sigma=1.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    temp0 = repo.create_state(
        StateCreate(
            entity_id=box.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Start 16 °C",
            value_num=16.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t0,
        )
    )
    tpl = repo.create_transformation(
        TransformationCreate(
            name="Wachstum",
            modality="template",
            driver="nature",
            duration_seconds=10 * 86_400,
            duration_sigma_seconds=1 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "output_property": "growth_g",
                "output_value": 100.0,
                "output_sigma": 5.0,
                "input_roles": {"growth_g": "consumes", "temperature_c": "triggers"},
                "modifiers": [
                    {
                        "entity_id": box.id,
                        "property_name": "temperature_c",
                        "applies_to": "duration",
                        "optimal_min": 18.0,
                        "optimal_max": 22.0,
                        "falloff_per_unit": 0.05,
                        "min_factor": 0.5,
                        "sigma_penalty_per_unit": 0.0,
                    }
                ],
            },
        )
    )

    fb_init = engine.instantiate_template(
        tpl.id,
        input_state_ids=[pins.id, temp0.id],
        target_entity_id=fungus.id,
        start_time_ms=t0,
    )
    pred_id = fb_init.created_state_ids[0]
    planned_tx = repo.list_transformations(modality="planned")[0]
    assert planned_tx.duration_seconds == pytest.approx(10 * 86_400 / 0.90)
    pred_init = repo.get_state(pred_id)
    assert pred_init is not None
    assert pred_init.time_ms == t0 + int((10 * 86_400 / 0.90) * 1000)

    # Raising temperature into optimal [18, 22] restores base 10 d duration live
    fb_warm = engine.record_observation(
        StateCreate(
            entity_id=box.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Warm 18 °C",
            value_num=18.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t0 + 2 * 86_400_000,
        )
    )
    assert pred_id in fb_warm.updated_state_ids
    planned_warm = repo.get_transformation(planned_tx.id)
    pred_warm = repo.get_state(pred_id)
    assert planned_warm is not None and planned_warm.duration_seconds == pytest.approx(10 * 86_400)
    assert pred_warm is not None and pred_warm.time_ms == t0 + 10 * 86_400_000

    # Harvest calibrates template -> duration_source='measured'; later temperature changes do NOT overwrite
    fb_harvest = engine.record_observation(
        StateCreate(
            entity_id=fungus.id,
            modality="observed",
            source="measured",
            property_name="growth_g",
            label="Ernte",
            value_num=100.0,
            value_sigma=2.0,
            value_unit="g",
            time_ms=t0 + 9 * 86_400_000,
        )
    )
    assert tpl.id in fb_harvest.calibrated_template_ids

    fb_cold = engine.record_observation(
        StateCreate(
            entity_id=box.id,
            modality="observed",
            source="measured",
            property_name="temperature_c",
            label="Später 12 °C",
            value_num=12.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t0 + 9 * 86_400_000 + 60_000,
        )
    )
    assert pred_id not in fb_cold.updated_state_ids
    planned_after = repo.get_transformation(planned_tx.id)
    assert planned_after is not None and planned_after.duration_seconds == pytest.approx(10 * 86_400)


def test_nested_composite_template_rollup_guards_and_chain_cascade(
    repo: MetaCoreRepository,
) -> None:
    engine = MetaCoreEngine(repo)
    batch = repo.create_entity(EntityCreate(name="Rezept-Testcharge", kind="worldline", category="rezept"))
    oven = repo.create_entity(EntityCreate(name="Ofen", kind="worldline", category="rezept"))
    t0 = 1_795_000_000_000

    raw = repo.create_state(
        StateCreate(
            entity_id=batch.id,
            modality="observed",
            source="measured",
            property_name="raw_g",
            label="200 g Rohmasse",
            value_num=200.0,
            value_sigma=2.0,
            value_unit="g",
            time_ms=t0,
        )
    )
    oven_slot = repo.create_state(
        StateCreate(
            entity_id=oven.id,
            modality="observed",
            source="measured",
            property_name="occupancy",
            label="Ofen bereit",
            value_text="bereit",
            time_ms=t0,
        )
    )
    oven_temp = repo.create_state(
        StateCreate(
            entity_id=oven.id,
            modality="observed",
            source="measured",
            property_name="oven_temp_c",
            label="Ofen 190 °C",
            value_num=190.0,
            value_sigma=2.0,
            value_unit="°C",
            time_ms=t0,
        )
    )

    tpl_macro = repo.create_transformation(
        TransformationCreate(
            name="Makro-Zubereitung",
            modality="template",
            driver="actor",
            duration_seconds=3 * 86_400,
            duration_sigma_seconds=0.3 * 86_400,
            formula={
                "kind": "duration_step",
                "input_roles": {"raw_g": "consumes", "occupancy": "triggers", "oven_temp_c": "triggers"},
            },
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="1.1 Vorheizen",
            modality="template",
            driver="actor",
            parent_id=tpl_macro.id,
            duration_seconds=1 * 86_400,
            duration_sigma_seconds=0.1 * 86_400,
            formula={
                "kind": "product_and_split",
                "output_property": "heated_g",
                "output_unit": "g",
                "factor": {"mean": 1.5, "sigma": 0.05, "source": "ai_prior"},
                "split_parts": 1,
            },
            meta={"step_order": 1},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="1.2 Garen",
            modality="template",
            driver="actor",
            parent_id=tpl_macro.id,
            duration_seconds=2 * 86_400,
            duration_sigma_seconds=0.2 * 86_400,
            formula={
                "kind": "duration_step",
                "inherit_input_value": True,
                "output_property": "cooked_g",
                "output_unit": "g",
                "input_roles": {"heated_g": "consumes", "occupancy": "uses", "oven_temp_c": "triggers"},
                "guards": [{"entity_id": oven.id, "property_name": "oven_temp_c", "min_value": 180.0}],
            },
            meta={"step_order": 2},
        )
    )
    tpl_pack = repo.create_transformation(
        TransformationCreate(
            name="Verpacken",
            modality="template",
            driver="actor",
            duration_seconds=1 * 86_400,
            duration_sigma_seconds=0.1 * 86_400,
            formula={
                "kind": "duration_step",
                "inherit_input_value": True,
                "scale_mean": 0.9,
                "scale_sigma": 0.0,
                "output_property": "final_g",
                "output_unit": "g",
            },
        )
    )

    fb_macro = engine.instantiate_template(
        tpl_macro.id,
        input_state_ids=[raw.id, oven_slot.id, oven_temp.id],
        target_entity_id=batch.id,
        start_time_ms=t0,
    )
    cooked_id = fb_macro.created_state_ids[0]
    fb_pack = engine.instantiate_template(
        tpl_pack.id,
        input_state_ids=[cooked_id],
        target_entity_id=batch.id,
    )
    final_id = fb_pack.created_state_ids[0]
    goal = repo.create_state(
        StateCreate(
            entity_id=batch.id,
            modality="desired",
            source="user_goal",
            property_name="final_g",
            label="Mindestens 200 g bis Tag 6",
            value_unit="g",
            target_min=200.0,
            target_latest_ms=t0 + 6 * 86_400_000,
        )
    )

    planned_txs = repo.list_transformations(modality="planned")
    parent_tx = next(t for t in planned_txs if t.template_id == tpl_macro.id)
    children = [t for t in planned_txs if t.parent_id == parent_tx.id]
    assert len(children) == 2
    assert parent_tx.duration_seconds == pytest.approx(3 * 86_400)
    assert parent_tx.duration_sigma_seconds == pytest.approx(math.hypot(0.1 * 86_400, 0.2 * 86_400))

    heated_states = repo.list_states(entity_id=batch.id, property_name="heated_g")
    assert len(heated_states) == 1
    assert heated_states[0].meta.get("internal_to_parent_tx_id") == parent_tx.id
    assert engine.evaluate_goal(goal.id).stage == 2

    # Failing nested guard (140 °C < 180 °C) rolls up and breaks downstream goal -> Stage 4
    fb_cold = engine.record_observation(
        StateCreate(
            entity_id=oven.id,
            modality="observed",
            source="measured",
            property_name="oven_temp_c",
            label="Ofen zu kalt (140 °C)",
            value_num=140.0,
            value_sigma=2.0,
            value_unit="°C",
            time_ms=t0 + 3600_000,
        )
    )
    assert fb_cold.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_cold.goal_evaluations[0].broken_dimensions

    engine.record_observation(
        StateCreate(
            entity_id=oven.id,
            modality="observed",
            source="measured",
            property_name="oven_temp_c",
            label="Ofen wieder heiß (190 °C)",
            value_num=190.0,
            value_sigma=2.0,
            value_unit="°C",
            time_ms=t0 + 7200_000,
        )
    )
    assert engine.evaluate_goal(goal.id).stage == 2

    # Downstream cascade: delayed cooked_g at +7 d shifts final_g to +8 d -> wann breaks goal
    fb_delay = engine.record_observation(
        StateCreate(
            entity_id=batch.id,
            modality="observed",
            source="measured",
            property_name="cooked_g",
            label="Garen fertig (260 g nach 7 d)",
            value_num=260.0,
            value_sigma=4.0,
            value_unit="g",
            time_ms=t0 + 7 * 86_400_000,
            sigma_seconds=600.0,
        )
    )
    assert final_id in fb_delay.updated_state_ids
    final_st = repo.get_state(final_id)
    assert final_st is not None
    assert final_st.time_ms == t0 + 8 * 86_400_000
    assert final_st.value_num == pytest.approx(234.0)
    assert fb_delay.goal_evaluations[0].stage == 4
    assert "wann" in fb_delay.goal_evaluations[0].broken_dimensions

    # Abort goal propagates downward to nested children and releases their uses edges
    fb_abort = engine.resolve_escalation(goal.id, "abort_goal")
    assert fb_abort.goal_evaluations[0].status == "cancelled"
    assert repo.get_transformation(parent_tx.id).modality == "cancelled"  # type: ignore[union-attr]
    for child in children:
        assert repo.get_transformation(child.id).modality == "cancelled"  # type: ignore[union-attr]
        for u_edge in repo.list_edges(transformation_id=child.id, direction="in", role="uses"):
            assert u_edge.resource_released is True
