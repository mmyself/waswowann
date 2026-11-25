"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AnalysisResult, GeminiNote, ObservationDetail } from "@/lib/types";

export default function AnalysisPanel({ detail, onSave }: { detail: ObservationDetail; onSave: (detail: ObservationDetail) => void }) {
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const gemini = detail.gemini;
  const result = gemini.result ?? null;
  useEffect(() => {
    if (!["queued", "running"].includes(gemini.status)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try { onSave(await api.observation(detail.id, controller.signal)); }
      catch { /* Keep the current result visible and try again on the next poll. */ }
    }, 1500);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [detail.id, gemini.status, detail.updated_at, onSave]);

  const start = async (event: FormEvent) => {
    event.preventDefault(); setPending(true); setError("");
    try {
      const created = await api.startAnalysis(detail.id, question.trim() || undefined, question.trim() ? gemini.note_id ?? undefined : undefined);
      setQuestion("");
      onSave({ ...detail, gemini: { ...gemini, status: created.status as GeminiNote["status"], note_id: created.note_id } });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Analyse konnte nicht gestartet werden."); }
    finally { setPending(false); }
  };

  const adopt = async () => {
    if (!result) return;
    try {
      const updated = await api.updateObservation(detail.id, {
        ...(result.title ? { title: result.title } : {}),
        ...(result.category ? { category: result.category } : {}),
        ...(result.environment && result.environment !== "unknown" ? { environment: result.environment } : {}),
      });
      onSave(updated);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Vorschlag konnte nicht übernommen werden."); }
  };
  const status = gemini.status === "ready" ? "Analyse abgeschlossen" : gemini.status === "running" ? "Analyse läuft …" : gemini.status === "queued" ? "Analyse wartet …" : gemini.status === "error" ? "Analyse fehlgeschlagen" : gemini.status === "unavailable" ? "Analyse nicht eingerichtet" : "Noch keine Analyse";

  return <section className="detail-card gemini-card" aria-labelledby="analysis-heading">
    <div className="section-heading"><div><p className="eyebrow">Bildeinordnung</p><h3 id="analysis-heading">Gemini-Analyse</h3></div><span className={`weather-status weather-status--${gemini.status}`}>{status}</span></div>
    {result ? <div className="analysis-result">
      {(result.common_name || result.title || result.scientific_name) && <h4>{result.common_name ?? result.title ?? result.scientific_name}<small>{(result.common_name || result.title) && result.scientific_name ? result.scientific_name : ""}</small></h4>}
      {(result.description || (gemini.text && !gemini.text.trim().startsWith("{"))) && <p className="analysis-text">{result.description || gemini.text}</p>}
      {result.confidence != null && <p className="analysis-confidence">Modell-Konfidenz <strong>{Math.round(result.confidence * 100)}%</strong></p>}
      {(result.estimated_sigma_meters != null || result.estimated_sigma_seconds != null || result.uncertainty_rationale) && (
        <p className="analysis-confidence">
          KI-Schätzung Unsicherheit:{" "}
          {result.estimated_sigma_meters != null && <strong>± {Math.round(result.estimated_sigma_meters)} m</strong>}
          {result.estimated_sigma_meters != null && result.estimated_sigma_seconds != null ? " · " : ""}
          {result.estimated_sigma_seconds != null && <strong>± {Math.round(result.estimated_sigma_seconds)} s</strong>}
          {result.uncertainty_rationale ? ` — ${result.uncertainty_rationale}` : ""}
        </p>
      )}
      {result.tags?.length ? <div className="analysis-tags">{result.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}
      {result.alternatives?.length ? <div className="analysis-alternatives"><strong>Mögliche Alternativen</strong>{result.alternatives.map((alternative, index) => typeof alternative === "string" ? <p key={`${alternative}-${index}`}>{alternative}</p> : <p key={`${alternative.name}-${index}`}>{alternative.name}{alternative.scientific_name ? ` · ${alternative.scientific_name}` : ""}{alternative.confidence != null ? ` · ${Math.round(alternative.confidence * 100)}%` : ""}{alternative.rationale ? ` — ${alternative.rationale}` : ""}</p>)}</div> : null}
      {result.questions?.length ? <div className="analysis-alternatives"><strong>Hilfreiche Prüfmerkmale vor Ort</strong>{result.questions.map((item, index) => <p key={`${item}-${index}`}>· {item}</p>)}</div> : null}
      {result.warnings?.length ? <ul className="analysis-warnings">{result.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul> : null}
      {(result.title || result.category || (result.environment && result.environment !== "unknown")) &&
        (result.title !== detail.title || result.category !== detail.category || (result.environment && result.environment !== "unknown" && result.environment !== detail.environment)) && (
          <button type="button" className="secondary-action" onClick={adopt}>Vorschlag übernehmen</button>
        )}
    </div> : <p className="placeholder-copy">{gemini.status === "error" ? `Die Analyse ist fehlgeschlagen${gemini.error_code ? ` (${gemini.error_code})` : ""}.` : gemini.status === "unavailable" ? "Analyse ist nicht konfiguriert. Deine Beobachtung bleibt trotzdem nutzbar." : gemini.status === "queued" || gemini.status === "running" ? "Die Bildanalyse wird verarbeitet. Dieser Eintrag aktualisiert sich automatisch." : "Für dieses Bild liegt noch keine Bildeinordnung vor."}</p>}
    {gemini.history && gemini.history.length > 1 ? <details className="analysis-history"><summary>Rückfragenverlauf ({gemini.history.length})</summary>{gemini.history.map((item) => <article key={item.note_id}><strong>{item.question_text || "Erste Einordnung"}{item.result?.apply_correction ? " · Eintrag korrigiert" : ""}</strong><span>{item.status === "ready" ? item.result?.description || (item.text && !item.text.trim().startsWith("{") ? item.text : null) || item.result?.common_name || item.result?.title || "Abgeschlossen" : item.status === "error" ? "Fehlgeschlagen" : "Wird verarbeitet"}</span></article>)}</details> : null}
    <form className="analysis-question" onSubmit={start}><label htmlFor={`question-${detail.id}`}>Rückfrage oder Korrektur</label><textarea id={`question-${detail.id}`} rows={2} maxLength={1200} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="z. B. ‚Nenne den Titel nur Igel-Stachelbart‘ oder Frage stellen …" /><button type="submit" className="secondary-action" disabled={pending || ["queued", "running"].includes(gemini.status)}>{pending ? "Wird gestartet …" : result ? "Rückfrage senden" : "Analyse anfragen"}</button></form>
    {error && <p className="form-error" role="alert">{error}</p>}
    <p className="analysis-disclaimer">KI-Vorschläge sind keine verlässliche Artbestimmung und keine Verzehrfreigabe.</p>
  </section>;
}
