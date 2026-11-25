"""Data bridge synchronizing legacy WasWoWann SQLite observations into MetaCore worldlines."""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.config as config_module
from app.db import db as legacy_db

_METACORE_SRC = config_module.PROJECT_DIR / "metacore" / "src"
if str(_METACORE_SRC) not in sys.path:
    sys.path.insert(0, str(_METACORE_SRC))

from metacore.clocks import TimeSample, analyze_periodicity, get_current_clock_phases  # noqa: E402
from metacore.config import Settings as MetaCoreSettings  # noqa: E402
from metacore.db import db as mc_db, migrate as mc_migrate, now_ms as mc_now_ms  # noqa: E402
from metacore.engine import MetaCoreEngine, MutationFeedback  # noqa: E402
from metacore.math_fusion import GeoPoint, generalize_habitat  # noqa: E402
from metacore.models import (  # noqa: E402
    EdgeCreate,
    Entity,
    EntityCreate,
    State,
    StateCreate,
    Transformation,
    TransformationCreate,
)
from metacore.repository import MetaCoreRepository  # noqa: E402
from metacore.visualizer import export_graph_snapshot  # noqa: E402

DAY_MS = 86_400_000
DAYS_PER_YEAR = 365.2425
MONTHS_DE = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


def open_metacore() -> tuple[MetaCoreRepository, MetaCoreEngine]:
    """Open the configured MetaCore SQLite database, applying migrations if needed."""
    mc_path = config_module.settings.resolved_metacore_db_path
    mc_path.parent.mkdir(parents=True, exist_ok=True)
    mc_settings = MetaCoreSettings(database_path=mc_path)
    mc_migrate(mc_settings)
    repo = MetaCoreRepository(mc_settings)
    return repo, MetaCoreEngine(repo)


def _is_valid_lat_lon(lat: Any, lon: Any) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90.0 <= float(lat) <= 90.0
        and -180.0 <= float(lon) <= 180.0
    )


def _positive_finite(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value) and float(value) > 0.0:
        return float(value)
    return default


