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
    SeriesAggregation,
    aggregate_series,
    calibrate_parameter,
    combine_goal_probability,
    compute_modifier_factor,
    probability_in_bounds,
    probability_within_radius,
    propagate_linear_combination,
    propagate_product,
    propagate_ratio,
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
        guard_logic = str(tx.formula.get("guard_logic", "AND"))
        p_pre *= self._evaluate_condition_group(
            guards,
            logic=guard_logic,
            in_states=in_states,
        )

        return max(0.0, min(1.0, p_pre))

    def _resolve_observed_candidates(
        self,
        *,
        entity_id: str | None,
        category: str | None,
        property_name: str | None,
        in_states: Sequence[State],
        default_entity_id: str | None,
    ) -> list[State]:
        candidates: list[State] = []
        if entity_id and property_name:
            candidates = self.repo.list_states(
                entity_id=entity_id, property_name=property_name, modality="observed"
            )
        elif category and property_name:
            matching_ent_ids = {
                ent.id
                for ent in self.repo.list_entities(include_archived=False)
                if ent.category == category
            }
            candidates = [
                st
                for st in self.repo.list_states(property_name=property_name, modality="observed")
                if st.entity_id in matching_ent_ids
            ]

        if not candidates:
            for st in in_states:
                if (entity_id is None or st.entity_id == entity_id) and (
                    property_name is None or st.property_name == property_name
                ):
                    obs_for_ent = self.repo.list_states(
                        entity_id=st.entity_id, property_name=st.property_name, modality="observed"
                    )
                    if obs_for_ent:
                        candidates.extend(obs_for_ent)
                    else:
                        candidates.append(st)

        if not candidates and property_name:
            target_ent = entity_id or default_entity_id
            if target_ent:
                candidates = self.repo.list_states(
                    entity_id=target_ent, property_name=property_name, modality="observed"
                )
            if not candidates and entity_id is None:
                candidates = self.repo.list_states(property_name=property_name, modality="observed")

        deduped: dict[str, State] = {st.id: st for st in candidates}
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
    ) -> State | None:
        candidates = [
            st
            for st in self._resolve_observed_candidates(
                entity_id=entity_id,
                category=None,
                property_name=property_name,
                in_states=in_states,
                default_entity_id=default_entity_id,
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
        num_state = self._resolve_latest_numeric_state(
            entity_id=cond.get("numerator_entity_id"),
            property_name=cond.get("numerator_property"),
            in_states=in_states,
            default_entity_id=default_entity_id,
        )
        den_state = self._resolve_latest_numeric_state(
            entity_id=cond.get("denominator_entity_id"),
            property_name=cond.get("denominator_property"),
            in_states=in_states,
            default_entity_id=default_entity_id,
        )
        if num_state is None or den_state is None or num_state.value_num is None or den_state.value_num is None:
            return 0.0
        if abs(den_state.value_num) < 1e-12:
            return 0.0

        ratio = propagate_ratio(
            GaussianValue(num_state.value_num, num_state.value_sigma or 0.0, source=num_state.source),
            GaussianValue(den_state.value_num, den_state.value_sigma or 0.0, source=den_state.source),
        )
        min_ratio = cond.get("min_ratio", cond.get("min_value"))
        max_ratio = cond.get("max_ratio", cond.get("max_value"))
        return probability_in_bounds(
            ratio,
            target_min=float(min_ratio) if min_ratio is not None else None,
            target_max=float(max_ratio) if max_ratio is not None else None,
        )

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

        candidates = self._resolve_observed_candidates(
            entity_id=cond.get("entity_id"),
            category=cond.get("category", default_category),
            property_name=cond.get("property_name"),
            in_states=in_states,
            default_entity_id=default_entity_id,
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
            numeric_samples = [
                GaussianValue(st.value_num, st.value_sigma or 0.0, source=st.source)
                for st in candidates
                if st.value_num is not None
            ]
            if not numeric_samples:
                return 0.0
            agg_mode: SeriesAggregation = str(cond.get("aggregation", "latest"))  # type: ignore[assignment]
            aggregated = aggregate_series(numeric_samples, agg_mode)
            return probability_in_bounds(
                aggregated,
                target_min=float(min_val) if min_val is not None else None,
                target_max=float(max_val) if max_val is not None else None,
            )

        return 1.0

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
                duration_seconds=mod_dur_s,
                duration_sigma_seconds=mod_dur_sigma_s,
                duration_source=template.duration_source,
                cost=template.cost,
                success_prob=template.success_prob,
                formula=formula,
                meta={"scheduled_start_ms": base_start},
            )
        )

        input_roles: dict[str, EdgeRole] = dict(formula.get("input_roles", {}))
        for ing in formula.get("ingredients", []):
            if isinstance(ing, dict) and ing.get("property_name") and ing.get("role"):
                input_roles.setdefault(str(ing["property_name"]), ing["role"])
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
        else:
            out_val = formula.get("output_value", primary_in.value_num)
            out_sig = formula.get("output_sigma", primary_in.value_sigma)
            if out_val is not None and modifiers:
                mod_g, _, _ = self._apply_modifiers(
                    output_value=GaussianValue(
                        float(out_val),
                        float(out_sig or 0.0),
                        source="ai_prior" if template.duration_source == "ai_prior" else "derived",
                    ),
                    duration_seconds=None,
                    duration_sigma_seconds=None,
                    modifiers=modifiers,
                    inputs=inputs,
                )
                assert mod_g is not None
                out_val = mod_g.mean
                out_sig = mod_g.sigma
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
            )
            if st is None or st.value_num is None:
                continue
            factor = compute_modifier_factor(
                GaussianValue(st.value_num, st.value_sigma or 0.0, source=st.source),
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
            st = next(
                (
                    item
                    for item in inputs
                    if item.property_name == prop and (ent_id is None or item.entity_id == ent_id)
                ),
                None,
            )
            if st is None or st.value_num is None:
                raise ValueError(f"Fehlender numerischer Zutat-Zustand für '{prop}'")
            return st

        mode = str(formula.get("mode", "weighted_sum"))
        if mode == "weighted_sum":
            terms = [
                (
                    GaussianValue(
                        st.value_num, st.value_sigma or 0.0, source=st.source  # type: ignore[arg-type]
                    ),
                    GaussianValue(
                        float(spec.get("weight", 1.0)),
                        float(spec.get("weight_sigma", 0.0)),
                        source=spec.get("source", "derived"),
                    ),
                )
                for spec in ingredients
                if (st := _match_input(spec)) is not None
            ]
            return propagate_linear_combination(terms), {}

        if mode == "limiting_factor":
            caps: list[tuple[str, GaussianValue]] = []
            for spec in ingredients:
                per_unit = spec.get("per_unit")
                if per_unit is None or float(per_unit) <= 0.0:
                    continue
                st = _match_input(spec)
                cap = propagate_ratio(
                    GaussianValue(
                        st.value_num, st.value_sigma or 0.0, source=st.source  # type: ignore[arg-type]
                    ),
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

        updated_state_ids.extend(self._reapply_modifiers_for_observation(obs))

        return self._build_feedback(
            created_state_ids=[obs.id],
            updated_state_ids=updated_state_ids,
            triggered_template_ids=triggered_ids,
            calibrated_template_ids=calibrated_ids,
            summary=f"Beobachtung '{obs.label}' gespeichert.",
        )

    def _reapply_modifiers_for_observation(self, obs: State) -> list[str]:
        """Re-evaluate duration modifiers on active planned transformations when a relevant observation arrives."""
        if obs.value_num is None:
            return []
        updated_ids: list[str] = []
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
                in_sigmas_s = [s.sigma_seconds for s in in_states if s.sigma_seconds is not None]
                base_time_sigma = max(in_sigmas_s) if in_sigmas_s else 0.0
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
