"""Deterministic graph propagation, calibration, conflict detection, and 4-stage escalation engine."""

from __future__ import annotations

import copy
import math
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field

from metacore.db import now_ms
from metacore.math_fusion import (
    GaussianValue,
    GeoPoint,
    calibrate_parameter,
    combine_goal_probability,
    probability_in_bounds,
    probability_within_radius,
    propagate_product,
    propagate_split,
)
from metacore.models import (
    Edge,
    EdgeCreate,
    EdgeRole,
    State,
    StateCreate,
    StrictModel,
    Transformation,
    TransformationCreate,
)
from metacore.repository import MetaCoreRepository

BrokenDimension = Literal["wann", "wo", "wie_viel", "vorbedingung"]
GoalStatus = Literal["on_track", "at_risk", "broken", "cancelled"]
ResolutionAction = Literal["widen_tolerance", "switch_branch", "abort_goal"]


class BranchOption(StrictModel):
    """Evaluated CHOICE_OR or WORLD_OR alternative branch for a goal."""

    branch_key: str
    transformation_id: str
    transformation_name: str
    is_selected: bool
    cost: float
    goal_probability: float
    broken_dimensions: list[BrokenDimension] = Field(default_factory=list)


class GoalEvaluation(StrictModel):
    """Dynamically computed status, probability, and broken dimensions for a goal."""

    goal_id: str
    entity_id: str
    property_name: str
    label: str
    stage: int
    status: GoalStatus
    goal_probability: float
    dimension_probs: dict[str, float] = Field(default_factory=dict)
    broken_dimensions: list[BrokenDimension] = Field(default_factory=list)
    predicted_state_id: str | None = None
    alternative_branches: list[BranchOption] = Field(default_factory=list)
    summary: str = ""


class ResourceConflict(StrictModel):
    """Overlapping active `uses` reservations on the same entity."""

    entity_id: str
    entity_name: str
    transformation_a_id: str
    transformation_a_name: str
    transformation_b_id: str
    transformation_b_name: str
    overlap_start_ms: int
    overlap_end_ms: int


class MutationFeedback(StrictModel):
    """Structured consequence report returned after every graph mutation."""

    created_state_ids: list[str] = Field(default_factory=list)
    updated_state_ids: list[str] = Field(default_factory=list)
    triggered_template_ids: list[str] = Field(default_factory=list)
    calibrated_template_ids: list[str] = Field(default_factory=list)
    goal_evaluations: list[GoalEvaluation] = Field(default_factory=list)
    uses_conflicts: list[ResourceConflict] = Field(default_factory=list)
    open_ai_prior_state_ids: list[str] = Field(default_factory=list)
    summary: str = ""


