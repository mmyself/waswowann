"""Reference seed: Grain spawn (Körnerbrut 400 g -> 2 bags) with ai_prior calibration."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_koernerbrut(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    dry_grams: float = 400.0,
    bags: int = 2,
    species: str = "Igel-Stachelbart",
    start_ms: int = 1_795_000_000_000,
) -> dict[str, str]:
    """Create entities, template, dry grain observation, instantiation, and mass goal."""
    charge = repo.create_entity(
        EntityCreate(
            name=f"Weizencharge {int(dry_grams)} g ({species})",
            kind="worldline",
            category="rezept",
            meta={"species": species},
        )
    )
    incubator = repo.create_entity(
        EntityCreate(name="Inkubator", kind="worldline", category="rezept")
    )
    bag_entities = [
        repo.create_entity(
            EntityCreate(
                name=f"Beutel #{i}",
                kind="worldline",
                category="rezept",
                parent_entity_id=charge.id,
            )
        )
        for i in range(1, bags + 1)
    ]

    dry_obs = repo.create_state(
        StateCreate(
            entity_id=charge.id,
            modality="observed",
            source="measured",
            measurement_source="Waage_Kueche",
            property_name="mass",
            label=f"{int(dry_grams)} g trockener Weizen",
            value_num=dry_grams,
            value_sigma=0.0,
            value_unit="g",
            time_ms=start_ms,
        )
    )
    inc_slot = repo.create_state(
        StateCreate(
            entity_id=incubator.id,
            modality="observed",
            source="measured",
            property_name="occupancy",
            label="Inkubator-Regal",
            value_text="bereit",
            time_ms=start_ms,
        )
    )

    tpl = repo.create_transformation(
        TransformationCreate(
            name="Weizen einweichen & aufteilen",
            modality="template",
            driver="actor",
            duration_seconds=3 * 86_400,
            duration_sigma_seconds=3600.0,
            duration_source="ai_prior",
            formula={
                "kind": "product_and_split",
                "input_roles": {"mass": "consumes", "occupancy": "uses"},
                "output_property": "mass",
                "output_unit": "g",
                "factor": {"mean": 1.75, "sigma": 0.10, "source": "ai_prior"},
                "split_parts": bags,
                "split_sigma": 6.0,
            },
            meta={"category": "rezept", "species": species},
        )
    )

    fb = engine.instantiate_template(
        tpl.id,
        input_state_ids=[dry_obs.id, inc_slot.id],
        target_entity_id=charge.id,
        start_time_ms=start_ms,
        split_parts=bags,
        split_entities=[b.id for b in bag_entities],
    )

    goal = repo.create_state(
        StateCreate(
            entity_id=charge.id,
            modality="desired",
            source="user_goal",
            property_name="mass",
            label="Mindestens 600 g abgetropfte Körnerbrut",
            value_unit="g",
            target_min=600.0,
            target_preferred=700.0,
            target_latest_ms=start_ms + 5 * 86_400_000,
        )
    )

    result: dict[str, str] = {
        "charge_id": charge.id,
        "incubator_id": incubator.id,
        "dry_obs_id": dry_obs.id,
        "template_id": tpl.id,
        "total_pred_id": fb.created_state_ids[0],
        "goal_id": goal.id,
    }
    for i, b in enumerate(bag_entities, start=1):
        result[f"bag_{i}_id"] = b.id
        result[f"bag_{i}_pred_id"] = fb.created_state_ids[i]
    return result
