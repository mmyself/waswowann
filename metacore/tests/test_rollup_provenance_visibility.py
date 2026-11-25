"""Tests for Package 3: raw_samples roll-up, evidence_refs provenance, assertion_confidence, and visibility."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from metacore.config import Settings
from metacore.db import connect, migrate, now_ms
from metacore.engine import MetaCoreEngine
from metacore.mcp_server import create_server
from metacore.models import (
    EntityCreate,
    EvidenceRef,
    RawSampleCreate,
    StateCreate,
    TransformationCreate,
)
from metacore.repository import MetaCoreRepository


def _decode_tool_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw:
        text = getattr(raw[0], "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError(f"Unexpected MCP payload: {type(raw)}")


def test_raw_samples_rollup_windows_units_contexts_and_triggers(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    terrarium = repo.create_entity(EntityCreate(name="Pilzbox_Alpha", kind="worldline", category="klima"))

    # Trigger template that fires when daily rolled-up temperature drops below 16 °C
    cold_alert_tpl = repo.create_transformation(
        TransformationCreate(
            name="Kältewarnung Box",
            modality="template",
            driver="actor",
            duration_seconds=3600.0,
            formula={
                "kind": "duration_step",
                "output_property": "heating",
                "output_text": "aktiv",
                "triggers": {
                    "logic": "AND",
                    "conditions": [
                        {
                            "property_name": "temp",
                            "match_context": {"probe": "inside"},
                            "unit": "°C",
                            "max_value": 16.0,
                        }
                    ],
                },
            },
        )
    )

    # 2026-10-10T10:00Z .. 2026-10-11T10:00Z (two distinct UTC/Berlin days, two distinct contexts, mixed °C and K)
    day1_base = 1_791_626_400_000  # 2026-10-10T10:00:00Z
    day2_base = day1_base + 86_400_000

    samples = [
        # Day 1 inside (°C + K mixed, rising +1 °C/h)
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day1_base,
            value_num=20.0,
            value_sigma=0.2,
            value_unit="°C",
            measurement_source="dht22_a",
            context={"probe": "inside"},
            evidence_ref="csv://logger_2026_10_10#row=1",
        ),
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day1_base + 3_600_000,
            value_num=294.15,  # 21.0 °C in Kelvin
            value_sigma=0.2,
            value_unit="K",
            measurement_source="dht22_a",
            context={"probe": "inside"},
            evidence_ref="csv://logger_2026_10_10#row=2",
        ),
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day1_base + 7_200_000,
            value_num=22.0,
            value_sigma=0.2,
            value_unit="°C",
            measurement_source="dht22_a",
            context={"probe": "inside"},
            evidence_ref="csv://logger_2026_10_10#row=3",
        ),
        # Day 1 outside context (must stay isolated from inside bucket!)
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day1_base + 3_600_000,
            value_num=10.0,
            value_sigma=0.5,
            value_unit="°C",
            measurement_source="dht22_out",
            context={"probe": "outside"},
        ),
        # Day 2 inside (cold 14 °C -> should produce another rolled-up State AND trigger alert!)
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day2_base,
            value_num=14.0,
            value_sigma=0.2,
            value_unit="°C",
            measurement_source="dht22_a",
            context={"probe": "inside"},
        ),
        RawSampleCreate(
            entity_id=terrarium.id,
            property_name="temp",
            time_ms=day2_base + 3_600_000,
            value_num=14.0,
            value_sigma=0.2,
            value_unit="°C",
            measurement_source="dht22_a",
            context={"probe": "inside"},
        ),
    ]
    inserted = engine.ingest_raw_samples(samples)
    assert len(inserted) == 6
    assert repo.list_states(entity_id=terrarium.id) == []

    # Roll up into day buckets in °C
    rolled = engine.rollup_raw_samples(
        entity_id=terrarium.id,
        property_name="temp",
        window_kind="day",
        target_unit="°C",
    )
    assert len(rolled) == 3  # day1-inside, day1-outside, day2-inside

    day1_in = next(
        s
        for s in rolled
        if s.context == {"probe": "inside"} and abs((s.value_num or 0.0) - 21.0) < 0.01
    )
    assert day1_in.value_unit == "°C"
    assert day1_in.meta["rollup"]["n"] == 3
    assert day1_in.meta["rollup"]["min"] == pytest.approx(20.0)
    assert day1_in.meta["rollup"]["max"] == pytest.approx(22.0)
    assert day1_in.meta["rollup"]["slope_per_hour"] == pytest.approx(1.0)
    assert any(ref.kind == "raw_samples" for ref in day1_in.evidence_refs)
    assert any(ref.ref == "csv://logger_2026_10_10#row=1" for ref in day1_in.evidence_refs)

    # Trigger must have matched on Day 2 cold bucket (14 °C)
    day2_in = next(
        s
        for s in rolled
        if s.context == {"probe": "inside"} and abs((s.value_num or 0.0) - 14.0) < 0.01
    )
    assert cold_alert_tpl.id in engine._matching_trigger_templates(day2_in)

    # Idempotent: unrolled queue is now empty
    assert repo.list_raw_samples(entity_id=terrarium.id, only_unrolled=True) == []
    assert engine.rollup_raw_samples(entity_id=terrarium.id, property_name="temp") == []

    # Clock-phase bucketing (e.g. solar_day day vs night)
    night_sample = RawSampleCreate(
        entity_id=terrarium.id,
        property_name="temp",
        time_ms=day2_base + 12 * 3_600_000,  # 22:00 UTC -> night
        value_num=12.5,
        value_unit="°C",
        context={"probe": "inside"},
    )
    day_sample = RawSampleCreate(
        entity_id=terrarium.id,
        property_name="temp",
        time_ms=day2_base + 1_000,  # 10:00 UTC -> daylight
        value_num=18.5,
        value_unit="°C",
        context={"probe": "inside"},
    )
    engine.ingest_raw_samples([night_sample, day_sample])
    clock_rolled = engine.rollup_raw_samples(
        entity_id=terrarium.id,
        property_name="temp",
        window_kind="clock_phase",
        clock_id="solar_day",
        lat=51.0,
        lon=10.0,
    )
    assert len(clock_rolled) == 2
    assert {s.meta["rollup"]["bucket_key"].split(":")[-1] for s in clock_rolled} == {"day", "night"}


def test_evidence_refs_and_assertion_confidence_scaling(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    specimen = repo.create_entity(EntityCreate(name="Exemplar_X", kind="worldline", category="fund"))
    t0 = 1_796_500_000_000

    obs = repo.create_state(
        StateCreate(
            entity_id=specimen.id,
            modality="observed",
            source="measured",
            property_name="species_guess",
            label="Evtl. Igel-Stachelbart",
            value_text="hericium",
            distribution_kind="discrete",
            time_ms=t0,
            assertion_confidence=0.6,
            evidence_refs=[
                EvidenceRef(kind="photo", ref="img://fund_042.jpg", label="Foto Lamellen", checksum="sha256:abc")
            ],
        )
    )
    assert obs.assertion_confidence == pytest.approx(0.6)
    assert obs.evidence_refs[0].checksum == "sha256:abc"

    # Guard on species_guess reflects assertion_confidence 0.6
    p_cond = engine._evaluate_condition(
        {"entity_id": specimen.id, "property_name": "species_guess", "required_text": "hericium"},
        in_states=(),
    )
    assert p_cond == pytest.approx(0.6)

    # Goal directly evaluated against uncertain state -> Stage 3 (0.6 between threshold_fail 0.2 and threshold_ok 0.8)
    goal = repo.create_state(
        StateCreate(
            entity_id=specimen.id,
            modality="desired",
            source="user_goal",
            property_name="species_guess",
            label="Sicher bestimmt",
            target_value_text="hericium",
            threshold_ok=0.8,
            threshold_fail=0.2,
        )
    )
    ev = engine.evaluate_goal(goal.id)
    assert ev.stage == 3
    assert ev.goal_probability == pytest.approx(0.6)


def test_visibility_local_only_boundary_in_mcp(settings: Settings) -> None:
    server = create_server(settings)
    repo = MetaCoreRepository(settings)

    async def run() -> None:
        # Public entity with one normal state and one local_only state
        await server.call_tool(
            "record_observation",
            {
                "entity_id_or_name": "Hof_Sensor",
                "property_name": "temp",
                "label": "Öffentlich 19 °C",
                "value_num": 19.0,
                "value_unit": "°C",
                "evidence_refs": [{"kind": "sensor_log", "ref": "log://hof/1"}],
                "assertion_confidence": 0.95,
                "visibility": "normal",
            },
        )
        await server.call_tool(
            "record_observation",
            {
                "entity_id_or_name": "Hof_Sensor",
                "property_name": "secret_note",
                "label": "Vertraulich",
                "value_text": "private_pin_1234",
                "distribution_kind": "discrete",
                "visibility": "local_only",
            },
        )
        # Local repository still reads everything
        ent = repo.find_entities("Hof_Sensor")[0]
        assert len(repo.list_states(entity_id=ent.id)) == 2
        assert len(repo.list_states(entity_id=ent.id, exclude_local_only=True)) == 1

        # MCP inspect_worldline filters out local_only state
        world = _decode_tool_result(
            await server.call_tool("inspect_worldline", {"entity_id_or_name": "Hof_Sensor"})
        )
        props = {s["property_name"] for s in world["history_observed"]}
        assert props == {"temp"}

        # Fully local_only entity + goal: locally inspectable, hidden in MCP
        secret_ent = repo.create_entity(
            EntityCreate(name="Privat_Tagebuch", kind="worldline", visibility="local_only")
        )
        repo.create_state(
            StateCreate(
                entity_id=secret_ent.id,
                modality="desired",
                source="user_goal",
                property_name="mood",
                label="Privates Ziel",
                target_min=7.0,
                visibility="local_only",
            )
        )
        repo.create_state(
            StateCreate(
                entity_id=secret_ent.id,
                modality="predicted",
                source="ai_prior",
                property_name="mood",
                label="Geheime Prognose",
                value_num=5.0,
                value_sigma=1.0,
                visibility="local_only",
            )
        )

        brief = _decode_tool_result(
            await server.call_tool("get_focus_briefing", {"horizon_days": 14})
        )
        assert all(g["entity_id"] != secret_ent.id for g in brief["goal_evaluations"])
        assert all(p["entity_id"] != secret_ent.id for p in brief["upcoming_predictions"])
        assert all(p["label"] != "Geheime Prognose" for p in brief["open_ai_priors"])

        with pytest.raises((ValueError, ToolError)):
            await server.call_tool("inspect_worldline", {"entity_id_or_name": "Privat_Tagebuch"})

    asyncio.run(run())


def test_inplace_upgrade_from_v1_database_to_v3(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v1.sqlite3"
    v1_sql = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "metacore"
        / "migrations"
        / "001_initial.sql"
    ).read_text(encoding="utf-8")

    ts = now_ms()
    with connect(db_path) as conn:
        conn.executescript(v1_sql)
        conn.execute(
            "INSERT INTO entities (id, name, kind, created_at_ms, updated_at_ms) VALUES ('ent_legacy', 'Legacy Entität', 'worldline', ?, ?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO states (id, entity_id, modality, source, property_name, label, value_num, value_unit, created_at_ms, updated_at_ms) VALUES ('st_legacy', 'ent_legacy', 'observed', 'measured', 'mass', '500 g', 500.0, 'g', ?, ?)",
            (ts, ts),
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()

    cfg = Settings(database_path=db_path)
    version = migrate(cfg)
    assert version == 3

    repo = MetaCoreRepository(cfg)
    ent = repo.get_entity("ent_legacy")
    st = repo.get_state("st_legacy")
    assert ent is not None and ent.visibility == "normal"
    assert st is not None
    assert st.context == {}
    assert st.evidence_refs == []
    assert st.assertion_confidence == 1.0
    assert st.visibility == "normal"