class MetaCoreEngine:
    """Orchestrates template instantiation, calibration, evaluation, and escalation."""

    def __init__(self, repo: MetaCoreRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------------
    # Goal evaluation & 4-stage escalation
    # ------------------------------------------------------------------

    def evaluate_all_goals(self) -> list[GoalEvaluation]:
        """Evaluate every desired (or cancelled) goal dynamically."""
        goals = [
            *self.repo.list_states(modality="desired"),
            *self.repo.list_states(modality="cancelled"),
        ]
        seen: set[str] = set()
        results: list[GoalEvaluation] = []
        for goal in goals:
            if goal.id in seen:
                continue
            seen.add(goal.id)
            if goal.modality == "desired" or goal.source == "user_goal":
                results.append(self.evaluate_goal(goal.id))
        return results

    def evaluate_goal(self, goal_id: str) -> GoalEvaluation:
        """Compute stage (2/3/4), probability, broken dimensions, and branch options for a goal."""
        goal = self.repo.get_state(goal_id)
        if goal is None:
            raise KeyError(f"Unbekanntes Ziel: {goal_id}")
        if goal.modality == "cancelled":
            return GoalEvaluation(
                goal_id=goal.id,
                entity_id=goal.entity_id,
                property_name=goal.property_name,
                label=goal.label,
                stage=0,
                status="cancelled",
                goal_probability=0.0,
                summary="Ziel sauber abgebrochen.",
            )
        if goal.modality != "desired":
            raise ValueError(f"Zustand {goal_id} ist kein Ziel (modality={goal.modality})")

        candidates = self._find_candidates_for_goal(goal)
        if not candidates:
            return GoalEvaluation(
                goal_id=goal.id,
                entity_id=goal.entity_id,
                property_name=goal.property_name,
                label=goal.label,
                stage=4,
                status="broken",
                goal_probability=0.0,
                dimension_probs={"vorbedingung": 0.0},
                broken_dimensions=["vorbedingung"],
                summary="Kein erreichbarer Zustand oder Planpfad für dieses Ziel.",
            )

        evaluated: list[tuple[State | None, Transformation | None, Edge | None, float, dict[str, float], list[BrokenDimension]]] = []
        for pred, tx, edge in candidates:
            prob, dims, broken = self._score_candidate(goal, pred, tx, edge)
            evaluated.append((pred, tx, edge, prob, dims, broken))

        # Prefer selected CHOICE_OR branch; otherwise pick the highest-probability candidate
        selected = [item for item in evaluated if item[2] is None or item[2].is_selected]
        chosen = max(selected or evaluated, key=lambda item: item[3])
        pred_state, _, _, goal_prob, dim_probs, broken_dims = chosen

        branch_options: list[BranchOption] = []
        for _, tx, edge, prob, _, brk in evaluated:
            if tx is not None and edge is not None and edge.logic_group in ("CHOICE_OR", "WORLD_OR"):
                branch_options.append(
                    BranchOption(
                        branch_key=edge.branch_key,
                        transformation_id=tx.id,
                        transformation_name=tx.name,
                        is_selected=edge.is_selected,
                        cost=tx.cost,
                        goal_probability=prob,
                        broken_dimensions=brk,
                    )
                )

        if goal_prob >= goal.threshold_ok:
            stage = 2
            status: GoalStatus = "on_track"
            summary = f"Stufe 2 (im Korridor): Zielwahrscheinlichkeit {goal_prob:.0%} >= {goal.threshold_ok:.0%}."
        elif goal_prob >= goal.threshold_fail:
            stage = 3
            status = "at_risk"
            summary = (
                f"Stufe 3 (Plan-Anpassung nötig): Zielwahrscheinlichkeit {goal_prob:.0%} "
                f"unter {goal.threshold_ok:.0%}."
            )
        else:
            stage = 4
            status = "broken"
            if not broken_dims and dim_probs:
                lowest_dim = min(dim_probs, key=lambda k: dim_probs[k])
                if lowest_dim in ("wann", "wo", "wie_viel", "vorbedingung"):
                    broken_dims = [lowest_dim]  # type: ignore[list-item]
            summary = (
                f"Stufe 4 (Ziel-Eskalation): Zielwahrscheinlichkeit {goal_prob:.0%} < {goal.threshold_fail:.0%}; "
                f"gebrochen: {', '.join(broken_dims)}."
            )

        return GoalEvaluation(
            goal_id=goal.id,
            entity_id=goal.entity_id,
            property_name=goal.property_name,
            label=goal.label,
            stage=stage,
            status=status,
            goal_probability=goal_prob,
            dimension_probs=dim_probs,
            broken_dimensions=broken_dims,
            predicted_state_id=pred_state.id if pred_state else None,
            alternative_branches=branch_options,
            summary=summary,
        )

    def _find_candidates_for_goal(
        self, goal: State
    ) -> list[tuple[State | None, Transformation | None, Edge | None]]:
        """Collect (predicted_or_observed_state, producing_tx, branch_edge) candidates for a goal."""
        candidates: list[tuple[State | None, Transformation | None, Edge | None]] = []
        seen_pairs: set[tuple[str | None, str | None]] = set()

        predicted = [
            s
            for s in self.repo.list_states(
                entity_id=goal.entity_id, property_name=goal.property_name, modality="predicted"
            )
        ]
        for pred in predicted:
            out_edges = self.repo.list_edges(state_id=pred.id, direction="out")
            active_edges = [
                e
                for e in out_edges
                if (tx := self.repo.get_transformation(e.transformation_id)) is not None
                and tx.modality != "cancelled"
            ]
            if active_edges:
                for edge in active_edges:
                    tx = self.repo.get_transformation(edge.transformation_id)
                    key = (pred.id, tx.id if tx else None)
                    if key not in seen_pairs:
                        seen_pairs.add(key)
                        candidates.append((pred, tx, edge))
            else:
                key = (pred.id, None)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    candidates.append((pred, None, None))

        if candidates:
            return candidates

        # Fallback: direct edge attached to the goal itself or latest observed state
        goal_edges = self.repo.list_edges(state_id=goal.id, direction="out")
        observed = self.repo.list_states(
            entity_id=goal.entity_id, property_name=goal.property_name, modality="observed"
        )
        latest_obs = observed[-1] if observed else None
        if goal_edges:
            for edge in goal_edges:
                tx = self.repo.get_transformation(edge.transformation_id)
                if tx is not None and tx.modality != "cancelled":
                    candidates.append((latest_obs, tx, edge))
            if candidates:
                return candidates

        if latest_obs is not None:
            return [(latest_obs, None, None)]
        return []

    def _score_candidate(
        self,
        goal: State,
        pred: State | None,
        tx: Transformation | None,
        edge: Edge | None,
    ) -> tuple[float, dict[str, float], list[BrokenDimension]]:
        dim_probs: dict[str, float] = {}
        broken: list[BrokenDimension] = []

        # 1. Quantity / property value ("wie_viel")
        if goal.target_min is not None or goal.target_max is not None:
            if pred is not None and pred.value_num is not None:
                p_qty = probability_in_bounds(
                    GaussianValue(pred.value_num, pred.value_sigma or 0.0),
                    target_min=goal.target_min,
                    target_max=goal.target_max,
                )
            else:
                p_qty = 0.0
            dim_probs["wie_viel"] = p_qty
            if p_qty < goal.threshold_fail:
                broken.append("wie_viel")
        elif goal.target_value_text is not None:
            p_qty = 1.0 if (pred is not None and pred.value_text == goal.target_value_text) else 0.0
            dim_probs["wie_viel"] = p_qty
            if p_qty < goal.threshold_fail:
                broken.append("wie_viel")

        # 2. Time ("wann")
        if goal.target_earliest_ms is not None or goal.target_latest_ms is not None:
            if pred is not None and pred.time_ms is not None:
                sigma_ms = (pred.sigma_seconds or 0.0) * 1000.0
                p_time = probability_in_bounds(
                    GaussianValue(float(pred.time_ms), sigma_ms),
                    target_min=float(goal.target_earliest_ms)
                    if goal.target_earliest_ms is not None
                    else None,
                    target_max=float(goal.target_latest_ms)
                    if goal.target_latest_ms is not None
                    else None,
                )
            else:
                p_time = 0.0
            dim_probs["wann"] = p_time
            if p_time < goal.threshold_fail:
                broken.append("wann")

        # 3. Space ("wo")
        if goal.target_radius_meters is not None and goal.lat is not None and goal.lon is not None:
            if pred is not None and pred.lat is not None and pred.lon is not None:
                p_space = probability_within_radius(
                    GeoPoint(pred.lat, pred.lon, pred.sigma_meters or 1.0),
                    goal.lat,
                    goal.lon,
                    goal.target_radius_meters,
                )
            else:
                p_space = 0.0
            dim_probs["wo"] = p_space
            if p_space < goal.threshold_fail:
                broken.append("wo")
        elif goal.location_label:
            p_space = 1.0 if (pred is not None and pred.location_label == goal.location_label) else 0.0
            dim_probs["wo"] = p_space
            if p_space < goal.threshold_fail:
                broken.append("wo")

        # 4. Preconditions & branch probability ("vorbedingung")
        p_pre = self._evaluate_preconditions(tx, edge)
        dim_probs["vorbedingung"] = p_pre
        if p_pre < goal.threshold_fail:
            broken.append("vorbedingung")

        combined = combine_goal_probability(list(dim_probs.values()))
        return combined, dim_probs, broken

    def _evaluate_preconditions(self, tx: Transformation | None, edge: Edge | None) -> float:
        if tx is None:
            return 1.0
        if tx.modality == "cancelled":
            return 0.0

        p_pre = tx.success_prob
        if edge is not None and edge.logic_group == "WORLD_OR" and edge.probability is not None:
            p_pre *= edge.probability

        in_edges = self.repo.list_edges(transformation_id=tx.id, direction="in")
        in_states: list[State] = []
        for in_edge in in_edges:
            if in_edge.logic_group == "CHOICE_OR" and not in_edge.is_selected:
                continue
            st = self.repo.get_state(in_edge.state_id)
            if st is None or st.modality == "cancelled":
                return 0.0
            in_states.append(st)

        guards: list[dict[str, Any]] = list(tx.formula.get("guards", []))
        for guard in guards:
            p_pre *= self._evaluate_single_guard(guard, in_states)

        return max(0.0, min(1.0, p_pre))

    def _evaluate_single_guard(self, guard: dict[str, Any], in_states: Sequence[State]) -> float:
        ent_id = guard.get("entity_id")
        prop = guard.get("property_name")

        target_state: State | None = None
        if ent_id and prop:
            observed = self.repo.list_states(entity_id=ent_id, property_name=prop, modality="observed")
            if observed:
                target_state = observed[-1]
        if target_state is None:
            for st in in_states:
                if (ent_id is None or st.entity_id == ent_id) and (
                    prop is None or st.property_name == prop
                ):
                    latest = self.repo.list_states(
                        entity_id=st.entity_id, property_name=st.property_name, modality="observed"
                    )
                    target_state = latest[-1] if latest else st
                    break

        if target_state is None:
            return 0.0

        required_text = guard.get("required_text")
        if required_text is not None and target_state.value_text != required_text:
            return 0.0

        forbidden_texts = guard.get("forbidden_texts", [])
        if target_state.value_text is not None and target_state.value_text in forbidden_texts:
            return 0.0

        min_val = guard.get("min_value")
        max_val = guard.get("max_value")
        if min_val is not None or max_val is not None:
            if target_state.value_num is None:
                return 0.0
            return probability_in_bounds(
                GaussianValue(target_state.value_num, target_state.value_sigma or 0.0),
                target_min=float(min_val) if min_val is not None else None,
                target_max=float(max_val) if max_val is not None else None,
            )

        return 1.0

    # ------------------------------------------------------------------
    # Resource conflict detection (`uses`)
    # ------------------------------------------------------------------

    def detect_uses_conflicts(self) -> list[ResourceConflict]:
        """Find overlapping, unreleased `uses` reservations on the same entity."""
        planned_txs = self.repo.list_transformations(modality="planned")
        reservations_by_entity: dict[str, list[tuple[Transformation, int, int]]] = {}

        for tx in planned_txs:
            window = self._tx_time_window(tx)
            if window is None:
                continue
            start_ms, end_ms = window
            if start_ms >= end_ms:
                continue

            uses_edges = self.repo.list_edges(
                transformation_id=tx.id, direction="in", role="uses"
            )
            for edge in uses_edges:
                if edge.resource_released or not edge.is_selected:
                    continue
                st = self.repo.get_state(edge.state_id)
                if st is None:
                    continue
                reservations_by_entity.setdefault(st.entity_id, []).append((tx, start_ms, end_ms))

        conflicts: list[ResourceConflict] = []
        for entity_id, items in sorted(reservations_by_entity.items()):
            entity = self.repo.get_entity(entity_id)
            entity_name = entity.name if entity else entity_id
            for i in range(len(items)):
                tx_a, sa, ea = items[i]
                for j in range(i + 1, len(items)):
                    tx_b, sb, eb = items[j]
                    if tx_a.id == tx_b.id:
                        continue
                    ov_start = max(sa, sb)
                    ov_end = min(ea, eb)
                    if ov_start < ov_end:
                        conflicts.append(
                            ResourceConflict(
                                entity_id=entity_id,
                                entity_name=entity_name,
                                transformation_a_id=tx_a.id,
                                transformation_a_name=tx_a.name,
                                transformation_b_id=tx_b.id,
                                transformation_b_name=tx_b.name,
                                overlap_start_ms=ov_start,
                                overlap_end_ms=ov_end,
                            )
                        )
        return conflicts

    def _tx_time_window(self, tx: Transformation) -> tuple[int, int] | None:
        explicit_start = tx.meta.get("scheduled_start_ms")
        in_edges = self.repo.list_edges(transformation_id=tx.id, direction="in")
        out_edges = self.repo.list_edges(transformation_id=tx.id, direction="out")

        in_times = [
            st.time_ms
            for e in in_edges
            if (st := self.repo.get_state(e.state_id)) is not None and st.time_ms is not None
        ]
        out_times = [
            st.time_ms
            for e in out_edges
            if (st := self.repo.get_state(e.state_id)) is not None and st.time_ms is not None
        ]

        start_ms = int(explicit_start) if explicit_start is not None else (min(in_times) if in_times else None)
        if start_ms is None:
            return None

        if out_times:
            end_ms = max(out_times)
        elif tx.duration_seconds is not None:
            end_ms = start_ms + int(tx.duration_seconds * 1000)
        else:
            return None
        return start_ms, end_ms

    # ------------------------------------------------------------------
    # Template instantiation
    # ------------------------------------------------------------------

    def instantiate_template(
        self,
        template_id: str,
        *,
        input_state_ids: Sequence[str],
        target_entity_id: str | None = None,
        start_time_ms: int | None = None,
        ai_priors: dict[str, tuple[float, float]] | None = None,
        split_parts: int | None = None,
        split_entities: Sequence[str] | None = None,
        branch_key: str = "default",
    ) -> MutationFeedback:
        """Instantiate a reusable template into planned transformation(s) and predicted states."""
        template = self.repo.get_transformation(template_id)
        if template is None:
            raise KeyError(f"Unbekannte Vorlage: {template_id}")
        if template.modality != "template":
            raise ValueError(f"Transformation {template_id} ist keine Vorlage (template)")
        if not input_state_ids:
            raise ValueError("instantiate_template erfordert mindestens eine Input-Zustands-ID")

        inputs: list[State] = []
        for sid in input_state_ids:
            st = self.repo.get_state(sid)
            if st is None:
                raise KeyError(f"Unbekannter Input-Zustand: {sid}")
            inputs.append(st)

        formula = copy.deepcopy(template.formula)
        if ai_priors and "factor" in ai_priors:
            mean, sigma = ai_priors["factor"]
            formula["factor"] = {"mean": mean, "sigma": sigma, "source": "ai_prior"}

        base_start = start_time_ms
        if base_start is None:
            times = [s.time_ms for s in inputs if s.time_ms is not None]
            base_start = max(times) if times else now_ms()

        dur_s = template.duration_seconds or 0.0
        dur_sigma_s = template.duration_sigma_seconds
        in_sigmas_s = [s.sigma_seconds for s in inputs if s.sigma_seconds is not None]
        base_time_sigma = max(in_sigmas_s) if in_sigmas_s else 0.0
        out_time_ms = base_start + int(dur_s * 1000)
        out_time_sigma = math.hypot(base_time_sigma, dur_sigma_s or 0.0) or None

        planned_tx = self.repo.create_transformation(
            TransformationCreate(
                name=template.name,
                modality="planned",
                driver=template.driver,
                template_id=template.id,
                parent_id=template.parent_id,
                duration_seconds=template.duration_seconds,
                duration_sigma_seconds=template.duration_sigma_seconds,
                duration_source=template.duration_source,
                cost=template.cost,
                success_prob=template.success_prob,
                formula=formula,
                meta={"scheduled_start_ms": base_start},
            )
        )

        input_roles: dict[str, EdgeRole] = formula.get("input_roles", {})
        for st in inputs:
            role: EdgeRole = input_roles.get(st.property_name, "consumes")
            self.repo.create_edge(
                EdgeCreate(
                    transformation_id=planned_tx.id,
                    state_id=st.id,
                    direction="in",
                    role=role,
                    branch_key=branch_key,
                )
            )

        created_state_ids: list[str] = []
        primary_in = next((s for s in inputs if s.value_num is not None), inputs[0])
        out_entity_id = target_entity_id or primary_in.entity_id
        out_prop = str(formula.get("output_property", primary_in.property_name))
        out_unit = str(formula.get("output_unit", primary_in.value_unit))

        kind = formula.get("kind", "duration_step")
        if kind == "product_and_split" and primary_in.value_num is not None:
            factor_cfg = formula.get("factor", {"mean": 1.0, "sigma": 0.0, "source": "ai_prior"})
            factor_source = factor_cfg.get("source", "ai_prior")
            total = propagate_product(
                GaussianValue(primary_in.value_num, primary_in.value_sigma or 0.0, source=primary_in.source),
                GaussianValue(float(factor_cfg["mean"]), float(factor_cfg["sigma"]), source=factor_source),
            )
            total_state = self.repo.create_state(
                StateCreate(
                    entity_id=out_entity_id,
                    modality="predicted",
                    source="ai_prior" if factor_source == "ai_prior" else "derived",
                    property_name=out_prop,
                    label=f"{template.name} (Gesamt)",
                    value_num=total.mean,
                    value_sigma=total.sigma,
                    value_unit=out_unit,
                    lat=primary_in.lat,
                    lon=primary_in.lon,
                    sigma_meters=primary_in.sigma_meters,
                    location_label=primary_in.location_label,
                    time_ms=out_time_ms,
                    sigma_seconds=out_time_sigma,
                    meta={"planned_tx_id": planned_tx.id, "role": "total"},
                )
            )
            created_state_ids.append(total_state.id)
            self.repo.create_edge(
                EdgeCreate(
                    transformation_id=planned_tx.id,
                    state_id=total_state.id,
                    direction="out",
                    role="produces",
                    branch_key=branch_key,
                )
            )

            parts = split_parts if split_parts is not None else int(formula.get("split_parts", 1))
            split_sigma = float(formula.get("split_sigma", 0.0))
            if parts > 1 or split_entities:
                part_entities = list(split_entities) if split_entities else [out_entity_id] * parts
                k = len(part_entities)
                per_part = propagate_split(total, k, split_sigma=split_sigma)
                for idx, ent_id in enumerate(part_entities, start=1):
                    part_state = self.repo.create_state(
                        StateCreate(
                            entity_id=ent_id,
                            modality="predicted",
                            source="derived",
                            property_name=out_prop,
                            label=f"{template.name} (Teil #{idx})",
                            value_num=per_part.mean,
                            value_sigma=per_part.sigma,
                            value_unit=out_unit,
                            lat=primary_in.lat,
                            lon=primary_in.lon,
                            sigma_meters=primary_in.sigma_meters,
                            location_label=primary_in.location_label,
                            time_ms=out_time_ms,
                            sigma_seconds=out_time_sigma,
                            meta={"planned_tx_id": planned_tx.id, "part_index": idx, "parts": k},
                        )
                    )
                    created_state_ids.append(part_state.id)
                    self.repo.create_edge(
                        EdgeCreate(
                            transformation_id=planned_tx.id,
                            state_id=part_state.id,
                            direction="out",
                            role="produces",
                            branch_key=branch_key,
                            ratio_mean=1.0 / k,
                            ratio_sigma=split_sigma,
                        )
                    )
        else:
            out_val = formula.get("output_value", primary_in.value_num)
            out_sig = formula.get("output_sigma", primary_in.value_sigma)
            out_state = self.repo.create_state(
                StateCreate(
                    entity_id=out_entity_id,
                    modality="predicted",
                    source="ai_prior" if template.duration_source == "ai_prior" else "derived",
                    property_name=out_prop,
                    label=f"{template.name} (Erwartet)",
                    value_num=out_val,
                    value_sigma=out_sig if out_val is not None else None,
                    value_unit=out_unit,
                    value_text=formula.get("output_text"),
                    lat=formula.get("lat", primary_in.lat),
                    lon=formula.get("lon", primary_in.lon),
                    sigma_meters=formula.get("sigma_meters", primary_in.sigma_meters),
                    location_label=str(formula.get("location_label", primary_in.location_label)),
                    time_ms=out_time_ms,
                    sigma_seconds=out_time_sigma,
                    meta={"planned_tx_id": planned_tx.id},
                )
            )
            created_state_ids.append(out_state.id)
            self.repo.create_edge(
                EdgeCreate(
                    transformation_id=planned_tx.id,
                    state_id=out_state.id,
                    direction="out",
                    role="produces",
                    branch_key=branch_key,
                )
            )

        return self._build_feedback(
            created_state_ids=created_state_ids,
            summary=f"Vorlage '{template.name}' instanziiert ({len(created_state_ids)} Prognose-Zustände).",
        )

    # ------------------------------------------------------------------
    # Observation ingestion, Stage-1 triggers & ai_prior calibration
    # ------------------------------------------------------------------

    def record_observation(self, data: StateCreate) -> MutationFeedback:
        """Persist a real observation, check Stage-1 triggers, calibrate priors, and re-evaluate goals."""
        if data.modality != "observed":
            raise ValueError("record_observation erfordert modality='observed'")

        obs = self.repo.create_state(data)
        triggered_ids = self._matching_trigger_templates(obs)
        calibrated_ids: list[str] = []
        updated_state_ids: list[str] = []

        # Calibrate any planned transformation output matching (entity_id, property_name)
        planned_txs = self.repo.list_transformations(modality="planned")
        for tx in planned_txs:
            if tx.template_id is None:
                continue
            out_edges = self.repo.list_edges(transformation_id=tx.id, direction="out")
            matching_total = [
                (edge, st)
                for edge in out_edges
                if edge.ratio_mean is None
                and (st := self.repo.get_state(edge.state_id)) is not None
                and st.modality == "predicted"
                and st.entity_id == obs.entity_id
                and st.property_name == obs.property_name
            ]
            if not matching_total:
                continue

            in_edges = self.repo.list_edges(transformation_id=tx.id, direction="in")
            in_states = [
                st
                for e in in_edges
                if (st := self.repo.get_state(e.state_id)) is not None
            ]
            primary_in = next((s for s in in_states if s.value_num is not None and s.value_num > 0), None)

            if tx.formula.get("kind") == "product_and_split" and obs.value_num is not None and primary_in is not None:
                template = self.repo.get_transformation(tx.template_id)
                factor_cfg = (template.formula if template else tx.formula).get("factor")
                if factor_cfg is not None:
                    obs_factor = obs.value_num / primary_in.value_num
                    obs_factor_sigma = max(
                        1e-4,
                        (obs.value_sigma if obs.value_sigma is not None else max(1.0, obs.value_num * 0.02))
                        / primary_in.value_num,
                    )
                    calibrated = calibrate_parameter(
                        GaussianValue(
                            float(factor_cfg["mean"]),
                            float(factor_cfg["sigma"]),
                            factor_cfg.get("source", "ai_prior"),
                        ),
                        [
                            GaussianValue(
                                obs_factor,
                                obs_factor_sigma,
                                source="measured",
                                measurement_source=obs.measurement_source,
                            )
                        ],
                    )
                    new_factor = {
                        "mean": calibrated.mean,
                        "sigma": calibrated.sigma,
                        "source": calibrated.source,
                    }
                    if template is not None:
                        tpl_formula = copy.deepcopy(template.formula)
                        tpl_formula["factor"] = new_factor
                        self.repo.update_transformation(template.id, formula=tpl_formula)
                        if template.id not in calibrated_ids:
                            calibrated_ids.append(template.id)

                    tx_formula = copy.deepcopy(tx.formula)
                    tx_formula["factor"] = new_factor
                    self.repo.update_transformation(tx.id, formula=tx_formula)

                    # Update total predicted state and propagate to split outputs
                    for _, total_st in matching_total:
                        updated_total = self.repo.update_state(
                            total_st.id,
                            value_num=obs.value_num,
                            value_sigma=obs.value_sigma if obs.value_sigma is not None else 0.0,
                            source="measured",
                            measurement_source=obs.measurement_source,
                        )
                        updated_state_ids.append(updated_total.id)

                    split_edges = [e for e in out_edges if e.ratio_mean is not None]
                    obs_total_val = GaussianValue(
                        obs.value_num,
                        obs.value_sigma if obs.value_sigma is not None else 0.0,
                        source="measured",
                    )
                    parts_count = len(split_edges)
                    if parts_count > 0:
                        split_sigma = float(tx.formula.get("split_sigma", 0.0))
                        per_part = propagate_split(obs_total_val, parts_count, split_sigma=split_sigma)
                        for sp_edge in split_edges:
                            part_st = self.repo.get_state(sp_edge.state_id)
                            if part_st is not None:
                                upd = self.repo.update_state(
                                    part_st.id,
                                    value_num=per_part.mean,
                                    value_sigma=per_part.sigma,
                                    source="derived",
                                )
                                updated_state_ids.append(upd.id)

            # Duration calibration for timed steps
            if (
                obs.time_ms is not None
                and tx.duration_seconds is not None
                and in_states
                and in_states[0].time_ms is not None
            ):
                observed_dur_s = (obs.time_ms - in_states[0].time_ms) / 1000.0
                if observed_dur_s >= 0.0:
                    template = self.repo.get_transformation(tx.template_id)
                    prior_dur = GaussianValue(
                        (template.duration_seconds if template and template.duration_seconds is not None else tx.duration_seconds),
                        (template.duration_sigma_seconds if template and template.duration_sigma_seconds is not None else (tx.duration_sigma_seconds or 3600.0)),
                        source=(template.duration_source if template else tx.duration_source),
                    )
                    cal_dur = calibrate_parameter(
                        prior_dur,
                        [
                            GaussianValue(
                                observed_dur_s,
                                max(1.0, obs.sigma_seconds or 60.0),
                                source="measured",
                                measurement_source=obs.measurement_source,
                            )
                        ],
                    )
                    if template is not None:
                        self.repo.update_transformation(
                            template.id,
                            duration_seconds=cal_dur.mean,
                            duration_sigma_seconds=max(1.0, cal_dur.sigma),
                            duration_source="measured",
                        )
                        if template.id not in calibrated_ids:
                            calibrated_ids.append(template.id)

        return self._build_feedback(
            created_state_ids=[obs.id],
            updated_state_ids=updated_state_ids,
            triggered_template_ids=triggered_ids,
            calibrated_template_ids=calibrated_ids,
            summary=f"Beobachtung '{obs.label}' gespeichert.",
        )

    def _matching_trigger_templates(self, obs: State) -> list[str]:
        matched: list[str] = []
        for tpl in self.repo.list_transformations(modality="template"):
            trig = tpl.formula.get("trigger")
            if not isinstance(trig, dict):
                continue
            if trig.get("property_name") != obs.property_name:
                continue
            ent = self.repo.get_entity(obs.entity_id)
            if trig.get("category") and (ent is None or ent.category != trig["category"]):
                continue
            min_v = trig.get("min_value")
            max_v = trig.get("max_value")
            eq_txt = trig.get("equals_text")
            if min_v is not None and (obs.value_num is None or obs.value_num < float(min_v)):
                continue
            if max_v is not None and (obs.value_num is None or obs.value_num > float(max_v)):
                continue
            if eq_txt is not None and obs.value_text != eq_txt:
                continue
            matched.append(tpl.id)
        return matched

    # ------------------------------------------------------------------
    # Escalation resolution
    # ------------------------------------------------------------------

    def resolve_escalation(
        self,
        goal_id: str,
        action: ResolutionAction,
        params: dict[str, Any] | None = None,
    ) -> MutationFeedback:
        """Resolve a Stage-3/4 conflict by widening bounds, switching branches, or aborting cleanly."""
        goal = self.repo.get_state(goal_id)
        if goal is None:
            raise KeyError(f"Unbekanntes Ziel: {goal_id}")
        opts = params or {}
        updated_ids: list[str] = []

        if action == "widen_tolerance":
            allowed = {
                k: opts[k]
                for k in (
                    "target_min",
                    "target_max",
                    "target_preferred",
                    "target_earliest_ms",
                    "target_latest_ms",
                    "target_radius_meters",
                    "threshold_ok",
                    "threshold_fail",
                )
                if k in opts
            }
            if not allowed:
                raise ValueError("widen_tolerance benötigt mindestens eine anzupassende Grenze")
            self.repo.update_state(goal_id, **allowed)
            updated_ids.append(goal_id)
            msg = f"Ziel-Grenzen für '{goal.label}' angepasst."

        elif action == "switch_branch":
            target_key = opts.get("branch_key")
            if not target_key:
                raise ValueError("switch_branch erfordert branch_key")
            candidates = self._find_candidates_for_goal(goal)
            choice_edges = [
                edge
                for _, _, edge in candidates
                if edge is not None and edge.logic_group == "CHOICE_OR"
            ]
            # Also collect upstream CHOICE_OR input edges if branches live on tx inputs
            for _, tx, _ in candidates:
                if tx is not None:
                    for e in self.repo.list_edges(transformation_id=tx.id, direction="in"):
                        if e.logic_group == "CHOICE_OR":
                            choice_edges.append(e)

            if not any(e.branch_key == target_key for e in choice_edges):
                raise ValueError(f"Unbekannter CHOICE_OR-Zweig: {target_key}")

            seen_edge_ids: set[str] = set()
            for edge in choice_edges:
                if edge.id in seen_edge_ids:
                    continue
                seen_edge_ids.add(edge.id)
                is_chosen = edge.branch_key == target_key
                self.repo.update_edge(
                    edge.id,
                    is_selected=is_chosen,
                    outcome="chosen" if is_chosen else "not_chosen",
                )
            msg = f"Auf Wahl-Zweig '{target_key}' umgeschaltet."

        elif action == "abort_goal":
            self.repo.update_state(goal_id, modality="cancelled")
            updated_ids.append(goal_id)
            cancelled_tx_ids: set[str] = set()
            for _, tx, _ in self._find_candidates_for_goal(goal):
                if tx is not None and tx.modality == "planned":
                    cancelled_tx_ids.add(tx.id)
            for edge in self.repo.list_edges(state_id=goal_id):
                tx = self.repo.get_transformation(edge.transformation_id)
                if tx is not None and tx.modality == "planned":
                    cancelled_tx_ids.add(tx.id)

            for tx_id in cancelled_tx_ids:
                self.repo.update_transformation(tx_id, modality="cancelled")
                for out_edge in self.repo.list_edges(transformation_id=tx_id, direction="out"):
                    st = self.repo.get_state(out_edge.state_id)
                    if st is not None and st.modality == "predicted":
                        self.repo.update_state(st.id, modality="cancelled")
                        updated_ids.append(st.id)
                for in_edge in self.repo.list_edges(transformation_id=tx_id, direction="in", role="uses"):
                    self.repo.update_edge(in_edge.id, resource_released=True)
            msg = f"Ziel '{goal.label}' abgebrochen; belegte uses-Ressourcen freigegeben."

        else:
            raise ValueError(f"Unbekannte Eskalationsaktion: {action}")

        return self._build_feedback(updated_state_ids=updated_ids, summary=msg)

    def _build_feedback(
        self,
        *,
        created_state_ids: list[str] | None = None,
        updated_state_ids: list[str] | None = None,
        triggered_template_ids: list[str] | None = None,
        calibrated_template_ids: list[str] | None = None,
        summary: str = "",
    ) -> MutationFeedback:
        open_priors = [
            s.id
            for s in self.repo.list_states(modality="predicted")
            if s.source == "ai_prior"
        ]
        return MutationFeedback(
            created_state_ids=created_state_ids or [],
            updated_state_ids=updated_state_ids or [],
            triggered_template_ids=triggered_template_ids or [],
            calibrated_template_ids=calibrated_template_ids or [],
            goal_evaluations=self.evaluate_all_goals(),
            uses_conflicts=self.detect_uses_conflicts(),
            open_ai_prior_state_ids=open_priors,
            summary=summary,
        )
