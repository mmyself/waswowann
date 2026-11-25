"""End-to-end scenario tests for Milestone M5 across all 4 reference seeds."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

from metacore.config import Settings
from metacore.engine import MetaCoreEngine
from metacore.math_fusion import GeoPoint, fuse_locations
from metacore.mcp_server import create_server
from metacore.models import StateCreate
from metacore.repository import MetaCoreRepository
from metacore.seeds import (
    seed_igel_stachelbart,
    seed_koernerbrut,
    seed_riesenschirmling,
    seed_roboter_wasser,
    seed_substrat_rezept,
)


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    return json.loads(raw[0].text)


def test_e2e_koernerbrut_400g_calibration_and_release(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    ids = seed_koernerbrut(repo, engine, dry_grams=400.0, bags=2, species="Igel-Stachelbart")

    total_pred = repo.get_state(ids["total_pred_id"])
    bag1_pred = repo.get_state(ids["bag_1_pred_id"])
    assert total_pred is not None and total_pred.value_num == pytest.approx(700.0)
    assert total_pred.value_sigma == pytest.approx(40.0)
    assert bag1_pred is not None and bag1_pred.value_num == pytest.approx(350.0)
    assert bag1_pred.value_sigma == pytest.approx(math.hypot(20.0, 6.0))
    assert engine.evaluate_goal(ids["goal_id"]).stage == 2

    # Real weighing: 680 ± 10 g calibrates template and shrinks bag sigma
    fb = engine.record_observation(
        StateCreate(
            entity_id=ids["charge_id"],
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label="Abgetropft gewogen: 680 g",
            value_num=680.0,
            value_sigma=10.0,
            value_unit="g",
            time_ms=1_795_000_000_000 + 3 * 86_400_000,
        )
    )
    assert ids["template_id"] in fb.calibrated_template_ids
    tpl = repo.get_transformation(ids["template_id"])
    assert tpl is not None and tpl.formula["factor"]["source"] == "measured"
    assert repo.get_state(ids["bag_1_pred_id"]).value_sigma == pytest.approx(math.hypot(5.0, 6.0))  # type: ignore[union-attr]

    # Clean abort releases incubator `uses` reservation
    fb_abort = engine.resolve_escalation(ids["goal_id"], "abort_goal")
    assert fb_abort.goal_evaluations[0].status == "cancelled"
    uses_edges = repo.list_edges(role="uses")
    assert uses_edges and all(e.resource_released for e in uses_edges)


def test_e2e_riesenschirmling_habitat_trigger_frost_and_zero_obs(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    ids = seed_riesenschirmling(repo, engine)

    habitat = repo.get_entity(ids["habitat_id"])
    assert habitat is not None and habitat.source == "derived"
    assert 100.0 <= habitat.meta["habitat_sigma_meters"] <= 150.0
    assert engine.evaluate_goal(ids["goal_id"]).stage == 2

    # Stage 1: rain >= 35 mm activates seasonal trigger template
    fb_rain = engine.record_observation(
        StateCreate(
            entity_id=ids["weather_id"],
            modality="observed",
            source="measured",
            measurement_source="Wetterstation",
            property_name="rain_mm",
            label="Herbstregen 40 mm",
            value_num=40.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=1_795_100_000_000,
        )
    )
    assert ids["trigger_template_id"] in fb_rain.triggered_template_ids

    # Ground frost -4 °C breaks vorbedingung -> Stage 4
    fb_frost = engine.record_observation(
        StateCreate(
            entity_id=ids["weather_id"],
            modality="observed",
            source="measured",
            measurement_source="Wetterstation",
            property_name="temperature_c",
            label="Bodenfrost -4 °C",
            value_num=-4.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=1_795_200_000_000,
        )
    )
    assert fb_frost.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_frost.goal_evaluations[0].broken_dimensions

    # Zero-observation is stored cleanly as valid measured evidence
    fb_zero = engine.record_observation(
        StateCreate(
            entity_id=ids["habitat_id"],
            modality="observed",
            source="measured",
            measurement_source="Kontrollgang",
            property_name="presence",
            label="Kontrollgang ohne Fund (0)",
            value_num=0.0,
            value_sigma=0.0,
            value_unit="stk",
            lat=50.812,
            lon=12.915,
            sigma_meters=140.0,
            time_ms=1_795_300_000_000,
        )
    )
    zero_state = repo.get_state(fb_zero.created_state_ids[0])
    assert zero_state is not None and zero_state.value_num == 0.0


def test_e2e_roboter_r1_conflict_branch_switch_locked_door_and_abort(
    repo: MetaCoreRepository,
) -> None:
    engine = MetaCoreEngine(repo)
    ids = seed_roboter_wasser(repo, engine)

    assert len(engine.detect_uses_conflicts()) == 1
    assert engine.evaluate_goal(ids["goal_id"]).stage == 2

    # Switch CHOICE_OR branch arm2 -> sideboard
    fb_sw = engine.resolve_escalation(ids["goal_id"], "switch_branch", {"branch_key": "sideboard"})
    assert fb_sw.goal_evaluations[0].predicted_state_id == ids["pred_sideboard_id"]
    assert repo.get_edge(ids["edge_arm2_id"]).outcome == "not_chosen"  # type: ignore[union-attr]
    assert repo.get_edge(ids["edge_sideboard_id"]).outcome == "chosen"  # type: ignore[union-attr]

    # Door locked breaks both branches -> Stage 4 (vorbedingung)
    fb_lock = engine.record_observation(
        StateCreate(
            entity_id=ids["door_id"],
            modality="observed",
            source="measured",
            measurement_source="R1_Kraftsensor",
            property_name="door_state",
            label="Tür T1 verriegelt",
            value_text="verriegelt",
            distribution_kind="discrete",
            time_ms=1_795_000_005_000,
        )
    )
    assert fb_lock.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_lock.goal_evaluations[0].broken_dimensions

    # Abort resolves the uses conflict cleanly
    fb_abort = engine.resolve_escalation(ids["goal_id"], "abort_goal")
    assert fb_abort.uses_conflicts == []
    assert fb_abort.goal_evaluations[0].status == "cancelled"


def test_e2e_igel_stachelbart_indoor_dedup_calibration_and_mcp_inspect(
    settings: Settings,
) -> None:
    server = create_server(settings)
    repo = MetaCoreRepository(settings)
    engine = MetaCoreEngine(repo)
    ids = seed_igel_stachelbart(repo, engine, t0_ms=1_794_767_346_000)

    # Box environment entity tracks humidity (80 ± 10 %) and temp trajectory (16 °C -> 18 °C)
    box = repo.get_entity(ids["box_id"])
    assert box is not None and box.name == "Indoor Fruchtungsbox"
    hum_states = repo.list_states(entity_id=ids["box_id"], property_name="humidity_pct", modality="observed")
    temp_states = repo.list_states(entity_id=ids["box_id"], property_name="temperature_c", modality="observed")
    assert len(hum_states) == 4
    assert all(s.value_num == pytest.approx(80.0) and s.value_sigma == pytest.approx(10.0) for s in hum_states)
    assert [s.value_num for s in temp_states] == [16.0, 16.0, 18.0, 18.0]

    # Indoor photos from same Handy_Indoor source must NOT artificially shrink sigma_meters
    loc_states = repo.list_states(entity_id=ids["hericium_id"], property_name="location")
    fused_loc = fuse_locations(
        [
            GeoPoint(s.lat, s.lon, s.sigma_meters, measurement_source=s.measurement_source)  # type: ignore[arg-type]
            for s in loc_states
        ]
    )
    assert fused_loc.sigma_meters == pytest.approx(12.0)

    # Real harvest observed ~8.92 days later (matching real Hericium timelapse end) calibrates duration
    fb_harvest = engine.record_observation(
        StateCreate(
            entity_id=ids["hericium_id"],
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="harvest_ready",
            label="Igel-Stachelbart geerntet (265 g)",
            value_num=265.0,
            value_sigma=5.0,
            value_unit="g",
            location_label="Indoor Fruchtungsbox",
            time_ms=1_795_538_193_000,
            sigma_seconds=60.0,
        )
    )
    assert ids["template_id"] in fb_harvest.calibrated_template_ids
    cal_tpl = repo.get_transformation(ids["template_id"])
    assert cal_tpl is not None and cal_tpl.duration_source == "measured"
    assert cal_tpl.duration_seconds is not None
    assert 8.5 * 86_400 <= cal_tpl.duration_seconds <= 9.2 * 86_400

    world = _decode(
        asyncio.run(
            server.call_tool(
                "inspect_worldline", {"entity_id_or_name": "Igel-Stachelbart (Hericium erinaceus)"}
            )
        )
    )
    assert world["entity"]["id"] == ids["hericium_id"]
    assert len(world["history_observed"]) >= 6
    assert world["goal_evaluations"][0]["stage"] == 2


def test_e2e_igel_stachelbart_climate_guard_and_live_modifier(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    t0 = 1_794_767_346_000
    ids = seed_igel_stachelbart(repo, engine, t0_ms=t0, include_warmup_phase=False)

    # Cool 16 °C start stretches expected duration beyond 8 d (factor 0.96 -> ~8.333 d)
    planned_init = repo.list_transformations(modality="planned")[0]
    assert planned_init.duration_seconds is not None
    assert planned_init.duration_seconds == pytest.approx(8 * 86_400 / 0.96)
    assert engine.evaluate_goal(ids["goal_id"]).stage == 2

    # Warm-up to 18 °C on day 4 pulls predicted duration back to 8.0 d live
    fb_warm = engine.record_observation(
        StateCreate(
            entity_id=ids["box_id"],
            modality="observed",
            source="measured",
            measurement_source="Thermometer_Box",
            property_name="temperature_c",
            label="Box-Temperatur Tag 4 (18 °C)",
            value_num=18.0,
            value_sigma=0.5,
            value_unit="°C",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0 + 4 * 86_400_000,
            sigma_seconds=30.0,
        )
    )
    assert ids["pred_harvest_id"] in fb_warm.updated_state_ids
    planned_warm = repo.get_transformation(planned_init.id)
    assert planned_warm is not None
    assert planned_warm.duration_seconds == pytest.approx(8.0 * 86_400, rel=1e-3)
    assert fb_warm.goal_evaluations[0].stage == 2

    # Humidity dropping to 50 ± 5 % breaks vorbedingung -> Stage 4; restoring 80 ± 10 % returns Stage 2
    fb_dry = engine.record_observation(
        StateCreate(
            entity_id=ids["box_id"],
            modality="observed",
            source="measured",
            measurement_source="Hygrometer_Box",
            property_name="humidity_pct",
            label="Trockene Box (50 ± 5 %)",
            value_num=50.0,
            value_sigma=5.0,
            value_unit="%",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0 + 5 * 86_400_000,
            sigma_seconds=30.0,
        )
    )
    assert fb_dry.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_dry.goal_evaluations[0].broken_dimensions

    fb_ok = engine.record_observation(
        StateCreate(
            entity_id=ids["box_id"],
            modality="observed",
            source="measured",
            measurement_source="Hygrometer_Box",
            property_name="humidity_pct",
            label="Luftbefeuchter aktiv (80 ± 10 %)",
            value_num=80.0,
            value_sigma=10.0,
            value_unit="%",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0 + 6 * 86_400_000,
            sigma_seconds=30.0,
        )
    )
    assert fb_ok.goal_evaluations[0].stage == 2
    assert fb_ok.goal_evaluations[0].status == "on_track"


def test_e2e_riesenschirmling_does_not_trigger_without_cooling(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    ids = seed_riesenschirmling(repo, engine)

    # If temperatures warmed back up instead of cooling down, 40 mm rain alone does NOT fire
    engine.record_observation(
        StateCreate(
            entity_id=ids["weather_id"],
            modality="observed",
            source="measured",
            measurement_source="Wetterstation",
            property_name="temperature_c",
            label="Föhnwärme +18 °C",
            value_num=18.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=1_795_050_000_000,
        )
    )
    fb_rain = engine.record_observation(
        StateCreate(
            entity_id=ids["weather_id"],
            modality="observed",
            source="measured",
            measurement_source="Wetterstation",
            property_name="rain_mm",
            label="Warmer Herbstregen 40 mm",
            value_num=40.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=1_795_100_000_000,
        )
    )
    assert ids["trigger_template_id"] not in fb_rain.triggered_template_ids


def test_e2e_substrat_rezept_ratios_and_modifiers(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    ids = seed_substrat_rezept(repo, engine)

    total_pred = repo.get_state(ids["total_pred_id"])
    assert total_pred is not None
    assert total_pred.value_num == pytest.approx(1400.0)
    assert total_pred.value_sigma == pytest.approx(math.sqrt(150.0))
    assert engine.evaluate_goal(ids["goal_id"]).stage == 2

    # Insufficient water (500 g vs 500 g sawdust -> ratio 1.0 < 1.35) breaks vorbedingung
    fb_dry = engine.record_observation(
        StateCreate(
            entity_id=ids["water_ent_id"],
            modality="observed",
            source="measured",
            measurement_source="Messbecher",
            property_name="water_mass",
            label="Nur 500 g Wasser",
            value_num=500.0,
            value_sigma=10.0,
            value_unit="g",
            time_ms=1_795_000_600_000,
        )
    )
    assert fb_dry.goal_evaluations[0].stage == 4
    assert "vorbedingung" in fb_dry.goal_evaluations[0].broken_dimensions

    # Topping up to 760 g restores ratio 1.52 -> Stage 2
    fb_ok = engine.record_observation(
        StateCreate(
            entity_id=ids["water_ent_id"],
            modality="observed",
            source="measured",
            measurement_source="Messbecher",
            property_name="water_mass",
            label="Wasser 760 g",
            value_num=760.0,
            value_sigma=10.0,
            value_unit="g",
            time_ms=1_795_001_200_000,
        )
    )
    assert fb_ok.goal_evaluations[0].stage == 2
    assert fb_ok.goal_evaluations[0].status == "on_track"
