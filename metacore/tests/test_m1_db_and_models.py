"""Tests for Milestone M1: SQLite schema, v2 invariants, Pydantic models, and repository."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from metacore.config import Settings
from metacore.db import connect, migrate, now_ms
from metacore.models import EdgeCreate, EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def test_migrate_creates_tables_and_is_idempotent(settings: Settings) -> None:
    version_first = migrate(settings)
    version_second = migrate(settings)
    assert version_first == 3
    assert version_second == 3

    with connect(settings.database_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert tables == {"entities", "states", "transformations", "edges", "raw_samples"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_multiple_property_states_on_same_entity(repo: MetaCoreRepository) -> None:
    bag = repo.create_entity(
        EntityCreate(name="Beutel #1", kind="worldline", category="rezept")
    )
    t = 1_795_000_000_000

    mass = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="predicted",
            source="derived",
            property_name="mass",
            label="Masse Beutel #1",
            value_num=350.0,
            value_sigma=21.0,
            value_unit="g",
            time_ms=t,
            sigma_seconds=3600.0,
        )
    )
    colonization = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="observed",
            source="measured",
            measurement_source="Auge",
            property_name="colonization_pct",
            label="Durchwachsung 40 %",
            value_num=40.0,
            value_sigma=5.0,
            value_unit="%",
            time_ms=t,
        )
    )
    temp = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="observed",
            source="measured",
            measurement_source="Thermometer_Kueche",
            property_name="temperature_c",
            label="Temperatur 22 °C",
            value_num=22.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t,
        )
    )
    contaminated = repo.create_state(
        StateCreate(
            entity_id=bag.id,
            modality="observed",
            source="measured",
            measurement_source="Auge",
            property_name="contaminated",
            label="Keine Kontamination",
            value_text="nein",
            distribution_kind="discrete",
            time_ms=t,
        )
    )

    all_states = repo.list_states(entity_id=bag.id)
    assert [s.id for s in all_states] == [mass.id, colonization.id, temp.id, contaminated.id]
    assert {s.property_name for s in all_states} == {
        "mass",
        "colonization_pct",
        "temperature_c",
        "contaminated",
    }
    assert repo.list_states(entity_id=bag.id, property_name="temperature_c")[0].measurement_source == (
        "Thermometer_Kueche"
    )


def test_qualitative_null_observation_and_derived_entity(repo: MetaCoreRepository) -> None:
    door = repo.create_entity(
        EntityCreate(name="Flurtür T1", kind="worldline", category="robotik")
    )
    door_state = repo.create_state(
        StateCreate(
            entity_id=door.id,
            modality="observed",
            source="measured",
            measurement_source="R1_Kamera",
            property_name="door_state",
            label="Tür verriegelt",
            value_text="verriegelt",
            distribution_kind="discrete",
            time_ms=1_795_000_000_000,
        )
    )
    assert door_state.value_text == "verriegelt"
    assert door_state.value_num is None

    habitat = repo.create_entity(
        EntityCreate(
            name="Myzelgebiet Nordwiese",
            kind="worldline",
            category="pilz",
            source="derived",
        )
    )
    assert habitat.source == "derived"

    null_obs = repo.create_state(
        StateCreate(
            entity_id=habitat.id,
            modality="observed",
            source="measured",
            measurement_source="Kontrollgang_2026_11",
            property_name="presence",
            label="Kontrollgang ohne Fund (0)",
            value_num=0.0,
            value_sigma=0.0,
            value_unit="stk",
            lat=50.812,
            lon=12.914,
            sigma_meters=140.0,
            time_ms=1_795_100_000_000,
            meta={"search_minutes": 45},
        )
    )
    assert null_obs.value_num == 0.0
    assert null_obs.meta["search_minutes"] == 45


def test_goal_rejects_uncertainty_sigmas_and_invalid_bounds(
    settings: Settings, repo: MetaCoreRepository
) -> None:
    entity = repo.create_entity(EntityCreate(name="Körnerbrut", kind="worldline"))

    valid_goal = repo.create_state(
        StateCreate(
            entity_id=entity.id,
            modality="desired",
            source="user_goal",
            property_name="mass",
            label="Mindestens 600 g Brut bis Tag 24",
            value_unit="g",
            lat=50.812,
            lon=12.914,
            target_min=600.0,
            target_max=900.0,
            target_preferred=700.0,
            target_earliest_ms=1_795_000_000_000,
            target_latest_ms=1_796_700_000_000,
            target_radius_meters=50.0,
            threshold_ok=0.85,
            threshold_fail=0.2,
        )
    )
    assert valid_goal.target_min == 600.0
    assert valid_goal.value_sigma is None

    # Pydantic rejects desired state with any sigma field
    for bad_kwargs in (
        {"value_num": 700.0, "value_sigma": 20.0},
        {"lat": 50.0, "lon": 12.0, "sigma_meters": 10.0},
        {"time_ms": 1_795_000_000_000, "sigma_seconds": 60.0},
        {"target_min": 800.0, "target_max": 500.0},
        {"target_earliest_ms": 2000, "target_latest_ms": 1000},
        {"threshold_ok": 0.5, "threshold_fail": 0.7},
        {"lat": 50.0, "lon": None},
    ):
        with pytest.raises(ValidationError):
            StateCreate(
                entity_id=entity.id,
                modality="desired",
                source="user_goal",
                property_name="mass",
                label="Ungültig",
                **bad_kwargs,
            )

    with pytest.raises(ValidationError):
        StateCreate(
            entity_id=entity.id,
            modality="observed",
            source="measured",
            property_name="mass",
            label="Negatives Sigma",
            lat=50.0,
            lon=12.0,
            sigma_meters=0.0,
        )

    # SQLite CHECK constraint also blocks desired + value_sigma directly at DB level
    ts = now_ms()
    with pytest.raises(sqlite3.IntegrityError), connect(settings.database_path) as conn:
        conn.execute(
            """
            INSERT INTO states (
                id, entity_id, modality, source, property_name, label,
                value_num, value_sigma, created_at_ms, updated_at_ms
            ) VALUES ('st_bad', ?, 'desired', 'user_goal', 'mass', 'Bad', 700.0, 20.0, ?, ?)
            """,
            (entity.id, ts, ts),
        )


def test_edges_outcomes_and_cascade_delete(
    settings: Settings, repo: MetaCoreRepository
) -> None:
    charge = repo.create_entity(EntityCreate(name="Weizencharge", kind="worldline"))
    bag1 = repo.create_entity(
        EntityCreate(
            name="Beutel #1",
            kind="worldline",
            parent_entity_id=charge.id,
        )
    )
    assert repo.get_entity(bag1.id) is not None

    st_in = repo.create_state(
        StateCreate(
            entity_id=charge.id,
            modality="observed",
            source="measured",
            property_name="mass",
            label="680 g abgetropft",
            value_num=680.0,
            value_sigma=10.0,
            value_unit="g",
        )
    )
    st_out = repo.create_state(
        StateCreate(
            entity_id=bag1.id,
            modality="predicted",
            source="derived",
            property_name="mass",
            label="Beutel #1 (~340 g)",
            value_num=340.0,
            value_sigma=8.0,
            value_unit="g",
        )
    )

    tx = repo.create_transformation(
        TransformationCreate(
            name="Auf 2 Beutel aufteilen",
            modality="happened",
            driver="actor",
            duration_seconds=300.0,
            duration_sigma_seconds=60.0,
            duration_source="measured",
            formula={"bags": 2},
        )
    )
    assert repo.get_transformation(tx.id) is not None

    edge_in = repo.create_edge(
        EdgeCreate(
            transformation_id=tx.id,
            state_id=st_in.id,
            direction="in",
            role="consumes",
            logic_group="CHOICE_OR",
            branch_key="2_beutel",
            is_selected=True,
            outcome="chosen",
        )
    )
    edge_out = repo.create_edge(
        EdgeCreate(
            transformation_id=tx.id,
            state_id=st_out.id,
            direction="out",
            role="produces",
            logic_group="AND",
            outcome="occurred",
            ratio_mean=0.5,
            ratio_sigma=0.02,
        )
    )

    assert len(repo.list_edges(transformation_id=tx.id)) == 2
    assert edge_in.outcome == "chosen"
    assert edge_out.ratio_mean == 0.5

    # Deleting entity cascades to its states and edges
    with connect(settings.database_path) as conn:
        conn.execute("DELETE FROM entities WHERE id = ?", (bag1.id,))
        conn.commit()

    assert repo.get_state(st_out.id) is None
    assert [e.id for e in repo.list_edges(transformation_id=tx.id)] == [edge_in.id]
