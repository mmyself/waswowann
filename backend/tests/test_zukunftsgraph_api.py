from __future__ import annotations

import json
import math
from fastapi.testclient import TestClient

from app.db import db
from app.services.gaussian_fusion import recompute_observation_kalman

HEADERS = {"Origin": "http://localhost:3000", "Content-Type": "application/json"}


def _insert_obs_with_photos(config, *, obs_id: str, title: str, environment: str, photos: list[dict]) -> None:
    now = 1_795_500_000_000
    with db(config) as conn:
        conn.execute(
            """
            INSERT INTO observations(id, category, title, description, environment, created_at_ms, updated_at_ms)
            VALUES (?, 'Pilz', ?, 'Testbeschreibung', ?, ?, ?)
            """,
            (obs_id, title, environment, now, now),
        )
        for idx, p in enumerate(photos):
            conn.execute(
                """
                INSERT INTO media(
                    id, obs_id, source, external_id, original_name, local_path, thumbnail_path,
                    mime_type, width, height, sha256, exif_json, created_at_ms,
                    observed_at_ms, sigma_seconds, time_source, lat, lon, sigma_meters, location_source
                ) VALUES (?, ?, 'local', ?, ?, 'img.jpg', 'thumb.jpg', 'image/jpeg', 100, 100, ?, '{}', ?, ?, ?, 'exif', ?, ?, ?, ?)
                """,
                (
                    p["id"],
                    obs_id,
                    f"ext-{p['id']}",
                    f"{p['id']}.jpg",
                    f"sha-{obs_id}-{idx}",
                    now + idx * 1000,
                    p["time_ms"],
                    60.0,
                    p["lat"],
                    p["lon"],
                    p["sigma_meters"],
                    p.get("location_source", "exif"),
                ),
            )
        recompute_observation_kalman(conn, obs_id, config=config)


def test_sync_idempotent_and_preserves_indoor_sigma(client: TestClient, config):
    _insert_obs_with_photos(
        config,
        obs_id="indoor-hericium",
        title="Igel-Stachelbart (Hericium erinaceus)",
        environment="indoor",
        photos=[
            {"id": "m-in-1", "lat": 53.55828, "lon": 9.80831, "sigma_meters": 12.0, "time_ms": 1_795_500_000_000},
            {"id": "m-in-2", "lat": 53.55829, "lon": 9.80832, "sigma_meters": 12.0, "time_ms": 1_795_586_400_000},
            {"id": "m-in-3", "lat": 53.55828, "lon": 9.80830, "sigma_meters": 12.0, "time_ms": 1_795_672_800_000},
        ],
    )
    _insert_obs_with_photos(
        config,
        obs_id="outdoor-parasol",
        title="Gemeiner Riesenschirmling",
        environment="outdoor",
        photos=[
            {"id": "m-out-1", "lat": 53.56120, "lon": 9.79730, "sigma_meters": 10.0, "time_ms": 1_795_100_000_000},
            {"id": "m-out-2", "lat": 53.56120, "lon": 9.79730, "sigma_meters": 10.0, "time_ms": 1_795_186_400_000},
        ],
    )

    first = client.post("/api/zukunftsgraph/sync", headers=HEADERS, json={"seed_plans": True})
    assert first.status_code == 200
    payload1 = first.json()
    assert payload1["sync"]["entities_synced"] == 2

    second = client.post("/api/zukunftsgraph/sync", headers=HEADERS, json={"seed_plans": True})
    assert second.status_code == 200
    payload2 = second.json()
    assert payload2["sync"]["total_entities"] == payload1["sync"]["total_entities"]
    assert payload2["sync"]["total_states"] == payload1["sync"]["total_states"]

    snap = payload2["snapshot"]
    fusions = {f["entity_id"]: f for f in snap["entity_geo_fusions"]}
    # Repeated indoor photos share indoor:{obs_id} source -> sigma stays >= 12.0 m
    assert fusions["obs:indoor-hericium"]["sigma_meters"] >= 12.0
    # Two independent outdoor photos at 10 m fuse to ~7.07 m
    assert math.isclose(fusions["obs:outdoor-parasol"]["sigma_meters"], 10.0 / math.sqrt(2.0), rel_tol=0.05)

    assert len(snap["spatial_predictions"]) >= 2
    assert len(snap["seasonal_series"]) >= 2
    # Nested parent_id child steps are exported
    nested_children = [t for t in snap["transformations"] if t["parent_id"] is not None]
    assert len(nested_children) == 3


