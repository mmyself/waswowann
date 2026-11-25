"use client";

import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { api, ApiError, mediaUrl } from "@/lib/api";
import { CATEGORIES, MediaItem, MediaMeasurementPatch, MergeCandidate, ObservationDetail, WorldlineInspection, ZukunftsgraphSnapshot } from "@/lib/types";
import { DISPLAY_TIMEZONE, formatDate, formatDateSpan, formatPhotoDate, fromLocalInput, toLocalInput } from "@/lib/time-filter";
import { formatMetaDate, goalStatusLabel } from "@/lib/zukunftsgraph";
import AnalysisPanel from "./AnalysisPanel";

const MiniLocationPicker = dynamic(() => import("./MiniLocationPicker"), { ssr: false });
const MANUAL_SIGMA_METERS = "5.3";
const meters = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

type DraftLocation = { lat: number | null; lon: number | null };
interface Props {
  detail: ObservationDetail | null;
  loading: boolean;
  onSave: (detail: ObservationDetail) => void;
  onMeasurementsChanged?: () => void;
  pickedDraftLocation?: { lat: number; lon: number; seq: number } | null;
  onDraftLocationChange?: (coords: DraftLocation | null) => void;
  onOpenGraph?: (entityId: string) => void;
  onSnapshotChange?: (snapshot: ZukunftsgraphSnapshot) => void;
}

type PhotoForm = { lat: string; lon: string; observedAt: string; sigmaMeters: string; sigmaSeconds: string };

export default function ObservationDetails({ detail, loading, ...props }: Props) {
  if (loading) return <aside className="detail-sidebar"><div className="detail-loading"><span className="loading-orbit" /><p>Funddetails werden geöffnet …</p></div></aside>;
  if (!detail) return <aside className="detail-sidebar"><div className="detail-empty"><span className="empty-coordinates">52° · 13°</span><p className="eyebrow">Dein Archiv</p><h2>Ein Fund.<br />Eine Geschichte.</h2><p>Wähle einen Punkt auf der Karte oder einen Eintrag in der Liste, um die Geschichte dahinter zu öffnen.</p><div className="empty-rule"><span>01</span><span>Ort, Zeit und Erinnerung</span></div></div></aside>;
  return <DetailContent key={detail.id} detail={detail} {...props} />;
}