def _upsert_entity(
    repo: MetaCoreRepository,
    *,
    entity_id: str,
    name: str,
    kind: str,
    category: str,
    source: str = "observed",
    parent_entity_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Entity:
    meta_dict = dict(meta or {})
    existing = repo.get_entity(entity_id)
    if existing is None:
        return repo.create_entity(
            EntityCreate(
                id=entity_id,
                name=name,
                kind=kind,  # type: ignore[arg-type]
                category=category,
                source=source,  # type: ignore[arg-type]
                parent_entity_id=parent_entity_id,
                meta=meta_dict,
            )
        )
    if (
        existing.name == name
        and existing.category == category
        and existing.source == source
        and existing.parent_entity_id == parent_entity_id
        and existing.meta == meta_dict
    ):
        return existing

    ts = mc_now_ms()
    with mc_db(repo.config) as conn:
        conn.execute(
            """
            UPDATE entities
            SET name = ?, category = ?, source = ?, parent_entity_id = ?, meta_json = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (name, category, source, parent_entity_id, json.dumps(meta_dict), ts, entity_id),
        )
    updated = repo.get_entity(entity_id)
    assert updated is not None
    return updated


def _upsert_observed_state(repo: MetaCoreRepository, payload: StateCreate) -> State:
    assert payload.id is not None
    existing = repo.get_state(payload.id)
    if existing is None:
        return repo.create_state(payload)
    updates = payload.model_dump(exclude={"id"})
    current = existing.model_dump(exclude={"id", "created_at_ms", "updated_at_ms"})
    if current == updates:
        return existing
    return repo.update_state(payload.id, **updates)


def _seed_hericium_plan(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    hericium_entity: Entity,
    hericium_coords: tuple[float, float, float] | None,
    anchor_ms: int,
) -> None:
    """Seed indoor Igel-Stachelbart climate series, fruiting modifier plan, and harvest goal."""
    if repo.get_state("goal:hericium_harvest") is not None:
        return

    box = _upsert_entity(
        repo,
        entity_id="ent:indoor_klimabox",
        name="Indoor Fruchtungsbox",
        kind="worldline",
        category="umgebung",
        meta={"environment": "indoor"},
    )

    lat = hericium_coords[0] if hericium_coords else None
    lon = hericium_coords[1] if hericium_coords else None
    sigma_m = hericium_coords[2] if hericium_coords else None

    hum_start_ms = anchor_ms - 6 * DAY_MS
    hum_0 = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:hericium_hum_0",
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Hygrometer_Box",
            property_name="humidity_pct",
            label="Luftfeuchtigkeit Start (80 ± 10 %)",
            value_num=80.0,
            value_sigma=10.0,
            value_unit="%",
            location_label="Indoor Fruchtungsbox",
            time_ms=hum_start_ms,
            sigma_seconds=60.0,
        ),
    )
    temp_early = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:hericium_temp_16",
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Thermometer_Box",
            property_name="temperature_c",
            label="Box-Temperatur Tag 0–4 (16 °C)",
            value_num=16.0,
            value_sigma=0.5,
            value_unit="°C",
            location_label="Indoor Fruchtungsbox",
            time_ms=hum_start_ms,
            sigma_seconds=60.0,
        ),
    )
    pins_obs = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:hericium_pins_obs",
            entity_id=hericium_entity.id,
            modality="observed",
            source="measured",
            measurement_source=f"indoor:{hericium_entity.meta.get('legacy_obs_id', hericium_entity.id)}",
            property_name="harvest_ready",
            label="Primordien gesichtet (Igel-Stachelbart)",
            value_num=24.0,
            value_sigma=4.0,
            value_unit="g",
            lat=lat,
            lon=lon,
            sigma_meters=sigma_m,
            location_label="Indoor Fruchtungsbox",
            time_ms=anchor_ms - 4 * DAY_MS,
            sigma_seconds=120.0,
        ),
    )

    tpl_id = "tpl:hericium_fruiting"
    if repo.get_transformation(tpl_id) is None:
        repo.create_transformation(
            TransformationCreate(
                id=tpl_id,
                name="Igel-Stachelbart Fruchtungsschub",
                modality="template",
                driver="nature",
                duration_seconds=8.0 * 86_400.0,
                duration_sigma_seconds=1.5 * 86_400.0,
                duration_source="ai_prior",
                formula={
                    "kind": "duration_step",
                    "output_property": "harvest_ready",
                    "output_unit": "g",
                    "output_value": 245.0,
                    "output_sigma": 30.0,
                    "location_label": "Indoor Fruchtungsbox",
                    "input_roles": {
                        "harvest_ready": "consumes",
                        "temperature_c": "triggers",
                        "humidity_pct": "triggers",
                    },
                    "guard_logic": "AND",
                    "guards": [
                        {
                            "entity_id": box.id,
                            "property_name": "humidity_pct",
                            "aggregation": "latest",
                            "min_value": 65.0,
                            "max_value": 98.0,
                        },
                        {
                            "entity_id": box.id,
                            "property_name": "temperature_c",
                            "aggregation": "latest",
                            "min_value": 12.0,
                            "max_value": 24.0,
                        },
                    ],
                    "modifiers": [
                        {
                            "entity_id": box.id,
                            "property_name": "temperature_c",
                            "applies_to": "duration",
                            "optimal_min": 17.5,
                            "optimal_max": 20.0,
                            "falloff_per_unit": 0.05,
                            "min_factor": 0.5,
                            "sigma_penalty_per_unit": 0.01,
                        },
                        {
                            "entity_id": box.id,
                            "property_name": "humidity_pct",
                            "applies_to": "duration",
                            "optimal_min": 75.0,
                            "optimal_max": 90.0,
                            "falloff_per_unit": 0.005,
                            "min_factor": 0.5,
                            "sigma_penalty_per_unit": 0.0,
                        },
                    ],
                },
                meta={"category": "Pilz", "species": "Hericium erinaceus"},
            )
        )

    fb = engine.instantiate_template(
        tpl_id,
        input_state_ids=[pins_obs.id, temp_early.id, hum_0.id],
        target_entity_id=hericium_entity.id,
        start_time_ms=anchor_ms - 4 * DAY_MS,
    )
    if fb.created_state_ids and lat is not None and lon is not None and sigma_m is not None:
        repo.update_state(fb.created_state_ids[0], lat=lat, lon=lon, sigma_meters=sigma_m)

    # Record recent warmup from ~16 °C to ~18 °C at steady 80 ± 10 % humidity
    engine.record_observation(
        StateCreate(
            id="st:hericium_hum_now",
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Hygrometer_Box",
            property_name="humidity_pct",
            label="Luftfeuchtigkeit aktuell (80 ± 10 %)",
            value_num=80.0,
            value_sigma=10.0,
            value_unit="%",
            location_label="Indoor Fruchtungsbox",
            time_ms=anchor_ms - DAY_MS,
            sigma_seconds=60.0,
        )
    )
    engine.record_observation(
        StateCreate(
            id="st:hericium_temp_18",
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Thermometer_Box",
            property_name="temperature_c",
            label="Box-Temperatur aktuell (18 °C)",
            value_num=18.0,
            value_sigma=0.5,
            value_unit="°C",
            location_label="Indoor Fruchtungsbox",
            time_ms=anchor_ms - DAY_MS,
            sigma_seconds=60.0,
        )
    )

    repo.create_state(
        StateCreate(
            id="goal:hericium_harvest",
            entity_id=hericium_entity.id,
            modality="desired",
            source="user_goal",
            property_name="harvest_ready",
            label="Igel-Stachelbart Ernte (≥ 180 g)",
            value_unit="g",
            location_label="Indoor Fruchtungsbox",
            target_min=180.0,
            target_latest_ms=anchor_ms + 9 * DAY_MS,
        )
    )


def _seed_outdoor_habitat_plan(
    repo: MetaCoreRepository,
    *,
    outdoor_points: list[GeoPoint],
    species_label: str,
    source_obs_ids: list[str],
    anchor_ms: int,
) -> None:
    """Seed generalized outdoor mushroom habitat, rain+temperature trigger, and spatial forecast."""
    if repo.get_state("goal:outdoor_scout") is not None:
        return

    pts = outdoor_points or [
        GeoPoint(lat=53.5580, lon=9.8110, sigma_meters=12.0, measurement_source="fallback_1")
    ]
    habitat_geo = generalize_habitat(pts, min_sigma_meters=35.0)

    weather = _upsert_entity(
        repo,
        entity_id="ent:wald_wetter",
        name="Herbstwetter Fundgebiet",
        kind="worldline",
        category="umgebung",
        meta={"environment": "outdoor"},
    )
    habitat = _upsert_entity(
        repo,
        entity_id="ent:outdoor_habitat",
        name=f"Myzel-Habitat ({species_label})",
        kind="worldline",
        category="Pilz",
        source="derived",
        meta={
            "derived_from_obs_ids": source_obs_ids,
            "habitat_sigma_meters": round(habitat_geo.sigma_meters, 2),
        },
    )

    if repo.get_transformation("tpl:outdoor_season_trigger") is None:
        repo.create_transformation(
            TransformationCreate(
                id="tpl:outdoor_season_trigger",
                name="Saison-Trigger (14d Regen + Bodentemperatur)",
                modality="template",
                driver="nature",
                duration_seconds=9.0 * 86_400.0,
                duration_sigma_seconds=3.0 * 86_400.0,
                duration_source="ai_prior",
                formula={
                    "triggers": {
                        "logic": "AND",
                        "category": "umgebung",
                        "conditions": [
                            {
                                "kind": "period_phase",
                                "clock": "year_solar",
                                "day_of_year_min": 240,
                                "day_of_year_max": 360,
                            },
                            {
                                "entity_id": weather.id,
                                "property_name": "rain_mm",
                                "aggregation": "sum",
                                "window_seconds": 14 * 86_400,
                                "min_value": 25.0,
                            },
                            {
                                "entity_id": weather.id,
                                "property_name": "temperature_c",
                                "aggregation": "mean",
                                "window_seconds": 14 * 86_400,
                                "min_value": 5.0,
                                "max_value": 16.0,
                            },
                        ],
                    }
                },
                meta={"category": "Pilz"},
            )
        )

    rain_1 = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:wald_rain_1",
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="DWD_Naehe",
            property_name="rain_mm",
            label="Niederschlag -7 d (22 mm)",
            value_num=22.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=anchor_ms - 7 * DAY_MS,
        ),
    )
    _upsert_observed_state(
        repo,
        StateCreate(
            id="st:wald_rain_2",
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="DWD_Naehe",
            property_name="rain_mm",
            label="Niederschlag -2 d (18 mm)",
            value_num=18.0,
            value_sigma=1.5,
            value_unit="mm",
            time_ms=anchor_ms - 2 * DAY_MS,
        ),
    )
    temp_ok = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:wald_temp_1",
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="DWD_Naehe",
            property_name="temperature_c",
            label="Bodentemperatur 10,5 °C",
            value_num=10.5,
            value_sigma=0.8,
            value_unit="°C",
            time_ms=anchor_ms - DAY_MS,
        ),
    )

    tx_flush = repo.create_transformation(
        TransformationCreate(
            id="tx:outdoor_next_flush",
            name=f"Herbstschub {species_label}",
            modality="planned",
            driver="nature",
            template_id="tpl:outdoor_season_trigger",
            duration_seconds=9.0 * 86_400.0,
            duration_sigma_seconds=3.0 * 86_400.0,
            duration_source="ai_prior",
            success_prob=0.90,
            formula={
                "guards": [
                    {
                        "kind": "period_phase",
                        "clock": "year_solar",
                        "day_of_year_min": 240,
                        "day_of_year_max": 360,
                    },
                    {
                        "entity_id": weather.id,
                        "property_name": "temperature_c",
                        "aggregation": "latest",
                        "min_value": 4.0,
                        "max_value": 18.0,
                    },
                    {
                        "entity_id": weather.id,
                        "property_name": "rain_mm",
                        "aggregation": "sum",
                        "window_seconds": 14 * 86_400,
                        "min_value": 25.0,
                    },
                ]
            },
            meta={"scheduled_start_ms": anchor_ms},
        )
    )

    pred_flush = repo.create_state(
        StateCreate(
            id="st:outdoor_flush_pred",
            entity_id=habitat.id,
            modality="predicted",
            source="derived",
            property_name="presence",
            label=f"Habitat-Prognose: {species_label}",
            value_num=3.0,
            value_sigma=1.0,
            value_unit="stk",
            lat=habitat_geo.lat,
            lon=habitat_geo.lon,
            sigma_meters=habitat_geo.sigma_meters,
            time_ms=anchor_ms + 8 * DAY_MS,
            sigma_seconds=2.5 * 86_400.0,
        )
    )
    repo.create_state(
        StateCreate(
            id="goal:outdoor_scout",
            entity_id=habitat.id,
            modality="desired",
            source="user_goal",
            property_name="presence",
            label=f"Kontrollgang: ≥ 1 Fund im Habitat ({species_label})",
            value_unit="stk",
            target_min=1.0,
            lat=habitat_geo.lat,
            lon=habitat_geo.lon,
            target_radius_meters=max(120.0, habitat_geo.sigma_meters * 2.5),
            target_latest_ms=anchor_ms + 16 * DAY_MS,
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_flush.id, state_id=rain_1.id, direction="in", role="triggers")
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_flush.id, state_id=temp_ok.id, direction="in", role="triggers")
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_flush.id, state_id=pred_flush.id, direction="out", role="produces")
    )


def _seed_nested_lifecycle_chain(repo: MetaCoreRepository, *, anchor_ms: int) -> None:
    """Seed a 3-step mushroom lifecycle chain with nested child steps (`parent_id`) and CHOICE_OR branches."""
    if repo.get_state("goal:chain_harvest") is not None:
        return

    charge = _upsert_entity(
        repo,
        entity_id="ent:chain_charge",
        name="Körnerbrut-Charge 400 g",
        kind="worldline",
        category="pilz_zyklus",
    )
    block = _upsert_entity(
        repo,
        entity_id="ent:chain_block",
        name="Substratblock (Buche)",
        kind="worldline",
        category="pilz_zyklus",
        parent_entity_id=charge.id,
    )
    harvest = _upsert_entity(
        repo,
        entity_id="ent:chain_harvest",
        name="Zuchtkultur Ernte-Charge",
        kind="worldline",
        category="pilz_zyklus",
        parent_entity_id=block.id,
    )

    grain = _upsert_observed_state(
        repo,
        StateCreate(
            id="st:chain_grain_obs",
            entity_id=charge.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label="400 g trockener Weizen",
            value_num=400.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=anchor_ms,
        ),
    )

    # Step 1: Körnerbrut ansetzen
    spawn_end_ms = anchor_ms + 10 * DAY_MS
    tx_spawn = repo.create_transformation(
        TransformationCreate(
            id="tx:chain_spawn",
            name="1. Körnerbrut vermehren",
            modality="planned",
            driver="actor",
            duration_seconds=10.0 * 86_400.0,
            duration_sigma_seconds=1.0 * 86_400.0,
            duration_source="ai_prior",
            success_prob=0.96,
            meta={"scheduled_start_ms": anchor_ms},
        )
    )
    pred_spawn = repo.create_state(
        StateCreate(
            id="st:chain_spawn_pred",
            entity_id=charge.id,
            modality="predicted",
            source="derived",
            property_name="spawn_mass",
            label="Durchwachsene Körnerbrut (~700 g)",
            value_num=700.0,
            value_sigma=35.0,
            value_unit="g",
            time_ms=spawn_end_ms,
            sigma_seconds=1.0 * 86_400.0,
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_spawn.id, state_id=grain.id, direction="in", role="consumes")
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_spawn.id, state_id=pred_spawn.id, direction="out", role="produces")
    )

    # Step 2 (Composite with 3 nested child steps via parent_id)
    block_end_ms = spawn_end_ms + 9 * DAY_MS
    tx_sub_parent = repo.create_transformation(
        TransformationCreate(
            id="tx:chain_substrate_parent",
            name="2. Substratblock herstellen (Composite)",
            modality="planned",
            driver="actor",
            duration_seconds=9.0 * 86_400.0,
            duration_sigma_seconds=1.1 * 86_400.0,
            duration_source="ai_prior",
            success_prob=0.95,
            meta={"scheduled_start_ms": spawn_end_ms, "is_composite": True},
        )
    )
    sub_steps = [
        ("tx:sub_soak", "2.1 Buchenspäne wässern", 1.0, 0.2, 0.99, 1),
        ("tx:sub_autoclave", "2.2 Sterilisieren (122 °C)", 0.5, 0.1, 0.98, 2),
        ("tx:sub_inoculate", "2.3 Beimpfen & Inkubieren", 7.5, 1.0, 0.98, 3),
    ]
    for cid, cname, dur_d, sig_d, prob, order in sub_steps:
        repo.create_transformation(
            TransformationCreate(
                id=cid,
                name=cname,
                modality="planned",
                driver="actor" if order < 3 else "nature",
                parent_id=tx_sub_parent.id,
                duration_seconds=dur_d * 86_400.0,
                duration_sigma_seconds=sig_d * 86_400.0,
                duration_source="ai_prior",
                success_prob=prob,
                meta={"step_order": order},
            )
        )

    pred_block = repo.create_state(
        StateCreate(
            id="st:chain_block_pred",
            entity_id=block.id,
            modality="predicted",
            source="derived",
            property_name="substrate_block_mass",
            label="Substratblock durchwachsen (2,5 kg)",
            value_num=2500.0,
            value_sigma=90.0,
            value_unit="g",
            time_ms=block_end_ms,
            sigma_seconds=1.4 * 86_400.0,
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_sub_parent.id, state_id=pred_spawn.id, direction="in", role="consumes")
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_sub_parent.id, state_id=pred_block.id, direction="out", role="produces")
    )

    # Step 3: CHOICE_OR alternatives (Standard Tent = Stage 3 at_risk vs Profi Box = Stage 2 on_track)
    tx_std = repo.create_transformation(
        TransformationCreate(
            id="tx:chain_branch_std",
            name="3A. Fruchtung (Standard-Zelt)",
            modality="planned",
            driver="nature",
            duration_seconds=11.0 * 86_400.0,
            duration_sigma_seconds=1.8 * 86_400.0,
            duration_source="ai_prior",
            cost=0.0,
            success_prob=0.80,
            meta={"scheduled_start_ms": block_end_ms},
        )
    )
    pred_std = repo.create_state(
        StateCreate(
            id="st:chain_harvest_std",
            entity_id=harvest.id,
            modality="predicted",
            source="ai_prior",
            property_name="harvest_ready",
            label="Ernte im Standard-Zelt (238 ± 35 g)",
            value_num=238.0,
            value_sigma=35.0,
            value_unit="g",
            time_ms=block_end_ms + 11 * DAY_MS,
            sigma_seconds=1.8 * 86_400.0,
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_std.id, state_id=pred_block.id, direction="in", role="consumes")
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_std.id,
            state_id=pred_std.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="standard_zelt",
            is_selected=True,
            outcome="chosen",
        )
    )

    tx_pro = repo.create_transformation(
        TransformationCreate(
            id="tx:chain_branch_pro",
            name="3B. Fruchtung (Profi-Klimabox)",
            modality="planned",
            driver="actor",
            duration_seconds=7.0 * 86_400.0,
            duration_sigma_seconds=0.8 * 86_400.0,
            duration_source="measured",
            cost=12.0,
            success_prob=0.98,
            meta={"scheduled_start_ms": block_end_ms},
        )
    )
    pred_pro = repo.create_state(
        StateCreate(
            id="st:chain_harvest_pro",
            entity_id=harvest.id,
            modality="predicted",
            source="derived",
            property_name="harvest_ready",
            label="Ernte in Profi-Klimabox (315 ± 20 g)",
            value_num=315.0,
            value_sigma=20.0,
            value_unit="g",
            time_ms=block_end_ms + 7 * DAY_MS,
            sigma_seconds=0.8 * 86_400.0,
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_pro.id, state_id=pred_block.id, direction="in", role="consumes")
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_pro.id,
            state_id=pred_pro.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="profi_klimabox",
            is_selected=False,
            outcome="pending",
        )
    )

    repo.create_state(
        StateCreate(
            id="goal:chain_harvest",
            entity_id=harvest.id,
            modality="desired",
            source="user_goal",
            property_name="harvest_ready",
            label="Zuchtkultur Ernteziel (≥ 230 g bis Tag 32)",
            value_unit="g",
            target_min=230.0,
            target_latest_ms=anchor_ms + 32 * DAY_MS,
        )
    )


def sync_legacy_observations(*, seed_plans: bool = True) -> dict[str, Any]:
    """Synchronize legacy SQLite observations/media into MetaCore worldlines idempotently."""
    repo, engine = open_metacore()
    cfg = config_module.settings
    cfg.database_path.parent.mkdir(parents=True, exist_ok=True)

    entities_synced = 0
    states_synced = 0
    latest_obs_ms = 0
    hericium_entity: Entity | None = None
    hericium_coords: tuple[float, float, float] | None = None
    outdoor_pilz_points: list[GeoPoint] = []
    outdoor_obs_ids: list[str] = []

    if cfg.database_path.is_file():
        with legacy_db(cfg) as conn:
            has_obs = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone()
            if has_obs is not None:
                obs_rows = conn.execute(
                    "SELECT id, category, title, description, environment FROM observations ORDER BY created_at_ms, id"
                ).fetchall()
                media_rows = conn.execute(
                    "SELECT id, obs_id, original_name, observed_at_ms, sigma_seconds, lat, lon, sigma_meters FROM media ORDER BY observed_at_ms IS NULL, observed_at_ms, created_at_ms, id"
                ).fetchall()
                gauss_rows = {
                    r["obs_id"]: r
                    for r in conn.execute("SELECT * FROM space_time_gauss").fetchall()
                }
            else:
                obs_rows, media_rows, gauss_rows = [], [], {}

        media_by_obs: dict[str, list[Any]] = {}
        for m in media_rows:
            media_by_obs.setdefault(m["obs_id"], []).append(m)

        for obs in obs_rows:
            obs_id = str(obs["id"])
            title = (obs["title"] or "Unbenannter Fund").strip()
            env = str(obs["environment"] or "unknown")
            ent_id = f"obs:{obs_id}"
            entity = _upsert_entity(
                repo,
                entity_id=ent_id,
                name=title,
                kind="worldline",
                category=str(obs["category"] or "Unbekannt"),
                meta={
                    "legacy_obs_id": obs_id,
                    "environment": env,
                    "description": str(obs["description"] or ""),
                },
            )
            entities_synced += 1

            for m in media_by_obs.get(obs_id, []):
                if not _is_valid_lat_lon(m["lat"], m["lon"]):
                    continue
                t_ms = m["observed_at_ms"] if isinstance(m["observed_at_ms"], int) else None
                if t_ms is not None:
                    latest_obs_ms = max(latest_obs_ms, t_ms)
                sig_m = _positive_finite(m["sigma_meters"], 12.0 if env == "indoor" else 8.0)
                if env == "indoor":
                    sig_m = max(12.0, sig_m)
                sig_s = _positive_finite(m["sigma_seconds"], 60.0) if t_ms is not None else None
                # Indoor repeated photos share one correlation source so uncertainty never shrinks artificially
                msrc = f"indoor:{obs_id}" if env == "indoor" else f"media:{m['id']}"
                _upsert_observed_state(
                    repo,
                    StateCreate(
                        id=f"media:{m['id']}:loc",
                        entity_id=entity.id,
                        modality="observed",
                        source="measured",
                        measurement_source=msrc,
                        property_name="location",
                        label=f"Foto {m['original_name'] or str(m['id'])[:8]}",
                        lat=float(m["lat"]),
                        lon=float(m["lon"]),
                        sigma_meters=sig_m,
                        time_ms=t_ms,
                        sigma_seconds=sig_s,
                        meta={"legacy_obs_id": obs_id, "legacy_media_id": str(m["id"])},
                    ),
                )
                states_synced += 1

            g = gauss_rows.get(obs_id)
            if g is not None:
                has_coords = _is_valid_lat_lon(g["lat"], g["lon"])
                g_lat = float(g["lat"]) if has_coords else None
                g_lon = float(g["lon"]) if has_coords else None
                g_sig_m = _positive_finite(g["sigma_meters"], 10.0) if has_coords else None
                g_t_ms = g["observed_at_ms"] if isinstance(g["observed_at_ms"], int) else None
                if g_t_ms is not None:
                    latest_obs_ms = max(latest_obs_ms, g_t_ms)
                g_sig_s = _positive_finite(g["sigma_seconds"], 60.0) if g_t_ms is not None else None
                try:
                    season_data = json.loads(g["season"]) if g["season"] else None
                except json.JSONDecodeError:
                    season_data = None
                try:
                    tod_data = json.loads(g["time_of_day"]) if g["time_of_day"] else None
                except json.JSONDecodeError:
                    tod_data = None

                _upsert_observed_state(
                    repo,
                    StateCreate(
                        id=f"obs:{obs_id}:presence",
                        entity_id=entity.id,
                        modality="observed",
                        source="measured",
                        measurement_source=f"obs_gauss:{obs_id}",
                        property_name="presence",
                        label=f"Fund: {title}",
                        value_num=1.0,
                        value_sigma=0.0,
                        value_unit="stk",
                        lat=g_lat,
                        lon=g_lon,
                        sigma_meters=g_sig_m,
                        time_ms=g_t_ms,
                        sigma_seconds=g_sig_s,
                        meta={
                            "legacy_obs_id": obs_id,
                            "season": season_data,
                            "time_of_day": tod_data,
                        },
                    ),
                )
                states_synced += 1

                if hericium_entity is None and (
                    "stachelbart" in title.lower() or "hericium" in title.lower()
                ):
                    hericium_entity = entity
                    if g_lat is not None and g_lon is not None and g_sig_m is not None:
                        hericium_coords = (g_lat, g_lon, max(12.0, g_sig_m))

                if env == "outdoor" and obs["category"] == "Pilz" and g_lat is not None and g_lon is not None:
                    outdoor_pilz_points.append(
                        GeoPoint(
                            lat=g_lat,
                            lon=g_lon,
                            sigma_meters=g_sig_m or 10.0,
                            measurement_source=f"obs:{obs_id}",
                        )
                    )
                    outdoor_obs_ids.append(obs_id)

    if seed_plans:
        anchor_ms = max(mc_now_ms(), latest_obs_ms)
        if hericium_entity is None:
            hericium_entity = _upsert_entity(
                repo,
                entity_id="ent:hericium_demo",
                name="Igel-Stachelbart (Hericium erinaceus)",
                kind="worldline",
                category="Pilz",
                meta={"environment": "indoor"},
            )
        _seed_hericium_plan(
            repo,
            engine,
            hericium_entity=hericium_entity,
            hericium_coords=hericium_coords,
            anchor_ms=anchor_ms,
        )
        _seed_outdoor_habitat_plan(
            repo,
            outdoor_points=outdoor_pilz_points[:4],
            species_label="Outdoor-Myzel (Pilze)",
            source_obs_ids=outdoor_obs_ids[:4],
            anchor_ms=anchor_ms,
        )
        _seed_nested_lifecycle_chain(repo, anchor_ms=anchor_ms)

    return {
        "metacore_db_path": str(cfg.resolved_metacore_db_path),
        "entities_synced": entities_synced,
        "states_synced": states_synced,
        "total_entities": len(repo.list_entities(include_archived=True)),
        "total_states": len(repo.list_states()),
    }


def _day_of_year_info(time_ms: int, tz_name: str) -> tuple[float, int, str]:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    dt = datetime.fromtimestamp(time_ms / 1000.0, tz=tz)
    sec = dt.hour * 3600 + dt.minute * 60 + dt.second
    doy_zero = (datetime(2000, dt.month, dt.day).timetuple().tm_yday - 1 + sec / 86_400.0) * DAYS_PER_YEAR / 366.0
    label = f"{dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"
    return round(doy_zero + 1.0, 2), dt.year, label


def build_live_snapshot(
    *,
    auto_sync: bool = True,
    last_feedback: MutationFeedback | None = None,
) -> dict[str, Any]:
    """Export the full live Zukunftsgraph snapshot enriched with map predictions & seasonal view."""
    if auto_sync:
        sync_legacy_observations(seed_plans=True)

    repo, engine = open_metacore()
    snap = export_graph_snapshot(
        repo,
        engine,
        title="WasWoWann Zukunftsgraph",
        subtitle="Live-Weltlinien, Habitate, Kausalpläne & 4-Stufen-Eskalation",
        last_feedback=last_feedback,
    )

    entities_by_id = {e["id"]: e for e in snap["entities"]}
    goal_by_key = {
        (g["entity_id"], g["property_name"]): g for g in snap["goal_evaluations"]
    }

    spatial_predictions: list[dict[str, Any]] = []
    seasonal_series: list[dict[str, Any]] = []
    latest_obs_ms = 0
    tz_name = config_module.settings.import_timezone

    for st in snap["states"]:
        modality = st["modality"]
        if modality == "observed" and st.get("time_ms") is not None:
            latest_obs_ms = max(latest_obs_ms, int(st["time_ms"]))

        ent = entities_by_id.get(st["entity_id"], {})
        ent_meta = ent.get("meta") or {}
        goal_eval = goal_by_key.get((st["entity_id"], st["property_name"]))

        if (
            modality == "predicted"
            and _is_valid_lat_lon(st.get("lat"), st.get("lon"))
            and st.get("sigma_meters") is not None
        ):
            spatial_predictions.append(
                {
                    "id": st["id"],
                    "entity_id": st["entity_id"],
                    "entity_name": ent.get("name", st["entity_id"]),
                    "legacy_obs_id": ent_meta.get("legacy_obs_id"),
                    "property_name": st["property_name"],
                    "label": st["label"],
                    "lat": float(st["lat"]),
                    "lon": float(st["lon"]),
                    "sigma_meters": float(st["sigma_meters"]),
                    "time_ms": st.get("time_ms"),
                    "sigma_seconds": st.get("sigma_seconds"),
                    "source": st["source"],
                    "value_num": st.get("value_num"),
                    "value_sigma": st.get("value_sigma"),
                    "value_unit": st.get("value_unit", ""),
                    "goal_id": goal_eval["goal_id"] if goal_eval else None,
                    "goal_stage": goal_eval["stage"] if goal_eval else None,
                    "goal_status": goal_eval["status"] if goal_eval else None,
                    "goal_probability": goal_eval["goal_probability"] if goal_eval else None,
                }
            )

        if (
            st.get("time_ms") is not None
            and (
                (modality == "observed" and st["property_name"] == "presence")
                or (modality == "predicted" and st["property_name"] in {"presence", "harvest_ready"})
            )
        ):
            t_ms = int(st["time_ms"])
            doy, year, date_label = _day_of_year_info(t_ms, tz_name)
            sigma_days = round(float(st["sigma_seconds"]) / 86_400.0, 2) if st.get("sigma_seconds") else 0.5
            seasonal_series.append(
                {
                    "id": st["id"],
                    "kind": "observed" if modality == "observed" else "predicted",
                    "entity_id": st["entity_id"],
                    "obs_id": ent_meta.get("legacy_obs_id"),
                    "title": ent.get("name", st["label"]),
                    "category": ent.get("category", "Pilz"),
                    "environment": ent_meta.get("environment", "unknown"),
                    "day_of_year": doy,
                    "sigma_days": sigma_days,
                    "year": year,
                    "date_label": date_label,
                    "time_ms": t_ms,
                    "lat": st.get("lat"),
                    "lon": st.get("lon"),
                    "goal_status": goal_eval["status"] if goal_eval else None,
                    "goal_probability": goal_eval["goal_probability"] if goal_eval else None,
                }
            )

    synced_count = sum(1 for e in snap["entities"] if (e.get("meta") or {}).get("legacy_obs_id"))
    ref_now = max(mc_now_ms(), latest_obs_ms)
    # Derive map/clock reference coordinates from spatial observations if present
    coord_pairs = [
        (float(s["lat"]), float(s["lon"]))
        for s in snap["states"]
        if _is_valid_lat_lon(s.get("lat"), s.get("lon"))
    ]
    ref_lat = sum(p[0] for p in coord_pairs) / len(coord_pairs) if coord_pairs else 53.55
    ref_lon = sum(p[1] for p in coord_pairs) / len(coord_pairs) if coord_pairs else 9.99

    snap["now_ms"] = ref_now
    snap["synced_observation_count"] = synced_count
    snap["spatial_predictions"] = spatial_predictions
    snap["seasonal_series"] = sorted(seasonal_series, key=lambda item: (item["day_of_year"], item["year"]))
    snap["current_clocks"] = [
        c.model_dump()
        for c in get_current_clock_phases(ref_now, tz_name=tz_name, lat=ref_lat, lon=ref_lon)
    ]
    snap["periodicity"] = engine.analyze_worldline_periods(
        property_name="presence",
        tz_name=tz_name,
        now_ms_override=ref_now,
    ).model_dump()
    return snap


def resolve_entity_id(repo: MetaCoreRepository, item_id: str) -> str | None:
    """Resolve either a MetaCore entity ID or a legacy observation ID to an existing entity ID."""
    if repo.get_entity(item_id) is not None:
        return item_id
    prefixed = f"obs:{item_id}"
    if repo.get_entity(prefixed) is not None:
        return prefixed
    return None


def inspect_worldline(item_id: str) -> dict[str, Any]:
    """Return observed history, future predictions, goals, linked transformations, and edges for one worldline."""
    repo, engine = open_metacore()
    entity_id = resolve_entity_id(repo, item_id)
    if entity_id is None:
        sync_legacy_observations(seed_plans=True)
        entity_id = resolve_entity_id(repo, item_id)
    if entity_id is None:
        raise KeyError(f"Weltlinie nicht gefunden: {item_id}")

    ent = repo.get_entity(entity_id)
    assert ent is not None
    states = repo.list_states(entity_id=ent.id)
    all_txs = repo.list_transformations()
    tx_map: dict[str, Transformation] = {}
    edges_seen: dict[str, dict[str, Any]] = {}

    for st in states:
        for edge in repo.list_edges(state_id=st.id):
            edges_seen[edge.id] = edge.model_dump()
            if edge.transformation_id not in tx_map:
                tx = repo.get_transformation(edge.transformation_id)
                if tx is not None:
                    tx_map[tx.id] = tx

    # Include nested child transformations attached to any linked composite transformation
    added = True
    while added:
        added = False
        for tx in all_txs:
            if tx.parent_id in tx_map and tx.id not in tx_map:
                tx_map[tx.id] = tx
                added = True

    goal_evals = [
        engine.evaluate_goal(s.id).model_dump()
        for s in states
        if s.modality == "desired" or (s.modality == "cancelled" and s.source == "user_goal")
    ]

    return {
        "entity": ent.model_dump(),
        "legacy_obs_id": ent.meta.get("legacy_obs_id"),
        "history_observed": [s.model_dump() for s in states if s.modality == "observed"],
        "future_predicted": [s.model_dump() for s in states if s.modality == "predicted"],
        "goals_desired": [s.model_dump() for s in states if s.modality in ("desired", "cancelled")],
        "transformations": [t.model_dump() for t in tx_map.values()],
        "edges": list(edges_seen.values()),
        "goal_evaluations": goal_evals,
        "periodicity": engine.analyze_worldline_periods(
            entity_id=ent.id,
            tz_name=config_module.settings.import_timezone,
        ).model_dump(),
    }


def analyze_custom_period_samples(
    timestamps_ms: list[int],
    *,
    label: str = "Benutzerdefinierte Reihe",
    tidal_offset_hours: float = 0.0,
    now_ms_override: int | None = None,
) -> dict[str, Any]:
    """Analyze an arbitrary timestamp sequence across all clocks and intervals."""
    samples = [TimeSample(time_ms=int(ts)) for ts in timestamps_ms]
    tz_name = config_module.settings.import_timezone
    ref_now = now_ms_override if now_ms_override is not None else (max(timestamps_ms) if timestamps_ms else mc_now_ms())
    return analyze_periodicity(
        samples,
        tz_name=tz_name,
        now_ms=ref_now,
        tidal_offset_hours=tidal_offset_hours,
        label=label,
    ).model_dump()
