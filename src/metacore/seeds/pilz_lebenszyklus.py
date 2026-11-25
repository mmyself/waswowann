"""Reference seed: Complete Hericium lifecycle with 3 macro-steps and nested child steps."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_pilz_lebenszyklus(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    t0_ms: int = 1_795_000_000_000,
) -> dict[str, str]:
    """Seed a multi-step mushroom lifecycle chain with hierarchical sub-steps inside Spawn Prep."""
    charge = repo.create_entity(
        EntityCreate(
            name="Weizencharge 400 g (Igel-Stachelbart)",
            kind="worldline",
            category="pilz_zyklus",
            meta={"species": "Igel-Stachelbart"},
        )
    )
    autoclave = repo.create_entity(
        EntityCreate(name="Autoklav", kind="worldline", category="pilz_zyklus")
    )
    incubator = repo.create_entity(
        EntityCreate(name="Inkubator", kind="worldline", category="pilz_zyklus")
    )
    sawdust_ent = repo.create_entity(
        EntityCreate(name="Buchen-Sägespäne", kind="worldline", category="rezept")
    )
    water_ent = repo.create_entity(
        EntityCreate(name="Wasser", kind="worldline", category="rezept")
    )
    block = repo.create_entity(
        EntityCreate(
            name="Substratblock (Hericium)",
            kind="worldline",
            category="rezept",
            parent_entity_id=charge.id,
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
    harvest_ent = repo.create_entity(
        EntityCreate(
            name="Igel-Stachelbart Ernte",
            kind="worldline",
            category="Pilz",
            parent_entity_id=block.id,
        )
    )

    # Initial measured observations at t0
    dry_grain = repo.create_state(
        StateCreate(
            entity_id=charge.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label="400 g trockener Weizen",
            value_num=400.0,
            value_sigma=5.0,
            value_unit="g",
            time_ms=t0_ms,
        )
    )
    autoclave_slot = repo.create_state(
        StateCreate(
            entity_id=autoclave.id,
            modality="observed",
            source="measured",
            property_name="occupancy",
            label="Autoklav-Kammer bereit",
            value_text="bereit",
            time_ms=t0_ms,
        )
    )
    steril_temp = repo.create_state(
        StateCreate(
            entity_id=autoclave.id,
            modality="observed",
            source="measured",
            measurement_source="Autoklav_Sensor",
            property_name="steril_temp_c",
            label="Autoklav 122 °C",
            value_num=122.0,
            value_sigma=0.3,
            value_unit="°C",
            time_ms=t0_ms,
        )
    )
    incubator_temp = repo.create_state(
        StateCreate(
            entity_id=incubator.id,
            modality="observed",
            source="measured",
            measurement_source="Inkubator_Sensor",
            property_name="temperature_c",
            label="Inkubator 21,5 °C",
            value_num=21.5,
            value_sigma=0.5,
            value_unit="°C",
            time_ms=t0_ms,
        )
    )
    sawdust = repo.create_state(
        StateCreate(
            entity_id=sawdust_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="substrate_mass",
            label="1000 g Buchen-Sägespäne",
            value_num=1000.0,
            value_sigma=10.0,
            value_unit="g",
            time_ms=t0_ms,
        )
    )
    water = repo.create_state(
        StateCreate(
            entity_id=water_ent.id,
            modality="observed",
            source="measured",
            measurement_source="Messbecher",
            property_name="water_mass",
            label="1500 g Wasser",
            value_num=1500.0,
            value_sigma=15.0,
            value_unit="g",
            time_ms=t0_ms,
        )
    )
    box_hum = repo.create_state(
        StateCreate(
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Hygrometer_Box",
            property_name="humidity_pct",
            label="Box-Feuchte 80 ± 10 %",
            value_num=80.0,
            value_sigma=10.0,
            value_unit="%",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0_ms,
        )
    )
    box_temp = repo.create_state(
        StateCreate(
            entity_id=box.id,
            modality="observed",
            source="measured",
            measurement_source="Thermometer_Box",
            property_name="temperature_c",
            label="Box-Temperatur 18 °C",
            value_num=18.0,
            value_sigma=0.5,
            value_unit="°C",
            location_label="Indoor Fruchtungsbox",
            time_ms=t0_ms,
        )
    )

    # Macro-Step 1: Nested Composite Template "Körnerbrut herstellen" (3 child templates via parent_id)
    tpl_spawn_parent = repo.create_transformation(
        TransformationCreate(
            name="Körnerbrut herstellen",
            modality="template",
            driver="actor",
            duration_seconds=15.5 * 86_400,
            duration_sigma_seconds=1.5 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "output_property": "spawn_mass",
                "output_unit": "g",
                "input_roles": {
                    "mass": "consumes",
                    "occupancy": "triggers",
                    "steril_temp_c": "triggers",
                    "temperature_c": "triggers",
                },
            },
            meta={"category": "pilz_zyklus", "species": "Igel-Stachelbart"},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="1.1 Weizen einweichen & abkochen",
            modality="template",
            driver="actor",
            parent_id=tpl_spawn_parent.id,
            duration_seconds=1.0 * 86_400,
            duration_sigma_seconds=0.1 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "product_and_split",
                "input_roles": {"mass": "consumes"},
                "output_property": "hydrated_mass",
                "output_unit": "g",
                "factor": {"mean": 1.75, "sigma": 0.10, "source": "ai_prior"},
                "split_parts": 1,
            },
            meta={"category": "pilz_zyklus", "step_order": 1},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="1.2 Abfüllen & Sterilisieren (Autoklav)",
            modality="template",
            driver="actor",
            parent_id=tpl_spawn_parent.id,
            duration_seconds=0.5 * 86_400,
            duration_sigma_seconds=0.08 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "inherit_input_value": True,
                "output_property": "sterile_mass",
                "output_unit": "g",
                "input_roles": {
                    "hydrated_mass": "consumes",
                    "occupancy": "uses",
                    "steril_temp_c": "triggers",
                },
                "guards": [
                    {
                        "entity_id": autoclave.id,
                        "property_name": "steril_temp_c",
                        "aggregation": "latest",
                        "min_value": 121.0,
                    }
                ],
            },
            meta={"category": "pilz_zyklus", "step_order": 2},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="1.3 Impfen & Durchwachsen (Inkubator)",
            modality="template",
            driver="nature",
            parent_id=tpl_spawn_parent.id,
            duration_seconds=14.0 * 86_400,
            duration_sigma_seconds=1.5 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "inherit_input_value": True,
                "output_property": "spawn_mass",
                "output_unit": "g",
                "input_roles": {
                    "sterile_mass": "consumes",
                    "temperature_c": "triggers",
                },
                "guards": [
                    {
                        "entity_id": incubator.id,
                        "property_name": "temperature_c",
                        "aggregation": "latest",
                        "min_value": 18.0,
                        "max_value": 25.0,
                    }
                ],
                "modifiers": [
                    {
                        "entity_id": incubator.id,
                        "property_name": "temperature_c",
                        "applies_to": "duration",
                        "optimal_min": 20.0,
                        "optimal_max": 24.0,
                        "falloff_per_unit": 0.04,
                        "min_factor": 0.5,
                        "sigma_penalty_per_unit": 0.01,
                    }
                ],
            },
            meta={"category": "pilz_zyklus", "step_order": 3},
        )
    )

    fb_spawn = engine.instantiate_template(
        tpl_spawn_parent.id,
        input_state_ids=[dry_grain.id, autoclave_slot.id, steril_temp.id, incubator_temp.id],
        target_entity_id=charge.id,
        start_time_ms=t0_ms,
    )
    pred_spawn_id = fb_spawn.created_state_ids[0]
    planned_txs = repo.list_transformations(modality="planned")
    spawn_parent_tx_id = next(t.id for t in planned_txs if t.template_id == tpl_spawn_parent.id)

    # Macro-Step 2: Substrat mischen & durchwachsen (takes pred_spawn + sawdust + water)
    tpl_sub = repo.create_transformation(
        TransformationCreate(
            name="Substrat mischen & durchwachsen",
            modality="template",
            driver="actor",
            duration_seconds=10.0 * 86_400,
            duration_sigma_seconds=1.0 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "recipe_mix",
                "mode": "weighted_sum",
                "output_property": "substrate_block_mass",
                "output_unit": "g",
                "split_parts": 1,
                "ingredients": [
                    {"property_name": "spawn_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                    {"property_name": "substrate_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                    {"property_name": "water_mass", "weight": 1.0, "weight_sigma": 0.0, "role": "consumes"},
                ],
                "ratio_guards": [
                    {
                        "kind": "ratio",
                        "numerator_entity_id": water_ent.id,
                        "numerator_property": "water_mass",
                        "denominator_entity_id": sawdust_ent.id,
                        "denominator_property": "substrate_mass",
                        "min_ratio": 1.35,
                        "max_ratio": 1.65,
                    }
                ],
            },
            meta={"category": "pilz_zyklus"},
        )
    )
    pred_spawn_state = repo.get_state(pred_spawn_id)
    assert pred_spawn_state is not None
    fb_block = engine.instantiate_template(
        tpl_sub.id,
        input_state_ids=[pred_spawn_id, sawdust.id, water.id],
        target_entity_id=block.id,
        start_time_ms=pred_spawn_state.time_ms,
    )
    pred_block_id = fb_block.created_state_ids[0]

    # Macro-Step 3: Fruchtungsschub (Indoor-Box)
    tpl_fruit = repo.create_transformation(
        TransformationCreate(
            name="Fruchtungsschub (Indoor-Box)",
            modality="template",
            driver="nature",
            duration_seconds=8.0 * 86_400,
            duration_sigma_seconds=1.0 * 86_400,
            duration_source="ai_prior",
            formula={
                "kind": "duration_step",
                "output_property": "harvest_ready",
                "output_unit": "g",
                "output_value": 380.0,
                "output_sigma": 45.0,
                "location_label": "Indoor Fruchtungsbox",
                "input_roles": {
                    "substrate_block_mass": "consumes",
                    "humidity_pct": "triggers",
                    "temperature_c": "triggers",
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
            },
            meta={"category": "pilz_zyklus"},
        )
    )
    pred_block_state = repo.get_state(pred_block_id)
    assert pred_block_state is not None
    fb_harvest = engine.instantiate_template(
        tpl_fruit.id,
        input_state_ids=[pred_block_id, box_hum.id, box_temp.id],
        target_entity_id=harvest_ent.id,
        start_time_ms=pred_block_state.time_ms,
    )
    pred_harvest_id = fb_harvest.created_state_ids[0]

    goal = repo.create_state(
        StateCreate(
            entity_id=harvest_ent.id,
            modality="desired",
            source="user_goal",
            property_name="harvest_ready",
            label="Igel-Stachelbart Ernte (≥ 250 g bis Tag 38)",
            value_unit="g",
            location_label="Indoor Fruchtungsbox",
            target_min=250.0,
            target_latest_ms=t0_ms + 38 * 86_400_000,
        )
    )

    return {
        "charge_id": charge.id,
        "autoclave_id": autoclave.id,
        "incubator_id": incubator.id,
        "sawdust_ent_id": sawdust_ent.id,
        "water_ent_id": water_ent.id,
        "block_id": block.id,
        "box_id": box.id,
        "harvest_ent_id": harvest_ent.id,
        "tpl_spawn_parent_id": tpl_spawn_parent.id,
        "tpl_sub_id": tpl_sub.id,
        "tpl_fruit_id": tpl_fruit.id,
        "spawn_parent_tx_id": spawn_parent_tx_id,
        "pred_spawn_id": pred_spawn_id,
        "pred_block_id": pred_block_id,
        "pred_harvest_id": pred_harvest_id,
        "goal_id": goal.id,
    }
