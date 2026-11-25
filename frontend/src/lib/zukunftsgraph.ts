import { DateTime } from "luxon";
import { DISPLAY_TIMEZONE } from "./time-filter";
import type {
  GoalEvaluation,
  GoalStatus,
  MetaCoreEdge,
  MetaCoreState,
  MetaCoreTransformation,
  PeriodStatus,
} from "./types";

export interface TransformationHierarchy {
  roots: MetaCoreTransformation[];
  childrenByParent: Record<string, MetaCoreTransformation[]>;
}

export function buildTransformationHierarchy(
  transformations: MetaCoreTransformation[]
): TransformationHierarchy {
  const byId = new Map(transformations.map((tx) => [tx.id, tx]));
  const childrenByParent: Record<string, MetaCoreTransformation[]> = {};
  const roots: MetaCoreTransformation[] = [];

  for (const tx of transformations) {
    if (tx.parent_id && byId.has(tx.parent_id)) {
      if (!childrenByParent[tx.parent_id]) childrenByParent[tx.parent_id] = [];
      childrenByParent[tx.parent_id].push(tx);
    } else {
      roots.push(tx);
    }
  }

  const orderValue = (tx: MetaCoreTransformation) => {
    const raw = tx.meta?.step_order;
    return typeof raw === "number" ? raw : Number.MAX_SAFE_INTEGER;
  };
  for (const key of Object.keys(childrenByParent)) {
    childrenByParent[key].sort((a, b) => orderValue(a) - orderValue(b) || a.created_at_ms - b.created_at_ms);
  }
  return { roots, childrenByParent };
}

export interface CausalNodeRef {
  kind: "state" | "tx";
  id: string;
}

export function computeCausalSteps(
  transformations: MetaCoreTransformation[],
  states: MetaCoreState[],
  edges: MetaCoreEdge[],
  includeCancelled = false
): {
  hierarchy: TransformationHierarchy;
  orderedRoots: MetaCoreTransformation[];
  inputsByTx: Record<string, Array<{ edge: MetaCoreEdge; state: MetaCoreState }>>;
  outputsByTx: Record<string, Array<{ edge: MetaCoreEdge; state: MetaCoreState }>>;
} {
  const visibleTxs = transformations.filter(
    (tx) => tx.modality !== "template" && (includeCancelled || tx.modality !== "cancelled")
  );
  const hierarchy = buildTransformationHierarchy(visibleTxs);
  const stateById = new Map(states.map((s) => [s.id, s]));

  const inputsByTx: Record<string, Array<{ edge: MetaCoreEdge; state: MetaCoreState }>> = {};
  const outputsByTx: Record<string, Array<{ edge: MetaCoreEdge; state: MetaCoreState }>> = {};
  for (const edge of edges) {
    const st = stateById.get(edge.state_id);
    if (!st) continue;
    const bucket = edge.direction === "in" ? inputsByTx : outputsByTx;
    if (!bucket[edge.transformation_id]) bucket[edge.transformation_id] = [];
    bucket[edge.transformation_id].push({ edge, state: st });
  }

  const producerByState = new Map<string, string>();
  for (const root of hierarchy.roots) {
    for (const out of outputsByTx[root.id] ?? []) {
      producerByState.set(out.state.id, root.id);
    }
  }

  const rootDepth = new Map<string, number>();
  const computeDepth = (txId: string, seen = new Set<string>()): number => {
    if (rootDepth.has(txId)) return rootDepth.get(txId)!;
    if (seen.has(txId)) return 0;
    seen.add(txId);
    let maxUp = -1;
    for (const inp of inputsByTx[txId] ?? []) {
      const prod = producerByState.get(inp.state.id);
      if (prod && prod !== txId) {
        maxUp = Math.max(maxUp, computeDepth(prod, seen));
      }
    }
    const d = maxUp + 1;
    rootDepth.set(txId, d);
    return d;
  };

  const orderedRoots = [...hierarchy.roots].sort((a, b) => {
    const da = computeDepth(a.id);
    const db = computeDepth(b.id);
    if (da !== db) return da - db;
    const sa = typeof a.meta?.scheduled_start_ms === "number" ? a.meta.scheduled_start_ms : a.created_at_ms;
    const sb = typeof b.meta?.scheduled_start_ms === "number" ? b.meta.scheduled_start_ms : b.created_at_ms;
    return sa - sb;
  });

  return { hierarchy, orderedRoots, inputsByTx, outputsByTx };
}

