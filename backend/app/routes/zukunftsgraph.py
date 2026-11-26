"""REST endpoints exposing MetaCore Zukunftsgraph snapshots, worldlines, observations, goals & escalations."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import app.config as config_module
from app.services.metacore_bridge import (
    analyze_custom_period_samples,
    build_live_snapshot,
    inspect_worldline,
    open_metacore,
    resolve_entity_id,
    sync_legacy_observations,
)
from metacore.clocks import ClockId
from metacore.db import now_ms as mc_now_ms
from metacore.models import (
    DurationSource,
    EvidenceRef,
    RawSampleCreate,
    StateCreate,
    TransformationCreate,
    TransformationDriver,
    Visibility,
)
from metacore.rollup import RollupWindowKind
from metacore.templates import TemplateKind, TemplateMaturity, extract_template_profile, search_template_catalog
from app.services.science_enrichment import (
    _looks_biological,
    _clean_query_for_taxon,
    find_existing_template_matches,
    lookup_ols_taxon,
    lookup_openalex_references,
    preview_template_enrichment,
)

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
    context: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    assertion_confidence: float = Field(default=1.0, gt=0.0, le=1.0)
    visibility: Visibility = "normal"


class MetaGoalRequest(BaseModel):
    entity_id_or_obs_id: str = Field(min_length=1, max_length=128)
    property_name: str = Field(default="harvest_ready", min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=240)
    value_unit: str = Field(default="", max_length=40)
    target_min: float | None = None
    target_max: float | None = None
    target_earliest_ms: int | None = None
    target_latest_ms: int | None = None
    target_radius_meters: float | None = Field(default=None, gt=0.0)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    priority: float = Field(default=1.0, ge=0.0)
    context: dict[str, str] = Field(default_factory=dict)
    visibility: Visibility = "normal"


class MetaGoalPatchRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=240)
    value_unit: str | None = Field(default=None, max_length=40)
    target_min: float | None = None
    target_max: float | None = None
    target_earliest_ms: int | None = None
    target_latest_ms: int | None = None
    target_radius_meters: float | None = Field(default=None, gt=0.0)
    priority: float | None = Field(default=None, ge=0.0)
    context: dict[str, str] | None = None


class RawSampleItemRequest(BaseModel):
    entity_id: str = Field(min_length=1, max_length=128)
    property_name: str = Field(min_length=1, max_length=120)
    time_ms: int
    value_num: float
    value_sigma: float | None = Field(default=None, ge=0.0)
    value_unit: str = Field(default="", max_length=40)
    measurement_source: str = Field(default="", max_length=200)
    context: dict[str, str] = Field(default_factory=dict)
    evidence_ref: str = Field(default="", max_length=500)


class RawSampleBatchRequest(BaseModel):
    samples: list[RawSampleItemRequest] = Field(min_length=1, max_length=5000)


class RawSampleRollupRequest(BaseModel):
    entity_id: str | None = Field(default=None, min_length=1, max_length=128)
    property_name: str | None = Field(default=None, min_length=1, max_length=120)
    window_kind: RollupWindowKind = "day"
    window_seconds: float = Field(default=3_600.0, gt=0.0)
    clock_id: ClockId = "solar_day"
    phase_bins: int | None = Field(default=None, ge=1, le=24)
    target_unit: str | None = Field(default=None, max_length=40)
    context_filter: dict[str, str] | None = None
    only_unrolled: bool = True


class EscalationResolveRequest(BaseModel):
    action: Literal["widen_tolerance", "switch_branch", "abort_goal"]
    params: dict[str, Any] = Field(default_factory=dict)


class PeriodAnalyzeRequest(BaseModel):
    timestamps_ms: list[int] = Field(default_factory=list, max_length=1000)
    label: str = Field(default="Zeitreihe", max_length=200)
    tidal_offset_hours: float = 0.0
    now_ms: int | None = None


class MetaTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    driver: TransformationDriver = "actor"
    domain: str = Field(default="general", min_length=1, max_length=80)
    category: str = Field(default="general", min_length=1, max_length=80)
    scientific_name: str | None = Field(default=None, max_length=200)
    canonical_name: str | None = Field(default=None, max_length=200)
    taxon_path: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    template_kind: TemplateKind | None = None
    parent_id: str | None = Field(default=None, max_length=128)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    duration_sigma_seconds: float | None = Field(default=None, gt=0.0)
    duration_source: DurationSource = "ai_prior"
    output_property: str | None = Field(default=None, max_length=120)
    output_value: float | None = None
    output_sigma: float | None = Field(default=None, ge=0.0)
    output_unit: str = Field(default="", max_length=40)
    applies_to: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)
    ontology_id: str | None = Field(default=None, max_length=80)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    auto_enrich: bool = True


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
        parsed_refs = [EvidenceRef(**ref) for ref in payload.evidence_refs]
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
                context=payload.context,
                evidence_refs=parsed_refs,
                assertion_confidence=payload.assertion_confidence,
                visibility=payload.visibility,
            )
        )
    except (ValueError, TypeError) as exc:
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
                value_unit=payload.value_unit,
                lat=goal_lat,
                lon=goal_lon,
                target_min=payload.target_min,
                target_max=payload.target_max,
                target_earliest_ms=payload.target_earliest_ms,
                target_latest_ms=payload.target_latest_ms,
                target_radius_meters=payload.target_radius_meters,
                priority=payload.priority,
                context=payload.context,
                visibility=payload.visibility,
            )
        )
    except (ValueError, TypeError) as exc:
        raise _error(400, "INVALID_GOAL", str(exc)) from exc

    evaluation = engine.evaluate_goal(goal_state.id)
    return {
        "goal": goal_state.model_dump(),
        "evaluation": evaluation.model_dump(),
        "snapshot": build_live_snapshot(auto_sync=False),
    }


@router.patch("/goals/{goal_id}")
def patch_goal(goal_id: str, payload: MetaGoalPatchRequest) -> dict[str, Any]:
    repo, engine = open_metacore()
    current = repo.get_state(goal_id)
    if current is None:
        raise _error(404, "GOAL_NOT_FOUND", f"Unbekanntes Ziel: {goal_id}")
    if current.modality != "desired":
        raise _error(400, "NOT_A_GOAL", f"Zustand '{goal_id}' ist kein aktives Ziel (desired).")

    updates: dict[str, Any] = {}
    for field in payload.model_fields_set:
        val = getattr(payload, field)
        if field in ("label", "value_unit", "priority", "context") and val is None:
            raise _error(400, "INVALID_GOAL_PATCH", f"Feld '{field}' darf nicht null sein.")
        updates[field] = val.strip() if field == "label" and isinstance(val, str) else val

    if not updates:
        evaluation = engine.evaluate_goal(goal_id)
        return {
            "goal": current.model_dump(),
            "evaluation": evaluation.model_dump(),
            "snapshot": build_live_snapshot(auto_sync=False),
        }

    try:
        updated = repo.update_state(goal_id, **updates)
    except (ValueError, TypeError) as exc:
        raise _error(400, "INVALID_GOAL_PATCH", str(exc)) from exc

    evaluation = engine.evaluate_goal(goal_id)
    return {
        "goal": updated.model_dump(),
        "evaluation": evaluation.model_dump(),
        "snapshot": build_live_snapshot(auto_sync=False),
    }


@router.post("/raw-samples", status_code=201)
def post_raw_samples(payload: RawSampleBatchRequest) -> dict[str, Any]:
    repo, engine = open_metacore()
    parsed: list[RawSampleCreate] = []
    for item in payload.samples:
        resolved = resolve_entity_id(repo, item.entity_id)
        if resolved is None:
            raise _error(404, "ENTITY_NOT_FOUND", f"Unbekannte Weltlinie: {item.entity_id}")
        try:
            parsed.append(
                RawSampleCreate(
                    entity_id=resolved,
                    property_name=item.property_name,
                    time_ms=item.time_ms,
                    value_num=item.value_num,
                    value_sigma=item.value_sigma,
                    value_unit=item.value_unit,
                    measurement_source=item.measurement_source,
                    context=item.context,
                    evidence_ref=item.evidence_ref,
                )
            )
        except (ValueError, TypeError) as exc:
            raise _error(400, "INVALID_RAW_SAMPLE", str(exc)) from exc

    created = engine.ingest_raw_samples(parsed)
    return {
        "inserted_count": len(created),
        "samples": [s.model_dump() for s in created],
    }


@router.post("/raw-samples/rollup")
def post_raw_samples_rollup(payload: RawSampleRollupRequest | None = None) -> dict[str, Any]:
    req = payload or RawSampleRollupRequest()
    repo, engine = open_metacore()
    resolved_ent: str | None = None
    if req.entity_id:
        resolved_ent = resolve_entity_id(repo, req.entity_id)
        if resolved_ent is None:
            raise _error(404, "ENTITY_NOT_FOUND", f"Unbekannte Weltlinie: {req.entity_id}")
    try:
        created = engine.rollup_raw_samples(
            entity_id=resolved_ent,
            property_name=req.property_name,
            window_kind=req.window_kind,
            window_seconds=req.window_seconds,
            clock_id=req.clock_id,
            phase_bins=req.phase_bins,
            target_unit=req.target_unit,
            context_filter=req.context_filter,
            only_unrolled=req.only_unrolled,
            tz_name=config_module.settings.import_timezone,
        )
    except (ValueError, TypeError) as exc:
        raise _error(400, "ROLLUP_FAILED", str(exc)) from exc

    return {
        "created_count": len(created),
        "created_states": [s.model_dump() for s in created],
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


@router.get("/periods")
def get_periods(
    entity_id: str | None = Query(default=None),
    category: str | None = Query(default=None),
    property_name: str | None = Query(default=None),
    tidal_offset_hours: float = Query(default=0.0),
) -> dict[str, Any]:
    repo, engine = open_metacore()
    resolved_id = resolve_entity_id(repo, entity_id) if entity_id else None
    if entity_id and resolved_id is None:
        raise _error(404, "ENTITY_NOT_FOUND", f"Unbekannte Weltlinie: {entity_id}")
    return engine.analyze_worldline_periods(
        entity_id=resolved_id,
        category=category,
        property_name=property_name,
        tz_name=config_module.settings.import_timezone,
        tidal_offset_hours=tidal_offset_hours,
    ).model_dump()


@router.post("/periods/analyze")
def post_analyze_periods(payload: PeriodAnalyzeRequest) -> dict[str, Any]:
    return analyze_custom_period_samples(
        payload.timestamps_ms,
        label=payload.label,
        tidal_offset_hours=payload.tidal_offset_hours,
        now_ms_override=payload.now_ms,
    )


@router.get("/templates")
def get_templates(
    query: str = Query(default=""),
    category: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    scientific_name: str | None = Query(default=None),
    driver: TransformationDriver | None = Query(default=None),
    template_kind: TemplateKind | None = Query(default=None),
    maturity: TemplateMaturity | None = Query(default=None),
    only_roots: bool = Query(default=True),
    include_archived: bool = Query(default=False),
    entity_id: str | None = Query(default=None),
) -> dict[str, Any]:
    repo, _ = open_metacore()
    resolved_entity: str | None = None
    if entity_id is not None and entity_id.strip():
        resolved_entity = resolve_entity_id(repo, entity_id.strip())
        if resolved_entity is None:
            return {
                "templates": [],
                "count": 0,
                "facets": {"domains": [], "scientific_names": [], "maturities": {}},
            }
    return search_template_catalog(
        repo,
        query=query,
        category=category,
        domain=domain,
        scientific_name=scientific_name,
        driver=driver,
        template_kind=template_kind,
        maturity=maturity,
        only_roots=only_roots,
        include_archived=include_archived,
        applies_to_entity_id=resolved_entity,
    )


@router.get("/templates/enrich-preview")
def get_templates_enrich_preview(
    name: str = Query(default=""),
    scientific_name: str | None = Query(default=None),
    category: str = Query(default="general"),
    domain: str = Query(default="general"),
    duration_source: DurationSource = Query(default="ai_prior"),
) -> dict[str, Any]:
    repo, _ = open_metacore()
    return preview_template_enrichment(
        repo,
        name=name,
        scientific_name=scientific_name,
        category=category,
        domain=domain,
        duration_source=duration_source,
    )


@router.post("/templates", status_code=201)
def post_template(payload: MetaTemplateCreateRequest) -> dict[str, Any]:
    repo, _ = open_metacore()
    if payload.parent_id and repo.get_transformation(payload.parent_id) is None:
        raise _error(404, "PARENT_TEMPLATE_NOT_FOUND", f"Eltern-Vorlage nicht gefunden: {payload.parent_id}")

    formula: dict[str, Any] = {}
    if payload.output_property and payload.output_property.strip():
        formula = {
            "kind": "duration_step",
            "output_property": payload.output_property.strip(),
            "output_unit": payload.output_unit.strip(),
        }
        if payload.output_value is not None:
            formula["output_value"] = payload.output_value
        if payload.output_sigma is not None:
            formula["output_sigma"] = payload.output_sigma

    sci = payload.scientific_name.strip() if payload.scientific_name and payload.scientific_name.strip() else None
    onto_id = payload.ontology_id.strip() if payload.ontology_id and payload.ontology_id.strip() else None
    taxon_path = [t.strip() for t in payload.taxon_path if t.strip()]
    aliases = [a.strip() for a in payload.aliases if a.strip()]
    evidence_refs = [dict(r) for r in payload.evidence_refs if isinstance(r, dict) and r.get("ref")]

    taxonomy_applied = False
    literature_added = 0

    if payload.auto_enrich:
        if (not onto_id or not taxon_path) and _looks_biological(
            name=payload.name,
            scientific_name=sci,
            category=payload.category,
            domain=payload.domain,
        ):
            taxon = lookup_ols_taxon(_clean_query_for_taxon(payload.name, sci))
            if taxon is not None:
                taxonomy_applied = True
                if not onto_id:
                    onto_id = taxon["ontology_id"]
                if not sci:
                    sci = taxon["scientific_name"]
                if not taxon_path:
                    taxon_path = list(taxon["taxon_path"])
                seen_low = {a.lower() for a in aliases}
                for syn in taxon.get("synonyms") or []:
                    if syn.lower() not in seen_low:
                        seen_low.add(syn.lower())
                        aliases.append(syn)

        if payload.duration_source == "ai_prior" and not evidence_refs:
            found_refs = lookup_openalex_references(
                _clean_query_for_taxon(payload.name, None) or payload.name,
                scientific_name=sci,
            )
            if found_refs:
                evidence_refs = found_refs
                literature_added = len(found_refs)

    existing_matches = find_existing_template_matches(
        repo,
        name=payload.name,
        scientific_name=sci,
    )

    canon = (
        payload.canonical_name.strip()
        if payload.canonical_name and payload.canonical_name.strip()
        else sci
    )
    meta: dict[str, Any] = {
        "domain": payload.domain.strip().lower() or "general",
        "category": payload.category.strip() or "general",
    }
    if sci:
        meta["scientific_name"] = sci
    if onto_id:
        meta["ontology_id"] = onto_id
    if canon:
        meta["canonical_name"] = canon
    if taxon_path:
        meta["taxon_path"] = taxon_path
    if aliases:
        meta["aliases"] = aliases
    if evidence_refs:
        meta["evidence_refs"] = evidence_refs
    if payload.template_kind:
        meta["template_kind"] = payload.template_kind
    if payload.applies_to:
        meta["applies_to"] = payload.applies_to
    if payload.notes.strip():
        meta["notes"] = payload.notes.strip()

    try:
        tpl = repo.create_transformation(
            TransformationCreate(
                name=payload.name.strip(),
                modality="template",
                driver=payload.driver,
                parent_id=payload.parent_id,
                duration_seconds=payload.duration_seconds,
                duration_sigma_seconds=payload.duration_sigma_seconds,
                duration_source=payload.duration_source,
                formula=formula,
                meta=meta,
            )
        )
    except (ValueError, TypeError) as exc:
        raise _error(400, "INVALID_TEMPLATE", str(exc)) from exc

    return {
        "template": tpl.model_dump(),
        "profile": extract_template_profile(tpl),
        "catalog": search_template_catalog(repo, only_roots=True),
        "enrichment": {
            "taxonomy_applied": taxonomy_applied,
            "literature_added": literature_added,
            "existing_matches": existing_matches,
        },
    }
