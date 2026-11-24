"""Reference seed: Robot R1 fetching a glass of water with `uses` conflicts and CHOICE_OR fallbacks."""

from __future__ import annotations

from metacore.engine import MetaCoreEngine
from metacore.models import EdgeCreate, EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def seed_roboter_wasser(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    t_now_ms: int = 1_795_000_000_000,
) -> dict[str, str]:
    """Seed Robot R1, Greifer 1 `uses` conflict, CHOICE_OR door fallbacks, and Couchtisch goal."""
    gripper1 = repo.create_entity(
        EntityCreate(name="Greifer 1", kind="worldline", category="robotik")
    )
    glass = repo.create_entity(EntityCreate(name="Glas G1", kind="worldline", category="robotik"))
    door = repo.create_entity(EntityCreate(name="Flurtür T1", kind="worldline", category="robotik"))

    g_hold = repo.create_state(
        StateCreate(
            entity_id=gripper1.id,
            modality="observed",
            source="measured",
            property_name="occupancy",
            label="Greifer 1 hält Glas G1",
            value_text="belegt",
            time_ms=t_now_ms,
        )
    )
    door_closed = repo.create_state(
        StateCreate(
            entity_id=door.id,
            modality="observed",
            source="measured",
            measurement_source="R1_Kamera",
            property_name="door_state",
            label="Tür T1 geschlossen",
            value_text="geschlossen",
            distribution_kind="discrete",
            time_ms=t_now_ms,
        )
    )

    door_guard = {
        "guards": [
            {
                "entity_id": door.id,
                "property_name": "door_state",
                "forbidden_texts": ["verriegelt"],
            }
        ]
    }
    tx_arm2 = repo.create_transformation(
        TransformationCreate(
            name="Tür T1 mit Greifer 2 öffnen & liefern",
            modality="planned",
            driver="actor",
            duration_seconds=84.0,
            duration_sigma_seconds=12.0,
            cost=1.0,
            formula=door_guard,
        )
    )
    tx_sideboard = repo.create_transformation(
        TransformationCreate(
            name="Glas auf Sideboard → Tür mit Greifer 1 öffnen",
            modality="planned",
            driver="actor",
            duration_seconds=106.0,
            duration_sigma_seconds=15.0,
            cost=2.5,
            formula=door_guard,
        )
    )
    tx_coffee = repo.create_transformation(
        TransformationCreate(
            name="Kaffeetasse greifen (konkurrierend)",
            modality="planned",
            driver="actor",
            duration_seconds=40.0,
            duration_sigma_seconds=5.0,
            meta={"scheduled_start_ms": t_now_ms + 10_000},
        )
    )

    repo.create_edge(
        EdgeCreate(transformation_id=tx_arm2.id, state_id=g_hold.id, direction="in", role="uses")
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_arm2.id, state_id=door_closed.id, direction="in", role="triggers"
        )
    )
    repo.create_edge(
        EdgeCreate(
            transformation_id=tx_sideboard.id,
            state_id=door_closed.id,
            direction="in",
            role="triggers",
        )
    )
    repo.create_edge(
        EdgeCreate(transformation_id=tx_coffee.id, state_id=g_hold.id, direction="in", role="uses")
    )

    pred_arm2 = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="predicted",
            source="derived",
            property_name="location",
            label="Ankunft Arm 2 (+84 s)",
            location_label="Couchtisch",
            time_ms=t_now_ms + 84_000,
            sigma_seconds=12.0,
        )
    )
    pred_side = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="predicted",
            source="derived",
            property_name="location",
            label="Ankunft Sideboard (+106 s)",
            location_label="Couchtisch",
            time_ms=t_now_ms + 106_000,
            sigma_seconds=15.0,
        )
    )

    e_arm2 = repo.create_edge(
        EdgeCreate(
            transformation_id=tx_arm2.id,
            state_id=pred_arm2.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="arm2",
            is_selected=True,
        )
    )
    e_side = repo.create_edge(
        EdgeCreate(
            transformation_id=tx_sideboard.id,
            state_id=pred_side.id,
            direction="out",
            role="produces",
            logic_group="CHOICE_OR",
            branch_key="sideboard",
            is_selected=False,
        )
    )

    goal = repo.create_state(
        StateCreate(
            entity_id=glass.id,
            modality="desired",
            source="user_goal",
            property_name="location",
            label="250 ml Wasser am Couchtisch in ≤ 120 s",
            location_label="Couchtisch",
            target_latest_ms=t_now_ms + 120_000,
        )
    )

    return {
        "gripper1_id": gripper1.id,
        "glass_id": glass.id,
        "door_id": door.id,
        "tx_arm2_id": tx_arm2.id,
        "tx_sideboard_id": tx_sideboard.id,
        "tx_coffee_id": tx_coffee.id,
        "edge_arm2_id": e_arm2.id,
        "edge_sideboard_id": e_side.id,
        "pred_arm2_id": pred_arm2.id,
        "pred_sideboard_id": pred_side.id,
        "goal_id": goal.id,
    }
