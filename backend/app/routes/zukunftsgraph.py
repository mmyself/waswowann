"""REST endpoints exposing MetaCore Zukunftsgraph snapshots, worldlines, observations, goals & escalations."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.metacore_bridge import (
    build_live_snapshot,
    inspect_worldline,
    open_metacore,
    resolve_entity_id,
    sync_legacy_observations,
)
from metacore.db import now_ms as mc_now_ms
from metacore.models import StateCreate

router = APIRouter(prefix="/api/zukunftsgraph", tags=["zukunftsgraph"])


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


class SyncRequest(BaseModel):
    seed_plans: bool = True


class MetaObservationRequest(BaseModel):
    entity_id: str = Field(min_length=1, max_length=128)
    property_name: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=240)
    value_num: float
    value_sigma: float = Field(default=0.0, ge=0.0)
    value_unit: str = Field(default="", max_length=40)
    time_ms: int | None = None
    measurement_source: str = Field(default="web_ui", max_length=200)


class MetaGoalRequest(BaseModel):
    entity_id_or_obs_id: str = Field(min_length=1, max_length=128)
    property_name: str = Field(default="harvest_ready", min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=240)
    target_min: float | None = None
    target_max: float | None = None
    target_latest_ms: int | None = None
    target_radius_meters: float | None = Field(default=None, gt=0.0)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    priority: float = Field(default=1.0, ge=0.0)


class EscalationResolveRequest(BaseModel):
    action: Literal["widen_tolerance", "switch_branch", "abort_goal"]
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/snapshot")
def get_snapshot(auto_sync: bool = Query(default=True)) -> dict[str, Any]:
    return build_live_snapshot(auto_sync=auto_sync)


@router.post("/sync")
def post_sync(payload: SyncRequest | None = None) -> dict[str, Any]:
    req = payload or SyncRequest()
    stats = sync_legacy_observations(seed_plans=req.seed_plans)
    return {
        "sync": stats,
        "snapshot": build_live_snapshot(auto_sync=False),
    }


@router.get("/worldlines/{item_id}")
def get_worldline(item_id: str) -> dict[str, Any]:
    try:
        return inspect_worldline(item_id)
    except KeyError as exc:
        raise _error(404, "WORLDLINE_NOT_FOUND", str(exc)) from exc


@router.post("/observations", status_code=201)
def post_observation(payload: MetaObservationRequest) -> dict[str, Any]:
    repo, engine = open_metacore()
    entity_id = resolve_entity_id(repo, payload.entity_id)
    if entity_id is None:
        raise _error(404, "ENTITY_NOT_FOUND", f"Unbekannte Weltlinie: {payload.entity_id}")

    t_ms = payload.time_ms if payload.time_ms is not None else mc_now_ms()
    lbl = (
        payload.label.strip()
        if payload.label and payload.label.strip()
        else f"{payload.property_name}: {payload.value_num} {payload.value_unit}".strip()
    )
    try:
        fb = engine.record_observation(
            StateCreate(
                entity_id=entity_id,
                modality="observed",
                source="measured",
                measurement_source=payload.measurement_source,
                property_name=payload.property_name,
                label=lbl,
                value_num=payload.value_num,
                value_sigma=payload.value_sigma,
                value_unit=payload.value_unit,
                time_ms=t_ms,
                sigma_seconds=60.0,
            )
        )
    except ValueError as exc:
        raise _error(400, "INVALID_OBSERVATION", str(exc)) from exc

    return {
        "feedback": fb.model_dump(),
        "snapshot": build_live_snapshot(auto_sync=False, last_feedback=fb),
    }


@router.post("/goals", status_code=201)
def post_goal(payload: MetaGoalRequest) -> dict[str, Any]:
    repo, engine = open_metacore()
    entity_id = resolve_entity_id(repo, payload.entity_id_or_obs_id)
    if entity_id is None:
        sync_legacy_observations(seed_plans=True)
        entity_id = resolve_entity_id(repo, payload.entity_id_or_obs_id)
    if entity_id is None:
        raise _error(404, "ENTITY_NOT_FOUND", f"Unbekannte Weltlinie: {payload.entity_id_or_obs_id}")

    goal_lat = payload.lat
    goal_lon = payload.lon
    if payload.target_radius_meters is not None and (goal_lat is None or goal_lon is None):
        for st in reversed(repo.list_states(entity_id=entity_id)):
            if st.lat is not None and st.lon is not None:
                goal_lat = st.lat
                goal_lon = st.lon
                break

    try:
        goal_state = repo.create_state(
            StateCreate(
                entity_id=entity_id,
                modality="desired",
                source="user_goal",
                property_name=payload.property_name,
                label=payload.label.strip(),
                lat=goal_lat,
                lon=goal_lon,
                target_min=payload.target_min,
                target_max=payload.target_max,
                target_latest_ms=payload.target_latest_ms,
                target_radius_meters=payload.target_radius_meters,
                priority=payload.priority,
            )
        )
    except ValueError as exc:
        raise _error(400, "INVALID_GOAL", str(exc)) from exc

    evaluation = engine.evaluate_goal(goal_state.id)
    return {
        "goal": goal_state.model_dump(),
        "evaluation": evaluation.model_dump(),
        "snapshot": build_live_snapshot(auto_sync=False),
    }


@router.post("/escalations/{goal_id}/resolve")
def post_resolve_escalation(goal_id: str, payload: EscalationResolveRequest) -> dict[str, Any]:
    _, engine = open_metacore()
    try:
        fb = engine.resolve_escalation(goal_id, payload.action, payload.params)
    except KeyError as exc:
        raise _error(404, "GOAL_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise _error(400, "ESCALATION_INVALID", str(exc)) from exc

    return {
        "feedback": fb.model_dump(),
        "snapshot": build_live_snapshot(auto_sync=False, last_feedback=fb),
    }
