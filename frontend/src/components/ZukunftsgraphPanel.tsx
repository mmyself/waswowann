"use client";

import { FormEvent, useMemo, useState } from "react";
import { DateTime } from "luxon";
import { api, ApiError } from "@/lib/api";
import { DISPLAY_TIMEZONE } from "@/lib/time-filter";
import {
  buildCausalSvgLayout,
  computeCausalSteps,
  formatDurationDays,
  formatGoalBounds,
  formatMetaDate,
  goalStatusLabel,
  periodStatusLabel,
  type CausalVisualStatus,
} from "@/lib/zukunftsgraph";
import type {
  EscalationAction,
  GoalEvaluation,
  MetaCoreState,
  MetaCoreTransformation,
  PeriodicityReport,
  SeasonalItem,
  ZukunftsgraphSnapshot,
} from "@/lib/types";

const MONTHS_AXIS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];

const STATUS_STYLE: Record<CausalVisualStatus, { stroke: string; fill: string; text: string }> = {
  on_track: { stroke: "#15803d", fill: "#ecfdf5", text: "#14532d" },
  at_risk: { stroke: "#ca8a04", fill: "#fefce8", text: "#713f12" },
  broken: { stroke: "#dc2626", fill: "#fef2f2", text: "#7f1d1d" },
  cancelled: { stroke: "#64748b", fill: "#f4f4f5", text: "#475569" },
  observed: { stroke: "#2563eb", fill: "#eff6ff", text: "#1e3a8a" },
  happened: { stroke: "#0f766e", fill: "#f0fdfa", text: "#134e4a" },
};

function msToIsoDate(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  return DateTime.fromMillis(ms, { zone: DISPLAY_TIMEZONE }).toISODate() ?? "";
}

function isoDateToMs(iso: string, endOfDay = false): number | null {
  const trimmed = iso.trim();
  if (!trimmed) return null;
  const dt = DateTime.fromISO(trimmed, { zone: DISPLAY_TIMEZONE });
  if (!dt.isValid) return null;
  return (endOfDay ? dt.endOf("day") : dt.startOf("day")).toMillis();
}

function parseContextInput(raw: string): Record<string, string> {
  const ctx: Record<string, string> = {};
  for (const part of raw.split(",")) {
    const idx = part.indexOf("=");
    if (idx <= 0) continue;
    const k = part.slice(0, idx).trim();
    const v = part.slice(idx + 1).trim();
    if (k && v) ctx[k] = v;
  }
  return ctx;
}

interface Props {
  snapshot: ZukunftsgraphSnapshot | null;
  view: "graph" | "season";
  focusedEntityId?: string | null;
  onSnapshotChange: (snapshot: ZukunftsgraphSnapshot) => void;
  onSelectObservation?: (obsId: string) => void;
}

