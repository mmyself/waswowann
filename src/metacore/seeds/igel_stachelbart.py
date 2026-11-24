"""Reference seed: Igel-Stachelbart (Hericium erinaceus) indoor fruiting & template calibration."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_igel_stachelbart(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    t0_ms: int = 1_794_767_346_000,
) -> dict[str, str]:
    """Seed Hericium erinaceus indoor observations (same source) and fruiting template."""
    hericium = repo.create_entity(
        EntityCreate(
            name="Igel-Stachelbart (Hericium erinaceus)",
            kind="worldline",
            category="Pilz",
            meta={"environment": "indoor"},
        )
    )

    # Repeated indoor phone photos share the same measurement_source -> sigma must NOT shrink artificially
    sample_offsets_days = (0, 2, 4, 6)
    location_state_ids: list[str] = []
    for day in sample_offsets_days:
        st_loc = repo.create_state(
            StateCreate(
                entity_id=hericium.id,
                modality="observed",
                source="measured",
                measurement_source="Handy_Indoor",
                property_name="location",
                label=f"Indoor-Foto Tag {day}",
                lat=53.55828,
                lon=9.80831,
                sigma_meters=12.0,
                location_label="Indoor Fruchtungsbox",
                time_ms=t0_ms + day * 86_400_000,
                sigma_seconds=30.0,
            )
        )
        location_state_ids.append(st_loc.id)

    pins_obs = repo.create_state(
        StateCreate(
            entity_id=hericium.id,
            modality="observed",
            source="measured",
            measurement_source="Handy_Indoor",
            property_name="harvest_ready",
            label="Erste Primordien (Igel-Stachelbart)",
            value_num=20.0,
            value_sigma=5.0,
            value_unit="g",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0_ms,
            sigma_seconds=30.0,
        )
    )

    # Fruiting template starts with an ai_prior duration (~8 days ± 2 d) until real harvest calibrates it
    tpl_flush = repo.create_transformation(
        TransformationCreate(
            name="Igel-Stachelbart Fruchtungsschub",
            modality="template",
            driver="nature",
            duration_seconds=8 * 86_400,
            duration_sigma_seconds=2 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "output_property": "harvest_ready",
                "output_unit": "g",
                "output_value": 280.0,
                "output_sigma": 45.0,
                "location_label": "Indoor Fruchtungsbox",
            },
            meta={"category": "Pilz", "species": "Hericium erinaceus"},
        )
    )

    fb = engine.instantiate_template(
        tpl_flush.id,
        input_state_ids=[pins_obs.id],
        target_entity_id=hericium.id,
        start_time_ms=t0_ms,
    )

    goal = repo.create_state(
        StateCreate(
            entity_id=hericium.id,
            modality="desired",
            source="user_goal",
            property_name="harvest_ready",
            label="Igel-Stachelbart erntereif (≥ 200 g bis Tag 12)",
            value_unit="g",
            location_label="Indoor Fruchtungsbox",
            target_min=200.0,
            target_latest_ms=t0_ms + 12 * 86_400_000,
        )
    )

    return {
        "hericium_id": hericium.id,
        "pins_obs_id": pins_obs.id,
        "template_id": tpl_flush.id,
        "pred_harvest_id": fb.created_state_ids[0],
        "goal_id": goal.id,
        "first_loc_state_id": location_state_ids[0],
    }