export function formatMetaDate(timeMs: number | null | undefined, withTime = false): string {
  if (timeMs === null || timeMs === undefined || !Number.isFinite(timeMs)) return "Ohne Zeitangabe";
  return DateTime.fromMillis(timeMs, { zone: DISPLAY_TIMEZONE, locale: "de" }).toFormat(
    withTime ? "d. LLL yyyy · HH:mm" : "d. LLL yyyy"
  );
}

export function formatDurationDays(seconds: number | null | undefined, sigmaSeconds?: number | null): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "Dauer offen";
  const days = seconds / 86_400;
  const sig = sigmaSeconds ? ` ± ${(sigmaSeconds / 86_400).toFixed(1)} d` : "";
  if (days >= 1) return `${days.toFixed(1)} d${sig}`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function goalStatusLabel(status: GoalStatus | null | undefined): string {
  switch (status) {
    case "on_track":
      return "Stufe 2 · Auf Kurs";
    case "at_risk":
      return "Stufe 3 · Gefährdet";
    case "broken":
      return "Stufe 4 · Gebrochen";
    case "cancelled":
      return "Abgebrochen";
    default:
      return "Prognose";
  }
}

export function periodStatusLabel(status: PeriodStatus): string {
  switch (status) {
    case "detected":
      return "Erkannt";
    case "weak":
      return "Schwach";
    case "none":
      return "Kein Takt";
    default:
      return "Zu wenig Zyklen";
  }
}

function formatShortDayMonth(timeMs: number): string {
  return DateTime.fromMillis(timeMs, { zone: DISPLAY_TIMEZONE, locale: "de" }).toFormat("d.M.");
}

export function formatGoalBounds(state: MetaCoreState): string {
  const parts: string[] = [];
  const unit = state.value_unit ? ` ${state.value_unit}` : "";
  if (state.target_min !== null && state.target_min !== undefined) {
    parts.push(`≥ ${Number.isInteger(state.target_min) ? state.target_min : state.target_min.toFixed(1)}${unit}`);
  }
  if (state.target_max !== null && state.target_max !== undefined) {
    parts.push(`≤ ${Number.isInteger(state.target_max) ? state.target_max : state.target_max.toFixed(1)}${unit}`);
  }
  if (state.target_radius_meters !== null && state.target_radius_meters !== undefined) {
    parts.push(`r ≤ ${Math.round(state.target_radius_meters)} m`);
  }
  const hasEarliest = state.target_earliest_ms !== null && state.target_earliest_ms !== undefined;
  const hasLatest = state.target_latest_ms !== null && state.target_latest_ms !== undefined;
  if (hasEarliest && hasLatest) {
    parts.push(`${formatShortDayMonth(state.target_earliest_ms!)}–${formatShortDayMonth(state.target_latest_ms!)}`);
  } else if (hasLatest) {
    parts.push(`bis ${formatShortDayMonth(state.target_latest_ms!)}`);
  } else if (hasEarliest) {
    parts.push(`ab ${formatShortDayMonth(state.target_earliest_ms!)}`);
  }
  return parts.join(" · ") || "—";
}

export type CausalVisualStatus = GoalStatus | "observed" | "happened";

export interface SvgGraphNode {
  key: string;
  id: string;
  kind: "state" | "tx";
  x: number;
  y: number;
  width: number;
  height: number;
  title: string;
  sublabel: string;
  badge: string;
  status: CausalVisualStatus;
  dashed: boolean;
  dotted: boolean;
  unselected: boolean;
  childCount: number;
}

export interface SvgGraphLink {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  role: string;
  status: CausalVisualStatus;
  dashed: boolean;
  unselected: boolean;
}

export interface CausalSvgLayout {
  width: number;
  height: number;
  nowX: number;
  columns: Array<{ label: string; x: number }>;
  nodes: SvgGraphNode[];
  links: SvgGraphLink[];
}