function DetailContent({ detail, onSave, onMeasurementsChanged, pickedDraftLocation, onDraftLocationChange, onOpenGraph, onSnapshotChange }: Omit<Props, "detail" | "loading"> & { detail: ObservationDetail }) {
  const [activeMediaId, setActiveMediaId] = useState<string | null>(null);
  const [editingObject, setEditingObject] = useState(false);
  const [editingPhoto, setEditingPhoto] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [photoError, setPhotoError] = useState("");
  const [objectError, setObjectError] = useState("");
  const [uploadError, setUploadError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [candidates, setCandidates] = useState<MergeCandidate[]>([]);
  const [mergeError, setMergeError] = useState("");
  const [confirmMergeId, setConfirmMergeId] = useState<string | null>(null);
  const [worldline, setWorldline] = useState<WorldlineInspection | null>(null);
  const [showGoalForm, setShowGoalForm] = useState(false);
  const [goalLabel, setGoalLabel] = useState("");
  const [goalProp, setGoalProp] = useState("presence");
  const [goalMin, setGoalMin] = useState("1");
  const [goalRadius, setGoalRadius] = useState("");
  const [goalError, setGoalError] = useState("");
  const addPhotoInputRef = useRef<HTMLInputElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const { id, media, lat, lon, sigma_meters, location_measurement_count, first_observed_at, last_observed_at } = detail;
  const mediaCount = media.length;
  const activeIndex = Math.max(0, media.findIndex((item) => item.id === activeMediaId));
  const photo = media[activeIndex];
  const categoryColor = detail.category === "Pilz" ? "rust" : detail.category === "Pflanze" ? "leaf" : "ochre";
  const note = detail.description.trim();
  const geminiText = (detail.gemini.result?.description ?? (!detail.gemini.text.trim().startsWith("{") ? detail.gemini.text : "")).trim();

  useEffect(() => {
    if (lat === null || lon === null) { setCandidates([]); return; }
    const controller = new AbortController();
    api.mergeCandidates(id, controller.signal).then(({ items }) => setCandidates(items)).catch(() => { if (!controller.signal.aborted) setCandidates([]); });
    return () => controller.abort();
  }, [id, lat, lon, detail.updated_at]);

  useEffect(() => {
    const controller = new AbortController();
    api.worldline(id, controller.signal).then((wl) => setWorldline(wl)).catch(() => { if (!controller.signal.aborted) setWorldline(null); });
    return () => controller.abort();
  }, [id, detail.updated_at]);

  useEffect(() => {
    const selected = timelineRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]');
    if (!selected || !timelineRef.current) return;
    const strip = timelineRef.current;
    const viewport = strip.getBoundingClientRect();
    const tile = selected.getBoundingClientRect();
    if (tile.left < viewport.left) strip.scrollLeft += tile.left - viewport.left;
    if (tile.right > viewport.right) strip.scrollLeft += tile.right - viewport.right;
  }, [photo?.id, activeIndex]);

  const choosePhoto = (id: string) => {
    setActiveMediaId(id);
    setEditingPhoto(false);
    setConfirmDelete(false);
    setPhotoError("");
  };

  const addPhotos = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    if (!files.length) return;
    setUploading(true);
    setUploadError("");
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      const { items } = await api.uploadFiles(files, tz, undefined, { targetObservationId: id });
      const failed = items.find((item) => item.result === "error");
      if (failed) setUploadError(`Nicht alle Fotos übernommen (${failed.error_code ?? "Fehler"}).`);
      const updated = await api.observation(id);
      const added = updated.media.find((item) => items.some((result) => result.result === "imported" && result.media_id === item.id));
      if (added) choosePhoto(added.id);
      onSave(updated);
      onMeasurementsChanged?.();
    } catch (cause) {
      setUploadError(cause instanceof ApiError ? cause.message : "Weitere Fotos konnten nicht hochgeladen werden.");
    } finally { setUploading(false); }
  };

  const saveObject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const title = String(data.get("title") ?? "").trim();
    if (!title) { setObjectError("Ein Titel ist erforderlich."); return; }
    setBusy(true);
    setObjectError("");
    try {
      onSave(await api.updateObservation(id, { ...Object.fromEntries(data), title }));
      setEditingObject(false);
    } catch (cause) {
      setObjectError(cause instanceof ApiError ? cause.message : "Der Fund konnte nicht gespeichert werden.");
    } finally { setBusy(false); }
  };

  const savePhoto = async (patch: MediaMeasurementPatch) => {
    setPhotoError("");
    if (!Object.keys(patch).length) { setEditingPhoto(false); return; }
    setBusy(true);
    try {
      onSave(await api.updateMedia(photo.id, patch));
      setEditingPhoto(false);
      onMeasurementsChanged?.();
    } catch (cause) {
      setPhotoError(cause instanceof ApiError ? cause.message : "Die Fotodaten konnten nicht gespeichert werden.");
    } finally { setBusy(false); }
  };

  const deletePhoto = async () => {
    setBusy(true);
    setPhotoError("");
    try {
      const updated = await api.deleteMedia(photo.id);
      choosePhoto(updated.media[Math.min(activeIndex, updated.media.length - 1)]?.id ?? "");
      onSave(updated);
      onMeasurementsChanged?.();
    } catch (cause) {
      setPhotoError(cause instanceof ApiError ? cause.message : "Das Foto konnte nicht gelöscht werden.");
    } finally { setBusy(false); }
  };

  const mergeObject = async (source: MergeCandidate) => {
    setBusy(true);
    setMergeError("");
    try {
      onSave(await api.mergeObservation(id, [source.observation_id]));
      setCandidates((items) => items.filter((item) => item.observation_id !== source.observation_id));
      setConfirmMergeId(null);
      onMeasurementsChanged?.();
    } catch (cause) {
      setMergeError(cause instanceof ApiError ? cause.message : "Zusammenführung fehlgeschlagen.");
    } finally { setBusy(false); }
  };

  const submitGoal = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!goalLabel.trim()) { setGoalError("Bitte eine Zielbeschreibung angeben."); return; }
    setBusy(true);
    setGoalError("");
    try {
      const targetMinNum = optionalNumber(goalMin);
      const targetRadNum = optionalNumber(goalRadius);
      const res = await api.createMetaGoal({
        entity_id_or_obs_id: id,
        property_name: goalProp.trim() || "presence",
        label: goalLabel.trim(),
        target_min: targetMinNum ?? undefined,
        target_radius_meters: targetRadNum ?? undefined,
      });
      onSnapshotChange?.(res.snapshot);
      const refreshed = await api.worldline(id);
      setWorldline(refreshed);
      setGoalLabel("");
      setShowGoalForm(false);
    } catch (cause) {
      setGoalError(cause instanceof ApiError ? cause.message : "Ziel konnte nicht gespeichert werden.");
    } finally { setBusy(false); }
  };

  return <aside className="detail-sidebar" aria-label="Funddetails">
    <div className="detail-content">
      <div className="detail-toolbar">
        <div className="detail-breadcrumb"><span>FUND</span><span className="breadcrumb-separator">/</span><span>{id.slice(0, 8)}</span></div>
        <button type="button" className={`edit-toggle ${editingObject ? "is-editing" : ""}`} disabled={busy} onClick={() => { setEditingObject((value) => !value); setObjectError(""); }}>{editingObject ? "Schließen" : "Fund bearbeiten"}</button>
      </div>
      <div className="detail-title-block">
        <div className="title-meta"><span className={`category-chip category-chip--${categoryColor}`}>{detail.category}</span>{detail.environment === "indoor" ? <span>Innenraum</span> : detail.environment === "outdoor" ? <span>Im Freien</span> : null}</div>
        <h1>{detail.title || "Unbenannter Fund"}</h1>
        {note && note !== geminiText && <p className="detail-description">{note}</p>}
      </div>
      {editingObject && <form className="edit-form" onSubmit={saveObject}>
        <div className="edit-heading"><p className="eyebrow">Fund bearbeiten</p></div>
        <label>Titel<input name="title" maxLength={200} required defaultValue={detail.title} /></label>
        <div className="form-two-col">
          <label>Kategorie<select name="category" defaultValue={detail.category}>{CATEGORIES.map((category) => <option key={category}>{category}</option>)}</select></label>
          <label>Umgebung<select name="environment" defaultValue={detail.environment ?? "unknown"}><option value="unknown">Unbekannt</option><option value="indoor">Innenraum / Wohnung</option><option value="outdoor">Im Freien</option></select></label>
        </div>
        <label>Beschreibung<textarea name="description" maxLength={10000} rows={3} defaultValue={detail.description} /></label>
        {objectError && <p className="form-error" role="alert">{objectError}</p>}
        <button type="submit" className="save-button" disabled={busy}>Fund speichern<span aria-hidden="true">↗</span></button>
      </form>}
      <section className="detail-card object-location" aria-label="Kalman-Fundort">
        <div className="section-heading"><div><p className="eyebrow">Messungen am Objekt</p><h3>Kalman-Fundort</h3></div><span className="soon-badge">{location_measurement_count} GPS-{location_measurement_count === 1 ? "Messung" : "Messungen"}{(detail.outlier_count ?? 0) > 0 ? ` · ${detail.outlier_count} Ausreißer gedämpft` : ""}</span></div>
        <dl className="measurement-facts">
          <div><dt>Position</dt><dd>{coordsLabel(lat, lon)}</dd></div>
          <div><dt>Unsicherheit (σ)</dt><dd>{sigma_meters === null ? "Unbekannt" : `±${meters.format(sigma_meters)} m`}</dd></div>
          <div><dt>Aufnahmezeitraum</dt><dd>{formatDateSpan(first_observed_at, last_observed_at)}</dd></div>
        </dl>
      </section>
      <section className="photo-timeline" aria-label="Foto-Zeitleiste">
        <div className="timeline-heading"><div><p className="eyebrow">Foto-Zeitleiste</p><h2>{mediaCount} {mediaCount === 1 ? "Foto" : "Fotos"}</h2></div>
          <button type="button" className="add-photo-button" disabled={uploading || busy} onClick={() => addPhotoInputRef.current?.click()}>{uploading ? "Lädt …" : "+ Foto hinzufügen"}</button>
          <input ref={addPhotoInputRef} className="sr-only" type="file" multiple accept="image/jpeg,image/png,image/webp,image/heic,image/heif,.jpg,.jpeg,.png,.webp,.heic,.heif,.zip" onChange={addPhotos} aria-label="Weitere Fotos zu diesem Fund hinzufügen" />
        </div>
        {uploadError && <p className="form-error" role="alert">{uploadError}</p>}
        <div className="photo-frame">
          {photo ? <>
            <img className="detail-photo" src={mediaUrl(photo.image_url) ?? ""} alt={`Foto ${activeIndex + 1} von ${mediaCount}: ${detail.title}`} />
            {mediaCount > 1 && <>
              <button type="button" className="photo-nav photo-nav--prev" disabled={busy} aria-label="Vorheriges Foto" onClick={() => choosePhoto(media[(activeIndex - 1 + mediaCount) % mediaCount].id)}>‹</button>
              <button type="button" className="photo-nav photo-nav--next" disabled={busy} aria-label="Nächstes Foto" onClick={() => choosePhoto(media[(activeIndex + 1) % mediaCount].id)}>›</button>
            </>}
            <a className="photo-open" href={mediaUrl(photo.image_url) ?? "#"} target="_blank" rel="noreferrer" aria-label="Foto in neuem Tab öffnen">↗</a>
            <div className="photo-caption"><span>{`${activeIndex + 1}/${mediaCount} · ${photo.filename}`}</span><span>{photo.width} × {photo.height}</span></div>
          </> : <div className="photo-placeholder"><span className="photo-placeholder__symbol" aria-hidden="true">✳</span><span>Kein Foto zu diesem Fund</span></div>}
        </div>
        {mediaCount > 0 && <div className="photo-thumbs" ref={timelineRef} role="group" aria-label="Fotos in zeitlicher Reihenfolge">
          {media.map((item) => <button key={item.id} type="button" disabled={busy} title={item.kalman_outlier ? `GPS-Ausreißer (${meters.format(item.kalman_distance_meters ?? 0)} m Abweichung)` : undefined} aria-label={`${formatPhotoDate(item.observed_at_ms, "d. LLL yyyy, HH:mm")}: ${item.filename}${item.kalman_outlier ? " (GPS-Ausreißer)" : ""}`} aria-pressed={item.id === photo.id} className={`photo-thumb ${item.id === photo.id ? "is-active" : ""}`} onClick={() => choosePhoto(item.id)}>
            <img src={mediaUrl(item.thumbnail_url) ?? ""} alt="" /><span>{formatPhotoDate(item.observed_at_ms)}{item.kalman_outlier ? " !" : ""}</span>
          </button>)}
        </div>}
      </section>
      {photo && <section className="detail-card photo-measurement" aria-label="Messwerte des ausgewählten Fotos">
        <div className="section-heading"><div><p className="eyebrow">Einzelmessung · Foto {activeIndex + 1}</p><h3>Ort & Aufnahmezeit</h3></div></div>
        <dl className="measurement-facts">
          <div><dt>Foto-GPS</dt><dd>{coordsLabel(photo.lat, photo.lon)}{photo.lat !== null && <small>{photo.sigma_meters !== null && `±${meters.format(photo.sigma_meters)} m`}{photo.effective_sigma_meters && photo.sigma_meters && photo.effective_sigma_meters > photo.sigma_meters ? ` (effektiv ±${meters.format(photo.effective_sigma_meters)} m, Innenraum)` : ""}{` · ${sourceName(photo.location_source)}`}{photo.kalman_outlier && ` · GPS-Ausreißer (${meters.format(photo.kalman_distance_meters ?? 0)} m Abweichung · ${Math.round((photo.kalman_weight ?? 0) * 100)} % Gewicht)`}</small>}</dd></div>
          <div><dt>Aufnahmezeit <span className="label-hint">{DISPLAY_TIMEZONE}</span></dt><dd>{formatPhotoDate(photo.observed_at_ms, "d. LLL yyyy, HH:mm:ss")}{photo.observed_at_ms !== null && <small>{photo.sigma_seconds !== null && `±${photo.sigma_seconds.toLocaleString("de-DE")} s · `}{sourceName(photo.time_source, "Übernommene Aufnahmezeit")}</small>}</dd></div>
        </dl>
        <div className="photo-measurement-actions">
          <button type="button" className="secondary-action" disabled={busy || uploading} onClick={() => { setEditingPhoto((value) => !value); setConfirmDelete(false); setPhotoError(""); }}>{editingPhoto ? "Bearbeitung schließen" : "Foto-Messwerte bearbeiten"}</button>
          {mediaCount > 1 && <button type="button" className="text-action" disabled={busy || uploading} onClick={() => { setConfirmDelete(true); setEditingPhoto(false); setPhotoError(""); }}>Foto löschen</button>}
        </div>
        {editingPhoto && <PhotoEditor key={photo.id} photo={photo} busy={busy} onSubmit={savePhoto} pickedDraftLocation={pickedDraftLocation} onDraftLocationChange={onDraftLocationChange} />}
        {confirmDelete && <div className="action-confirm" role="group" aria-label="Foto löschen bestätigen">
          <span>„{photo.filename}“ dauerhaft löschen? Der Kalman-Fundort wird neu berechnet.</span>
          <div><button type="button" className="confirm-button" disabled={busy} onClick={deletePhoto}>Endgültig löschen</button><button type="button" className="text-action" disabled={busy} onClick={() => setConfirmDelete(false)}>Abbrechen</button></div>
        </div>}
        {photoError && <p className="form-error" role="alert">{photoError}</p>}
        {Object.keys(photo.exif).length > 0 && <details className="exif-details" key={photo.id}><summary>Bildinformationen<span>{photo.source}</span></summary><div className="exif-grid">{Object.entries(photo.exif).map(([key, value]) => <div key={key}><span>{key.replaceAll("_", " ")}</span><strong>{value ?? "Unbekannt"}</strong></div>)}</div></details>}
      </section>}
      <AnalysisPanel detail={detail} onSave={onSave} />
      {worldline && <section className="detail-card zukunfts-worldline" aria-label="Zukunftsgraph-Weltlinie">
        <div className="section-heading">
          <div><p className="eyebrow">MetaCore Weltlinie</p><h3>Zukunftsgraph &amp; Ziele</h3></div>
          <span className="soon-badge">{worldline.history_observed.length} Ist · {worldline.future_predicted.length} Prognose</span>
        </div>
        {worldline.future_predicted.length > 0 && (
          <div className="worldline-list">
            {worldline.future_predicted.map((pred) => (
              <div key={pred.id} className="worldline-item is-predicted">
                <strong>{pred.label}</strong>
                <small>
                  {pred.value_num !== null ? `${pred.value_num.toFixed(1)}${pred.value_sigma ? ` ± ${pred.value_sigma.toFixed(1)}` : ""} ${pred.value_unit}` : ""}
                  {pred.time_ms ? ` · ${formatMetaDate(pred.time_ms)}` : ""}
                </small>
              </div>
            ))}
          </div>
        )}
        {worldline.goal_evaluations.length > 0 && (
          <div className="worldline-goals">
            {worldline.goal_evaluations.map((ev) => (
              <div key={ev.goal_id} className={`worldline-goal is-${ev.status}`}>
                <span className={`zg-stage-pill is-${ev.status}`}>{goalStatusLabel(ev.status)}</span>
                <strong>{ev.label} ({Math.round(ev.goal_probability * 100)} %)</strong>
              </div>
            ))}
          </div>
        )}
        <div className="worldline-actions">
          {onOpenGraph && <button type="button" className="secondary-action" onClick={() => onOpenGraph(worldline.entity.id)}>Im Zukunftsgraphen öffnen ↗</button>}
          <button type="button" className="text-action" onClick={() => setShowGoalForm((v) => !v)}>{showGoalForm ? "Abbrechen" : "+ Neues Ziel setzen"}</button>
        </div>
        {showGoalForm && (
          <form className="edit-form worldline-goal-form" onSubmit={submitGoal}>
            <label>Ziel-Titel<input value={goalLabel} onChange={(e) => setGoalLabel(e.target.value)} placeholder="z. B. Nachfolge-Fund prüfen" required /></label>
            <div className="form-two-col">
              <label>Eigenschaft<input value={goalProp} onChange={(e) => setGoalProp(e.target.value)} /></label>
              <label>Min-Wert<input inputMode="decimal" value={goalMin} onChange={(e) => setGoalMin(e.target.value)} /></label>
            </div>
            <label>Zielradius (m, optional)<input inputMode="decimal" value={goalRadius} onChange={(e) => setGoalRadius(e.target.value)} placeholder="z. B. 50" /></label>
            {goalError && <p className="form-error" role="alert">{goalError}</p>}
            <button type="submit" className="save-button" disabled={busy}>Ziel anlegen <span aria-hidden="true">↗</span></button>
          </form>
        )}
      </section>}
      {candidates.length > 0 && <section className="detail-card merge-candidates" aria-label="Nahe gelegene Funde zusammenführen">
        <div className="section-heading"><div><p className="eyebrow">Nahe gelegene Funde</p><h3>Dasselbe Objekt?</h3></div><span className="soon-badge">{candidates.length}</span></div>
        <p className="placeholder-copy">Führe nur Einträge zusammen, die wirklich dasselbe Objekt zeigen.</p>
        <div className="merge-candidate-list">
          {candidates.map((candidate) => <article key={candidate.observation_id} className="merge-candidate-card">
            <div className="merge-candidate-meta"><strong>{candidate.title}</strong><small>{candidate.category} · {meters.format(candidate.distance_meters)} m entfernt · {candidate.media_count} {candidate.media_count === 1 ? "Foto" : "Fotos"}</small><small>{formatDate(candidate.observed_at)}</small></div>
            {confirmMergeId !== candidate.observation_id ? <button type="button" className="secondary-action" disabled={busy || uploading} onClick={() => { setConfirmMergeId(candidate.observation_id); setMergeError(""); }}>Zusammenführen</button> : <div className="action-confirm" role="group" aria-label="Zusammenführung bestätigen">
              <span>Fotos und Analysen von „{candidate.title}“ werden hier übernommen. Der separate Eintrag entfällt.</span>
              <div><button type="button" className="confirm-button" disabled={busy} onClick={() => mergeObject(candidate)}>Funde zusammenführen</button><button type="button" className="text-action" disabled={busy} onClick={() => setConfirmMergeId(null)}>Abbrechen</button></div>
            </div>}
          </article>)}
        </div>
        {mergeError && <p className="form-error" role="alert">{mergeError}</p>}
      </section>}
      <footer className="detail-footer"><span>Erfasst {formatDate(detail.created_at)}</span><span>·</span><span>Zuletzt geändert {formatDate(detail.updated_at)}</span></footer>
    </div>
  </aside>;
}

