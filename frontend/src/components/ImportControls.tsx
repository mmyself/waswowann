"use client";

import { ChangeEvent, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { ImportJob, PublicConfig } from "@/lib/types";

interface Props {
  config: PublicConfig | null;
  onImported: () => void;
}

export default function ImportControls({ config, onImported }: Props) {
  const [job, setJob] = useState<ImportJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const zipInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    let active = true;
    const poll = async () => {
      try {
        const next = await api.importJob(job.id);
        if (!active) return;
        setJob(next);
        if (["completed", "partial", "failed"].includes(next.status)) {
          setMessage(`${next.imported} importiert · ${next.skipped} übersprungen · ${next.failed} fehlgeschlagen`);
          onImported();
        } else timer = setTimeout(poll, 1200);
      } catch (cause) { if (active) setError(errorText(cause)); }
    };
    let timer: ReturnType<typeof setTimeout>;
    timer = setTimeout(poll, 800);
    return () => { active = false; clearTimeout(timer); };
  }, [job, onImported]);

  const startLocal = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api.startLocalImport();
      setJob({ id: result.job_id, source: "local", status: "queued", total: null, processed: 0, imported: 0, skipped: 0, failed: 0, errors: [] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const uploadGoogleZip = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    setBusy(true); setError(""); setMessage("Google-Fotos-ZIP wird entpackt und einzeln an Gemini gesendet …");
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      const response = await api.uploadFiles([file], tz, undefined, { groupSameLocation: false });
      const imported = response.items.filter((item) => item.result === "imported").length;
      const skipped = response.items.filter((item) => item.result === "duplicate").length;
      const failed = response.items.filter((item) => item.result === "error").length;
      setMessage(
        `${imported} ${imported === 1 ? "Foto" : "Fotos"} einzeln importiert` +
          (skipped ? ` · ${skipped} bereits vorhanden` : "") +
          (failed ? ` · ${failed} fehlgeschlagen` : "") +
          (imported > 0 ? " · KI-Einordnung läuft" : "")
      );
      onImported();
    } catch (cause) {
      setMessage("");
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  };

  return <section className="import-controls" aria-label="Importieren">
    <div className="import-label"><span className="import-spark" aria-hidden="true">+</span><div><p className="eyebrow">Datenquellen</p><strong>Funde hinzufügen</strong></div></div>
    <button type="button" className="source-button" disabled={busy || !config?.local_import_enabled} onClick={startLocal}><span className="source-icon source-icon--outline">01</span><span><strong>Lokalen Ordner importieren</strong><small>{config?.local_import_enabled ? "Konfigurierter Ordner" : "Backend-Ordner nicht eingerichtet"}</small></span><span aria-hidden="true">↗</span></button>
    <button type="button" className="source-button" disabled={busy} onClick={() => zipInputRef.current?.click()}><span className="source-icon source-icon--outline">02</span><span><strong>Google-Fotos-ZIP</strong><small>Fotos einzeln + an Gemini</small></span><span aria-hidden="true">↗</span></button>
    <input ref={zipInputRef} className="sr-only" type="file" accept=".zip,application/zip,application/x-zip-compressed" onChange={uploadGoogleZip} aria-label="Google-Fotos-ZIP-Datei hochladen" />
    {job && <div className="job-progress" role="status"><span className="job-status">{job.status === "running" ? "Import läuft" : job.status === "queued" ? "Import wird vorbereitet" : `Import ${job.status === "completed" ? "abgeschlossen" : job.status === "partial" ? "teilweise abgeschlossen" : "fehlgeschlagen"}`}</span><span>{job.processed}{job.total ? ` / ${job.total}` : ""} · {job.imported} importiert</span>{job.errors.length > 0 && <span>{job.errors.length} Hinweise</span>}</div>}
    {error && <p className="import-message import-message--error" role="alert">{error}</p>}
    {message && <p className="import-message" role="status">{message}</p>}
  </section>;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "IMPORT_BUSY") return "Ein Import läuft bereits. Bitte warte, bis er abgeschlossen ist.";
    if (error.code === "LOCAL_IMPORT_NOT_CONFIGURED") return "Der lokale Importordner ist im Backend nicht eingerichtet.";
  }
  return error instanceof Error ? error.message : "Import fehlgeschlagen. Bitte erneut versuchen.";
}