def test_worldline_observation_goal_and_escalations(client: TestClient, config):
    _insert_obs_with_photos(
        config,
        obs_id="obs-waldfund",
        title="Nebelkappe (Clitocybe nebularis)",
        environment="outdoor",
        photos=[
            {"id": "m-wf-1", "lat": 53.5600, "lon": 9.8096, "sigma_meters": 8.0, "time_ms": 1_795_160_000_000},
        ],
    )

    snap_res = client.get("/api/zukunftsgraph/snapshot")
    assert snap_res.status_code == 200
    snap = snap_res.json()
    assert snap["synced_observation_count"] == 1

    # Inspect worldline via legacy obs_id
    wl_res = client.get("/api/zukunftsgraph/worldlines/obs-waldfund")
    assert wl_res.status_code == 200
    wl = wl_res.json()
    assert wl["legacy_obs_id"] == "obs-waldfund"
    assert len(wl["history_observed"]) >= 1

    # Create a spatial + quantitative goal via API
    goal_res = client.post(
        "/api/zukunftsgraph/goals",
        headers=HEADERS,
        json={
            "entity_id_or_obs_id": "obs-waldfund",
            "property_name": "presence",
            "label": "Kontrollbesuch Nebelkappe",
            "target_min": 1.0,
            "target_radius_meters": 50.0,
        },
    )
    assert goal_res.status_code == 201
    assert goal_res.json()["goal"]["lat"] == 53.5600

    # Record a live box temperature measurement
    obs_post = client.post(
        "/api/zukunftsgraph/observations",
        headers=HEADERS,
        json={
            "entity_id": "ent:indoor_klimabox",
            "property_name": "temperature_c",
            "label": "Box-Temperatur manuell 18,5 °C",
            "value_num": 18.5,
            "value_sigma": 0.4,
            "value_unit": "°C",
        },
    )
    assert obs_post.status_code == 201
    assert "feedback" in obs_post.json()

    # Escalation 1: switch CHOICE_OR branch on chain harvest from at_risk -> on_track
    goals = {g["goal_id"]: g for g in obs_post.json()["snapshot"]["goal_evaluations"]}
    assert goals["goal:chain_harvest"]["stage"] == 3
    assert goals["goal:chain_harvest"]["status"] == "at_risk"

    switch_res = client.post(
        "/api/zukunftsgraph/escalations/goal:chain_harvest/resolve",
        headers=HEADERS,
        json={"action": "switch_branch", "params": {"branch_key": "profi_klimabox"}},
    )
    assert switch_res.status_code == 200
    switched_goals = {g["goal_id"]: g for g in switch_res.json()["snapshot"]["goal_evaluations"]}
    assert switched_goals["goal:chain_harvest"]["status"] == "on_track"
    assert switched_goals["goal:chain_harvest"]["stage"] == 2

    # Escalation 2: abort_goal cascades downward to nested substeps & upward across the chain
    abort_res = client.post(
        "/api/zukunftsgraph/escalations/goal:chain_harvest/resolve",
        headers=HEADERS,
        json={"action": "abort_goal", "params": {}},
    )
    assert abort_res.status_code == 200
    aborted_txs = {t["id"]: t for t in abort_res.json()["snapshot"]["transformations"]}
    for tx_id in (
        "tx:chain_spawn",
        "tx:chain_substrate_parent",
        "tx:sub_soak",
        "tx:sub_autoclave",
        "tx:sub_inoculate",
        "tx:chain_branch_std",
        "tx:chain_branch_pro",
    ):
        assert aborted_txs[tx_id]["modality"] == "cancelled"

    missing = client.get("/api/zukunftsgraph/worldlines/does-not-exist")
    assert missing.status_code == 404


def test_clocks_and_periodicity_endpoints(client: TestClient, config):
    _insert_obs_with_photos(
        config,
        obs_id="obs-sat-check",
        title="Samstagsrunde",
        environment="outdoor",
        photos=[
            {"id": "m-sat-1", "lat": 53.55, "lon": 9.99, "sigma_meters": 8.0, "time_ms": 1_793_520_000_000},
        ],
    )
    snap = client.get("/api/zukunftsgraph/snapshot").json()
    assert len(snap["current_clocks"]) == 7
    assert "periodicity" in snap

    wl_periods = client.get("/api/zukunftsgraph/periods", params={"entity_id": "obs-sat-check"})
    assert wl_periods.status_code == 200
    assert wl_periods.json()["sample_count"] >= 1

    # Weekly series (5 Saturdays)
    sat0 = 1_791_442_800_000  # 2026-10-10T09:00 Europe/Berlin approx
    weekly_ts = [sat0 + i * 7 * 86_400_000 for i in range(5)]
    res_week = client.post(
        "/api/zukunftsgraph/periods/analyze",
        headers=HEADERS,
        json={"timestamps_ms": weekly_ts, "label": "Samstags-Takt"},
    )
    assert res_week.status_code == 200
    body_week = res_week.json()
    cands_week = {c["clock_id"]: c for c in body_week["candidates"]}
    assert cands_week["week_local"]["status"] == "detected"
    assert cands_week["relative_interval"]["status"] == "detected"

    # Tidal series (~12.4206012h * 3 apart)
    tide_ms = round(12.4206012 * 3600 * 1000)
    tidal_ts = [sat0 + i * 3 * tide_ms for i in range(5)]
    res_tide = client.post(
        "/api/zukunftsgraph/periods/analyze",
        headers=HEADERS,
        json={"timestamps_ms": tidal_ts, "label": "Gezeiten-Serie"},
    )
    assert res_tide.status_code == 200
    cands = {c["clock_id"]: c for c in res_tide.json()["candidates"]}
    assert cands["tidal_semidiurnal"]["status"] == "detected"


