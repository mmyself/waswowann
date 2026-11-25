"use client";

import { DateTime } from "luxon";
import { useMemo } from "react";
import { DISPLAY_TIMEZONE, FilterMode, formatRange, TimeWindow, toLocalInput, fromLocalInput } from "@/lib/time-filter";

interface Props {
  mode: FilterMode;
  onModeChange: (mode: FilterMode) => void;
  extent: TimeWindow | null;
  window: TimeWindow | null;
  onWindowChange: (window: TimeWindow) => void;
  visibleCount: number;
  totalCount: number;
  undatedCount: number;
  nowMs?: number | null;
  futurePredictionCount?: number;
}

export default function TimeFilter({ mode, onModeChange, extent, window, onWindowChange, visibleCount, totalCount, undatedCount, nowMs, futurePredictionCount }: Props) {
  const canFilter = Boolean(extent);
  const span = extent ? Math.max(1, extent.end - extent.start) : 1;
  const current = window ?? extent;
  const tick = useMemo(() => Math.max(1, Math.round(span / 250)), [span]);

  const setBound = (bound: "start" | "end", value: number) => {
    if (!current) return;
    if (bound === "start") onWindowChange({ start: Math.min(value, current.end), end: current.end });
    else onWindowChange({ start: current.start, end: Math.max(value, current.start) });
  };
  const setExact = (bound: "start" | "end", value: string) => {
    const epoch = fromLocalInput(value);
    if (epoch !== null) setBound(bound, epoch);
  };
  const endpoint = (epoch: number) => DateTime.fromMillis(epoch, { zone: DISPLAY_TIMEZONE, locale: "de" }).toFormat("d. LLL yyyy · HH:mm");

  return (
    <section className="time-panel" aria-label="Zeitfilter">
      <div className="time-panel__top">
        <span className="time-label">Zeitraum</span>
        {Boolean(extent && nowMs && extent.end > nowMs && (futurePredictionCount ?? 0) > 0) && (
          <span className="time-future-pill">JETZT → +{futurePredictionCount} Prognose{futurePredictionCount === 1 ? "" : "n"}</span>
        )}
        <div className="mode-switch" role="group" aria-label="Anzeigemodus">
          <button type="button" className={mode === "all" ? "is-active" : ""} onClick={() => onModeChange("all")}>Alle zeigen</button>
          <button type="button" className={mode === "window" ? "is-active" : ""} disabled={!canFilter} onClick={() => onModeChange("window")}>Zeitfenster</button>
        </div>
      </div>
      <div className={`time-controls ${mode === "window" ? "is-enabled" : ""}`}>
        {current ? <>
          <div className="range-ends"><span>{endpoint(current.start)}</span><span>{endpoint(current.end)}</span></div>
          <label className="sr-only" htmlFor="window-start">Frühester Zeitpunkt</label>
          <input id="window-start" aria-label="Frühester Zeitpunkt" type="range" min={extent?.start ?? 0} max={extent?.end ?? 1} step={tick} value={current.start} disabled={mode !== "window"} onChange={(event) => setBound("start", Number(event.target.value))} />
          <label className="sr-only" htmlFor="window-end">Spätester Zeitpunkt</label>
          <input id="window-end" aria-label="Spätester Zeitpunkt" type="range" min={extent?.start ?? 0} max={extent?.end ?? 1} step={tick} value={current.end} disabled={mode !== "window"} onChange={(event) => setBound("end", Number(event.target.value))} />
          <div className="exact-times">
            <label>Von <input aria-label="Von Datum und Uhrzeit" type="datetime-local" value={toLocalInput(current.start)} disabled={mode !== "window"} onChange={(event) => setExact("start", event.target.value)} /></label>
            <label>Bis <input aria-label="Bis Datum und Uhrzeit" type="datetime-local" value={toLocalInput(current.end)} disabled={mode !== "window"} onChange={(event) => setExact("end", event.target.value)} /></label>
          </div>
        </> : <p className="empty-inline">Noch keine datierten Beobachtungen.</p>}
      </div>
      {mode === "window" && (undatedCount > 0 || current) ? <div className="time-summary">
        {undatedCount > 0 && <span className="undated-note">{undatedCount} ohne Datum ausgeblendet</span>}
        {current && <span className="range-summary">{formatRange(current)}</span>}
        <span>{visibleCount} / {totalCount} Funde · Aufnahmezeiträume, die das Zeitfenster berühren</span>
      </div> : null}
    </section>
  );
}
