"""Reference seed: Multi-ingredient mushroom substrate mix with water/substrate ratio & temp modifier."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_substrat_rezept(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    substrate_grams: float = 500.0,
    water_grams: float = 750.0,
    spawn_grams: float = 150.0,
    incubator_temp_c: float = 21.0,
    start_ms: int = 1_795_000_000_000,
) -> dict[str, str]:
    """Seed a multi-input recipe_mix substrate scenario with ratio guard and temp modifier."""
    batch = repo.create_entity(
        EntityCreate(name="Substrat-Charge (Igel-Stachelbart)", kind="worldline", category="rezept")
    )
    substrate_ent = repo.create_entity(
        EntityCreate(name="Buchen-Sägespäne", kind="worldline", category="rezept")
    )
    water_ent = repo.create_entity(
        EntityCreate(name="Wasser-Zugabe", kind="worldline", category="rezept")
    )
    spawn_ent = repo.create_entity(
        EntityCreate(name="Körnerbrut-Impfung", kind="worldline", category="rezept")
    )
    incubator = repo.create_entity(
        EntityCreate(name="Inkubator (21 °C)", kind="worldline", category="rezept")
    )
    block_1 = repo.create_entity(
        EntityCreate(
            name="Substratblock #1",
            kind="worldline",
            category="rezept",
            parent_entity_id=batch.id,
        )
    )
    block_2 = repo.create_entity(
        EntityCreate(
            name="Substratblock #2",
            kind="worldline",
            category="rezept",
            parent_entity_id=batch.id,
        )
    )

    substrate_obs = repo.create_state(
        StateCreate(
            entity_id=substrate_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="substrate_mass",
            label=f"Buchen-Sägespäne {substrate_grams:.0f} g",
            value_num=substrate_grams,
            value_sigma=5.0,
            value_unit="g",
            time_ms=start_ms,
        )
    )
    water_obs = repo.create_state(
        StateCreate(
            entity_id=water_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Messbecher",
            property_name="water_mass",
            label=f"Wasser {water_grams:.0f} g",
            value_num=water_grams,
            value_sigma=10.0,
            value_unit="g",
            time_ms=start_ms,
        )
    )
    spawn_obs = repo.create_state(
        StateCreate(
            entity_id=spawn_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="spawn_mass",
            label=f"Körnerbrut {spawn_grams:.0f} g",
            value_num=spawn_grams,
            value_sigma=5.0,
            value_unit="g",
            time_ms=start_ms,
        )
    )
    temp_obs = repo.create_state(
        StateCreate(
            entity_id=incubator.id,
            modality="observed",
            source="measured",
            measurement_source="Inkubator_Sensor",
            property_name="temperature_c",
            label=f"Inkubator-Temperatur {incubator_temp_c:.0f} °C",
            value_num=incubator_temp_c,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=start_ms,
        )
    )

    tpl = repo.create_transformation(
        TransformationCreate(
            name="Substrat mischen & beimpfen",
            modality="template",
            driver="actor",
            duration_seconds=14 * 86_400,
            duration_sigma_seconds=2 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "recipe_mix",
                "mode": "weighted_sum",
                "output_property": "total_mass",
                "output_unit": "g",
                "split_parts": 2,
                "split_sigma": 8.0,
                "ingredients": [
                    {"property_name": "substrate_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                    {"property_name": "water_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                    {"property_name": "spawn_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                ],
                "input_roles": {"temperature_c": "triggers"},
                "ratio_guards": [
                    {
                        "kind": "ratio",
                        "numerator_entity_id": water_ent.id,
                        "numerator_property": "water_mass",
                        "denominator_entity_id": substrate_ent.id,
                        "denominator_property": "substrate_mass",
                        "min_ratio": 1.35,
                        "max_ratio": 1.65,
                    }
                ],
                "modifiers": [
                    {
                        "entity_id": incubator.id,
                        "property_name": "temperature_c",
                        "applies_to": "duration",
                        "optimal_min": 18.0,
                        "optimal_max": 24.0,
                        "falloff_per_unit": 0.03,
                        "min_factor": 0.5,
                        "sigma_penalty_per_unit": 0.01,
                    }
                ],
            },
            meta={"category": "rezept", "species": "Igel-Stachelbart"},
        )
    )

    fb = engine.instantiate_template(
        tpl.id,
        input_state_ids=[substrate_obs.id, water_obs.id, spawn_obs.id, temp_obs.id],
        target_entity_id=batch.id,
        start_time_ms=start_ms,
        split_parts=2,
        split_entities=[block_1.id, block_2.id],
    )

    goal = repo.create_state(
        StateCreate(
            entity_id=batch.id,
            modality="desired",
            source="user_goal",
            property_name="total_mass",
            label="≥ 1300 g optimales Substrat (Wasser:Späne 1,35–1,65)",
            value_unit="g",
            target_min=1300.0,
            target_preferred=1400.0,
            target_latest_ms=start_ms + 20 * 86_400_000,
        )
    )

    return {
        "batch_id": batch.id,
        "substrate_ent_id": substrate_ent.id,
        "water_ent_id": water_ent.id,
        "spawn_ent_id": spawn_ent.id,
        "incubator_id": incubator.id,
        "substrate_obs_id": substrate_obs.id,
        "water_obs_id": water_obs.id,
        "spawn_obs_id": spawn_obs.id,
        "temp_obs_id": temp_obs.id,
        "template_id": tpl.id,
        "total_pred_id": fb.created_state_ids[0],
        "block_1_id": block_1.id,
        "block_2_id": block_2.id,
        "block_1_pred_id": fb.created_state_ids[1],
        "block_2_pred_id": fb.created_state_ids[2],
        "goal_id": goal.id,
    }
