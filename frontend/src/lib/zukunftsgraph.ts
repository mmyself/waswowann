import { DateTime } from "luxon";
import { DISPLAY_TIMEZONE } from "./time-filter";
import type { GoalStatus, MetaCoreEdge, MetaCoreState, MetaCoreTransformation, PeriodStatus } from "./types";

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