function formatStateValueSummary(st: MetaCoreState): string {
  if (st.modality === "desired") return formatGoalBounds(st);
  if (st.value_num !== null && st.value_num !== undefined) {
    const valStr = Number.isInteger(st.value_num) ? String(st.value_num) : st.value_num.toFixed(1);
    const sigStr =
      st.value_sigma !== null && st.value_sigma !== undefined
        ? ` ± ${Number.isInteger(st.value_sigma) ? st.value_sigma : st.value_sigma.toFixed(1)}`
        : "";
    return `${valStr}${sigStr}${st.value_unit ? ` ${st.value_unit}` : ""}`;
  }
  if (st.value_text) return st.value_text;
  if (st.lat !== null && st.lat !== undefined && st.lon !== null && st.lon !== undefined) {
    return `${st.lat.toFixed(3)}, ${st.lon.toFixed(3)}`;
  }
  return st.time_ms ? formatMetaDate(st.time_ms) : "—";
}

export function buildCausalSvgLayout(
  transformations: MetaCoreTransformation[],
  states: MetaCoreState[],
  edges: MetaCoreEdge[],
  goalEvaluations: GoalEvaluation[],
  includeCancelled = false
): CausalSvgLayout {
  const causal = computeCausalSteps(transformations, states, edges, includeCancelled);
  const visibleTxs = causal.orderedRoots;
  const visibleTxIds = new Set(visibleTxs.map((t) => t.id));
  const stateById = new Map(states.map((s) => [s.id, s]));

  // Only render states that participate in a visible transformation edge or are desired goals
  const connectedStateIds = new Set<string>();
  for (const edge of edges) {
    if (visibleTxIds.has(edge.transformation_id) && stateById.has(edge.state_id)) {
      connectedStateIds.add(edge.state_id);
    }
  }
  const desiredGoals = states.filter(
    (s) =>
      (s.modality === "desired" || (includeCancelled && s.modality === "cancelled" && s.source === "user_goal")) &&
      connectedStateIds.size >= 0
  );
  for (const g of desiredGoals) connectedStateIds.add(g.id);

  const visibleStates = [...connectedStateIds]
    .map((id) => stateById.get(id)!)
    .filter((s) => includeCancelled || s.modality !== "cancelled");
  const visibleStateSet = new Set(visibleStates.map((s) => s.id));
  const visibleEdges = edges.filter((e) => visibleTxIds.has(e.transformation_id) && visibleStateSet.has(e.state_id));

  // Status resolution mirrors ZukunftsgraphPanel & visualizer
  const evalByGoalId = new Map(goalEvaluations.map((g) => [g.goal_id, g]));
  const evalByPredId = new Map<string, GoalEvaluation>();
  for (const ev of goalEvaluations) {
    if (ev.predicted_state_id) evalByPredId.set(ev.predicted_state_id, ev);
  }

  const txStatus = (tx: MetaCoreTransformation): CausalVisualStatus => {
    if (tx.modality === "cancelled") return "cancelled";
    if (tx.modality === "happened") return "happened";
    const outs = causal.outputsByTx[tx.id] ?? [];
    if (outs.length > 0 && outs.every((o) => !o.edge.is_selected || o.edge.outcome === "not_chosen")) {
      return "cancelled";
    }
    for (const o of outs) {
      const ev = evalByPredId.get(o.state.id);
      if (ev) return ev.status;
    }
    for (const ev of goalEvaluations) {
      const alt = ev.alternative_branches.find((b) => b.transformation_id === tx.id);
      if (alt) {
        if (alt.broken_dimensions.length > 0) return "broken";
        return alt.is_selected ? ev.status : "cancelled";
      }
      if (outs.some((o) => o.state.entity_id === ev.entity_id && o.state.property_name === ev.property_name)) {
        return ev.status;
      }
    }
    return "on_track";
  };

  const stateStatus = (st: MetaCoreState): CausalVisualStatus => {
    if (st.modality === "cancelled") return "cancelled";
    if (st.modality === "observed") return "observed";
    if (st.modality === "desired") {
      return evalByGoalId.get(st.id)?.status ?? "on_track";
    }
    const direct = evalByPredId.get(st.id);
    if (direct) return direct.status;
    const byProp = goalEvaluations.find(
      (g) => g.entity_id === st.entity_id && g.property_name === st.property_name
    );
    return byProp?.status ?? "on_track";
  };

  // Assign column ranks: observed (0), tx depth d -> tx col (1 + 2*d), out pred col (2 + 2*d), goals (last)
  const producerByState = new Map<string, string>();
  for (const tx of visibleTxs) {
    for (const o of causal.outputsByTx[tx.id] ?? []) {
      producerByState.set(o.state.id, tx.id);
    }
  }
  const txDepth = new Map<string, number>();
  const getDepth = (txId: string, seen = new Set<string>()): number => {
    if (txDepth.has(txId)) return txDepth.get(txId)!;
    if (seen.has(txId)) return 0;
    seen.add(txId);
    let d = 0;
    for (const inp of causal.inputsByTx[txId] ?? []) {
      const prod = producerByState.get(inp.state.id);
      if (prod && prod !== txId) d = Math.max(d, getDepth(prod, seen) + 1);
    }
    txDepth.set(txId, d);
    return d;
  };
  let maxDepth = 0;
  for (const tx of visibleTxs) maxDepth = Math.max(maxDepth, getDepth(tx.id));

  const obsCol = visibleStates.filter((s) => s.modality === "observed");
  const goalCol = visibleStates.filter(
    (s) => s.modality === "desired" || (s.modality === "cancelled" && s.source === "user_goal")
  );
  const predUnlinked = visibleStates.filter(
    (s) =>
      (s.modality === "predicted" || (s.modality === "cancelled" && s.source !== "user_goal")) &&
      !producerByState.has(s.id)
  );

  // Column buckets keyed by column index
  const colGap = 218;
  const startX = 116;
  const cardW = 178;
  const cardH = 54;
  const rowGap = 72;
  const topPad = 48;

  const colNodes: Array<{ key: string; colIdx: number; kind: "state" | "tx"; item: MetaCoreState | MetaCoreTransformation }> = [];
  for (const s of obsCol) colNodes.push({ key: `state:${s.id}`, colIdx: 0, kind: "state", item: s });
  for (const tx of visibleTxs) {
    const d = txDepth.get(tx.id) ?? 0;
    colNodes.push({ key: `tx:${tx.id}`, colIdx: 1 + d * 2, kind: "tx", item: tx });
    for (const o of causal.outputsByTx[tx.id] ?? []) {
      if (visibleStateSet.has(o.state.id) && o.state.modality !== "desired") {
        colNodes.push({ key: `state:${o.state.id}`, colIdx: 2 + d * 2, kind: "state", item: o.state });
      }
    }
  }
  for (const s of predUnlinked) colNodes.push({ key: `state:${s.id}`, colIdx: 2, kind: "state", item: s });
  const goalColIdx = visibleTxs.length > 0 ? 3 + maxDepth * 2 : 1;
  for (const g of goalCol) colNodes.push({ key: `state:${g.id}`, colIdx: goalColIdx, kind: "state", item: g });

  // Deduplicate node placements keeping the earliest column for states
  const placedMap = new Map<string, { key: string; colIdx: number; kind: "state" | "tx"; item: MetaCoreState | MetaCoreTransformation }>();
  for (const entry of colNodes) {
    if (!placedMap.has(entry.key)) placedMap.set(entry.key, entry);
  }

  const byCol = new Map<number, typeof colNodes>();
  for (const entry of placedMap.values()) {
    const arr = byCol.get(entry.colIdx) ?? [];
    arr.push(entry);
    byCol.set(entry.colIdx, arr);
  }

  const maxRows = Math.max(1, ...[...byCol.values()].map((a) => a.length));
  const totalCols = Math.max(2, goalColIdx + 1);
  const width = Math.max(920, startX * 2 + (totalCols - 1) * colGap);
  const height = Math.max(290, topPad + maxRows * rowGap + 24);
  const pos = new Map<string, { x: number; y: number }>();

  for (const [cIdx, entries] of byCol.entries()) {
    const totalH = (entries.length - 1) * rowGap;
    const y0 = topPad + (height - topPad - 24 - totalH) / 2;
    entries.forEach((entry, idx) => {
      pos.set(entry.key, { x: startX + cIdx * colGap, y: y0 + idx * rowGap });
    });
  }

  const columns: Array<{ label: string; x: number }> = [{ label: "Beobachtet (observed)", x: startX }];
  for (let d = 0; d <= maxDepth && visibleTxs.length > 0; d += 1) {
    columns.push({ label: d === 0 ? "Transformation" : `Schritt ${d + 1}`, x: startX + (1 + d * 2) * colGap });
    columns.push({ label: d === 0 ? "Prognose (predicted)" : `Ergebnis ${d + 1}`, x: startX + (2 + d * 2) * colGap });
  }
  columns.push({ label: "Ziele (desired)", x: startX + goalColIdx * colGap });

  const svgNodes: SvgGraphNode[] = [];
  for (const entry of placedMap.values()) {
    const p = pos.get(entry.key);
    if (!p) continue;
    if (entry.kind === "tx") {
      const tx = entry.item as MetaCoreTransformation;
      const status = txStatus(tx);
      const childCount = (causal.hierarchy.childrenByParent[tx.id] ?? []).length;
      const outEdges = causal.outputsByTx[tx.id] ?? [];
      const unselected = outEdges.length > 0 && outEdges.every((o) => !o.edge.is_selected);
      svgNodes.push({
        key: entry.key,
        id: tx.id,
        kind: "tx",
        x: p.x,
        y: p.y,
        width: cardW,
        height: cardH,
        title: tx.name,
        sublabel: `${formatDurationDays(tx.duration_seconds, tx.duration_sigma_seconds)} · P=${Math.round(tx.success_prob * 100)}%`,
        badge: `${tx.driver === "nature" ? "Natur" : "Aktor"} · ${tx.duration_source}`,
        status,
        dashed: tx.modality === "planned",
        dotted: false,
        unselected,
        childCount,
      });
    } else {
      const st = entry.item as MetaCoreState;
      const status = stateStatus(st);
      svgNodes.push({
        key: entry.key,
        id: st.id,
        kind: "state",
        x: p.x,
        y: p.y,
        width: cardW,
        height: cardH,
        title: st.label,
        sublabel: formatStateValueSummary(st),
        badge: `${st.property_name} · ${st.source}`,
        status,
        dashed: st.modality === "predicted",
        dotted: st.modality === "cancelled",
        unselected: false,
        childCount: 0,
      });
    }
  }

  const svgLinks: SvgGraphLink[] = [];
  for (const e of visibleEdges) {
    const fromKey = e.direction === "in" ? `state:${e.state_id}` : `tx:${e.transformation_id}`;
    const toKey = e.direction === "in" ? `tx:${e.transformation_id}` : `state:${e.state_id}`;
    const p1 = pos.get(fromKey);
    const p2 = pos.get(toKey);
    if (!p1 || !p2) continue;
    const st = stateById.get(e.state_id);
    const stStatus = st ? stateStatus(st) : "on_track";
    svgLinks.push({
      key: `edge:${e.id}`,
      x1: p1.x + cardW / 2,
      y1: p1.y,
      x2: p2.x - cardW / 2,
      y2: p2.y,
      role: e.role,
      status: e.is_selected ? stStatus : "cancelled",
      dashed: e.direction === "out" || !e.is_selected || e.role === "triggers",
      unselected: !e.is_selected,
    });
  }

  // Direct link from predicted state (or observed state) to its matching desired goal
  for (const goal of goalCol) {
    const gp = pos.get(`state:${goal.id}`);
    if (!gp) continue;
    const ev = evalByGoalId.get(goal.id);
    const srcStateId =
      ev?.predicted_state_id ??
      visibleStates.find(
        (s) =>
          s.id !== goal.id &&
          s.modality !== "desired" &&
          s.entity_id === goal.entity_id &&
          s.property_name === goal.property_name
      )?.id;
    if (!srcStateId) continue;
    const sp = pos.get(`state:${srcStateId}`);
    if (!sp) continue;
    svgLinks.push({
      key: `goal-link:${srcStateId}:${goal.id}`,
      x1: sp.x + cardW / 2,
      y1: sp.y,
      x2: gp.x - cardW / 2,
      y2: gp.y,
      role: "prüft",
      status: ev?.status ?? "on_track",
      dashed: true,
      unselected: false,
    });
  }

  return {
    width,
    height,
    nowX: startX + colGap * 0.5,
    columns,
    nodes: svgNodes,
    links: svgLinks,
  };
}
