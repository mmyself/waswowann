"use client";

import { ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError, BrowserLocation, UploadItemResult } from "@/lib/api";
import type { ObservationDetail } from "@/lib/types";
import ObservationDetails from "./ObservationDetails";

interface SelectedFile { file: File; url: string; }
const ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif,.jpg,.jpeg,.png,.webp,.heic,.heif,.zip";

export default function UploadWorkspace() {
  const search = useSearchParams();
  const observationId = search.get("observation");
  const [files, setFiles] = useState<SelectedFile[]>([]);
  const [browserLocation, setBrowserLocation] = useState<BrowserLocation | null>(null);
  const [locationAllowed, setLocationAllowed] = useState(false);
  const [locationError, setLocationError] = useState("");
  const [groupSameLocation, setGroupSameLocation] = useState(false);
  const [results, setResults] = useState<UploadItemResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<ObservationDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const urlsRef = useRef<string[]>([]);
  useEffect(() => () => urlsRef.current.forEach((url) => URL.revokeObjectURL(url)), []);

  const loadDetail = useCallback(async (id: string, signal?: AbortSignal) => {
    try {
      const next = await api.observation(id, signal);
      setDetail(next); setDetailError("");
      return next;
    } catch (cause) {
      if (!signal?.aborted) setDetailError(cause instanceof Error ? cause.message : "Fund konnte nicht geladen werden.");
      return null;
    }
  }, []);

  useEffect(() => {
    if (!observationId) { setDetail(null); return; }
    const controller = new AbortController();
    loadDetail(observationId, controller.signal);
    return () => controller.abort();
  }, [observationId, loadDetail]);

  useEffect(() => {
    const activeId = observationId ?? results.find((item) => item.observation_id)?.observation_id;
    if (!activeId || !detail || detail.id !== activeId) return;
    const active = ["queued", "running"].includes(detail.gemini.status);
    if (!active) return;
    const timer = window.setTimeout(() => loadDetail(activeId), 1600);
    return () => window.clearTimeout(timer);
  }, [detail, observationId, results, loadDetail]);

  const addFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const chosen = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    setFiles((previous) => {
      const next = [...previous, ...chosen.map((file) => ({ file, url: URL.createObjectURL(file) }))];
      const kept = next.slice(0, 100);
      const keptUrls = new Set(kept.map((item) => item.url));
      next.filter((item) => !keptUrls.has(item.url)).forEach((item) => URL.revokeObjectURL(item.url));
      urlsRef.current = kept.map((item) => item.url);
      return kept;
    });
    setResults([]); setError("");
  };

  const removeFile = (index: number) => setFiles((previous) => {
    const removed = previous[index];
    if (removed) { URL.revokeObjectURL(removed.url); urlsRef.current = urlsRef.current.filter((url) => url !== removed.url); }
    return previous.filter((_, itemIndex) => itemIndex !== index);
  });

  const requestLocation = () => {
    setLocationError("");
    if (!navigator.geolocation) { setLocationError("Dieser Browser unterstützt keine Standortabfrage."); return; }
    navigator.geolocation.getCurrentPosition((position) => {
      setBrowserLocation({ lat: position.coords.latitude, lon: position.coords.longitude, accuracy_meters: position.coords.accuracy, captured_at: new Date(position.timestamp).toISOString() });
      setLocationAllowed(true);
    }, () => { setBrowserLocation(null); setLocationAllowed(false); setLocationError("Standort nicht verfügbar. Du kannst trotzdem ohne Browser-Standort hochladen."); }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 });
  };

  const upload = async () => {
    if (!files.length) return;
    setBusy(true); setError(""); setResults([]);
    try {
      const response = await api.uploadFiles(
        files.map(({ file }) => file),
        Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        locationAllowed ? browserLocation ?? undefined : undefined,
        { groupSameLocation }
      );
      setResults(response.items);
      const first = response.items.find((item) => item.observation_id);
      if (first?.observation_id) await loadDetail(first.observation_id);
      urlsRef.current.forEach((url) => URL.revokeObjectURL(url)); urlsRef.current = [];
      setFiles([]);
    } catch (cause) {
      setError(cause instanceof ApiError ? `${cause.message}${cause.code ? ` (${cause.code})` : ""}` : cause instanceof Error ? cause.message : "Upload fehlgeschlagen.");
    } finally { setBusy(false); }
  };

  const activeObservation = observationId ?? results.find((item) => item.observation_id)?.observation_id;
  const selectedObservation = activeObservation && detail?.id === activeObservation ? detail : null;

  return <main className="app-shell upload-shell">
    <header className="topbar upload-topbar"><a className="wordmark" href="/" aria-label="Wann Wo Was Startseite"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>Wann Wo <span className="wordmark-light">Was</span></span></a><a className="upload-nav-link" href="/">← Zur Karte</a></header>
    <div className="upload-intro"><p className="eyebrow">Neuer Eintrag</p><h1>Ein Augenblick,<br /><em>festgehalten.</em></h1><p>Fotos auswählen, automatisch einordnen lassen und Fundangaben ergänzen. Originale werden nur für diesen Upload im Browser gehalten.</p></div>
    {!observationId && <section className="upload-card" aria-labelledby="upload-heading">
      <div className="upload-card-heading"><div><p className="eyebrow">01 / AUSWÄHLEN</p><h2 id="upload-heading">Deine Bilder</h2></div><span className="upload-limit">Bis zu 250 Bilder · Google-Fotos-ZIP möglich</span></div>
      <div className="upload-pickers"><button type="button" className="upload-choice" onClick={() => inputRef.current?.click()}><span className="upload-choice-icon">▧</span><strong>Dateien / Fotos auswählen</strong><small>Bilder, ZIP-Archiv (mit GPS) oder Kamera über Systemauswahl</small></button></div>
      <input ref={inputRef} className="sr-only" type="file" accept={ACCEPT} multiple onChange={addFiles} aria-label="Fotos oder ZIP-Datei auswählen" />
      {files.length > 0 && <div className="upload-preview-list" aria-label="Ausgewählte Dateien">{files.map(({ file, url }, index) => <article className="upload-preview" key={`${file.name}-${file.lastModified}-${index}`}>{file.type.startsWith("image/") || /\.(jpe?g|png|webp|heic|heif)$/i.test(file.name) ? <img src={url} alt="" /> : <span className="zip-preview">{file.name.toLowerCase().endsWith(".zip") ? "ZIP" : "DATEI"}</span>}<div><strong>{file.name}</strong><small>{formatBytes(file.size)}</small></div><button type="button" aria-label={`${file.name} entfernen`} onClick={() => removeFile(index)}>×</button></article>)}</div>}
      {files.length > 1 && <label className="location-confirm"><input type="checkbox" checked={groupSameLocation} onChange={(event) => setGroupSameLocation(event.target.checked)} />Diese Fotos zeigen dasselbe Objekt. Bilder, die am selben Ort aufgenommen wurden, in einem Fund zusammenfassen.</label>}
      <div className="location-consent"><div><p className="eyebrow">Optionaler Standort</p><strong>Geräteposition als mögliche Ortsangabe</strong><p>Das ist dein aktueller Standort, nicht zwingend der historische Aufnahmeort. EXIF-Ortsdaten haben Vorrang; bestätige die Verwendung für Fotos ohne EXIF-Ort.</p></div><button type="button" className="secondary-action" onClick={requestLocation}>{browserLocation ? "Standort erfasst" : "Standort erlauben"}</button></div>
      {browserLocation && <label className="location-confirm"><input type="checkbox" checked={locationAllowed} onChange={(event) => setLocationAllowed(event.target.checked)} />Für ausgewählte Fotos ohne EXIF-Ort den aktuellen Gerätestandort als Vorschlag mitsenden ({browserLocation.lat.toFixed(4)}, {browserLocation.lon.toFixed(4)}; Genauigkeit ca. {Math.round(browserLocation.accuracy_meters)} m). Ich bestätige, dass dies nicht automatisch der Aufnahmeort sein muss.</label>}
      {locationError && <p className="form-error" role="status">{locationError}</p>}{error && <p className="form-error" role="alert">{error}</p>}
      <button type="button" className="upload-submit" onClick={upload} disabled={busy || !files.length}>{busy ? "Wird hochgeladen …" : `Hochladen und einordnen${files.length ? ` · ${files.length}` : ""}`}<span aria-hidden="true">↗</span></button>
      {results.length > 0 && <section className="upload-results" aria-live="polite"><h3>Verarbeitung pro Datei</h3>{results.map((item, index) => <div className="upload-result" key={`${item.filename}-${index}`}><span className={`result-status result-status--${item.result}`} aria-hidden="true" /> <strong>{item.filename}</strong><span>{item.result === "imported" ? "Hinzugefügt" : item.result === "duplicate" ? "Bereits vorhanden" : item.error_code ?? "Fehler"}</span>{item.observation_id && <a href={`/upload?observation=${encodeURIComponent(item.observation_id)}`}>Fund öffnen ↗</a>}</div>)}</section>}
    </section>}
    {observationId && detailError && <div className="workspace-error" role="alert">{detailError}<button type="button" onClick={() => loadDetail(observationId)}>Erneut laden</button></div>}
    {observationId && !selectedObservation && <div className="initial-loading"><span className="loading-orbit" />Fund wird geöffnet …</div>}
    {selectedObservation && <section className="uploaded-observation"><div className="uploaded-observation-top"><div><p className="eyebrow">02 / BESTIMMUNG & DETAILS</p><h2>Dein Fund</h2></div><a className="map-action" href={`/?observation=${encodeURIComponent(selectedObservation.id)}`}>Auf Karte anzeigen <span aria-hidden="true">↗</span></a></div><p className="analysis-caveat">Die Bildeinordnung ist ein KI-Vorschlag, keine sichere Bestimmung oder Verzehrfreigabe. Prüfe Angaben sorgfältig.</p><ObservationDetails detail={selectedObservation} loading={false} onSave={(next) => setDetail(next)} /></section>}
    {activeObservation && selectedObservation && <footer className="upload-next"><span>Weitere Bilder hinzufügen?</span><a href="/upload">Neuen Upload starten ↗</a></footer>}
  </main>;
}

function formatBytes(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`; }
