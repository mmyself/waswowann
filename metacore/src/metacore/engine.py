"""Deterministic graph propagation, calibration, conflict detection, and 4-stage escalation engine."""

from __future__ import annotations

import copy
import math
from collections import deque
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field

from metacore.clocks import (
    ClockId,
    PeriodicityReport,
    TimeSample,
    analyze_periodicity,
    evaluate_phase_condition,
)
from metacore.db import now_ms
from metacore.math_fusion import (
    GaussianValue,
    GeoPoint,
    SeriesAggregation,
    aggregate_series,
    calibrate_parameter,
    combine_goal_probability,
    compute_modifier_factor,
    convert_gaussian_value,
    fuse_1d,
    make_context_key,
    probability_in_bounds,
    probability_within_radius,
    propagate_cumulative_balance,
    propagate_exponential_decay,
    propagate_linear_combination,
    propagate_product,
    propagate_ratio,
    propagate_split,
)
from metacore.models import (
    Edge,
    EdgeCreate,
    EdgeRole,
    RawSample,
    RawSampleCreate,
    State,
    StateCreate,
    StateModality,
    StrictModel,
    Transformation,
    TransformationCreate,
    context_matches,
)
from metacore.repository import MetaCoreRepository
from metacore.rollup import RollupWindowKind, compute_rollup_buckets
from metacore.units import UnitCompatibilityError, are_units_compatible, convert_gaussian

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
                entity_id=goal.entity_id,
                property_name=goal.property_name,
                modality="predicted",
                context_filter=goal.context or None,
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
            # If both a composite parent and its child produce `pred`, surface only the boundary parent
            active_tx_ids = {e.transformation_id for e in active_edges}
            filtered_edges: list[Edge] = []
            for e in active_edges:
                tx = self.repo.get_transformation(e.transformation_id)
                if tx is not None and tx.parent_id is not None and tx.parent_id in active_tx_ids:
                    continue
                filtered_edges.append(e)
            active_edges = filtered_edges
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
            entity_id=goal.entity_id,
            property_name=goal.property_name,
            modality="observed",
            context_filter=goal.context or None,
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
                pred_val = GaussianValue(
                    pred.value_num,
                    pred.value_sigma or 0.0,
                    source=pred.source,
                    unit=pred.value_unit,
                )
                try:
                    if goal.value_unit and pred_val.unit:
                        pred_val = convert_gaussian_value(pred_val, goal.value_unit)
                    p_qty = probability_in_bounds(
                        pred_val,
                        target_min=goal.target_min,
                        target_max=goal.target_max,
                    )
                except UnitCompatibilityError:
                    p_qty = 0.0
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

        path_conf = pred.assertion_confidence if pred is not None else 1.0
        combined = combine_goal_probability(list(dim_probs.values()), path_success_prob=path_conf)
        return combined, dim_probs, broken

    def _is_ancestor_tx(self, maybe_ancestor_id: str, node_id: str) -> bool:
        """Return True if `maybe_ancestor_id` is an ancestor of `node_id` via `parent_id`."""
        cur_id: str | None = node_id
        seen: set[str] = set()
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            tx = self.repo.get_transformation(cur_id)
            if tx is None:
                break
            if tx.parent_id == maybe_ancestor_id:
                return True
            cur_id = tx.parent_id
        return False

    def _evaluate_preconditions(
        self,
        tx: Transformation | None,
        edge: Edge | None,
        *,
        _visited: set[str] | None = None,
    ) -> float:
        if tx is None:
            return 1.0
        if tx.modality == "cancelled":
            return 0.0

        visited = _visited if _visited is not None else set()
        if tx.id in visited:
            return 1.0
        visited.add(tx.id)

        children = [
            c
            for c in self.repo.list_transformations(modality="planned")
            if c.parent_id == tx.id
        ]
        # Composite parents aggregate their children's success_prob; avoid double-counting own prior
        p_pre = 1.0 if children else tx.success_prob
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
        guard_logic = str(tx.formula.get("guard_logic", "AND"))
        out_states: list[State] = []
        for oe in self.repo.list_edges(transformation_id=tx.id, direction="out"):
            ost = self.repo.get_state(oe.state_id)
            if ost is not None and ost.time_ms is not None:
                out_states.append(ost)
        tx_ref_ms: int | None = None
        if out_states:
            tx_ref_ms = max(ost.time_ms for ost in out_states if ost.time_ms is not None)
        elif isinstance(tx.meta.get("scheduled_start_ms"), (int, float)):
            tx_ref_ms = int(tx.meta["scheduled_start_ms"])
        p_pre *= self._evaluate_condition_group(
            guards,
            logic=guard_logic,
            in_states=[*in_states, *out_states],
            ref_time_ms=tx_ref_ms,
        )

        # Evaluate nested child transformations belonging to this composite step
        for child in children:
            p_pre *= self._evaluate_preconditions(child, None, _visited=visited)

        # Traverse upstream producing planned transformations recursively across the chain
        for st in in_states:
            if st.modality != "predicted":
                continue
            prod_edges = self.repo.list_edges(state_id=st.id, direction="out")
            prod_pairs: list[tuple[Transformation, Edge]] = []
            for pe in prod_edges:
                if pe.logic_group == "CHOICE_OR" and not pe.is_selected:
                    continue
                up_tx = self.repo.get_transformation(pe.transformation_id)
                if (
                    up_tx is None
                    or up_tx.modality != "planned"
                    or up_tx.id == tx.id
                    or self._is_ancestor_tx(up_tx.id, tx.id)
                    or self._is_ancestor_tx(tx.id, up_tx.id)
                ):
                    continue
                prod_pairs.append((up_tx, pe))
            prod_ids = {up.id for up, _ in prod_pairs}
            for up_tx, pe in prod_pairs:
                if up_tx.parent_id is not None and up_tx.parent_id in prod_ids:
                    continue
                p_pre *= self._evaluate_preconditions(up_tx, pe, _visited=visited)

        return max(0.0, min(1.0, p_pre))

    def _resolve_observed_candidates(
        self,
        *,
        entity_id: str | None,
        category: str | None,
        property_name: str | None,
        in_states: Sequence[State],
        default_entity_id: str | None,
        match_context: dict[str, str] | None = None,
    ) -> list[State]:
        candidates: list[State] = []
        if entity_id and property_name:
            candidates = self.repo.list_states(
                entity_id=entity_id,
                property_name=property_name,
                modality="observed",
                context_filter=match_context,
            )
        elif category and property_name:
            matching_ent_ids = {
                ent.id
                for ent in self.repo.list_entities(include_archived=False)
                if ent.category == category
            }
            candidates = [
                st
                for st in self.repo.list_states(
                    property_name=property_name,
                    modality="observed",
                    context_filter=match_context,
                )
                if st.entity_id in matching_ent_ids
            ]

        if not candidates:
            for st in in_states:
                if (
                    (entity_id is None or st.entity_id == entity_id)
                    and (property_name is None or st.property_name == property_name)
                    and context_matches(st.context, match_context)
                ):
                    obs_for_ent = self.repo.list_states(
                        entity_id=st.entity_id,
                        property_name=st.property_name,
                        modality="observed",
                        context_filter=match_context,
                    )
                    if obs_for_ent:
                        candidates.extend(obs_for_ent)
                    else:
                        candidates.append(st)

        if not candidates and property_name:
            target_ent = entity_id or default_entity_id
            if target_ent:
                candidates = self.repo.list_states(
                    entity_id=target_ent,
                    property_name=property_name,
                    modality="observed",
                    context_filter=match_context,
                )
            if not candidates and entity_id is None:
                candidates = self.repo.list_states(
                    property_name=property_name,
                    modality="observed",
                    context_filter=match_context,
                )

        deduped: dict[str, State] = {
            st.id: st for st in candidates if context_matches(st.context, match_context)
        }
        return sorted(
            deduped.values(),
            key=lambda s: (
                s.time_ms if s.time_ms is not None else s.created_at_ms,
                s.created_at_ms,
                s.id,
            ),
        )

    def _resolve_latest_numeric_state(
        self,
        *,
        entity_id: str | None,
        property_name: str | None,
        in_states: Sequence[State],
        default_entity_id: str | None = None,
        match_context: dict[str, str] | None = None,
    ) -> State | None:
        candidates = [
            st
            for st in self._resolve_observed_candidates(
                entity_id=entity_id,
                category=None,
                property_name=property_name,
                in_states=in_states,
                default_entity_id=default_entity_id,
                match_context=match_context,
            )
            if st.value_num is not None
        ]
        return candidates[-1] if candidates else None

    def _evaluate_ratio_condition(
        self,
        cond: dict[str, Any],
        *,
        in_states: Sequence[State],
        default_entity_id: str | None,
    ) -> float:
        base_ctx = cond.get("match_context")
        num_state = self._resolve_latest_numeric_state(
            entity_id=cond.get("numerator_entity_id"),
            property_name=cond.get("numerator_property"),
            in_states=in_states,
            default_entity_id=default_entity_id,
            match_context=cond.get("numerator_context", base_ctx),
        )
        den_state = self._resolve_latest_numeric_state(
            entity_id=cond.get("denominator_entity_id"),
            property_name=cond.get("denominator_property"),
            in_states=in_states,
            default_entity_id=default_entity_id,
            match_context=cond.get("denominator_context", base_ctx),
        )
        if num_state is None or den_state is None or num_state.value_num is None or den_state.value_num is None:
            return 0.0

        num_g = GaussianValue(
            num_state.value_num,
            num_state.value_sigma or 0.0,
            source=num_state.source,
            unit=num_state.value_unit,
        )
        den_g = GaussianValue(
            den_state.value_num,
            den_state.value_sigma or 0.0,
            source=den_state.source,
            unit=den_state.value_unit,
        )
        if (
            num_g.unit
            and den_g.unit
            and num_g.unit != den_g.unit
            and are_units_compatible(num_g.unit, den_g.unit)
        ):
            num_g = convert_gaussian_value(num_g, den_g.unit)

        if abs(den_g.mean) < 1e-12:
            return 0.0

        ratio = propagate_ratio(num_g, den_g)
        min_ratio = cond.get("min_ratio", cond.get("min_value"))
        max_ratio = cond.get("max_ratio", cond.get("max_value"))
        p_ratio = probability_in_bounds(
            ratio,
            target_min=float(min_ratio) if min_ratio is not None else None,
            target_max=float(max_ratio) if max_ratio is not None else None,
        )
        return max(
            0.0,
            min(1.0, p_ratio * min(num_state.assertion_confidence, den_state.assertion_confidence)),
        )

    def _evaluate_forbid_coexistence(
        self,
        cond: dict[str, Any],
        *,
        in_states: Sequence[State],
        ref_time_ms: int | None,
        default_entity_id: str | None,
        default_category: str | None,
    ) -> float:
        items = list(cond.get("items", []))
        if not items and cond.get("a") is not None and cond.get("b") is not None:
            items = [cond["a"], cond["b"]]
        if len(items) < 2:
            return 1.0

        probs: list[float] = []
        latest_states: list[State | None] = []
        for item in items:
            item_prob = self._evaluate_condition(
                item,
                in_states=in_states,
                ref_time_ms=ref_time_ms,
                default_entity_id=default_entity_id,
                default_category=default_category,
            )
            probs.append(item_prob)
            if item.get("kind") in ("ratio", "period_phase", "forbid_coexistence"):
                latest_states.append(None)
            else:
                latest_states.append(
                    self._resolve_latest_numeric_or_any_state(
                        item,
                        in_states=in_states,
                        default_entity_id=default_entity_id,
                        default_category=default_category,
                    )
                )

        radius_raw = cond.get("same_location_radius_meters")
        if radius_raw is not None:
            radius_m = float(radius_raw)
            none_coexist = 1.0
            for i in range(len(probs)):
                for j in range(i + 1, len(probs)):
                    spatial_p = 1.0
                    st_a = latest_states[i]
                    st_b = latest_states[j]
                    if (
                        st_a is not None
                        and st_b is not None
                        and st_a.lat is not None
                        and st_a.lon is not None
                        and st_b.lat is not None
                        and st_b.lon is not None
                    ):
                        comb_sigma = math.hypot(st_a.sigma_meters or 1.0, st_b.sigma_meters or 1.0)
                        spatial_p = probability_within_radius(
                            GeoPoint(st_a.lat, st_a.lon, comb_sigma),
                            st_b.lat,
                            st_b.lon,
                            radius_m,
                        )
                    pair_p = probs[i] * probs[j] * spatial_p
                    none_coexist *= 1.0 - max(0.0, min(1.0, pair_p))
            p_coexist = 1.0 - none_coexist
        else:
            p_zero = 1.0
            for p in probs:
                p_zero *= 1.0 - p
            p_one = 0.0
            for i, p in enumerate(probs):
                others = 1.0
                for j, q in enumerate(probs):
                    if i != j:
                        others *= 1.0 - q
                p_one += p * others
            p_coexist = 1.0 - p_zero - p_one

        p_coexist = max(0.0, min(1.0, p_coexist))
        return max(0.0, min(1.0, 1.0 - p_coexist))

    def _resolve_latest_numeric_or_any_state(
        self,
        cond: dict[str, Any],
        *,
        in_states: Sequence[State],
        default_entity_id: str | None,
        default_category: str | None,
    ) -> State | None:
        cands = self._resolve_observed_candidates(
            entity_id=cond.get("entity_id"),
            category=cond.get("category", default_category),
            property_name=cond.get("property_name"),
            in_states=in_states,
            default_entity_id=default_entity_id,
            match_context=cond.get("match_context"),
        )
        return cands[-1] if cands else None

    def _evaluate_condition(
        self,
        cond: dict[str, Any],
        *,
        in_states: Sequence[State],
        ref_time_ms: int | None = None,
        default_entity_id: str | None = None,
        default_category: str | None = None,
    ) -> float:
        if cond.get("kind") == "ratio":
            return self._evaluate_ratio_condition(
                cond, in_states=in_states, default_entity_id=default_entity_id
            )
        if cond.get("kind") == "forbid_coexistence":
            return self._evaluate_forbid_coexistence(
                cond,
                in_states=in_states,
                ref_time_ms=ref_time_ms,
                default_entity_id=default_entity_id,
                default_category=default_category,
            )
        if cond.get("kind") == "period_phase":
            use_time = ref_time_ms
            use_sigma: float | None = None
            use_lat = cond.get("lat")
            use_lon = cond.get("lon")
            for st in reversed(in_states):
                if use_time is None and st.time_ms is not None:
                    use_time = st.time_ms
                if use_sigma is None and st.time_ms == use_time and st.sigma_seconds is not None:
                    use_sigma = st.sigma_seconds
                if (use_lat is None or use_lon is None) and st.lat is not None and st.lon is not None:
                    use_lat = st.lat
                    use_lon = st.lon
            if use_time is None:
                use_time = now_ms()
            return evaluate_phase_condition(
                cond,
                time_ms=use_time,
                sigma_seconds=use_sigma,
                lat=float(use_lat) if use_lat is not None else None,
                lon=float(use_lon) if use_lon is not None else None,
            )

        candidates = self._resolve_observed_candidates(
            entity_id=cond.get("entity_id"),
            category=cond.get("category", default_category),
            property_name=cond.get("property_name"),
            in_states=in_states,
            default_entity_id=default_entity_id,
            match_context=cond.get("match_context"),
        )
        if not candidates:
            return 0.0

        window_seconds = cond.get("window_seconds")
        if window_seconds is not None:
            ref_ms = (
                ref_time_ms
                if ref_time_ms is not None
                else max(s.time_ms if s.time_ms is not None else s.created_at_ms for s in candidates)
            )
            cutoff = ref_ms - int(float(window_seconds) * 1000)
            candidates = [
                st for st in candidates if st.time_ms is not None and cutoff <= st.time_ms <= ref_ms
            ]
            if not candidates:
                return 0.0

        latest_state = candidates[-1]
        required_text = cond.get("required_text", cond.get("equals_text"))
        if required_text is not None and latest_state.value_text != required_text:
            return 0.0

        forbidden_texts = cond.get("forbidden_texts", [])
        if latest_state.value_text is not None and latest_state.value_text in forbidden_texts:
            return 0.0

        min_val = cond.get("min_value")
        max_val = cond.get("max_value")
        if min_val is not None or max_val is not None:
            cond_unit = cond.get("unit")
            numeric_samples: list[GaussianValue] = []
            used_confidences: list[float] = []
            for st in candidates:
                if st.value_num is None:
                    continue
                gv = GaussianValue(
                    st.value_num,
                    st.value_sigma or 0.0,
                    source=st.source,
                    unit=st.value_unit,
                )
                if cond_unit:
                    try:
                        gv = convert_gaussian_value(gv, str(cond_unit))
                    except UnitCompatibilityError:
                        continue
                numeric_samples.append(gv)
                used_confidences.append(st.assertion_confidence)
            if not numeric_samples:
                return 0.0
            agg_mode: SeriesAggregation = str(cond.get("aggregation", "latest"))  # type: ignore[assignment]
            try:
                aggregated = aggregate_series(
                    numeric_samples,
                    agg_mode,
                    target_unit=str(cond_unit) if cond_unit else None,
                )
            except UnitCompatibilityError:
                return 0.0
            p_bounds = probability_in_bounds(
                aggregated,
                target_min=float(min_val) if min_val is not None else None,
                target_max=float(max_val) if max_val is not None else None,
            )
            eff_conf = (
                used_confidences[-1]
                if agg_mode == "latest"
                else min(used_confidences)
            )
            return max(0.0, min(1.0, p_bounds * eff_conf))

        return latest_state.assertion_confidence

    def _evaluate_condition_group(
        self,
        conditions: Sequence[dict[str, Any]],
        *,
        logic: str = "AND",
        in_states: Sequence[State] = (),
        ref_time_ms: int | None = None,
        default_entity_id: str | None = None,
        default_category: str | None = None,
    ) -> float:
        if not conditions:
            return 1.0
        probs = [
            self._evaluate_condition(
                cond,
                in_states=in_states,
                ref_time_ms=ref_time_ms,
                default_entity_id=default_entity_id,
                default_category=default_category,
            )
            for cond in conditions
        ]
        if logic.upper() == "OR":
            complement = 1.0
            for prob in probs:
                complement *= 1.0 - prob
            return max(0.0, min(1.0, 1.0 - complement))
        total = 1.0
        for prob in probs:
            total *= prob
        return max(0.0, min(1.0, total))

    def _evaluate_single_guard(self, guard: dict[str, Any], in_states: Sequence[State]) -> float:
        return self._evaluate_condition(guard, in_states=in_states)

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
                    if (
                        tx_a.id == tx_b.id
                        or self._is_ancestor_tx(tx_a.id, tx_b.id)
                        or self._is_ancestor_tx(tx_b.id, tx_a.id)
                    ):
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

    @staticmethod
    def _collect_formula_props(formula: dict[str, Any]) -> set[str]:
        props: set[str] = set()
        input_roles = formula.get("input_roles")
        if isinstance(input_roles, dict):
            props.update(str(k) for k in input_roles)
        for key in ("ingredients", "guards", "ratio_guards", "modifiers", "inflows", "outflows"):
            for item in formula.get(key, []):
                if not isinstance(item, dict):
                    continue
                for prop_key in ("property_name", "numerator_property", "denominator_property"):
                    if item.get(prop_key):
                        props.add(str(item[prop_key]))
                for sub_item in item.get("items", []):
                    if isinstance(sub_item, dict) and sub_item.get("property_name"):
                        props.add(str(sub_item["property_name"]))
        return props

    def _rollup_composite_tx(self, parent_tx_id: str) -> None:
        """Roll up start time, summed duration/cost, RSS sigma, and product success_prob from children."""
        parent = self.repo.get_transformation(parent_tx_id)
        if parent is None:
            return
        children = [
            t
            for t in self.repo.list_transformations(modality="planned")
            if t.parent_id == parent_tx_id
        ]
        if not children:
            return

        windows = [w for c in children if (w := self._tx_time_window(c)) is not None]
        if windows:
            rolled_start = min(w[0] for w in windows)
            rolled_end = max(w[1] for w in windows)
            rolled_dur_s: float | None = max(0.0, (rolled_end - rolled_start) / 1000.0)
        else:
            rolled_start = parent.meta.get("scheduled_start_ms")
            durs = [c.duration_seconds for c in children if c.duration_seconds is not None]
            rolled_dur_s = sum(durs) if durs else parent.duration_seconds

        sigmas = [c.duration_sigma_seconds for c in children if c.duration_sigma_seconds is not None]
        rolled_sigma_s = (math.hypot(*sigmas) or None) if sigmas else parent.duration_sigma_seconds
        rolled_cost = sum(c.cost for c in children)
        rolled_prob = math.prod(c.success_prob for c in children)

        meta_upd = dict(parent.meta)
        if rolled_start is not None:
            meta_upd["scheduled_start_ms"] = int(rolled_start)
        meta_upd["is_composite"] = True

        self.repo.update_transformation(
            parent_tx_id,
            duration_seconds=rolled_dur_s,
            duration_sigma_seconds=rolled_sigma_s,
            cost=rolled_cost,
            success_prob=max(1e-9, min(1.0, rolled_prob)),
            meta=meta_upd,
        )
        if parent.parent_id is not None:
            self._rollup_composite_tx(parent.parent_id)

    @staticmethod
    def _pick_primary_input(inputs: Sequence[State], edges: Sequence[Edge] = ()) -> State:
        if edges:
            causal_ids = {e.state_id for e in edges if e.role in ("consumes", "uses")}
            causal_num = next((s for s in inputs if s.id in causal_ids and s.value_num is not None), None)
            if causal_num is not None:
                return causal_num
            causal_any = next((s for s in inputs if s.id in causal_ids), None)
            if causal_any is not None:
                return causal_any
        return next((s for s in inputs if s.value_num is not None), inputs[0])

    def _compute_duration_step_output(
        self,
        *,
        template: Transformation | None,
        formula: dict[str, Any],
        primary_in: State,
        modifiers: Sequence[dict[str, Any]],
        inputs: Sequence[State],
    ) -> tuple[float | None, float | None]:
        out_unit = str(formula.get("output_unit", primary_in.value_unit))
        inherit_val = bool(formula.get("inherit_input_value", False))
        raw_out_val = None if inherit_val else formula.get("output_value")
        raw_out_sig = None if inherit_val else formula.get("output_sigma")
        dur_src = template.duration_source if template is not None else "derived"
        if raw_out_val is None and primary_in.value_num is not None:
            base_g = GaussianValue(
                primary_in.value_num,
                primary_in.value_sigma or 0.0,
                source=primary_in.source,
                unit=primary_in.value_unit,
            )
            if out_unit and primary_in.value_unit and are_units_compatible(primary_in.value_unit, out_unit):
                base_g = convert_gaussian_value(base_g, out_unit)
            if formula.get("scale_mean") is not None:
                scaled = propagate_product(
                    base_g,
                    GaussianValue(
                        float(formula["scale_mean"]),
                        float(formula.get("scale_sigma", 0.0)),
                        source=str(formula.get("scale_source", "derived")),
                    ),
                )
                out_val: float | None = scaled.mean
                out_sig: float | None = scaled.sigma
            else:
                out_val = base_g.mean
                out_sig = base_g.sigma if primary_in.value_sigma is not None else None
        else:
            out_val = float(raw_out_val) if raw_out_val is not None else primary_in.value_num
            out_sig = float(raw_out_sig) if raw_out_sig is not None else primary_in.value_sigma

        if out_val is not None and modifiers:
            mod_g, _, _ = self._apply_modifiers(
                output_value=GaussianValue(
                    float(out_val),
                    float(out_sig or 0.0),
                    source="ai_prior" if dur_src == "ai_prior" else "derived",
                ),
                duration_seconds=None,
                duration_sigma_seconds=None,
                modifiers=modifiers,
                inputs=inputs,
            )
            assert mod_g is not None
            out_val = mod_g.mean
            out_sig = mod_g.sigma
        return out_val, out_sig

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
        parent_tx_id: str | None = None,
    ) -> MutationFeedback:
        """Instantiate a reusable template (or composite hierarchy) into planned transformation(s)."""
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

        # Merge declarative ratio_guards into guards so evaluate_goal checks them uniformly
        guards_list: list[dict[str, Any]] = list(formula.get("guards", []))
        for rg in formula.get("ratio_guards", []):
            rg_copy = dict(rg)
            rg_copy.setdefault("kind", "ratio")
            if rg_copy not in guards_list:
                guards_list.append(rg_copy)
        if guards_list:
            formula["guards"] = guards_list

        base_start = start_time_ms
        if base_start is None:
            times = [s.time_ms for s in inputs if s.time_ms is not None]
            base_start = max(times) if times else now_ms()

        child_templates = sorted(
            [
                t
                for t in self.repo.list_transformations(modality="template")
                if t.parent_id == template.id
            ],
            key=lambda t: (int(t.meta.get("step_order", 0)), t.created_at_ms, t.id),
        )
        if child_templates:
            planned_parent = self.repo.create_transformation(
                TransformationCreate(
                    name=template.name,
                    modality="planned",
                    driver=template.driver,
                    template_id=template.id,
                    parent_id=parent_tx_id,
                    duration_seconds=template.duration_seconds,
                    duration_sigma_seconds=template.duration_sigma_seconds,
                    duration_source=template.duration_source,
                    cost=template.cost,
                    success_prob=template.success_prob,
                    formula=formula,
                    meta={"scheduled_start_ms": base_start, "is_composite": True},
                )
            )
            parent_roles: dict[str, EdgeRole] = dict(formula.get("input_roles", {}))
            for st in inputs:
                role: EdgeRole = parent_roles.get(st.property_name, "consumes")
                self.repo.create_edge(
                    EdgeCreate(
                        transformation_id=planned_parent.id,
                        state_id=st.id,
                        direction="in",
                        role=role,
                        branch_key=branch_key,
                    )
                )

            cur_start_ms: int | None = base_start
            prev_primary_output_id: str | None = None
            all_created_ids: list[str] = []
            final_output_ids: list[str] = []

            for idx, child_tpl in enumerate(child_templates):
                child_props = self._collect_formula_props(child_tpl.formula)
                if idx == 0:
                    child_in_ids = [inputs[0].id]
                    for st in inputs[1:]:
                        if not child_props or st.property_name in child_props:
                            child_in_ids.append(st.id)
                else:
                    assert prev_primary_output_id is not None
                    child_in_ids = [prev_primary_output_id]
                    for st in inputs:
                        if st.id != prev_primary_output_id and st.property_name in child_props:
                            child_in_ids.append(st.id)

                is_last = idx == len(child_templates) - 1
                child_fb = self.instantiate_template(
                    child_tpl.id,
                    input_state_ids=child_in_ids,
                    target_entity_id=target_entity_id,
                    start_time_ms=cur_start_ms,
                    ai_priors=ai_priors,
                    split_parts=split_parts if is_last else None,
                    split_entities=split_entities if is_last else None,
                    branch_key=branch_key,
                    parent_tx_id=planned_parent.id,
                )
                all_created_ids.extend(child_fb.created_state_ids)
                if not is_last:
                    for sid in child_fb.created_state_ids:
                        st_obj = self.repo.get_state(sid)
                        if st_obj is not None:
                            new_meta = dict(st_obj.meta)
                            new_meta.setdefault("internal_to_parent_tx_id", planned_parent.id)
                            self.repo.update_state(sid, meta=new_meta)
                else:
                    final_output_ids = list(child_fb.created_state_ids)

                prev_primary_output_id = child_fb.created_state_ids[0]
                prev_state = self.repo.get_state(prev_primary_output_id)
                if prev_state is not None and prev_state.time_ms is not None:
                    cur_start_ms = prev_state.time_ms

            planned_children = [
                t
                for t in self.repo.list_transformations(modality="planned")
                if t.parent_id == planned_parent.id
            ]
            if planned_children:
                last_child_tx = planned_children[-1]
                for oe in self.repo.list_edges(transformation_id=last_child_tx.id, direction="out"):
                    self.repo.create_edge(
                        EdgeCreate(
                            transformation_id=planned_parent.id,
                            state_id=oe.state_id,
                            direction="out",
                            role=oe.role,
                            branch_key=oe.branch_key,
                            ratio_mean=oe.ratio_mean,
                            ratio_sigma=oe.ratio_sigma,
                        )
                    )

            self._rollup_composite_tx(planned_parent.id)
            return self._build_feedback(
                created_state_ids=final_output_ids,
                summary=(
                    f"Verschachtelte Vorlage '{template.name}' mit {len(child_templates)} "
                    f"Unterschritten instanziiert."
                ),
            )

        modifiers: list[dict[str, Any]] = list(formula.get("modifiers", []))
        _, mod_dur_s, mod_dur_sigma_s = self._apply_modifiers(
            output_value=None,
            duration_seconds=template.duration_seconds,
            duration_sigma_seconds=template.duration_sigma_seconds,
            modifiers=modifiers,
            inputs=inputs,
        )
        dur_s = mod_dur_s or 0.0
        dur_sigma_s = mod_dur_sigma_s
        input_roles: dict[str, EdgeRole] = dict(formula.get("input_roles", {}))
        for ing in formula.get("ingredients", []):
            if isinstance(ing, dict) and ing.get("property_name") and ing.get("role"):
                input_roles.setdefault(str(ing["property_name"]), ing["role"])
        causal_sigmas_s = [
            s.sigma_seconds
            for s in inputs
            if s.sigma_seconds is not None and input_roles.get(s.property_name, "consumes") in ("consumes", "uses")
        ]
        all_sigmas_s = [s.sigma_seconds for s in inputs if s.sigma_seconds is not None]
        sigmas_pool = causal_sigmas_s or all_sigmas_s
        base_time_sigma = max(sigmas_pool) if sigmas_pool else 0.0
        out_time_ms = base_start + int(dur_s * 1000)
        out_time_sigma = math.hypot(base_time_sigma, dur_sigma_s or 0.0) or None

        planned_tx = self.repo.create_transformation(
            TransformationCreate(
                name=template.name,
                modality="planned",
                driver=template.driver,
                template_id=template.id,
                parent_id=parent_tx_id,
                duration_seconds=mod_dur_s,
                duration_sigma_seconds=mod_dur_sigma_s,
                duration_source=template.duration_source,
                cost=template.cost,
                success_prob=template.success_prob,
                formula=formula,
                meta={"scheduled_start_ms": base_start},
            )
        )

        created_in_edges: list[Edge] = []
        for st in inputs:
            role: EdgeRole = input_roles.get(st.property_name, "consumes")
            created_in_edges.append(
                self.repo.create_edge(
                    EdgeCreate(
                        transformation_id=planned_tx.id,
                        state_id=st.id,
                        direction="in",
                        role=role,
                        branch_key=branch_key,
                    )
                )
            )

        created_state_ids: list[str] = []
        primary_in = self._pick_primary_input(inputs, created_in_edges)
        out_entity_id = target_entity_id or primary_in.entity_id
        out_prop = str(formula.get("output_property", primary_in.property_name))
        out_unit = str(formula.get("output_unit", primary_in.value_unit))

        kind = formula.get("kind", "duration_step")
        if kind == "product_and_split" and primary_in.value_num is not None:
            factor_cfg = formula.get("factor", {"mean": 1.0, "sigma": 0.0, "source": "ai_prior"})
            factor_source = factor_cfg.get("source", "ai_prior")
            in_g = GaussianValue(
                primary_in.value_num,
                primary_in.value_sigma or 0.0,
                source=primary_in.source,
                unit=primary_in.value_unit,
            )
            if out_unit and primary_in.value_unit and are_units_compatible(primary_in.value_unit, out_unit):
                in_g = convert_gaussian_value(in_g, out_unit)
            total = propagate_product(
                in_g,
                GaussianValue(float(factor_cfg["mean"]), float(factor_cfg["sigma"]), source=factor_source),
            )
            mod_total, _, _ = self._apply_modifiers(
                output_value=total,
                duration_seconds=None,
                duration_sigma_seconds=None,
                modifiers=modifiers,
                inputs=inputs,
            )
            assert mod_total is not None
            created_state_ids.extend(
                self._emit_total_and_split_outputs(
                    template=template,
                    planned_tx=planned_tx,
                    formula=formula,
                    primary_in=primary_in,
                    out_entity_id=out_entity_id,
                    out_prop=out_prop,
                    out_unit=out_unit,
                    total=mod_total,
                    total_source="ai_prior" if factor_source == "ai_prior" else "derived",
                    out_time_ms=out_time_ms,
                    out_time_sigma=out_time_sigma,
                    split_parts=split_parts,
                    split_entities=split_entities,
                    branch_key=branch_key,
                )
            )
        elif kind == "recipe_mix":
            raw_total, extra_meta = self._compute_recipe_mix(formula, inputs)
            mod_total, _, _ = self._apply_modifiers(
                output_value=raw_total,
                duration_seconds=None,
                duration_sigma_seconds=None,
                modifiers=modifiers,
                inputs=inputs,
            )
            assert mod_total is not None
            created_state_ids.extend(
                self._emit_total_and_split_outputs(
                    template=template,
                    planned_tx=planned_tx,
                    formula=formula,
                    primary_in=primary_in,
                    out_entity_id=out_entity_id,
                    out_prop=out_prop,
                    out_unit=out_unit,
                    total=mod_total,
                    total_source="ai_prior" if mod_total.source == "ai_prior" else "derived",
                    out_time_ms=out_time_ms,
                    out_time_sigma=out_time_sigma,
                    split_parts=split_parts,
                    split_entities=split_entities,
                    branch_key=branch_key,
                    extra_meta=extra_meta,
                )
            )
        elif kind == "decay_step" and primary_in.value_num is not None:
            raw_total = self._compute_decay_step(
                formula=formula,
                primary_in=primary_in,
                out_unit=out_unit,
                dur_s=dur_s,
                dur_sigma_s=dur_sigma_s,
            )
            mod_total, _, _ = self._apply_modifiers(
                output_value=raw_total,
                duration_seconds=None,
                duration_sigma_seconds=None,
                modifiers=modifiers,
                inputs=inputs,
            )
            assert mod_total is not None
            created_state_ids.extend(
                self._emit_total_and_split_outputs(
                    template=template,
                    planned_tx=planned_tx,
                    formula=formula,
                    primary_in=primary_in,
                    out_entity_id=out_entity_id,
                    out_prop=out_prop,
                    out_unit=out_unit or mod_total.unit,
                    total=mod_total,
                    total_source="ai_prior" if mod_total.source == "ai_prior" else "derived",
                    out_time_ms=out_time_ms,
                    out_time_sigma=out_time_sigma,
                    split_parts=split_parts,
                    split_entities=split_entities,
                    branch_key=branch_key,
                )
            )
        elif kind == "cumulative_balance":
            raw_total, extra_meta = self._compute_cumulative_balance(
                formula=formula,
                inputs=inputs,
                primary_in=primary_in,
                out_unit=out_unit,
                ref_time_ms=out_time_ms,
            )
            mod_total, _, _ = self._apply_modifiers(
                output_value=raw_total,
                duration_seconds=None,
                duration_sigma_seconds=None,
                modifiers=modifiers,
                inputs=inputs,
            )
            assert mod_total is not None
            created_state_ids.extend(
                self._emit_total_and_split_outputs(
                    template=template,
                    planned_tx=planned_tx,
                    formula=formula,
                    primary_in=primary_in,
                    out_entity_id=out_entity_id,
                    out_prop=out_prop,
                    out_unit=out_unit or mod_total.unit,
                    total=mod_total,
                    total_source="ai_prior" if mod_total.source == "ai_prior" else "derived",
                    out_time_ms=out_time_ms,
                    out_time_sigma=out_time_sigma,
                    split_parts=split_parts,
                    split_entities=split_entities,
                    branch_key=branch_key,
                    extra_meta=extra_meta,
                )
            )
        else:
            out_val, out_sig = self._compute_duration_step_output(
                template=template,
                formula=formula,
                primary_in=primary_in,
                modifiers=modifiers,
                inputs=inputs,
            )
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

    def _apply_modifiers(
        self,
        *,
        output_value: GaussianValue | None,
        duration_seconds: float | None,
        duration_sigma_seconds: float | None,
        modifiers: Sequence[dict[str, Any]],
        inputs: Sequence[State],
    ) -> tuple[GaussianValue | None, float | None, float | None]:
        cur_val = output_value
        cur_dur = duration_seconds
        cur_sig = duration_sigma_seconds
        for mod_spec in modifiers:
            st = self._resolve_latest_numeric_state(
                entity_id=mod_spec.get("entity_id"),
                property_name=mod_spec.get("property_name"),
                in_states=inputs,
                match_context=mod_spec.get("match_context"),
            )
            if st is None or st.value_num is None:
                continue
            mod_obs = GaussianValue(
                st.value_num,
                st.value_sigma or 0.0,
                source=st.source,
                unit=st.value_unit,
            )
            mod_unit = mod_spec.get("unit")
            if mod_unit:
                try:
                    mod_obs = convert_gaussian_value(mod_obs, str(mod_unit))
                except UnitCompatibilityError:
                    continue
            factor = compute_modifier_factor(
                mod_obs,
                optimal_min=float(mod_spec["optimal_min"]),
                optimal_max=float(mod_spec["optimal_max"]),
                falloff_per_unit=float(mod_spec["falloff_per_unit"]),
                min_factor=float(mod_spec.get("min_factor", 0.0)),
                sigma_penalty_per_unit=float(mod_spec.get("sigma_penalty_per_unit", 0.0)),
            )
            applies_to = str(mod_spec.get("applies_to", "output"))
            if applies_to == "output" and cur_val is not None:
                cur_val = propagate_product(cur_val, factor)
            elif applies_to == "duration" and cur_dur is not None and factor.mean > 0.0:
                orig_dur = cur_dur
                orig_sig = cur_sig or 0.0
                cur_dur = orig_dur / factor.mean
                scaled_sig = math.hypot(orig_sig / factor.mean, orig_dur * factor.sigma / factor.mean)
                cur_sig = scaled_sig if (cur_sig is not None or scaled_sig > 0.0) else None
        return cur_val, cur_dur, cur_sig

    def _compute_recipe_mix(
        self, formula: dict[str, Any], inputs: Sequence[State]
    ) -> tuple[GaussianValue, dict[str, Any]]:
        ingredients = list(formula.get("ingredients", []))
        if not ingredients:
            raise ValueError("recipe_mix erfordert mindestens ein ingredient in formula['ingredients']")

        def _match_input(spec: dict[str, Any]) -> State:
            prop = str(spec["property_name"])
            ent_id = spec.get("entity_id")
            req_ctx = spec.get("match_context")
            st = next(
                (
                    item
                    for item in inputs
                    if item.property_name == prop
                    and (ent_id is None or item.entity_id == ent_id)
                    and context_matches(item.context, req_ctx)
                ),
                None,
            )
            if st is None or st.value_num is None:
                raise ValueError(f"Fehlender numerischer Zutat-Zustand für '{prop}'")
            return st

        mode = str(formula.get("mode", "weighted_sum"))
        if mode == "weighted_sum":
            out_unit_raw = str(formula.get("output_unit", "") or "")
            terms: list[tuple[GaussianValue, GaussianValue]] = []
            for spec in ingredients:
                st = _match_input(spec)
                assert st.value_num is not None
                gv = GaussianValue(
                    st.value_num,
                    st.value_sigma or 0.0,
                    source=st.source,
                    unit=st.value_unit,
                )
                spec_unit = spec.get("unit")
                if spec_unit:
                    gv = convert_gaussian_value(gv, str(spec_unit))
                elif out_unit_raw and st.value_unit and are_units_compatible(st.value_unit, out_unit_raw):
                    gv = convert_gaussian_value(gv, out_unit_raw)
                terms.append(
                    (
                        gv,
                        GaussianValue(
                            float(spec.get("weight", 1.0)),
                            float(spec.get("weight_sigma", 0.0)),
                            source=spec.get("source", "derived"),
                        ),
                    )
                )
            non_empty_units = [gv.unit for gv, _ in terms if gv.unit]
            all_compatible = all(
                are_units_compatible(non_empty_units[0], u) for u in non_empty_units[1:]
            ) if non_empty_units else True
            if all_compatible:
                can_target = (
                    out_unit_raw
                    if (not non_empty_units or not out_unit_raw or are_units_compatible(non_empty_units[0], out_unit_raw))
                    else None
                )
                return propagate_linear_combination(terms, target_unit=can_target), {}
            # Heterogeneous ingredient dimensions (weights carry dimensional conversion to output_unit)
            dimensionless_terms = [
                (
                    GaussianValue(v.mean, v.sigma, source=v.source, measurement_source=v.measurement_source),
                    w,
                )
                for v, w in terms
            ]
            combined = propagate_linear_combination(dimensionless_terms)
            return (
                GaussianValue(
                    combined.mean,
                    combined.sigma,
                    source=combined.source,
                    unit=out_unit_raw,
                ),
                {},
            )

        if mode == "limiting_factor":
            caps: list[tuple[str, GaussianValue]] = []
            for spec in ingredients:
                per_unit = spec.get("per_unit")
                if per_unit is None or float(per_unit) <= 0.0:
                    continue
                st = _match_input(spec)
                assert st.value_num is not None
                gv = GaussianValue(
                    st.value_num,
                    st.value_sigma or 0.0,
                    source=st.source,
                    unit=st.value_unit,
                )
                spec_unit = spec.get("unit")
                if spec_unit:
                    gv = convert_gaussian_value(gv, str(spec_unit))
                cap = propagate_ratio(
                    gv,
                    GaussianValue(float(per_unit), 0.0, source="derived"),
                )
                caps.append((str(spec["property_name"]), cap))
            if not caps:
                raise ValueError("limiting_factor erfordert ingredients mit per_unit > 0")
            bottleneck_prop, bottleneck_cap = min(caps, key=lambda item: item[1].mean)
            unit_out = GaussianValue(
                float(formula.get("unit_output_mean", 1.0)),
                float(formula.get("unit_output_sigma", 0.0)),
                source=formula.get("unit_output_source", "derived"),
            )
            return propagate_product(bottleneck_cap, unit_out), {"limiting_ingredient": bottleneck_prop}

        raise ValueError(f"Unbekannter recipe_mix Modus: {mode}")

    @staticmethod
    def _parse_gaussian_param(
        raw: Any,
        *,
        default_mean: float = 0.0,
        default_sigma: float = 0.0,
        default_source: Any = "derived",
        default_unit: str = "",
    ) -> GaussianValue:
        if raw is None:
            return GaussianValue(default_mean, default_sigma, source=default_source, unit=default_unit)
        if isinstance(raw, (int, float)):
            return GaussianValue(float(raw), default_sigma, source=default_source, unit=default_unit)
        if isinstance(raw, dict):
            return GaussianValue(
                float(raw.get("mean", default_mean)),
                float(raw.get("sigma", default_sigma)),
                source=raw.get("source", default_source),
                unit=str(raw.get("unit", default_unit)),
            )
        raise ValueError(f"Ungültiger Parameter-Wert: {raw!r}")

    def _compute_decay_step(
        self,
        *,
        formula: dict[str, Any],
        primary_in: State,
        out_unit: str,
        dur_s: float,
        dur_sigma_s: float | None,
    ) -> GaussianValue:
        if primary_in.value_num is None:
            raise ValueError("decay_step erfordert numerischen Input-Zustand")
        init_g = GaussianValue(
            primary_in.value_num,
            primary_in.value_sigma or 0.0,
            source=primary_in.source,
            unit=primary_in.value_unit,
        )
        if out_unit and primary_in.value_unit and are_units_compatible(primary_in.value_unit, out_unit):
            init_g = convert_gaussian_value(init_g, out_unit)
        eq_g = self._parse_gaussian_param(
            formula.get("equilibrium"),
            default_mean=0.0,
            default_sigma=0.0,
            default_source="derived",
            default_unit=init_g.unit or out_unit,
        )
        if formula.get("tau_seconds") is not None:
            tau_g = self._parse_gaussian_param(
                formula["tau_seconds"], default_source="ai_prior"
            )
        elif formula.get("half_life_seconds") is not None:
            hl_g = self._parse_gaussian_param(
                formula["half_life_seconds"], default_source="ai_prior"
            )
            ln2 = math.log(2.0)
            tau_g = GaussianValue(hl_g.mean / ln2, hl_g.sigma / ln2, source=hl_g.source)
        else:
            raise ValueError("decay_step erfordert formula['tau_seconds'] oder formula['half_life_seconds']")

        dt_s = float(formula["dt_seconds"]) if formula.get("dt_seconds") is not None else float(dur_s)
        dt_sig = (
            float(formula["dt_sigma_seconds"])
            if formula.get("dt_sigma_seconds") is not None
            else float(dur_sigma_s or 0.0)
        )
        return propagate_exponential_decay(init_g, eq_g, dt_s, tau_g, dt_sigma_seconds=dt_sig)

    def _compute_cumulative_balance(
        self,
        *,
        formula: dict[str, Any],
        inputs: Sequence[State],
        primary_in: State,
        out_unit: str,
        ref_time_ms: int,
    ) -> tuple[GaussianValue, dict[str, Any]]:
        window_seconds = formula.get("window_seconds")
        cutoff_ms = (
            ref_time_ms - int(float(window_seconds) * 1000)
            if window_seconds is not None
            else None
        )

        flows: list[tuple[GaussianValue, float]] = []
        used_state_ids: set[str] = set()
        inflow_total = 0.0
        outflow_total = 0.0
        target_u = str(formula.get("output_unit", out_unit)) or None

        for sign, key in ((1.0, "inflows"), (-1.0, "outflows")):
            for spec in formula.get(key, []):
                if not isinstance(spec, dict) or not spec.get("property_name"):
                    continue
                prop = str(spec["property_name"])
                ent_id = spec.get("entity_id")
                req_ctx = spec.get("match_context")
                weight = float(spec.get("weight", 1.0))

                matching: dict[str, State] = {}
                for st in inputs:
                    if (
                        st.property_name == prop
                        and st.value_num is not None
                        and (ent_id is None or st.entity_id == ent_id)
                        and context_matches(st.context, req_ctx)
                        and (cutoff_ms is None or (st.time_ms is not None and cutoff_ms <= st.time_ms <= ref_time_ms))
                    ):
                        matching[st.id] = st
                if cutoff_ms is not None or not matching:
                    for st in self.repo.list_states(
                        entity_id=ent_id or primary_in.entity_id,
                        property_name=prop,
                        modality="observed",
                        context_filter=req_ctx,
                    ):
                        if st.value_num is None:
                            continue
                        if cutoff_ms is not None and (
                            st.time_ms is None or not (cutoff_ms <= st.time_ms <= ref_time_ms)
                        ):
                            continue
                        matching[st.id] = st

                ordered = sorted(
                    matching.values(),
                    key=lambda s: (s.time_ms if s.time_ms is not None else s.created_at_ms, s.id),
                )
                for st in ordered:
                    assert st.value_num is not None
                    used_state_ids.add(st.id)
                    gv = GaussianValue(
                        st.value_num,
                        st.value_sigma or 0.0,
                        source=st.source,
                        unit=st.value_unit,
                    )
                    spec_u = spec.get("unit") or target_u
                    if spec_u and gv.unit:
                        gv = convert_gaussian_value(gv, str(spec_u))
                    flows.append((gv, sign * weight))
                    if sign > 0:
                        inflow_total += gv.mean * weight
                    else:
                        outflow_total += gv.mean * weight

        include_initial = bool(formula.get("include_initial", True))
        initial_g: GaussianValue | None = None
        if include_initial and primary_in.value_num is not None and primary_in.id not in used_state_ids:
            initial_g = GaussianValue(
                primary_in.value_num,
                primary_in.value_sigma or 0.0,
                source=primary_in.source,
                unit=primary_in.value_unit,
            )

        min_clamp = float(formula["min_clamp"]) if formula.get("min_clamp") is not None else None
        max_clamp = float(formula["max_clamp"]) if formula.get("max_clamp") is not None else None
        balance = propagate_cumulative_balance(
            flows,
            initial=initial_g,
            min_clamp=min_clamp,
            max_clamp=max_clamp,
            target_unit=target_u,
        )
        return balance, {
            "inflow_total": inflow_total,
            "outflow_total": outflow_total,
            "flow_count": len(flows),
        }

    def _emit_total_and_split_outputs(
        self,
        *,
        template: Transformation,
        planned_tx: Transformation,
        formula: dict[str, Any],
        primary_in: State,
        out_entity_id: str,
        out_prop: str,
        out_unit: str,
        total: GaussianValue,
        total_source: Any,
        out_time_ms: int,
        out_time_sigma: float | None,
        split_parts: int | None,
        split_entities: Sequence[str] | None,
        branch_key: str,
        extra_meta: dict[str, Any] | None = None,
    ) -> list[str]:
        created_ids: list[str] = []
        base_meta: dict[str, Any] = {"planned_tx_id": planned_tx.id, "role": "total"}
        if extra_meta:
            base_meta.update(extra_meta)
        total_state = self.repo.create_state(
            StateCreate(
                entity_id=out_entity_id,
                modality="predicted",
                source=total_source,
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
                meta=base_meta,
            )
        )
        created_ids.append(total_state.id)
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
                part_meta: dict[str, Any] = {"planned_tx_id": planned_tx.id, "part_index": idx, "parts": k}
                if extra_meta:
                    part_meta.update(extra_meta)
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
                        meta=part_meta,
                    )
                )
                created_ids.append(part_state.id)
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
        return created_ids

    # ------------------------------------------------------------------
    # Observation ingestion, Stage-1 triggers & ai_prior calibration
    # ------------------------------------------------------------------

    def record_observation(self, data: StateCreate) -> MutationFeedback:
        """Persist a real observation, check triggers, calibrate priors, cascade chains, and re-evaluate goals."""
        if data.modality != "observed":
            raise ValueError("record_observation erfordert modality='observed'")

        obs = self.repo.create_state(data)
        triggered_ids = self._matching_trigger_templates(obs)
        calibrated_ids: list[str] = []
        updated_state_ids: list[str] = []

        # Calibrate any planned transformation output matching (entity_id, property_name)
        planned_txs = self.repo.list_transformations(modality="planned")
        composite_ids = {t.parent_id for t in planned_txs if t.parent_id is not None}
        rolled_parents: set[str] = set()

        for tx in planned_txs:
            if tx.template_id is None or tx.id in composite_ids:
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
            causal_ids = {e.state_id for e in in_edges if e.role in ("consumes", "uses")}
            primary_in = next(
                (s for s in in_states if s.id in causal_ids and s.value_num is not None and s.value_num > 0),
                next((s for s in in_states if s.value_num is not None and s.value_num > 0), None),
            )

            if tx.formula.get("kind") == "product_and_split" and obs.value_num is not None and primary_in is not None:
                template = self.repo.get_transformation(tx.template_id)
                factor_cfg = (template.formula if template else tx.formula).get("factor")
                if factor_cfg is not None:
                    obs_for_cal = GaussianValue(
                        obs.value_num,
                        obs.value_sigma if obs.value_sigma is not None else max(1.0, obs.value_num * 0.02),
                        source="measured",
                        measurement_source=obs.measurement_source,
                        unit=obs.value_unit,
                    )
                    if (
                        primary_in.value_unit
                        and obs.value_unit
                        and are_units_compatible(obs.value_unit, primary_in.value_unit)
                    ):
                        obs_for_cal = convert_gaussian_value(obs_for_cal, primary_in.value_unit)
                    obs_factor = obs_for_cal.mean / primary_in.value_num
                    obs_factor_sigma = max(1e-4, obs_for_cal.sigma / primary_in.value_num)
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

                    split_edges = [e for e in out_edges if e.ratio_mean is not None]
                    obs_total_val = GaussianValue(
                        obs.value_num,
                        obs.value_sigma if obs.value_sigma is not None else 0.0,
                        source="measured",
                        unit=obs.value_unit,
                    )
                    parts_count = len(split_edges)
                    if parts_count > 0:
                        split_sigma = float(tx.formula.get("split_sigma", 0.0))
                        per_part = propagate_split(obs_total_val, parts_count, split_sigma=split_sigma)
                        for sp_edge in split_edges:
                            part_st = self.repo.get_state(sp_edge.state_id)
                            if part_st is not None:
                                adj_part = per_part
                                if (
                                    part_st.value_unit
                                    and per_part.unit
                                    and are_units_compatible(per_part.unit, part_st.value_unit)
                                ):
                                    adj_part = convert_gaussian_value(per_part, part_st.value_unit)
                                upd_part_fields: dict[str, Any] = {
                                    "value_num": adj_part.mean,
                                    "value_sigma": adj_part.sigma,
                                    "source": "derived",
                                }
                                if obs.time_ms is not None:
                                    upd_part_fields["time_ms"] = obs.time_ms
                                if obs.sigma_seconds is not None:
                                    upd_part_fields["sigma_seconds"] = obs.sigma_seconds
                                upd = self.repo.update_state(part_st.id, **upd_part_fields)
                                if upd.id not in updated_state_ids:
                                    updated_state_ids.append(upd.id)

            elif tx.formula.get("kind") == "decay_step" and obs.value_num is not None:
                decay_in = primary_in or next((s for s in in_states if s.value_num is not None), None)
                start_ms_ref = tx.meta.get("scheduled_start_ms")
                if start_ms_ref is None and decay_in is not None and decay_in.time_ms is not None:
                    start_ms_ref = decay_in.time_ms
                dt_s = (
                    (obs.time_ms - int(start_ms_ref)) / 1000.0
                    if obs.time_ms is not None and start_ms_ref is not None
                    else float(tx.formula.get("dt_seconds") or tx.duration_seconds or 0.0)
                )
                if decay_in is not None and decay_in.value_num is not None and dt_s > 0.0:
                    template = self.repo.get_transformation(tx.template_id)
                    src_formula = template.formula if template else tx.formula
                    in_u = decay_in.value_unit
                    eq_g = self._parse_gaussian_param(
                        src_formula.get("equilibrium"),
                        default_mean=0.0,
                        default_sigma=0.0,
                        default_unit=in_u,
                    )
                    if in_u and eq_g.unit and are_units_compatible(eq_g.unit, in_u):
                        eq_g = convert_gaussian_value(eq_g, in_u)
                    obs_g = GaussianValue(
                        obs.value_num,
                        obs.value_sigma
                        if obs.value_sigma is not None
                        else max(1e-3, abs(obs.value_num - eq_g.mean) * 0.02),
                        source="measured",
                        measurement_source=obs.measurement_source,
                        unit=obs.value_unit,
                    )
                    if in_u and obs_g.unit and are_units_compatible(obs_g.unit, in_u):
                        obs_g = convert_gaussian_value(obs_g, in_u)
                    denom = decay_in.value_num - eq_g.mean
                    num = obs_g.mean - eq_g.mean
                    if abs(denom) > 1e-12:
                        ratio = num / denom
                        if 1e-4 < ratio < 1.0 - 1e-4:
                            ln_r = math.log(ratio)
                            tau_obs = -dt_s / ln_r
                            d_tau_d_obs = dt_s / (max(1e-12, abs(num)) * (ln_r**2))
                            tau_obs_sigma = max(1.0, obs_g.sigma * d_tau_d_obs)
                            use_half_life = (
                                src_formula.get("tau_seconds") is None
                                and src_formula.get("half_life_seconds") is not None
                            )
                            if use_half_life:
                                hl_g = self._parse_gaussian_param(
                                    src_formula["half_life_seconds"], default_source="ai_prior"
                                )
                                ln2 = math.log(2.0)
                                prior_tau = GaussianValue(
                                    hl_g.mean / ln2, hl_g.sigma / ln2, source=hl_g.source
                                )
                            else:
                                prior_tau = self._parse_gaussian_param(
                                    src_formula.get("tau_seconds"),
                                    default_mean=tau_obs,
                                    default_sigma=max(60.0, tau_obs * 0.25),
                                    default_source="ai_prior",
                                )
                            cal_tau = calibrate_parameter(
                                prior_tau,
                                [
                                    GaussianValue(
                                        tau_obs,
                                        tau_obs_sigma,
                                        source="measured",
                                        measurement_source=obs.measurement_source,
                                    )
                                ],
                            )
                            new_param = {
                                "mean": cal_tau.mean * math.log(2.0) if use_half_life else cal_tau.mean,
                                "sigma": cal_tau.sigma * math.log(2.0) if use_half_life else cal_tau.sigma,
                                "source": cal_tau.source,
                            }
                            param_key = "half_life_seconds" if use_half_life else "tau_seconds"
                            if template is not None:
                                tpl_formula = copy.deepcopy(template.formula)
                                tpl_formula[param_key] = new_param
                                self.repo.update_transformation(template.id, formula=tpl_formula)
                                if template.id not in calibrated_ids:
                                    calibrated_ids.append(template.id)
                            tx_formula = copy.deepcopy(tx.formula)
                            tx_formula[param_key] = new_param
                            self.repo.update_transformation(tx.id, formula=tx_formula)

            # Update predicted output state across all formula kinds (value + time)
            for _, total_st in matching_total:
                upd_total_fields: dict[str, Any] = {
                    "source": "measured",
                    "measurement_source": obs.measurement_source,
                }
                if obs.value_num is not None:
                    obs_mean, obs_sig = obs.value_num, (
                        obs.value_sigma if obs.value_sigma is not None else 0.0
                    )
                    if (
                        total_st.value_unit
                        and obs.value_unit
                        and are_units_compatible(obs.value_unit, total_st.value_unit)
                    ):
                        obs_mean, conv_sig = convert_gaussian(
                            obs_mean, obs_sig, obs.value_unit, total_st.value_unit
                        )
                        obs_sig = conv_sig if conv_sig is not None else 0.0
                    upd_total_fields["value_num"] = obs_mean
                    upd_total_fields["value_sigma"] = obs_sig
                if obs.time_ms is not None:
                    upd_total_fields["time_ms"] = obs.time_ms
                if obs.sigma_seconds is not None:
                    upd_total_fields["sigma_seconds"] = obs.sigma_seconds
                updated_total = self.repo.update_state(total_st.id, **upd_total_fields)
                if updated_total.id not in updated_state_ids:
                    updated_state_ids.append(updated_total.id)

            if tx.parent_id is not None:
                rolled_parents.add(tx.parent_id)

            # Duration calibration for timed steps
            start_ref_ms = tx.meta.get("scheduled_start_ms")
            if start_ref_ms is None and in_states and in_states[0].time_ms is not None:
                start_ref_ms = in_states[0].time_ms
            if obs.time_ms is not None and tx.duration_seconds is not None and start_ref_ms is not None:
                observed_dur_s = (obs.time_ms - int(start_ref_ms)) / 1000.0
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

        for parent_id in sorted(rolled_parents):
            self._rollup_composite_tx(parent_id)

        for sid in self._reapply_modifiers_for_observation(obs):
            if sid not in updated_state_ids:
                updated_state_ids.append(sid)

        for sid in self.propagate_downstream(list(updated_state_ids)):
            if sid not in updated_state_ids:
                updated_state_ids.append(sid)

        return self._build_feedback(
            created_state_ids=[obs.id],
            updated_state_ids=updated_state_ids,
            triggered_template_ids=triggered_ids,
            calibrated_template_ids=calibrated_ids,
            summary=f"Beobachtung '{obs.label}' gespeichert.",
        )

    def propagate_downstream(self, start_state_ids: Sequence[str]) -> list[str]:
        """Propagate time and quantity updates topologically across chained planned transformations."""
        if not start_state_ids:
            return []
        planned_txs = self.repo.list_transformations(modality="planned")
        composite_ids = {t.parent_id for t in planned_txs if t.parent_id is not None}

        queue: deque[str] = deque(start_state_ids)
        visited_txs: set[str] = set()
        updated_ids: list[str] = []
        rolled_parents: set[str] = set()

        while queue:
            st_id = queue.popleft()
            for in_edge in self.repo.list_edges(state_id=st_id, direction="in"):
                tx = self.repo.get_transformation(in_edge.transformation_id)
                if (
                    tx is None
                    or tx.modality != "planned"
                    or tx.id in visited_txs
                    or tx.id in composite_ids
                ):
                    continue
                visited_txs.add(tx.id)

                tx_in_edges = self.repo.list_edges(transformation_id=tx.id, direction="in")
                in_states: list[State] = [
                    st
                    for e in tx_in_edges
                    if (st := self.repo.get_state(e.state_id)) is not None
                ]
                if not in_states:
                    continue

                causal_ids = {e.state_id for e in tx_in_edges if e.role in ("consumes", "uses")}
                causal_timed = [s for s in in_states if s.id in causal_ids and s.time_ms is not None]
                all_timed = [s for s in in_states if s.time_ms is not None]
                time_pool = causal_timed or all_timed
                if time_pool:
                    new_start_ms = max(s.time_ms for s in time_pool if s.time_ms is not None)
                    sigmas = [s.sigma_seconds for s in time_pool if s.sigma_seconds is not None]
                    base_time_sigma = max(sigmas) if sigmas else 0.0
                else:
                    new_start_ms = int(tx.meta.get("scheduled_start_ms", now_ms()))
                    base_time_sigma = 0.0

                tpl = self.repo.get_transformation(tx.template_id) if tx.template_id else None
                modifiers = list(tx.formula.get("modifiers", []))
                if tpl is not None and tpl.duration_source != "measured":
                    _, mod_dur_s, mod_dur_sigma_s = self._apply_modifiers(
                        output_value=None,
                        duration_seconds=tpl.duration_seconds,
                        duration_sigma_seconds=tpl.duration_sigma_seconds,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                else:
                    mod_dur_s, mod_dur_sigma_s = tx.duration_seconds, tx.duration_sigma_seconds

                tx_meta = dict(tx.meta)
                tx_meta["scheduled_start_ms"] = new_start_ms
                self.repo.update_transformation(
                    tx.id,
                    duration_seconds=mod_dur_s,
                    duration_sigma_seconds=mod_dur_sigma_s,
                    meta=tx_meta,
                )

                dur_s = mod_dur_s or 0.0
                out_time_ms = new_start_ms + int(dur_s * 1000)
                out_time_sigma = math.hypot(base_time_sigma, mod_dur_sigma_s or 0.0) or None

                primary_in = self._pick_primary_input(in_states, tx_in_edges)
                kind = tx.formula.get("kind", "duration_step")
                mod_total: GaussianValue | None = None
                if kind == "product_and_split" and primary_in.value_num is not None:
                    factor_cfg = tx.formula.get("factor", {"mean": 1.0, "sigma": 0.0, "source": "ai_prior"})
                    out_unit_tx = str(tx.formula.get("output_unit", primary_in.value_unit))
                    in_g = GaussianValue(
                        primary_in.value_num,
                        primary_in.value_sigma or 0.0,
                        source=primary_in.source,
                        unit=primary_in.value_unit,
                    )
                    if (
                        out_unit_tx
                        and primary_in.value_unit
                        and are_units_compatible(primary_in.value_unit, out_unit_tx)
                    ):
                        in_g = convert_gaussian_value(in_g, out_unit_tx)
                    total = propagate_product(
                        in_g,
                        GaussianValue(
                            float(factor_cfg["mean"]),
                            float(factor_cfg["sigma"]),
                            source=factor_cfg.get("source", "ai_prior"),
                        ),
                    )
                    mod_total, _, _ = self._apply_modifiers(
                        output_value=total,
                        duration_seconds=None,
                        duration_sigma_seconds=None,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                elif kind == "recipe_mix":
                    raw_total, _ = self._compute_recipe_mix(tx.formula, in_states)
                    mod_total, _, _ = self._apply_modifiers(
                        output_value=raw_total,
                        duration_seconds=None,
                        duration_sigma_seconds=None,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                elif kind == "decay_step" and primary_in.value_num is not None:
                    out_unit_tx = str(tx.formula.get("output_unit", primary_in.value_unit))
                    raw_total = self._compute_decay_step(
                        formula=tx.formula,
                        primary_in=primary_in,
                        out_unit=out_unit_tx,
                        dur_s=dur_s,
                        dur_sigma_s=mod_dur_sigma_s,
                    )
                    mod_total, _, _ = self._apply_modifiers(
                        output_value=raw_total,
                        duration_seconds=None,
                        duration_sigma_seconds=None,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                elif kind == "cumulative_balance":
                    out_unit_tx = str(tx.formula.get("output_unit", primary_in.value_unit))
                    raw_total, _ = self._compute_cumulative_balance(
                        formula=tx.formula,
                        inputs=in_states,
                        primary_in=primary_in,
                        out_unit=out_unit_tx,
                        ref_time_ms=out_time_ms,
                    )
                    mod_total, _, _ = self._apply_modifiers(
                        output_value=raw_total,
                        duration_seconds=None,
                        duration_sigma_seconds=None,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                else:
                    d_val, d_sig = self._compute_duration_step_output(
                        template=tpl,
                        formula=tx.formula,
                        primary_in=primary_in,
                        modifiers=modifiers,
                        inputs=in_states,
                    )
                    if d_val is not None:
                        mod_total = GaussianValue(d_val, float(d_sig or 0.0), source="derived")

                out_edges = self.repo.list_edges(transformation_id=tx.id, direction="out")
                split_edges = [e for e in out_edges if e.ratio_mean is not None]
                total_edges = [e for e in out_edges if e.ratio_mean is None]

                for te in total_edges:
                    out_st = self.repo.get_state(te.state_id)
                    if out_st is None or out_st.modality != "predicted" or out_st.source == "measured":
                        continue
                    upd: dict[str, Any] = {"time_ms": out_time_ms, "sigma_seconds": out_time_sigma}
                    if mod_total is not None:
                        upd["value_num"] = mod_total.mean
                        upd["value_sigma"] = mod_total.sigma
                    self.repo.update_state(out_st.id, **upd)
                    if out_st.id not in updated_ids:
                        updated_ids.append(out_st.id)
                    queue.append(out_st.id)

                if split_edges and mod_total is not None:
                    split_sigma = float(tx.formula.get("split_sigma", 0.0))
                    per_part = propagate_split(mod_total, len(split_edges), split_sigma=split_sigma)
                    for se in split_edges:
                        part_st = self.repo.get_state(se.state_id)
                        if part_st is None or part_st.modality != "predicted" or part_st.source == "measured":
                            continue
                        self.repo.update_state(
                            part_st.id,
                            value_num=per_part.mean,
                            value_sigma=per_part.sigma,
                            time_ms=out_time_ms,
                            sigma_seconds=out_time_sigma,
                        )
                        if part_st.id not in updated_ids:
                            updated_ids.append(part_st.id)
                        queue.append(part_st.id)

                if tx.parent_id is not None:
                    rolled_parents.add(tx.parent_id)

        for parent_id in sorted(rolled_parents):
            self._rollup_composite_tx(parent_id)

        return updated_ids

    def _reapply_modifiers_for_observation(self, obs: State) -> list[str]:
        """Re-evaluate duration modifiers on active planned transformations when a relevant observation arrives."""
        if obs.value_num is None:
            return []
        updated_ids: list[str] = []
        rolled_parents: set[str] = set()
        for tx in self.repo.list_transformations(modality="planned"):
            if tx.template_id is None or tx.duration_source == "measured":
                continue
            modifiers = list(tx.formula.get("modifiers", []))
            if not modifiers:
                continue
            relevant = any(
                str(mod.get("applies_to", "output")) == "duration"
                and mod.get("property_name") == obs.property_name
                and (mod.get("entity_id") is None or mod.get("entity_id") == obs.entity_id)
                for mod in modifiers
            )
            if not relevant:
                continue
            template = self.repo.get_transformation(tx.template_id)
            if template is None or template.duration_source == "measured":
                continue
            in_edges = self.repo.list_edges(transformation_id=tx.id, direction="in")
            in_states = [
                st
                for e in in_edges
                if (st := self.repo.get_state(e.state_id)) is not None
            ]
            _, mod_dur_s, mod_dur_sigma_s = self._apply_modifiers(
                output_value=None,
                duration_seconds=template.duration_seconds,
                duration_sigma_seconds=template.duration_sigma_seconds,
                modifiers=modifiers,
                inputs=in_states,
            )
            self.repo.update_transformation(
                tx.id,
                duration_seconds=mod_dur_s,
                duration_sigma_seconds=mod_dur_sigma_s,
            )
            scheduled_start_ms = tx.meta.get("scheduled_start_ms")
            if scheduled_start_ms is not None and mod_dur_s is not None:
                causal_ids = {e.state_id for e in in_edges if e.role in ("consumes", "uses")}
                causal_sigmas = [
                    s.sigma_seconds
                    for s in in_states
                    if s.id in causal_ids and s.sigma_seconds is not None
                ]
                all_sigmas = [s.sigma_seconds for s in in_states if s.sigma_seconds is not None]
                sigmas_pool = causal_sigmas or all_sigmas
                base_time_sigma = max(sigmas_pool) if sigmas_pool else 0.0
                out_time_ms = int(scheduled_start_ms) + int(mod_dur_s * 1000)
                out_time_sigma = math.hypot(base_time_sigma, mod_dur_sigma_s or 0.0) or None
                for out_edge in self.repo.list_edges(transformation_id=tx.id, direction="out"):
                    out_st = self.repo.get_state(out_edge.state_id)
                    if out_st is not None and out_st.modality == "predicted":
                        self.repo.update_state(
                            out_st.id,
                            time_ms=out_time_ms,
                            sigma_seconds=out_time_sigma,
                        )
                        if out_st.id not in updated_ids:
                            updated_ids.append(out_st.id)
            if tx.parent_id is not None:
                rolled_parents.add(tx.parent_id)
        for parent_id in sorted(rolled_parents):
            self._rollup_composite_tx(parent_id)
        return updated_ids

    def _matching_trigger_templates(self, obs: State) -> list[str]:
        matched: list[str] = []
        obs_entity = self.repo.get_entity(obs.entity_id)
        ref_ms = obs.time_ms if obs.time_ms is not None else now_ms()

        for tpl in self.repo.list_transformations(modality="template"):
            raw_triggers = tpl.formula.get("triggers")
            legacy_trigger = tpl.formula.get("trigger")

            if isinstance(raw_triggers, dict):
                logic = str(raw_triggers.get("logic", "AND")).upper()
                conditions = list(raw_triggers.get("conditions", []))
                default_cat = raw_triggers.get("category")
                group_min_prob = float(raw_triggers.get("min_prob", 0.5 if logic == "OR" else 0.0))
            elif isinstance(raw_triggers, list):
                logic = "AND"
                conditions = list(raw_triggers)
                default_cat = None
                group_min_prob = 0.0
            elif isinstance(legacy_trigger, dict):
                logic = "AND"
                conditions = [legacy_trigger]
                default_cat = legacy_trigger.get("category")
                group_min_prob = float(legacy_trigger.get("min_prob", 0.5))
            else:
                continue

            if not conditions:
                continue

            relevant = False
            for cond in conditions:
                props = {
                    cond.get("property_name"),
                    cond.get("numerator_property"),
                    cond.get("denominator_property"),
                }
                if obs.property_name not in props:
                    continue
                if not context_matches(obs.context, cond.get("match_context")):
                    continue
                cat = cond.get("category", default_cat)
                if cat is None or (obs_entity is not None and obs_entity.category == cat):
                    relevant = True
                    break
            if not relevant:
                continue

            probs = [
                self._evaluate_condition(
                    cond,
                    in_states=(),
                    ref_time_ms=ref_ms,
                    default_entity_id=obs.entity_id,
                    default_category=default_cat,
                )
                for cond in conditions
            ]

            if logic == "OR":
                complement = 1.0
                for p in probs:
                    complement *= 1.0 - p
                group_prob = 1.0 - complement
                if group_prob >= group_min_prob:
                    matched.append(tpl.id)
            else:
                all_cond_ok = all(
                    prob >= float(cond.get("min_prob", 0.5))
                    for cond, prob in zip(conditions, probs, strict=True)
                )
                combined = 1.0
                for p in probs:
                    combined *= p
                if all_cond_ok and combined >= group_min_prob:
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

            all_planned = self.repo.list_transformations(modality="planned")
            active_goals = [
                (g.entity_id, g.property_name)
                for g in self.repo.list_states(modality="desired")
                if g.id != goal_id
            ]
            changed = True
            while changed:
                changed = False
                # Propagate downward to nested child steps
                for ptx in all_planned:
                    if ptx.parent_id in cancelled_tx_ids and ptx.id not in cancelled_tx_ids:
                        cancelled_tx_ids.add(ptx.id)
                        changed = True
                # Propagate upward to upstream planned producers that have no remaining active consumers
                for tx_id in list(cancelled_tx_ids):
                    for in_edge in self.repo.list_edges(transformation_id=tx_id, direction="in"):
                        in_st = self.repo.get_state(in_edge.state_id)
                        if in_st is None or in_st.modality != "predicted":
                            continue
                        still_consumed = any(
                            e.transformation_id not in cancelled_tx_ids
                            and (ct := self.repo.get_transformation(e.transformation_id)) is not None
                            and ct.modality == "planned"
                            for e in self.repo.list_edges(state_id=in_st.id, direction="in")
                        )
                        still_desired = (in_st.entity_id, in_st.property_name) in active_goals
                        if not still_consumed and not still_desired:
                            for oe in self.repo.list_edges(state_id=in_st.id, direction="out"):
                                up_tx = self.repo.get_transformation(oe.transformation_id)
                                if (
                                    up_tx is not None
                                    and up_tx.modality == "planned"
                                    and up_tx.id not in cancelled_tx_ids
                                ):
                                    cancelled_tx_ids.add(up_tx.id)
                                    changed = True

            for tx_id in cancelled_tx_ids:
                self.repo.update_transformation(tx_id, modality="cancelled")
                for out_edge in self.repo.list_edges(transformation_id=tx_id, direction="out"):
                    st = self.repo.get_state(out_edge.state_id)
                    if st is not None and st.modality == "predicted":
                        self.repo.update_state(st.id, modality="cancelled")
                        if st.id not in updated_ids:
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

    def fuse_property_states(
        self,
        entity_id: str,
        property_name: str,
        *,
        context_filter: dict[str, str] | None = None,
        target_unit: str | None = None,
        modality: StateModality = "observed",
        require_same_context: bool | None = None,
    ) -> GaussianValue:
        """Fuse numeric states of a property with strict unit normalization and context isolation."""
        states = [
            st
            for st in self.repo.list_states(
                entity_id=entity_id,
                property_name=property_name,
                modality=modality,
                context_filter=context_filter,
            )
            if st.value_num is not None
        ]
        if not states:
            raise ValueError(
                f"Keine numerischen '{modality}'-Zustände für ({entity_id}, {property_name}) gefunden."
            )
        vals = [
            GaussianValue(
                mean=st.value_num,  # type: ignore[arg-type]
                sigma=st.value_sigma if st.value_sigma is not None else 0.0,
                source=st.source,
                measurement_source=st.measurement_source,
                unit=st.value_unit,
                context=make_context_key(st.context),
            )
            for st in states
        ]
        enforce_ctx = (context_filter is None) if require_same_context is None else require_same_context
        return fuse_1d(vals, target_unit=target_unit, require_same_context=enforce_ctx)

    # ------------------------------------------------------------------
    # High-frequency raw time-series ingestion & roll-up
    # ------------------------------------------------------------------

    def ingest_raw_samples(self, samples: Sequence[RawSampleCreate]) -> list[RawSample]:
        """Store high-frequency raw telemetry samples without polluting the causal graph."""
        return self.repo.insert_raw_samples(samples)

    def rollup_raw_samples(
        self,
        *,
        entity_id: str | None = None,
        property_name: str | None = None,
        window_kind: RollupWindowKind = "day",
        window_seconds: float = 3_600.0,
        tz_name: str = "Europe/Berlin",
        clock_id: ClockId = "solar_day",
        phase_bins: int | None = None,
        lat: float = 51.0,
        lon: float = 10.0,
        target_unit: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        only_unrolled: bool = True,
        context_filter: dict[str, str] | None = None,
    ) -> list[State]:
        """Compress buffered raw_samples into observed graph States (one per context & time bucket)."""
        samples = self.repo.list_raw_samples(
            entity_id=entity_id,
            property_name=property_name,
            start_ms=start_ms,
            end_ms=end_ms,
            only_unrolled=only_unrolled,
            context_filter=context_filter,
        )
        if not samples:
            return []

        buckets = compute_rollup_buckets(
            samples,
            window_kind=window_kind,
            window_seconds=window_seconds,
            tz_name=tz_name,
            clock_id=clock_id,
            phase_bins=phase_bins,
            lat=lat,
            lon=lon,
            target_unit=target_unit,
        )
        created_states: list[State] = []
        for bucket, state_payload in buckets:
            fb = self.record_observation(state_payload)
            state_id = fb.created_state_ids[0]
            self.repo.mark_raw_samples_rolled_up(bucket.sample_ids, state_id)
            created = self.repo.get_state(state_id)
            if created is not None:
                created_states.append(created)
        return created_states

    # ------------------------------------------------------------------
    # Multi-clock periodicity & interval analysis
    # ------------------------------------------------------------------

    def analyze_worldline_periods(
        self,
        *,
        entity_id: str | None = None,
        category: str | None = None,
        property_name: str | None = None,
        context_filter: dict[str, str] | None = None,
        tz_name: str = "Europe/Berlin",
        now_ms_override: int | None = None,
        tidal_offset_hours: float = 0.0,
    ) -> PeriodicityReport:
        """Analyze observed timestamps across all periodic clocks and endogenous intervals."""
        allowed_ent_ids: set[str] | None = None
        if category is not None:
            allowed_ent_ids = {
                e.id
                for e in self.repo.list_entities(include_archived=False)
                if e.category == category
            }

        states = self.repo.list_states(
            entity_id=entity_id,
            property_name=property_name,
            modality="observed",
            context_filter=context_filter,
        )
        samples: list[TimeSample] = []
        for st in states:
            if st.time_ms is None:
                continue
            if allowed_ent_ids is not None and st.entity_id not in allowed_ent_ids:
                continue
            samples.append(
                TimeSample(
                    time_ms=st.time_ms,
                    sigma_seconds=st.sigma_seconds,
                    lat=st.lat,
                    lon=st.lon,
                )
            )

        ent_label = ""
        if entity_id:
            ent = self.repo.get_entity(entity_id)
            ent_label = ent.name if ent else entity_id
        elif category:
            ent_label = f"Kategorie: {category}"
        else:
            ent_label = "Archiv"

        return analyze_periodicity(
            samples,
            tz_name=tz_name,
            now_ms=now_ms_override if now_ms_override is not None else now_ms(),
            tidal_offset_hours=tidal_offset_hours,
            entity_id=entity_id,
            label=ent_label,
        )
