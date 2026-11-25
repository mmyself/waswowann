"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { api } from "@/lib/api";
import { clampWindow, extentToWindow, filterObservations, FilterMode, mergeFutureExtent, TimeWindow } from "@/lib/time-filter";
import type { ObservationDetail, ObservationListResponse, PublicConfig, ZukunftsgraphSnapshot } from "@/lib/types";
import TimeFilter from "./TimeFilter";
import ObservationList from "./ObservationList";
import ObservationDetails from "./ObservationDetails";
import ImportControls from "./ImportControls";
import ZukunftsgraphPanel from "./ZukunftsgraphPanel";
import ChatPanel from "./ChatPanel";

const ObservationMap = dynamic(() => import("./ObservationMap"), { ssr: false, loading: () => <div className="map-loading"><span className="loading-orbit" />Karte wird vorbereitet …</div> });

export default function Workspace() {
  const search = useSearchParams();
  const requestedObservation = search.get("observation");
  const [data, setData] = useState<ObservationListResponse | null>(null);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [snapshot, setSnapshot] = useState<ZukunftsgraphSnapshot | null>(null);
  const [viewMode, setViewMode] = useState<"map" | "graph" | "season" | "chat">("map");
  const [focusedEntityId, setFocusedEntityId] = useState<string | null>(null);
  const [mode, setMode] = useState<FilterMode>("all");
  const [window, setWindow] = useState<TimeWindow | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(requestedObservation);
  const [detail, setDetail] = useState<ObservationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshIndex, setRefreshIndex] = useState(0);
  const [draftLocation, setDraftLocation] = useState<{ lat: number | null; lon: number | null } | null>(null);
  const [pickedDraftLocation, setPickedDraftLocation] = useState<{ lat: number; lon: number; seq: number } | null>(null);
  const sideStrandRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(() => setRefreshIndex((value) => value + 1), []);
  useEffect(() => {
    const hasPendingTitles = (data?.items ?? []).some((item) => {
      const t = (item.title || "").trim().toLowerCase();
      return !t || t === "unbekannt" || t.endsWith(".jpg") || t.endsWith(".jpeg") || t.endsWith(".png") || t.endsWith(".webp");
    });
    if (!hasPendingTitles) return;
    const timer = globalThis.window.setTimeout(() => setRefreshIndex((value) => value + 1), 3000);
    return () => globalThis.window.clearTimeout(timer);
  }, [data?.items]);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api.observations(controller.signal),
      api.config().catch(() => null),
      api.zukunftsgraphSnapshot(controller.signal).catch(() => null),
    ]).then(([list, publicConfig, nextSnapshot]) => {
      setData(list); setConfig(publicConfig); setSnapshot(nextSnapshot); setError("");
      if (mode === "all") setWindow(extentToWindow(list.time_extent));
      else if (window && list.time_extent) {
        const nextExtent = extentToWindow(list.time_extent);
        if (nextExtent) setWindow(clampWindow(window, nextExtent));
      }
    }).catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Daten konnten nicht geladen werden."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // Preserve user-selected window when data reloads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshIndex, requestedObservation]);

  const visibleItems = useMemo(() => filterObservations(data?.items ?? [], mode, window), [data?.items, mode, window]);
  const visibleIds = useMemo(() => new Set(visibleItems.map((item) => item.id)), [visibleItems]);
  const selectedVisible = selectedId !== null && visibleIds.has(selectedId);

  useEffect(() => {
    if (data && selectedId && !selectedVisible) { setSelectedId(null); setDetail(null); setDetailLoading(false); }
  }, [data, selectedId, selectedVisible]);

  useEffect(() => {
    if (!selectedId || !selectedVisible) { setDetail(null); return; }
    setDetail(null);
    const controller = new AbortController();
    setDetailLoading(true);
    api.observation(selectedId, controller.signal).then((next) => { setDetail(next); setError(""); })
      .catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Funddetails konnten nicht geladen werden."); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selectedId, selectedVisible]);

  const choose = useCallback((id: string) => {
    setSelectedId(id);
    setDraftLocation(null);
    setPickedDraftLocation(null);
    if (typeof globalThis.window !== "undefined" && globalThis.window.innerWidth <= 900) {
      globalThis.window.setTimeout(() => sideStrandRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
    }
  }, []);
  const pickDraftLocation = useCallback((lat: number, lon: number) => {
    setPickedDraftLocation((prev) => ({ lat, lon, seq: (prev?.seq ?? 0) + 1 }));
  }, []);
  const saveDetail = useCallback((updated: ObservationDetail) => {
    setDetail((current) => current?.id === updated.id ? updated : current);
    const { id, title, category, environment, lat, lon, sigma_meters, sigma_seconds, observed_at, media_count, location_measurement_count, first_observed_at, last_observed_at, source, thumbnail_url } = updated;
    const summary = { id, title, category, environment, lat, lon, sigma_meters, sigma_seconds, observed_at, media_count, location_measurement_count, first_observed_at, last_observed_at, source, thumbnail_url };
    setData((current) => current ? { ...current, items: current.items.map((item) => item.id === id ? summary : item) } : current);
  }, []);
  const baseExtent = extentToWindow(data?.time_extent ?? null);
  const futureTimesMs = useMemo(
    () => (snapshot?.spatial_predictions ?? []).map((p) => p.time_ms).filter((t): t is number => typeof t === "number"),
    [snapshot?.spatial_predictions]
  );
  const timeExtent = useMemo(() => mergeFutureExtent(baseExtent, futureTimesMs), [baseExtent, futureTimesMs]);
  const refresh = useCallback(() => reload(), [reload]);
  const openGraphForEntity = useCallback((entityId: string) => {
    setFocusedEntityId(entityId);
    setViewMode("graph");
  }, []);
  const selectFromGraph = useCallback((obsId: string) => {
    setViewMode("map");
    choose(obsId);
  }, [choose]);
  const activeGoalCount = useMemo(
    () => (snapshot?.goal_evaluations ?? []).filter((g) => g.status !== "cancelled").length,
    [snapshot?.goal_evaluations]
  );

  return <main className="app-shell">
    <header className="topbar">
      <a className="wordmark" href="/" aria-label="Was Wo Wann Startseite"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>Was Wo <span className="wordmark-light">Wann</span></span></a>
      <a className="upload-nav-link" href="/upload">+ Fund hochladen</a>
      <ImportControls config={config} onImported={refresh} />
    </header>
    <div className="workspace-grid">
      <section className="map-column" aria-label="Karte und Zeitfilter">
        {error && <div className="workspace-error" role="alert"><span>{error}</span><button type="button" onClick={reload}>Erneut laden</button></div>}
        {Boolean(data?.total) && (
          <div className="workspace-view-tabs" role="tablist" aria-label="Arbeitsansichten">
            <button type="button" role="tab" aria-selected={viewMode === "map"} className={viewMode === "map" ? "is-active" : ""} onClick={() => setViewMode("map")}>Karte &amp; Archiv</button>
            <button type="button" role="tab" aria-selected={viewMode === "graph"} className={viewMode === "graph" ? "is-active" : ""} onClick={() => setViewMode("graph")}>Zukunftsgraph{activeGoalCount > 0 ? ` (${activeGoalCount})` : ""}</button>
            <button type="button" role="tab" aria-selected={viewMode === "season"} className={viewMode === "season" ? "is-active" : ""} onClick={() => setViewMode("season")}>Perioden &amp; Jahr</button>
            <button type="button" role="tab" aria-selected={viewMode === "chat"} className={viewMode === "chat" ? "is-active" : ""} onClick={() => setViewMode("chat")}>Chat &amp; Sprache</button>
          </div>
        )}
        {loading && !data ? <div className="initial-loading"><span className="loading-orbit" />Archiv wird geöffnet …</div> : !data?.total ? <div className="first-run"><span className="first-run-number">01 / START</span><h2>Hier beginnt <em>dein Archiv.</em></h2><p>Lade eigene Bilder hoch, um Karte, Zeitschieber und Funddetails direkt zu nutzen.</p><a className="first-run-upload" href="/upload">Ersten Fund hochladen <span aria-hidden="true">↗</span></a></div> : viewMode === "map" ? <ObservationMap observations={visibleItems} selectedId={selectedVisible ? selectedId : null} onSelect={choose} selectedMedia={selectedVisible && detail?.id === selectedId ? detail?.media : undefined} draftLocation={draftLocation} onDraftLocationPick={pickDraftLocation} spatialPredictions={snapshot?.spatial_predictions} /> : viewMode === "chat" ? <ChatPanel scopeType="global" scopeId="global" mode="full" snapshot={snapshot} onSnapshotChange={setSnapshot} onRefreshObservations={reload} /> : <ZukunftsgraphPanel snapshot={snapshot} view={viewMode} focusedEntityId={focusedEntityId} onSnapshotChange={setSnapshot} onSelectObservation={selectFromGraph} />}
        {viewMode === "map" && <TimeFilter mode={mode} onModeChange={setMode} extent={timeExtent} window={window} onWindowChange={setWindow} visibleCount={visibleItems.length} totalCount={data?.total ?? 0} undatedCount={mode === "window" ? (data?.undated_count ?? 0) : 0} nowMs={snapshot?.now_ms} futurePredictionCount={snapshot?.spatial_predictions?.length ?? 0} />}
      </section>
      <div className="side-strand" ref={sideStrandRef}>
        {selectedVisible ? <div className="strand-back-bar"><button type="button" className="strand-back-btn" onClick={() => { setSelectedId(null); setDetail(null); setDraftLocation(null); setPickedDraftLocation(null); }}>← Zurück zur Liste ({visibleItems.length})</button></div> : null}
        {selectedVisible ? <ObservationDetails detail={detail?.id === selectedId ? detail : null} loading={detailLoading} onSave={saveDetail} onMeasurementsChanged={reload} pickedDraftLocation={pickedDraftLocation} onDraftLocationChange={setDraftLocation} onOpenGraph={openGraphForEntity} onSnapshotChange={setSnapshot} /> : <aside className="detail-sidebar" aria-label="Beobachtungs-Strang"><ObservationList observations={visibleItems} selectedId={null} onSelect={choose} total={data?.total ?? 0} /></aside>}
      </div>
    </div>
  </main>;
}