def test_goal_patch_metadata_and_raw_samples_rollup_api(client: TestClient, config):
    _insert_obs_with_photos(
        config,
        obs_id="indoor-hericium",
        title="Igel-Stachelbart (Hericium erinaceus)",
        environment="indoor",
        photos=[
            {"id": "m-her-1", "lat": 53.55828, "lon": 9.80831, "sigma_meters": 12.0, "time_ms": 1_795_500_000_000},
        ],
    )
    snap = client.get("/api/zukunftsgraph/snapshot").json()
    hericium_goal = next(s for s in snap["states"] if s["id"] == "goal:hericium_harvest")
    pred_state = next(
        s for s in snap["states"]
        if s["entity_id"] == hericium_goal["entity_id"] and s["property_name"] == "harvest_ready" and s["modality"] == "predicted"
    )
    assert pred_state["time_ms"] is not None

    # Tightening target_latest_ms well before predicted harvest flips goal to Stage 4 (wann broken)
    tight_patch = client.patch(
        "/api/zukunftsgraph/goals/goal:hericium_harvest",
        headers=HEADERS,
        json={
            "target_min": 200.0,
            "target_earliest_ms": pred_state["time_ms"] - 6 * 86_400_000,
            "target_latest_ms": pred_state["time_ms"] - 3 * 86_400_000,
        },
    )
    assert tight_patch.status_code == 200
    tight_eval = tight_patch.json()["evaluation"]
    assert tight_eval["stage"] == 4
    assert "wann" in tight_eval["broken_dimensions"]

    # Widening latest bound & keeping quantity restores Stage 2 (on_track)
    wide_patch = client.patch(
        "/api/zukunftsgraph/goals/goal:hericium_harvest",
        headers=HEADERS,
        json={
            "target_min": 180.0,
            "target_earliest_ms": None,
            "target_latest_ms": pred_state["time_ms"] + 5 * 86_400_000,
        },
    )
    assert wide_patch.status_code == 200
    assert wide_patch.json()["evaluation"]["stage"] == 2
    assert wide_patch.json()["goal"]["target_earliest_ms"] is None

    # Record observation with context, assertion_confidence, and evidence_refs
    obs_res = client.post(
        "/api/zukunftsgraph/observations",
        headers=HEADERS,
        json={
            "entity_id": "ent:indoor_klimabox",
            "property_name": "temperature_c",
            "label": "Schatten-Sensor 291.65 K",
            "value_num": 291.65,
            "value_sigma": 0.4,
            "value_unit": "K",
            "context": {"probe": "shade"},
            "assertion_confidence": 0.9,
            "evidence_refs": [{"kind": "sensor_log", "ref": "csv://box/1"}],
        },
    )
    assert obs_res.status_code == 201
    created_id = obs_res.json()["feedback"]["created_state_ids"][0]
    created_state = next(s for s in obs_res.json()["snapshot"]["states"] if s["id"] == created_id)
    assert created_state["context"] == {"probe": "shade"}
    assert created_state["assertion_confidence"] == 0.9
    assert created_state["evidence_refs"][0]["ref"] == "csv://box/1"

    # Ingest raw high-frequency samples and roll up in °C
    t0 = 1_795_600_000_000
    raw_res = client.post(
        "/api/zukunftsgraph/raw-samples",
        headers=HEADERS,
        json={
            "samples": [
                {
                    "entity_id": "ent:indoor_klimabox",
                    "property_name": "temperature_c",
                    "time_ms": t0,
                    "value_num": 18.0,
                    "value_sigma": 0.2,
                    "value_unit": "°C",
                    "context": {"probe": "box_logger"},
                },
                {
                    "entity_id": "ent:indoor_klimabox",
                    "property_name": "temperature_c",
                    "time_ms": t0 + 1_800_000,
                    "value_num": 292.15,
                    "value_sigma": 0.2,
                    "value_unit": "K",
                    "context": {"probe": "box_logger"},
                },
            ]
        },
    )
    assert raw_res.status_code == 201
    assert raw_res.json()["inserted_count"] == 2

    rollup_res = client.post(
        "/api/zukunftsgraph/raw-samples/rollup",
        headers=HEADERS,
        json={
            "entity_id": "ent:indoor_klimabox",
            "property_name": "temperature_c",
            "window_kind": "day",
            "target_unit": "°C",
        },
    )
    assert rollup_res.status_code == 200
    assert rollup_res.json()["created_count"] == 1
    rolled = rollup_res.json()["created_states"][0]
    assert rolled["value_unit"] == "°C"
    assert math.isclose(rolled["value_num"], 18.5, rel_tol=1e-4)
    assert rolled["meta"]["rollup"]["n"] == 2
