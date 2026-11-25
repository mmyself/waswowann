"""Reference seed: Igel-Stachelbart (Hericium erinaceus) indoor fruiting & climate tracking."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_igel_stachelbart(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    t0_ms: int = 1_794_767_346_000,
    include_warmup_phase: bool = True,
) -> dict[str, str]:
    """Seed Hericium erinaceus indoor observations, box climate series, and fruiting template."""
    hericium = repo.create_entity(
        EntityCreate(
            name="Igel-Stachelbart (Hericium erinaceus)",
            kind="worldline",
            category="Pilz",
            meta={"environment": "indoor"},
        )
    )
    box = repo.create_entity(
        EntityCreate(
            name="Indoor Fruchtungsbox",
            kind="worldline",
            category="umgebung",
            meta={"environment": "indoor"},
        )
    )

    # Repeated indoor phone photos share the same measurement_source -> sigma must NOT shrink artificially
    sample_offsets_days = (0, 2, 4, 6) if include_warmup_phase else (0, 2)
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

    # Track initial indoor box climate (days 0 & 2: ~16 °C and 80 ± 10 % humidity)
    temp_day0_id = ""
    hum_day0_id = ""
    for day in (0, 2):
        hum_st = repo.create_state(
            StateCreate(
                entity_id=box.id,
                modality="observed",
                source="measured",
                measurement_source="Hygrometer_Box",
                property_name="humidity_pct",
                label=f"Luftfeuchtigkeit Tag {day} (80 ± 10 %)",
                value_num=80.0,
                value_sigma=10.0,
                value_unit="%",
                location_label="Indoor Fruchtungsbox",
                time_ms=t0_ms + day * 86_400_000,
                sigma_seconds=30.0,
            )
        )
        temp_st = repo.create_state(
            StateCreate(
                entity_id=box.id,
                modality="observed",
                source="measured",
                measurement_source="Thermometer_Box",
                property_name="temperature_c",
                label=f"Box-Temperatur Tag {day} (16 °C)",
                value_num=16.0,
                value_sigma=0.5,
                value_unit="°C",
                location_label="Indoor Fruchtungsbox",
                time_ms=t0_ms + day * 86_400_000,
                sigma_seconds=30.0,
            )
        )
        if day == 0:
            hum_day0_id = hum_st.id
            temp_day0_id = temp_st.id

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

    # Fruiting template starts with an ai_prior duration (~8 days ± 2 d) modulated by temp/humidity
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
                        "min_value": 60.0,
                        "max_value": 100.0,
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
                        "optimal_min": 17.0,
                        "optimal_max": 20.0,
                        "falloff_per_unit": 0.04,
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

    # Keep pins_obs.id first so in_states[0].time_ms remains t0_ms for harvest duration calibration
    fb = engine.instantiate_template(
        tpl_flush.id,
        input_state_ids=[pins_obs.id, temp_day0_id, hum_day0_id],
        target_entity_id=hericium.id,
        start_time_ms=t0_ms,
    )

    if include_warmup_phase:
        # From day 4 onward temperature rose to ~18 °C while humidity remained ~80 ± 10 %
        for day in (4, 6):
            engine.record_observation(
                StateCreate(
                    entity_id=box.id,
                    modality="observed",
                    source="measured",
                    measurement_source="Hygrometer_Box",
                    property_name="humidity_pct",
                    label=f"Luftfeuchtigkeit Tag {day} (80 ± 10 %)",
                    value_num=80.0,
                    value_sigma=10.0,
                    value_unit="%",
                    location_label="Indoor Fruchtungsbox",
                    time_ms=t0_ms + day * 86_400_000,
                    sigma_seconds=30.0,
                )
            )
            engine.record_observation(
                StateCreate(
                    entity_id=box.id,
                    modality="observed",
                    source="measured",
                    measurement_source="Thermometer_Box",
                    property_name="temperature_c",
                    label=f"Box-Temperatur Tag {day} (18 °C)",
                    value_num=18.0,
                    value_sigma=0.5,
                    value_unit="°C",
                    location_label="Indoor Fruchtungsbox",
                    time_ms=t0_ms + day * 86_400_000,
                    sigma_seconds=30.0,
                )
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
        "box_id": box.id,
        "pins_obs_id": pins_obs.id,
        "temp_day0_id": temp_day0_id,
        "hum_day0_id": hum_day0_id,
        "template_id": tpl_flush.id,
        "pred_harvest_id": fb.created_state_ids[0],
        "goal_id": goal.id,
        "first_loc_state_id": location_state_ids[0],
    }