function PhotoEditor({ photo, busy, onSubmit, pickedDraftLocation, onDraftLocationChange }: Pick<Props, "pickedDraftLocation" | "onDraftLocationChange"> & { photo: MediaItem; busy: boolean; onSubmit: (patch: MediaMeasurementPatch) => void }) {
  const [original] = useState<PhotoForm>(() => ({
    lat: photo.lat?.toString() ?? "", lon: photo.lon?.toString() ?? "",
    observedAt: photo.observed_at_ms === null ? "" : toLocalInput(photo.observed_at_ms, DISPLAY_TIMEZONE, true),
    sigmaMeters: photo.sigma_meters?.toString() ?? "", sigmaSeconds: photo.sigma_seconds?.toString() ?? "",
  }));
  const [form, setForm] = useState(original);
  const [error, setError] = useState("");
  const lastPick = useRef(pickedDraftLocation);
  const update = (key: keyof PhotoForm, value: string) => setForm((old) => ({ ...old, [key]: value }));
  const pick = (lat: number, lon: number) => setForm((old) => ({ ...old, lat: lat.toFixed(6), lon: lon.toFixed(6), sigmaMeters: old.sigmaMeters || MANUAL_SIGMA_METERS }));
  const lat = optionalNumber(form.lat);
  const lon = optionalNumber(form.lon);
  const validCoords = lat !== null && lon !== null && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;

  useEffect(() => {
    if (pickedDraftLocation && pickedDraftLocation !== lastPick.current) {
      lastPick.current = pickedDraftLocation;
      pick(pickedDraftLocation.lat, pickedDraftLocation.lon);
    }
  }, [pickedDraftLocation]);
  useEffect(() => {
    onDraftLocationChange?.(validCoords ? { lat, lon } : { lat: null, lon: null });
  }, [validCoords, lat, lon, onDraftLocationChange]);
  useEffect(() => () => onDraftLocationChange?.(null), [onDraftLocationChange]);

  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    if (!validCoords && (lat !== null || lon !== null)) { setError("Breiten- und Längengrad bitte gemeinsam und als gültige Koordinaten angeben oder leeren."); return; }
    const timeChanged = form.observedAt !== original.observedAt;
    const time = timeChanged ? fromLocalInput(form.observedAt) : photo.observed_at_ms;
    if (form.observedAt && time === null) { setError("Diese Ortszeit ist ungültig oder liegt in einer mehrdeutigen Zeitumstellung."); return; }
    const sigmaMeters = optionalNumber(form.sigmaMeters);
    const sigmaSeconds = optionalNumber(form.sigmaSeconds);
    const patch: MediaMeasurementPatch = {};
    if (lat !== photo.lat || lon !== photo.lon) { patch.lat = lat; patch.lon = lon; }
    if (time !== photo.observed_at_ms) patch.observed_at_ms = time;
    if (lat !== null && sigmaMeters !== photo.sigma_meters) patch.sigma_meters = sigmaMeters;
    else if (patch.lat != null && sigmaMeters === null) patch.sigma_meters = Number(MANUAL_SIGMA_METERS);
    if (time !== null && sigmaSeconds !== photo.sigma_seconds) patch.sigma_seconds = sigmaSeconds;
    onSubmit(patch);
  };

  return <form className="edit-form photo-editor" onSubmit={save}>
    {onDraftLocationChange ? <p className="location-picker-hint">Klicke auf die Karte oder ziehe den Marker zum Aufnahmeort dieses Fotos.</p> : <MiniLocationPicker lat={validCoords ? lat : null} lon={validCoords ? lon : null} onPick={pick} />}
    <div className="form-two-col"><label>Breitengrad<input inputMode="decimal" value={form.lat} onChange={(event) => update("lat", event.target.value)} placeholder="z. B. 52.5200" /></label><label>Längengrad<input inputMode="decimal" value={form.lon} onChange={(event) => update("lon", event.target.value)} placeholder="z. B. 13.4050" /></label></div>
    <label>Aufnahmezeit <span className="label-hint">{DISPLAY_TIMEZONE}</span><input type="datetime-local" step="0.001" value={form.observedAt} onChange={(event) => update("observedAt", event.target.value)} /></label>
    <div className="form-two-col"><label>Ort ± Meter<input type="number" min="0.01" step="any" value={form.sigmaMeters} onChange={(event) => update("sigmaMeters", event.target.value)} placeholder="nicht angegeben" /></label><label>Zeit ± Sekunden<input type="number" min="0.01" step="any" value={form.sigmaSeconds} onChange={(event) => update("sigmaSeconds", event.target.value)} placeholder="nicht angegeben" /></label></div>
    {error && <p className="form-error" role="alert">{error}</p>}
    <button type="submit" className="save-button" disabled={busy}>{busy ? "Speichert …" : "Foto-Messwerte speichern"}<span aria-hidden="true">↗</span></button>
  </form>;
}

function coordsLabel(lat: number | null, lon: number | null): string {
  return lat === null || lon === null ? "Ort unbekannt" : `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

function optionalNumber(value: string): number | null {
  return value.trim() ? Number(value.replace(",", ".")) : null;
}

function sourceName(source: string, inherited = "Übernommener Fundort"): string {
  return ({ exif: "EXIF", google: "Google Photos", manual: "Manuell ergänzt", demo: "Demodaten", browser: "Browser-GPS", legacy: inherited, unknown: "Quelle unbekannt" } as Record<string, string>)[source] ?? source;
}
