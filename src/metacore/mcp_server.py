"""Semantic MCP server exposing 8 world-model tools over MetaCoreEngine."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from metacore.config import Settings, default_settings
from metacore.db import migrate, now_ms
from metacore.engine import MetaCoreEngine, ResolutionAction
from metacore.models import (
    DistributionKind,
    DurationSource,
    Entity,
    EntityCreate,
    EntityKind,
    StateCreate,
    TransformationCreate,
    TransformationDriver,
    ValueSource,
)
from metacore.repository import MetaCoreRepository


def _resolve_entity(
    repo: MetaCoreRepository,
    id_or_name: str,
    *,
    create_if_missing: bool,
    kind: EntityKind = "worldline",
    category: str = "general",
    parent_entity_id: str | None = None,
) -> Entity:
    key = id_or_name.strip()
    if not key:
        raise ValueError("entity_id_or_name darf nicht leer sein")
    by_id = repo.get_entity(key)
    if by_id is not None:
        return by_id
    matches = [
        ent
        for ent in repo.find_entities(key, include_archived=True)
        if ent.name.lower() == key.lower()
    ]
    if matches:
        return matches[0]
    if not create_if_missing:
        raise KeyError(f"Unbekannte Entität / Weltlinie: {id_or_name}")
    return repo.create_entity(
        EntityCreate(
            name=key,
            kind=kind,
            category=category,
            parent_entity_id=parent_entity_id,
        )
    )


def create_server(config: Settings | None = None) -> FastMCP:
    """Create and configure the semantic FastMCP server for MetaCore."""
    active = config or default_settings
    migrate(active)
    repo = MetaCoreRepository(active)
    engine = MetaCoreEngine(repo)

    mcp = FastMCP(
        name="wowannwas-metacore",
        instructions=(
            "Semantischer Zugriff auf das WoWannWas-Weltmodell (Weltlinien, Eigenschafts-Zustände, "
            "Transformationen, Vorlagen und 4-stufige Ziel-Eskalation)."
        ),
    )

    @mcp.tool()
    def get_focus_briefing(horizon_days: int = 14) -> dict[str, Any]:
        """Liefert die aktuelle Lage rund um Jetzt: Ziele (Stufe 2–4), uses-Konflikte, anstehende Prognosen und offene ai_prior-Schätzungen."""
        if horizon_days < 0:
            raise ValueError("horizon_days muss >= 0 sein")
        now = now_ms()
        horizon_end = now + horizon_days * 86_400_000
        evaluations = engine.evaluate_all_goals()
        conflicts = engine.detect_uses_conflicts()

        upcoming: list[dict[str, Any]] = []
        open_priors: list[dict[str, Any]] = []
        for st in repo.list_states(modality="predicted"):
            ent = repo.get_entity(st.entity_id)
            ent_name = ent.name if ent else st.entity_id
            if st.time_ms is None or st.time_ms <= horizon_end:
                upcoming.append(
                    {
                        "state_id": st.id,
                        "entity_id": st.entity_id,
                        "entity_name": ent_name,
                        "property_name": st.property_name,
                        "label": st.label,
                        "value_num": st.value_num,
                        "value_sigma": st.value_sigma,
                        "value_unit": st.value_unit,
                        "time_ms": st.time_ms,
                        "source": st.source,
                    }
                )
            if st.source == "ai_prior":
                open_priors.append(
                    {
                        "state_id": st.id,
                        "entity_name": ent_name,
                        "property_name": st.property_name,
                        "label": st.label,
                        "value_num": st.value_num,
                        "value_sigma": st.value_sigma,
                        "value_unit": st.value_unit,
                    }
                )

        return {
            "now_ms": now,
            "horizon_days": horizon_days,
            "goal_evaluations": [ev.model_dump() for ev in evaluations],
            "uses_conflicts": [c.model_dump() for c in conflicts],
            "upcoming_predictions": upcoming,
            "open_ai_priors": open_priors,
        }

    @mcp.tool()
    def inspect_worldline(entity_id_or_name: str) -> dict[str, Any]:
        """Zeigt Historie (observed), Zukunft (predicted/desired), verknüpfte Transformationen und Zielbewertungen einer Weltlinie."""
        ent = _resolve_entity(repo, entity_id_or_name, create_if_missing=False)
        children = [e for e in repo.list_entities(include_archived=True) if e.parent_entity_id == ent.id]
        states = repo.list_states(entity_id=ent.id)

        history_observed = [s.model_dump() for s in states if s.modality == "observed"]
        future_predicted = [s.model_dump() for s in states if s.modality == "predicted"]
        goals_desired = [s.model_dump() for s in states if s.modality in ("desired", "cancelled")]

        edges = []
        txs: dict[str, dict[str, Any]] = {}
        for st in states:
            for edge in repo.list_edges(state_id=st.id):
                edges.append(edge.model_dump())
                if edge.transformation_id not in txs:
                    tx = repo.get_transformation(edge.transformation_id)
                    if tx is not None:
                        txs[tx.id] = tx.model_dump()

        goal_evals = [
            engine.evaluate_goal(s.id).model_dump()
            for s in states
            if s.modality == "desired" or (s.modality == "cancelled" and s.source == "user_goal")
        ]

        return {
            "entity": ent.model_dump(),
            "child_entities": [c.model_dump() for c in children],
            "history_observed": history_observed,
            "future_predicted": future_predicted,
            "goals_desired": goals_desired,
            "transformations": list(txs.values()),
            "edges": edges,
            "goal_evaluations": goal_evals,
        }

    @mcp.tool()
    def search_templates(query: str = "", category: str | None = None) -> dict[str, Any]:
        """Durchsucht gespeicherte Vorlagen (Rezepte/Skills/Naturprozesse) inkl. Kalibrierungsstatus."""
        needle = query.strip().lower()
        items: list[dict[str, Any]] = []
        for tpl in repo.list_transformations(modality="template"):
            tpl_cat = str(tpl.meta.get("category", ""))
            if category is not None and tpl_cat != category:
                continue
            haystack = f"{tpl.name} {json.dumps(tpl.formula)} {json.dumps(tpl.meta)}".lower()
            if needle and needle not in haystack:
                continue
            factor_src = (
                tpl.formula["factor"].get("source")
                if isinstance(tpl.formula.get("factor"), dict)
                else None
            )
            items.append(
                {
                    "template": tpl.model_dump(),
                    "calibration_status": {
                        "factor_source": factor_src,
                        "duration_source": tpl.duration_source,
                    },
                }
            )
        return {"templates": items, "count": len(items)}

    @mcp.tool()
    def define_template(
        name: str,
        driver: TransformationDriver = "actor",
        category: str = "general",
        parent_id: str | None = None,
        duration_seconds: float | None = None,
        duration_sigma_seconds: float | None = None,
        duration_source: DurationSource = "ai_prior",
        cost: float = 0.0,
        success_prob: float = 1.0,
        formula: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Speichert eine wiederverwendbare zeit-/ortlose Vorlage (inkl. Formeln, parent_id für Verschachtelung und ai_prior-Parameter)."""
        merged_meta = dict(meta or {})
        merged_meta.setdefault("category", category)
        tpl = repo.create_transformation(
            TransformationCreate(
                name=name,
                modality="template",
                driver=driver,
                parent_id=parent_id,
                duration_seconds=duration_seconds,
                duration_sigma_seconds=duration_sigma_seconds,
                duration_source=duration_source,
                cost=cost,
                success_prob=success_prob,
                formula=formula or {},
                meta=merged_meta,
            )
        )
        return {"template": tpl.model_dump()}

    @mcp.tool()
    def instantiate_template(
        template_id: str,
        input_state_ids: list[str],
        target_entity_id_or_name: str | None = None,
        start_time_ms: int | None = None,
        ai_priors: dict[str, list[float]] | None = None,
        split_parts: int | None = None,
        split_entities: list[str] | None = None,
        branch_key: str = "default",
    ) -> dict[str, Any]:
        """Bindet eine Vorlage an konkrete Eingabezustände/Entitäten und berechnet die Zukunfts-Prognosen."""
        converted_priors: dict[str, tuple[float, float]] | None = None
        if ai_priors is not None:
            converted_priors = {}
            for key, pair in ai_priors.items():
                if len(pair) != 2:
                    raise ValueError(f"ai_priors['{key}'] muss genau [mean, sigma] enthalten")
                converted_priors[key] = (float(pair[0]), float(pair[1]))

        target_ent: Entity | None = None
        if target_entity_id_or_name is not None:
            target_ent = _resolve_entity(
                repo, target_entity_id_or_name, create_if_missing=True
            )

        resolved_splits: list[str] | None = None
        if split_entities is not None:
            parent_id = target_ent.id if target_ent is not None else None
            resolved_splits = [
                _resolve_entity(
                    repo, item, create_if_missing=True, parent_entity_id=parent_id
                ).id
                for item in split_entities
            ]

        fb = engine.instantiate_template(
            template_id,
            input_state_ids=input_state_ids,
            target_entity_id=target_ent.id if target_ent is not None else None,
            start_time_ms=start_time_ms,
            ai_priors=converted_priors,
            split_parts=split_parts,
            split_entities=resolved_splits,
            branch_key=branch_key,
        )
        return fb.model_dump()

    @mcp.tool()
    def record_observation(
        entity_id_or_name: str,
        property_name: str,
        label: str,
        source: ValueSource = "measured",
        measurement_source: str = "user_input",
        category: str = "general",
        value_num: float | None = None,
        value_sigma: float | None = None,
        value_unit: str = "",
        value_text: str | None = None,
        distribution_kind: DistributionKind = "normal",
        lat: float | None = None,
        lon: float | None = None,
        sigma_meters: float | None = None,
        location_label: str = "",
        time_ms: int | None = None,
        sigma_seconds: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Erfasst eine reale Messung oder Null-Beobachtung, löst Trigger aus, kalibriert Vorlagen und liefert das Folgen-Feedback."""
        ent = _resolve_entity(
            repo, entity_id_or_name, create_if_missing=True, category=category
        )
        fb = engine.record_observation(
            StateCreate(
                entity_id=ent.id,
                modality="observed",
                source=source,
                measurement_source=measurement_source,
                property_name=property_name,
                label=label,
                value_num=value_num,
                value_sigma=value_sigma,
                value_unit=value_unit,
                value_text=value_text,
                distribution_kind=distribution_kind,
                lat=lat,
                lon=lon,
                sigma_meters=sigma_meters,
                location_label=location_label,
                time_ms=time_ms if time_ms is not None else now_ms(),
                sigma_seconds=sigma_seconds,
                meta=meta or {},
            )
        )
        payload = fb.model_dump()
        payload["observation_id"] = fb.created_state_ids[0]
        payload["entity_id"] = ent.id
        return payload

    @mcp.tool()
    def set_goal(
        entity_id_or_name: str,
        property_name: str,
        label: str,
        category: str = "general",
        value_unit: str = "",
        target_min: float | None = None,
        target_max: float | None = None,
        target_preferred: float | None = None,
        target_value_text: str | None = None,
        target_earliest_ms: int | None = None,
        target_latest_ms: int | None = None,
        lat: float | None = None,
        lon: float | None = None,
        target_radius_meters: float | None = None,
        location_label: str = "",
        threshold_ok: float = 0.8,
        threshold_fail: float = 0.2,
        priority: float = 1.0,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Setzt ein desired-Ziel mit harten/weichen Ziel-Grenzen und bewertet sofort seine Machbarkeit."""
        ent = _resolve_entity(
            repo, entity_id_or_name, create_if_missing=True, category=category
        )
        goal = repo.create_state(
            StateCreate(
                entity_id=ent.id,
                modality="desired",
                source="user_goal",
                property_name=property_name,
                label=label,
                value_unit=value_unit,
                target_min=target_min,
                target_max=target_max,
                target_preferred=target_preferred,
                target_value_text=target_value_text,
                target_earliest_ms=target_earliest_ms,
                target_latest_ms=target_latest_ms,
                lat=lat,
                lon=lon,
                target_radius_meters=target_radius_meters,
                location_label=location_label,
                threshold_ok=threshold_ok,
                threshold_fail=threshold_fail,
                priority=priority,
                meta=meta or {},
            )
        )
        evaluation = engine.evaluate_goal(goal.id)
        conflicts = engine.detect_uses_conflicts()
        return {
            "goal": goal.model_dump(),
            "evaluation": evaluation.model_dump(),
            "uses_conflicts": [c.model_dump() for c in conflicts],
        }

    @mcp.tool()
    def resolve_escalation(
        goal_id: str,
        action: ResolutionAction,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Löst eine Stufe-3/4-Eskalation auf (widen_tolerance, switch_branch, abort_goal) und gibt das neue Weltmodell-Feedback zurück."""
        fb = engine.resolve_escalation(goal_id, action, params)
        return fb.model_dump()

    return mcp


def main() -> None:
    """Run the MetaCore MCP server over stdio."""
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