export default function ZukunftsgraphPanel({
  snapshot,
  view,
  focusedEntityId,
  onSnapshotChange,
  onSelectObservation,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [includeCancelled, setIncludeCancelled] = useState(false);
  const [expandedTxIds, setExpandedTxIds] = useState<Record<string, boolean>>({});
  const [selectedNode, setSelectedNode] = useState<{ kind: "tx" | "state"; id: string } | null>(null);
  const [obsEntityId, setObsEntityId] = useState<string>("");
  const [obsProp, setObsProp] = useState("temperature_c");
  const [obsVal, setObsVal] = useState("18.5");
  const [obsSigma, setObsSigma] = useState("0.5");
  const [obsUnit, setObsUnit] = useState("°C");
  const [obsContext, setObsContext] = useState("");
  const [obsConfidence, setObsConfidence] = useState("100");
  const [editingGoalId, setEditingGoalId] = useState<string | null>(null);
  const [goalMinDraft, setGoalMinDraft] = useState("");
  const [goalUnitDraft, setGoalUnitDraft] = useState("");
  const [goalEarliestDraft, setGoalEarliestDraft] = useState("");
  const [goalLatestDraft, setGoalLatestDraft] = useState("");
  const [periodPreset, setPeriodPreset] = useState<"archive" | "weekly" | "tidal" | "flush">("archive");
  const [customPeriodReport, setCustomPeriodReport] = useState<PeriodicityReport | null>(null);
  const [periodBusy, setPeriodBusy] = useState(false);

  const entityMap = useMemo(
    () => new Map((snapshot?.entities ?? []).map((e) => [e.id, e])),
    [snapshot?.entities]
  );
  const stateMap = useMemo(
    () => new Map((snapshot?.states ?? []).map((s) => [s.id, s])),
    [snapshot?.states]
  );
  const txMap = useMemo(
    () => new Map((snapshot?.transformations ?? []).map((t) => [t.id, t])),
    [snapshot?.transformations]
  );

  const causal = useMemo(
    () =>
      computeCausalSteps(
        snapshot?.transformations ?? [],
        snapshot?.states ?? [],
        snapshot?.edges ?? [],
        includeCancelled
      ),
    [snapshot?.transformations, snapshot?.states, snapshot?.edges, includeCancelled]
  );

  const activeGoals = useMemo(
    () =>
      (snapshot?.goal_evaluations ?? []).filter(
        (g) => includeCancelled || g.status !== "cancelled"
      ),
    [snapshot?.goal_evaluations, includeCancelled]
  );

  const svgLayout = useMemo(
    () =>
      buildCausalSvgLayout(
        snapshot?.transformations ?? [],
        snapshot?.states ?? [],
        snapshot?.edges ?? [],
        snapshot?.goal_evaluations ?? [],
        includeCancelled
      ),
    [snapshot?.transformations, snapshot?.states, snapshot?.edges, snapshot?.goal_evaluations, includeCancelled]
  );

  const groupedSeasons = useMemo(() => {
    const groups = new Map<string, { title: string; category: string; items: SeasonalItem[] }>();
    for (const item of snapshot?.seasonal_series ?? []) {
      const key = item.title.replace(/\s*\(.*?\)\s*/g, "").trim() || item.title;
      if (!groups.has(key)) groups.set(key, { title: key, category: item.category, items: [] });
      groups.get(key)!.items.push(item);
    }
    return [...groups.values()];
  }, [snapshot?.seasonal_series]);

  if (!snapshot) {
    return (
      <section className="zg-panel zg-panel--empty">
        <p className="eyebrow">MetaCore Engine</p>
        <h2>Zukunftsgraph wird initialisiert …</h2>
        <p>Starte das Backend mit MetaCore-Synchronisation, um Weltlinien, Kausalpläne und Jahresvergleiche zu sehen.</p>
      </section>
    );
  }

  const handleSync = async () => {
    setBusy(true);
    setActionError("");
    try {
      const res = await api.syncZukunftsgraph(true);
      onSnapshotChange(res.snapshot);
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "Synchronisation fehlgeschlagen.");
    } finally {
      setBusy(false);
    }
  };

  const handleEscalation = async (goal: GoalEvaluation, action: EscalationAction, branchKey?: string) => {
    setBusy(true);
    setActionError("");
    try {
      let params: Record<string, unknown> = {};
      if (action === "switch_branch" && branchKey) {
        params = { branch_key: branchKey };
      } else if (action === "widen_tolerance") {
        const goalState = stateMap.get(goal.goal_id);
        if (goalState?.target_latest_ms) {
          params = { target_latest_ms: goalState.target_latest_ms + 7 * 86_400_000 };
        }
        if (goalState?.target_min != null) {
          params = { ...params, target_min: Math.max(0, Math.round(goalState.target_min * 0.8 * 10) / 10) };
        }
        if (!Object.keys(params).length) params = { threshold_ok: 0.7 };
      }
      const res = await api.resolveEscalation(goal.goal_id, action, params);
      onSnapshotChange(res.snapshot);
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "Aktion konnte nicht ausgeführt werden.");
    } finally {
      setBusy(false);
    }
  };

  const handleRecordObservation = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const fallbackEnt = snapshot.entities.find((e) => e.id === "ent:indoor_klimabox")?.id ?? snapshot.entities[0]?.id;
    const entityId = obsEntityId || fallbackEnt;
    if (!entityId) return;
    const valNum = Number(obsVal.replace(",", "."));
    const sigNum = Number(obsSigma.replace(",", "."));
    const confPct = Number(obsConfidence.replace(",", "."));
    if (!Number.isFinite(valNum) || !Number.isFinite(sigNum) || sigNum < 0) {
      setActionError("Bitte gültigen Messwert und Standardabweichung (≥ 0) eingeben.");
      return;
    }
    if (!Number.isFinite(confPct) || confPct <= 0 || confPct > 100) {
      setActionError("Bitte eine gültige Sicherheit zwischen 1 % und 100 % angeben.");
      return;
    }
    setBusy(true);
    setActionError("");
    try {
      const ctx = parseContextInput(obsContext);
      const res = await api.recordMetaObservation({
        entity_id: entityId,
        property_name: obsProp.trim() || "temperature_c",
        value_num: valNum,
        value_sigma: sigNum,
        value_unit: obsUnit.trim(),
        time_ms: snapshot.now_ms,
        context: Object.keys(ctx).length ? ctx : undefined,
        assertion_confidence: confPct / 100,
      });
      onSnapshotChange(res.snapshot);
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "Messwert konnte nicht erfasst werden.");
    } finally {
      setBusy(false);
    }
  };

  const openGoalEditor = (goalId: string) => {
    const st = stateMap.get(goalId);
    if (!st) return;
    setEditingGoalId(goalId);
    setGoalMinDraft(st.target_min !== null && st.target_min !== undefined ? String(st.target_min) : "");
    setGoalUnitDraft(st.value_unit || "");
    setGoalEarliestDraft(msToIsoDate(st.target_earliest_ms));
    setGoalLatestDraft(msToIsoDate(st.target_latest_ms));
    setActionError("");
  };

  const handleSaveGoalEdit = async (event: FormEvent<HTMLFormElement>, goalId: string) => {
    event.preventDefault();
    const minStr = goalMinDraft.trim();
    const parsedMin = minStr === "" ? null : Number(minStr.replace(",", "."));
    if (minStr !== "" && (parsedMin === null || !Number.isFinite(parsedMin))) {
      setActionError("Bitte eine gültige Mindestmenge eingeben.");
      return;
    }
    const earliestMs = isoDateToMs(goalEarliestDraft, false);
    const latestMs = isoDateToMs(goalLatestDraft, true);
    if (earliestMs !== null && latestMs !== null && earliestMs > latestMs) {
      setActionError("Das Frühestens-Datum darf nicht nach dem Spätestens-Datum liegen.");
      return;
    }
    setBusy(true);
    setActionError("");
    try {
      const res = await api.updateMetaGoal(goalId, {
        value_unit: goalUnitDraft.trim(),
        target_min: parsedMin,
        target_earliest_ms: earliestMs,
        target_latest_ms: latestMs,
      });
      onSnapshotChange(res.snapshot);
      setEditingGoalId(null);
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "Ziel konnte nicht aktualisiert werden.");
    } finally {
      setBusy(false);
    }
  };

  const toggleExpand = (txId: string) => {
    setExpandedTxIds((prev) => ({ ...prev, [txId]: !prev[txId] }));
  };

  const inspectedTx: MetaCoreTransformation | null =
    selectedNode?.kind === "tx" ? txMap.get(selectedNode.id) ?? null : null;
  const inspectedState: MetaCoreState | null =
    selectedNode?.kind === "state" ? stateMap.get(selectedNode.id) ?? null : null;
  const inspectedEntity = inspectedState
    ? entityMap.get(inspectedState.entity_id) ?? null
    : inspectedTx
      ? (() => {
          const out = causal.outputsByTx[inspectedTx.id]?.[0];
          return out ? entityMap.get(out.state.entity_id) ?? null : null;
        })()
      : null;

  if (view === "season") {
    const activePeriodReport = periodPreset === "archive" ? snapshot.periodicity ?? null : customPeriodReport;
    const clocks = activePeriodReport?.current_clocks?.length
      ? activePeriodReport.current_clocks
      : snapshot.current_clocks ?? [];

    const runPreset = async (preset: "archive" | "weekly" | "tidal" | "flush") => {
      setPeriodPreset(preset);
      if (preset === "archive") {
        setCustomPeriodReport(null);
        return;
      }
      setPeriodBusy(true);
      setActionError("");
      try {
        const anchor = snapshot.now_ms;
        let timestamps: number[] = [];
        let label = "";
        if (preset === "weekly") {
          label = "Testreihe: Wochenend-Takt (5× Sa)";
          const day = 86_400_000;
          timestamps = [4, 3, 2, 1, 0].map((w) => anchor - w * 7 * day);
        } else if (preset === "tidal") {
          label = "Testreihe: Gezeiten / Ebbe (M2 12,42 h)";
          const tideMs = Math.round(12.4206012 * 3600 * 1000);
          timestamps = [4, 3, 2, 1, 0].map((k) => anchor - k * 3 * tideMs);
        } else {
          label = "Testreihe: Eigen-Intervall (~14 d Flushes)";
          const day = 86_400_000;
          timestamps = [anchor - 42 * day, anchor - 28 * day, anchor - 13 * day, anchor];
        }
        const rep = await api.analyzePeriods({ timestamps_ms: timestamps, label, now_ms: anchor });
        setCustomPeriodReport(rep);
      } catch (cause) {
        setActionError(cause instanceof ApiError ? cause.message : "Periodenanalyse fehlgeschlagen.");
      } finally {
        setPeriodBusy(false);
      }
    };

    return (
      <section className="zg-panel" aria-label="Perioden und Jahresansicht">
        {clocks.length > 0 && (
          <div className="clocks-section">
            <div className="zg-section-title">
              <h3>Aktuelle Uhren im JETZT</h3>
              <span>{formatMetaDate(snapshot.now_ms, true)}</span>
            </div>
            <div className="clocks-strip">
              {clocks.map((c) => (
                <div key={c.clock_id} className="clock-chip">
                  <small>{c.clock_name}</small>
                  <strong>{c.label}</strong>
                  <span>{c.detail} · Phase {Math.round(c.phase * 100)} %</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="period-detector">
          <div className="zg-head">
            <div>
              <p className="eyebrow">Takt- &amp; Perioden-Erkennung</p>
              <h2>Zu welcher Periode gehört die Reihe?</h2>
            </div>
            <div className="zg-actions">
              <button type="button" className={periodPreset === "archive" ? "secondary-action is-active-pill" : "secondary-action"} disabled={periodBusy} onClick={() => runPreset("archive")}>Archiv-Funde</button>
              <button type="button" className={periodPreset === "weekly" ? "secondary-action is-active-pill" : "secondary-action"} disabled={periodBusy} onClick={() => runPreset("weekly")}>Wochen-Takt</button>
              <button type="button" className={periodPreset === "tidal" ? "secondary-action is-active-pill" : "secondary-action"} disabled={periodBusy} onClick={() => runPreset("tidal")}>Gezeiten (12,4 h)</button>
              <button type="button" className={periodPreset === "flush" ? "secondary-action is-active-pill" : "secondary-action"} disabled={periodBusy} onClick={() => runPreset("flush")}>14-Tage-Zyklus</button>
            </div>
          </div>

          {activePeriodReport && (
            <>
              <div className="zg-feedback" role="status">
                <strong>{activePeriodReport.label || "Analyse"}:</strong> {activePeriodReport.summary} ({activePeriodReport.distinct_event_count} getrennte Ereignisse)
              </div>
              <div className="period-table">
                {activePeriodReport.candidates.map((cand) => (
                  <div key={cand.clock_id} className={`period-row is-${cand.status}`}>
                    <div className="period-row__name">
                      <strong>{cand.clock_name}</strong>
                      <small>{cand.explanation}</small>
                    </div>
                    <span className={`zg-stage-pill is-period-${cand.status}`}>{periodStatusLabel(cand.status)}</span>
                    <div className="period-bar-wrap">
                      <div className="period-bar"><i style={{ width: `${Math.round(cand.concentration * 100)}%` }} /></div>
                      <small>R={Math.round(cand.concentration * 100)} % · {cand.covered_cycles} Zyk.</small>
                    </div>
                    <div className="period-next">
                      {cand.next_window_center_ms ? (
                        <>
                          <small>Nächstes Fenster</small>
                          <strong>{formatMetaDate(cand.next_window_center_ms, true)}</strong>
                        </>
                      ) : (
                        <small>{cand.mean_label || "—"}</small>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="zg-head">
          <div>
            <p className="eyebrow">Jahresansicht (Tag 1–366)</p>
            <h2>Saisonaler Vergleich über Jahre</h2>
          </div>
          <div className="zg-legend">
            <span><i className="zg-dot zg-dot--obs" /> Beobachtet</span>
            <span><i className="zg-dot zg-dot--pred" /> Prognose-Fenster (±σ)</span>
          </div>
        </div>
        <div className="season-board">
          <div className="season-axis">
            <div className="season-axis__label">Objekt / Habitat</div>
            <div className="season-axis__months">
              {MONTHS_AXIS.map((m) => (
                <span key={m}>{m}</span>
              ))}
            </div>
          </div>
          {groupedSeasons.map((group) => (
            <div key={group.title} className="season-row">
              <div className="season-row__title">
                <strong>{group.title}</strong>
                <small>{group.category} · {group.items.length} Eintr.</small>
              </div>
              <div className="season-row__track">
                {group.items.map((item) => {
                  const leftPct = Math.min(99, Math.max(1, ((item.day_of_year - 1) / 366) * 100));
                  const widthPct = Math.min(35, Math.max(2, (item.sigma_days / 366) * 200));
                  const statusCls = item.goal_status ? `is-${item.goal_status}` : "";
                  if (item.kind === "predicted") {
                    return (
                      <div
                        key={item.id}
                        className={`season-window ${statusCls}`}
                        style={{ left: `${Math.max(0, leftPct - widthPct / 2)}%`, width: `${widthPct}%` }}
                        title={`${item.title}: ${item.date_label} (±${item.sigma_days} d)`}
                      >
                        <span>{item.date_label}</span>
                      </div>
                    );
                  }
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`season-marker ${item.obs_id ? "is-clickable" : ""}`}
                      style={{ left: `${leftPct}%` }}
                      title={`${item.title} (${item.date_label})`}
                      onClick={() => item.obs_id && onSelectObservation?.(item.obs_id)}
                    >
                      <small>{item.year}</small>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </section>
    );
  }

  return (
    <section className="zg-panel" aria-label="Interaktiver Zukunftsgraph">
      <div className="zg-head">
        <div>
          <p className="eyebrow">MetaCore Kausal-Graph</p>
          <h2>{snapshot.title}</h2>
          <p className="zg-sub">{snapshot.synced_observation_count} Funde als Weltlinien · {snapshot.entities.length} Entitäten · Stand {formatMetaDate(snapshot.now_ms, true)}</p>
        </div>
        <div className="zg-actions">
          <label className="zg-toggle">
            <input type="checkbox" checked={includeCancelled} onChange={(e) => setIncludeCancelled(e.target.checked)} />
            Abgebrochene zeigen
          </label>
          <button type="button" className="secondary-action" disabled={busy} onClick={handleSync}>
            {busy ? "Synchronisiert …" : "Mit Archiv synchronisieren"}
          </button>
        </div>
      </div>

      {actionError && <p className="form-error" role="alert">{actionError}</p>}
      {snapshot.last_feedback?.summary && (
        <div className="zg-feedback" role="status">
          <strong>Engine-Rückmeldung:</strong> {snapshot.last_feedback.summary}
        </div>
      )}

      {snapshot.uses_conflicts.length > 0 && (
        <div className="zg-conflict-banner" role="alert">
          {snapshot.uses_conflicts.map((c, idx) => (
            <div key={`${c.entity_id}-${idx}`}>
              <strong>Ressourcen-Konflikt ({c.entity_name}):</strong> „{c.transformation_a_name}“ überlappt mit „{c.transformation_b_name}“
            </div>
          ))}
        </div>
      )}

      <div className="zg-goals-grid">
        {activeGoals.map((goal) => {
          const goalState = stateMap.get(goal.goal_id);
          const isEditingThisGoal = editingGoalId === goal.goal_id;
          return (
            <article key={goal.goal_id} className={`zg-goal-card is-${goal.status}`}>
              <div className="zg-goal-top">
                <span className={`zg-stage-pill is-${goal.status}`}>{goalStatusLabel(goal.status)}</span>
                <strong>{Math.round(goal.goal_probability * 100)} %</strong>
              </div>
              <h3>{goal.label}</h3>
              {goalState && (
                <p className="zg-goal-bounds">{formatGoalBounds(goalState)}</p>
              )}
              <p className="zg-goal-summary">{goal.summary}</p>
              <div className="zg-dims">
                {Object.entries(goal.dimension_probs).map(([dim, prob]) => (
                  <div key={dim} className={`zg-dim ${goal.broken_dimensions.includes(dim) ? "is-broken" : ""}`}>
                    <span>{dim}</span>
                    <div className="zg-dim-bar"><i style={{ width: `${Math.round(prob * 100)}%` }} /></div>
                    <small>{Math.round(prob * 100)}%</small>
                  </div>
                ))}
              </div>
              {goal.status !== "cancelled" && (
                <div className="zg-goal-actions">
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={busy}
                    onClick={() => (isEditingThisGoal ? setEditingGoalId(null) : openGoalEditor(goal.goal_id))}
                  >
                    {isEditingThisGoal ? "Editor schließen" : "Menge & Zeitpunkt ändern"}
                  </button>
                  {goal.alternative_branches
                    .filter((b) => !b.is_selected)
                    .map((b) => (
                      <button
                        key={b.branch_key}
                        type="button"
                        className="secondary-action"
                        disabled={busy}
                        onClick={() => handleEscalation(goal, "switch_branch", b.branch_key)}
                      >
                        Auf Zweig „{b.branch_key}“ wechseln ({Math.round(b.goal_probability * 100)} %)
                      </button>
                    ))}
                  {goal.status !== "on_track" && (
                    <button
                      type="button"
                      className="secondary-action"
                      disabled={busy}
                      onClick={() => handleEscalation(goal, "widen_tolerance")}
                    >
                      Toleranz lockern
                    </button>
                  )}
                  <button
                    type="button"
                    className="text-action"
                    disabled={busy}
                    onClick={() => handleEscalation(goal, "abort_goal")}
                  >
                    Ziel abbrechen
                  </button>
                </div>
              )}
              {isEditingThisGoal && (
                <form className="zg-goal-editor" onSubmit={(e) => handleSaveGoalEdit(e, goal.goal_id)}>
                  <div className="form-two-col">
                    <label>
                      Mindestmenge
                      <input
                        inputMode="decimal"
                        value={goalMinDraft}
                        onChange={(e) => setGoalMinDraft(e.target.value)}
                        placeholder="z. B. 200"
                      />
                    </label>
                    <label>
                      Einheit
                      <input
                        value={goalUnitDraft}
                        onChange={(e) => setGoalUnitDraft(e.target.value)}
                        maxLength={20}
                        placeholder="g, ml, °C"
                      />
                    </label>
                  </div>
                  <div className="form-two-col">
                    <label>
                      Frühestens (ab)
                      <input
                        type="date"
                        value={goalEarliestDraft}
                        onChange={(e) => setGoalEarliestDraft(e.target.value)}
                      />
                    </label>
                    <label>
                      Spätestens (bis)
                      <input
                        type="date"
                        value={goalLatestDraft}
                        onChange={(e) => setGoalLatestDraft(e.target.value)}
                      />
                    </label>
                  </div>
                  <div className="zg-goal-actions">
                    <button type="submit" className="save-button" disabled={busy}>
                      Speichern &amp; neu bewerten ↗
                    </button>
                  </div>
                </form>
              )}
            </article>
          );
        })}
      </div>

      {svgLayout.nodes.length > 0 && (
        <div className="zg-svg-card">
          <div className="zg-section-title">
            <h3>Kausal-Graph (Was → Wo → Wann auf der Weltlinie)</h3>
            <span>{svgLayout.nodes.length} Knoten · {svgLayout.links.length} Verbindungen · Klick für Kausal-Linse</span>
          </div>
          <div className="zg-svg-wrap">
            <svg
              viewBox={`0 0 ${svgLayout.width} ${svgLayout.height}`}
              width="100%"
              role="img"
              aria-label="Interaktiver Zukunftsgraph SVG"
            >
              <defs>
                {(Object.keys(STATUS_STYLE) as CausalVisualStatus[]).map((stKey) => (
                  <marker
                    key={stKey}
                    id={`zg-arrow-${stKey}`}
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="3"
                    orient="auto"
                  >
                    <path d="M0,0 L8,3 L0,6 Z" fill={STATUS_STYLE[stKey].stroke} />
                  </marker>
                ))}
              </defs>
              <line
                x1={svgLayout.nowX}
                y1={28}
                x2={svgLayout.nowX}
                y2={svgLayout.height - 10}
                stroke="#0f172a"
                strokeWidth="1.5"
                strokeDasharray="4 4"
                opacity="0.35"
              />
              <rect x={svgLayout.nowX - 24} y={8} width={48} height={18} rx={9} fill="#0f172a" />
              <text x={svgLayout.nowX} y={20} textAnchor="middle" fontSize="9.5" fontWeight="700" fill="#ffffff">
                JETZT
              </text>
              {svgLayout.columns.map((col, idx) => (
                <text
                  key={`${col.label}-${idx}`}
                  x={col.x}
                  y={24}
                  textAnchor="middle"
                  fontSize="10"
                  fontWeight="700"
                  fill="#64748b"
                >
                  {col.label}
                </text>
              ))}
              {svgLayout.links.map((lk) => {
                const pal = STATUS_STYLE[lk.status];
                const dx = Math.max(28, (lk.x2 - lk.x1) * 0.45);
                const d = `M ${lk.x1} ${lk.y1} C ${lk.x1 + dx} ${lk.y1}, ${lk.x2 - dx} ${lk.y2}, ${lk.x2} ${lk.y2}`;
                return (
                  <g key={lk.key} opacity={lk.unselected ? 0.32 : 0.9}>
                    <path
                      d={d}
                      fill="none"
                      stroke={pal.stroke}
                      strokeWidth={lk.unselected ? 1.2 : 1.8}
                      strokeDasharray={lk.dashed ? "5 4" : undefined}
                      markerEnd={`url(#zg-arrow-${lk.status})`}
                    />
                    <text
                      x={(lk.x1 + lk.x2) / 2}
                      y={(lk.y1 + lk.y2) / 2 - 4}
                      textAnchor="middle"
                      fontSize="8.5"
                      fill="#64748b"
                    >
                      {lk.role}
                    </text>
                  </g>
                );
              })}
              {svgLayout.nodes.map((node) => {
                const pal = STATUS_STYLE[node.status];
                const isSelected = selectedNode?.id === node.id;
                const titleShort = node.title.length > 25 ? `${node.title.slice(0, 24)}…` : node.title;
                const subShort = node.sublabel.length > 28 ? `${node.sublabel.slice(0, 27)}…` : node.sublabel;
                const badgeShort = node.badge.length > 28 ? `${node.badge.slice(0, 27)}…` : node.badge;
                return (
                  <g
                    key={node.key}
                    className="zg-svg-node"
                    opacity={node.unselected ? 0.45 : 1}
                    onClick={() => setSelectedNode({ kind: node.kind, id: node.id })}
                  >
                    <rect
                      x={node.x - node.width / 2}
                      y={node.y - node.height / 2}
                      width={node.width}
                      height={node.height}
                      rx={node.kind === "tx" ? 14 : 7}
                      fill={pal.fill}
                      stroke={pal.stroke}
                      strokeWidth={isSelected ? 2.8 : 1.6}
                      strokeDasharray={node.dashed ? "6 3" : node.dotted ? "2 3" : undefined}
                    />
                    <text
                      x={node.x}
                      y={node.y - 8}
                      textAnchor="middle"
                      fontSize="10.5"
                      fontWeight="700"
                      fill={pal.text}
                    >
                      {titleShort}
                    </text>
                    <text x={node.x} y={node.y + 7} textAnchor="middle" fontSize="9.5" fill="#0f172a">
                      {subShort}
                    </text>
                    <text x={node.x} y={node.y + 19} textAnchor="middle" fontSize="8" fill="#64748b">
                      {badgeShort}
                    </text>
                    {node.childCount > 0 && (
                      <text
                        x={node.x + node.width / 2 - 8}
                        y={node.y - node.height / 2 + 11}
                        textAnchor="end"
                        fontSize="8"
                        fontWeight="700"
                        fill="#7c3aed"
                      >
                        +{node.childCount}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          </div>
        </div>
      )}

      <div className="zg-main-grid">
        <div className="zg-chain-list">
          <div className="zg-section-title">
            <h3>Kausale Prozessketten &amp; verschachtelte Unterschritte</h3>
            <span>{causal.orderedRoots.length} Makroschritte · Linienstil = Modalität</span>
          </div>
          {causal.orderedRoots.map((tx) => {
            const children = causal.hierarchy.childrenByParent[tx.id] ?? [];
            const expanded = Boolean(expandedTxIds[tx.id]);
            const inputs = causal.inputsByTx[tx.id] ?? [];
            const outputs = causal.outputsByTx[tx.id] ?? [];
            const isFocused = Boolean(
              focusedEntityId &&
                (inputs.some((i) => i.state.entity_id === focusedEntityId) ||
                  outputs.some((o) => o.state.entity_id === focusedEntityId))
            );
            return (
              <div
                key={tx.id}
                className={`zg-tx-card is-${tx.modality} ${isFocused ? "is-focused" : ""} ${
                  selectedNode?.id === tx.id ? "is-selected" : ""
                }`}
              >
                <div className="zg-tx-header">
                  <button type="button" className="zg-tx-title" onClick={() => setSelectedNode({ kind: "tx", id: tx.id })}>
                    <span className="zg-driver-badge">{tx.driver === "nature" ? "Natur" : "Aktor"}</span>
                    <strong>{tx.name}</strong>
                  </button>
                  <div className="zg-tx-meta">
                    <span>{formatDurationDays(tx.duration_seconds, tx.duration_sigma_seconds)}</span>
                    <span>P={Math.round(tx.success_prob * 100)} %</span>
                    <span className="zg-src-tag">{tx.duration_source}</span>
                  </div>
                </div>

                <div className="zg-io-row">
                  <div className="zg-io-col">
                    <small className="eyebrow">Eingänge</small>
                    <div className="zg-pills">
                      {inputs.map(({ edge, state }) => (
                        <button
                          key={edge.id}
                          type="button"
                          className={`zg-state-pill is-${state.modality} is-src-${state.source} ${edge.is_selected ? "" : "is-unselected"}`}
                          onClick={() => setSelectedNode({ kind: "state", id: state.id })}
                        >
                          <em>{edge.role}</em> {state.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <span className="zg-arrow" aria-hidden="true">→</span>
                  <div className="zg-io-col">
                    <small className="eyebrow">Wirkungen</small>
                    <div className="zg-pills">
                      {outputs.map(({ edge, state }) => (
                        <button
                          key={edge.id}
                          type="button"
                          className={`zg-state-pill is-${state.modality} is-src-${state.source} ${edge.is_selected ? "" : "is-unselected"}`}
                          onClick={() => setSelectedNode({ kind: "state", id: state.id })}
                        >
                          {edge.logic_group === "CHOICE_OR" && <em>[{edge.branch_key}] </em>}
                          {state.label}
                          {state.time_ms ? ` · ${formatMetaDate(state.time_ms)}` : ""}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                {children.length > 0 && (
                  <div className="zg-nested-wrap">
                    <button type="button" className="zg-expand-btn" onClick={() => toggleExpand(tx.id)}>
                      {expanded ? `▼ ${children.length} Unterschritte einklappen` : `▶ +${children.length} Unterschritte anzeigen (Zoom-In)`}
                    </button>
                    {expanded && (
                      <ol className="zg-nested-children">
                        {children.map((child) => (
                          <li key={child.id} className={`zg-child-step is-${child.modality}`}>
                            <button type="button" onClick={() => setSelectedNode({ kind: "tx", id: child.id })}>
                              <strong>{child.name}</strong>
                              <span>{formatDurationDays(child.duration_seconds, child.duration_sigma_seconds)} · P={Math.round(child.success_prob * 100)} %</span>
                            </button>
                          </li>
                        ))}
                      </ol>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <aside className="zg-side-column">
          <div className="zg-inspector">
            <p className="eyebrow">Kausal-Linse</p>
            {inspectedTx ? (
              <div>
                <h3>{inspectedTx.name}</h3>
                <p>Modalität: <strong>{inspectedTx.modality}</strong> · Treiber: <strong>{inspectedTx.driver}</strong></p>
                <p>Dauer: <strong>{formatDurationDays(inspectedTx.duration_seconds, inspectedTx.duration_sigma_seconds)}</strong> ({inspectedTx.duration_source})</p>
                {Object.keys(inspectedTx.formula).length > 0 && (
                  <pre className="zg-json">{JSON.stringify(inspectedTx.formula, null, 2)}</pre>
                )}
              </div>
            ) : inspectedState ? (
              <div>
                <h3>{inspectedState.label}</h3>
                <p>Eigenschaft: <code>{inspectedState.property_name}</code> · Quelle: <strong>{inspectedState.source}</strong></p>
                {inspectedState.modality === "desired" && (
                  <>
                    <p>Ziel-Grenzen: <strong>{formatGoalBounds(inspectedState)}</strong></p>
                    {(inspectedState.target_earliest_ms || inspectedState.target_latest_ms) && (
                      <p>
                        Ziel-Zeitfenster:{" "}
                        <strong>
                          {inspectedState.target_earliest_ms ? formatMetaDate(inspectedState.target_earliest_ms, true) : "offen"}
                          {" – "}
                          {inspectedState.target_latest_ms ? formatMetaDate(inspectedState.target_latest_ms, true) : "offen"}
                        </strong>
                      </p>
                    )}
                  </>
                )}
                {inspectedState.value_num !== null && (
                  <p>Wert: <strong>{inspectedState.value_num.toFixed(1)}{inspectedState.value_sigma ? ` ± ${inspectedState.value_sigma.toFixed(1)}` : ""} {inspectedState.value_unit}</strong></p>
                )}
                {inspectedState.time_ms !== null && <p>Zeit: <strong>{formatMetaDate(inspectedState.time_ms, true)}</strong></p>}
                {inspectedState.lat !== null && inspectedState.lon !== null && (
                  <p>Ort: <strong>{inspectedState.lat.toFixed(5)}, {inspectedState.lon.toFixed(5)}</strong> (±{inspectedState.sigma_meters?.toFixed(1) ?? "?"} m)</p>
                )}
                {inspectedState.context && Object.keys(inspectedState.context).length > 0 && (
                  <p>
                    Kontext:{" "}
                    <strong>
                      {Object.entries(inspectedState.context)
                        .map(([k, v]) => `${k}=${v}`)
                        .join(", ")}
                    </strong>
                  </p>
                )}
                {inspectedState.assertion_confidence !== undefined && inspectedState.assertion_confidence < 1.0 && (
                  <p>Epistemische Sicherheit: <strong>{Math.round(inspectedState.assertion_confidence * 100)} %</strong></p>
                )}
                {inspectedState.evidence_refs && inspectedState.evidence_refs.length > 0 && (
                  <p>
                    Belege:{" "}
                    <strong>
                      {inspectedState.evidence_refs.map((r) => r.label || r.ref).join(", ")}
                    </strong>
                  </p>
                )}
                {inspectedState.meta?.rollup != null && (
                  <p>
                    Roll-Up:{" "}
                    <strong>
                      n={String((inspectedState.meta.rollup as Record<string, unknown>).n ?? "?")}
                    </strong>
                  </p>
                )}
                {inspectedEntity && typeof inspectedEntity.meta?.legacy_obs_id === "string" && (
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => onSelectObservation?.(String(inspectedEntity.meta.legacy_obs_id))}
                  >
                    Fund im Archiv öffnen ↗
                  </button>
                )}
              </div>
            ) : (
              <p className="empty-inline">Klicke einen Prozess oder Zustand an, um Formeln, Unsicherheiten (±σ) und Fund-Verknüpfungen anzuzeigen.</p>
            )}
          </div>

          <form className="zg-record-form" onSubmit={handleRecordObservation}>
            <p className="eyebrow">Live-Messwert erfassen</p>
            <label>
              Weltlinie
              <select
                value={obsEntityId || snapshot.entities.find((e) => e.id === "ent:indoor_klimabox")?.id || snapshot.entities[0]?.id || ""}
                onChange={(e) => setObsEntityId(e.target.value)}
              >
                {snapshot.entities.map((ent) => (
                  <option key={ent.id} value={ent.id}>{ent.name}</option>
                ))}
              </select>
            </label>
            <div className="form-two-col">
              <label>
                Eigenschaft
                <select value={obsProp} onChange={(e) => {
                  const p = e.target.value;
                  setObsProp(p);
                  if (p === "humidity_pct") { setObsVal("82"); setObsSigma("8"); setObsUnit("%"); }
                  else if (p === "rain_mm") { setObsVal("24"); setObsSigma("2"); setObsUnit("mm"); }
                  else if (p === "harvest_ready") { setObsVal("240"); setObsSigma("15"); setObsUnit("g"); }
                  else { setObsVal("18.5"); setObsSigma("0.5"); setObsUnit("°C"); }
                }}>
                  <option value="temperature_c">temperature_c</option>
                  <option value="humidity_pct">humidity_pct</option>
                  <option value="rain_mm">rain_mm</option>
                  <option value="harvest_ready">harvest_ready</option>
                </select>
              </label>
              <label>Einheit<input value={obsUnit} onChange={(e) => setObsUnit(e.target.value)} maxLength={12} /></label>
            </div>
            <div className="form-two-col">
              <label>Wert<input inputMode="decimal" value={obsVal} onChange={(e) => setObsVal(e.target.value)} required /></label>
              <label>± σ<input inputMode="decimal" value={obsSigma} onChange={(e) => setObsSigma(e.target.value)} required /></label>
            </div>
            <div className="form-two-col">
              <label>Kontext (k=v)<input value={obsContext} onChange={(e) => setObsContext(e.target.value)} placeholder="z. B. probe=shade" /></label>
              <label>Sicherheit (%)<input inputMode="decimal" value={obsConfidence} onChange={(e) => setObsConfidence(e.target.value)} /></label>
            </div>
            <button type="submit" className="save-button" disabled={busy}>
              Messwert übernehmen <span aria-hidden="true">↗</span>
            </button>
          </form>
        </aside>
      </div>
    </section>
  );
}
