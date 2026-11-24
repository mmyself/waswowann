"""Reference seed: Parasol mushroom (Riesenschirmling) multi-year habitat prediction."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.math_fusion import GeoPoint, generalize_habitat
from metacore.models import EdgeCreate, EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_riesenschirmling(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    now_oct_ms: int = 1_795_000_000_000,
) -> dict[str, str]:
    """Seed two year-1 finds ~180 m apart, derived habitat area, rain trigger, and frost guard."""
    weather = repo.create_entity(
        EntityCreate(name="Herbstwetter Nordwiese", kind="worldline", category="pilz")
    )
    find_1_ent = repo.create_entity(
        EntityCreate(name="Riesenschirmling Fund #1", kind="worldline", category="pilz")
    )
    find_2_ent = repo.create_entity(
        EntityCreate(name="Riesenschirmling Fund #2", kind="worldline", category="pilz")
    )

    pt1 = GeoPoint(lat=50.8120, lon=12.9140, sigma_meters=12.0, measurement_source="Foto_1")
    pt2 = GeoPoint(lat=50.8120, lon=12.91656, sigma_meters=15.0, measurement_source="Foto_2")
    habitat_geo = generalize_habitat([pt1, pt2])

    year1_ms = now_oct_ms - 365 * 86_400_000
    f1 = repo.create_state(
        StateCreate(
            entity_id=find_1_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Foto_1",
            property_name="presence",
            label="Riesenschirmling #1 (Jahr 1)",
            value_num=1.0,
            value_sigma=0.0,
            value_unit="stk",
            lat=pt1.lat,
            lon=pt1.lon,
            sigma_meters=pt1.sigma_meters,
            time_ms=year1_ms,
            sigma_seconds=600.0,
        )
    )
    f2 = repo.create_state(
        StateCreate(
            entity_id=find_2_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Foto_2",
            property_name="presence",
            label="Riesenschirmling #2 (~180 m, Jahr 1)",
            value_num=1.0,
            value_sigma=0.0,
            value_unit="stk",
            lat=pt2.lat,
            lon=pt2.lon,
            sigma_meters=pt2.sigma_meters,
            time_ms=year1_ms + 3 * 86_400_000,
            sigma_seconds=600.0,
        )
    )

    habitat = repo.create_entity(
        EntityCreate(
            name="Myzelgebiet Nordwiese",
            kind="worldline",
            category="pilz",
            source="derived",
            meta={"habitat_sigma_meters": habitat_geo.sigma_meters},
        )
    )

    tpl_trigger = repo.create_transformation(
        TransformationCreate(
            name="Schirmling Saison-Trigger (Herbstregen)",
            modality="template",
            driver="nature",
            formula={"trigger": {"property_name": "rain_mm", "category": "pilz", "min_value": 35.0}},
            meta={"category": "pilz"},
        )
    )

    temp_ok = repo.create_state(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            measurement_source="Wetterstation",
            property_name="temperature_c",
            label="Bodentemperatur +11 °C",
            value_num=11.0,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=now_oct_ms,
        )
    )

    tx_flush = repo.create_transformation(
        TransformationCreate(
            name="Herbstschub Myzelgebiet Nordwiese",
            modality="planned",
            driver="nature",
            duration_seconds=21 * 86_400,
            duration_sigma_seconds=10 * 86_400,
            success_prob=0.90,
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
            label="Prognose: Riesenschirmlinge in Nordwiese",
            value_num=3.0,
            value_sigma=1.0,
            value_unit="stk",
            lat=habitat_geo.lat,
            lon=habitat_geo.lon,
            sigma_meters=habitat_geo.sigma_meters,
            time_ms=now_oct_ms + 21 * 86_400_000,
            sigma_seconds=7 * 86_400,
        )
    )

    goal = repo.create_state(
        StateCreate(
            entity_id=habitat.id,
            modality="desired",
            source="user_goal",
            property_name="presence",
            label="Vorhersage prüfen: ≥ 1 Schirmling im Habitat",
            value_unit="stk",
            target_min=1.0,
            lat=habitat_geo.lat,
            lon=habitat_geo.lon,
            target_radius_meters=300.0,
            target_latest_ms=now_oct_ms + 45 * 86_400_000,
        )
    )

    repo.create_edge(
        EdgeCreate(transformation_id=tx_flush.id, state_id=temp_ok.id, direction="in", role="triggers")
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_flush.id, state_id=pred_flush.id, direction="out", role="produces"
        )
    )

    return {
        "weather_id": weather.id,
        "find1_state_id": f1.id,
        "find2_state_id": f2.id,
        "habitat_id": habitat.id,
        "trigger_template_id": tpl_trigger.id,
        "flush_tx_id": tx_flush.id,
        "pred_flush_id": pred_flush.id,
        "goal_id": goal.id,
    }
