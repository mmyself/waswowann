"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatDurationDays } from "@/lib/zukunftsgraph";
import type { TemplateCatalogItem, TemplateCatalogResponse, TemplateKind, TemplateMaturity } from "@/lib/types";

function maturityLabel(m: TemplateMaturity, count: number): string {
  if (m === "proven") return `Bewährt (n=${count})`;
  if (m === "calibrated") return `Kalibriert (n=${count})`;
  if (m === "archived") return "Archiviert";
  return "KI-Prior (n=0)";
}

function kindLabel(k: TemplateKind): string {
  if (k === "playbook") return "Playbook";
  if (k === "trigger") return "Trigger";
  if (k === "model") return "Modell";
  return "Schritt";
}

function formatCalibrationLine(entry: Record<string, unknown>): string {
  const flush = entry.flush !== undefined ? `Schub ${String(entry.flush)}` : "Messung";
  const date = typeof entry.harvest_date === "string" ? entry.harvest_date : typeof entry.date === "string" ? entry.date : "";
  const yieldVal = entry.yield_g !== undefined ? `${String(entry.yield_g)} g` : "";
  const durVal = entry.duration_days !== undefined ? `${String(entry.duration_days)} d` : "";
  return [flush, date, [yieldVal, durVal].filter(Boolean).join(" in ")].filter(Boolean).join(" · ");
}

export default function TemplatesPanel() {
  const [catalog, setCatalog] = useState<TemplateCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [domainFilter, setDomainFilter] = useState<string>("");
  const [driverFilter, setDriverFilter] = useState<"" | "actor" | "nature">("");
  const [maturityFilter, setMaturityFilter] = useState<"" | TemplateMaturity>("");
  const [onlyRoots, setOnlyRoots] = useState(true);
  const [expandedIds, setExpandedIds] = useState<Record<string, boolean>>({});
  const [showCreateForm, setShowCreateForm] = useState(false);

  const [newName, setNewName] = useState("");
  const [newSciName, setNewSciName] = useState("");
  const [newAliases, setNewAliases] = useState("");
  const [newDomain, setNewDomain] = useState("pilzzucht");
  const [newCategory, setNewCategory] = useState("Pilz");
  const [newDriver, setNewDriver] = useState<"actor" | "nature">("actor");
  const [newKind, setNewKind] = useState<TemplateKind>("step");
  const [newDays, setNewDays] = useState("");
  const [newOutProp, setNewOutProp] = useState("");
  const [newOutVal, setNewOutVal] = useState("");
  const [newOutUnit, setNewOutUnit] = useState("");

  const loadCatalog = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError("");
      api
        .templates(
          {
            query: query.trim() || undefined,
            domain: domainFilter || undefined,
            driver: driverFilter || undefined,
            maturity: maturityFilter || undefined,
            only_roots: onlyRoots,
          },
          signal
        )
        .then((res) => setCatalog(res))
        .catch((cause) => {
          if (!signal?.aborted) {
            setError(cause instanceof ApiError ? cause.message : "Vorlagen konnten nicht geladen werden.");
          }
        })
        .finally(() => {
          if (!signal?.aborted) setLoading(false);
        });
    },
    [query, domainFilter, driverFilter, maturityFilter, onlyRoots]
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => loadCatalog(controller.signal), query ? 180 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadCatalog, query]);

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!newName.trim()) {
      setError("Bitte einen Vorlagen-Namen eingeben.");
      return;
    }
    const daysNum = newDays.trim() ? Number(newDays.trim().replace(",", ".")) : undefined;
    if (newDays.trim() && (daysNum === undefined || !Number.isFinite(daysNum) || daysNum < 0)) {
      setError("Bitte eine gültige Dauer in Tagen (≥ 0) eingeben.");
      return;
    }
    const outValNum = newOutVal.trim() ? Number(newOutVal.trim().replace(",", ".")) : undefined;
    if (newOutVal.trim() && (outValNum === undefined || !Number.isFinite(outValNum))) {
      setError("Bitte einen gültigen Erwartungswert eingeben.");
      return;
    }

    setBusy(true);
    setError("");
    try {
      const aliases = newAliases
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await api.createTemplate({
        name: newName.trim(),
        driver: newDriver,
        domain: newDomain.trim() || "general",
        category: newCategory.trim() || "general",
        scientific_name: newSciName.trim() || undefined,
        aliases: aliases.length ? aliases : undefined,
        template_kind: newKind,
        duration_seconds: daysNum !== undefined ? daysNum * 86_400 : undefined,
        duration_sigma_seconds: daysNum !== undefined && daysNum > 0 ? Math.max(3600, daysNum * 0.2 * 86_400) : undefined,
        output_property: newOutProp.trim() || undefined,
        output_value: outValNum,
        output_unit: newOutUnit.trim() || undefined,
      });
      setCatalog(res.catalog);
      setNewName("");
      setNewSciName("");
      setNewAliases("");
      setNewDays("");
      setNewOutProp("");
      setNewOutVal("");
      setNewOutUnit("");
      setShowCreateForm(false);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Vorlage konnte nicht erstellt werden.");
    } finally {
      setBusy(false);
    }
  };

  const renderCard = (item: TemplateCatalogItem) => {
    const { template, profile, children } = item;
    const expanded = Boolean(expandedIds[template.id]);
    const durText = formatDurationDays(profile.total_duration_seconds, template.duration_sigma_seconds);
    return (
      <article key={template.id} className={`tpl-card is-${profile.maturity}`}>
        <div className="tpl-card__top">
          <div className="tpl-badges">
            <span className="tpl-domain-badge">{profile.domain}</span>
            <span className="zg-driver-badge">{template.driver === "nature" ? "Natur" : "Aktor"}</span>
            <span className="tpl-kind-badge">{kindLabel(profile.template_kind)}</span>
          </div>
          <span className={`tpl-maturity is-${profile.maturity}`}>
            {maturityLabel(profile.maturity, profile.observations_count)}
          </span>
        </div>

        <h3 className="tpl-card__title">{template.name}</h3>
        {profile.scientific_name && (
          <p className="tpl-sci" title={profile.taxon_path.join(" → ") || undefined}>
            <em>{profile.scientific_name}</em>
            {profile.taxon_path.length > 1 && <small> · {profile.taxon_path.slice(0, -1).join(" › ")}</small>}
          </p>
        )}

        <div className="tpl-metrics">
          <span>Dauer: <strong>{durText}</strong></span>
          {profile.produces_property && (
            <span>
              Wirkung: <strong>
                {profile.expected_output_num !== null ? `${profile.expected_output_num}${profile.expected_output_sigma ? ` ± ${profile.expected_output_sigma}` : ""} ${profile.produces_unit}`.trim() : profile.produces_property}
              </strong>
            </span>
          )}
        </div>

        {profile.aliases.length > 0 && (
          <p className="tpl-aliases">
            <span>Aliase:</span> {profile.aliases.join(" · ")}
          </p>
        )}

        {profile.calibration_history.length > 0 && (
          <div className="tpl-history">
            {profile.calibration_history.map((entry, idx) => (
              <small key={idx}>✓ {formatCalibrationLine(entry)}</small>
            ))}
          </div>
        )}

        {children.length > 0 && (
          <div className="tpl-children-wrap">
            <button
              type="button"
              className="zg-expand-btn"
              onClick={() => setExpandedIds((prev) => ({ ...prev, [template.id]: !prev[template.id] }))}
            >
              {expanded ? `▼ ${children.length} Schritte einklappen` : `▶ ${children.length} Playbook-Schritte anzeigen`}
            </button>
            {expanded && (
              <ol className="tpl-children-list">
                {children.map((ch) => (
                  <li key={ch.template.id}>
                    <strong>{ch.template.name}</strong>
                    <span>
                      {formatDurationDays(ch.template.duration_seconds, ch.template.duration_sigma_seconds)}
                      {ch.profile.expected_output_num !== null ? ` · ~${ch.profile.expected_output_num} ${ch.profile.produces_unit}` : ""}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </article>
    );
  };

  return (
    <section className="tpl-panel" aria-label="Vorlagen & Wissensbibliothek">
      <div className="zg-head">
        <div>
          <p className="eyebrow">MetaCore Weltmodell-Bibliothek</p>
          <h2>Vorlagen, Playbooks &amp; Taxonomie</h2>
          <p className="zg-sub">
            Wissenschaftliche Namen (Binomen), Aliase &amp; kalibrierte Erfahrungswerte — {catalog?.count ?? 0} passende Einträge
          </p>
        </div>
        <div className="zg-actions">
          <button type="button" className="secondary-action" disabled={busy} onClick={() => setShowCreateForm((v) => !v)}>
            {showCreateForm ? "Formular schließen" : "+ Neue Vorlage"}
          </button>
        </div>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}

      {showCreateForm && (
        <form className="zg-new-goal-form" onSubmit={handleCreate}>
          <div className="zg-section-title">
            <h3>Neue Vorlage im Katalog speichern</h3>
            <span>Mit wissenschaftlichem Namen &amp; Aliasen gegen Duplikate</span>
          </div>
          <div className="form-two-col">
            <label>
              Name der Vorlage
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="z. B. Austernseitling Stroh-Beutel" required />
            </label>
            <label>
              Wissenschaftlicher Name (Botanik / Zoologie)
              <input value={newSciName} onChange={(e) => setNewSciName(e.target.value)} placeholder="z. B. Pleurotus ostreatus / Canis lupus" />
            </label>
          </div>
          <label>
            Aliase &amp; Trivialnamen (kommagetrennt)
            <input value={newAliases} onChange={(e) => setNewAliases(e.target.value)} placeholder="z. B. Austernpilz, Kalbfleischpilz, Oyster Mushroom" />
          </label>
          <div className="form-two-col">
            <label>
              Domain
              <input value={newDomain} onChange={(e) => setNewDomain(e.target.value)} placeholder="pilzzucht, natur, kueche, garten" />
            </label>
            <label>
              Kategorie (Objekt-Bezug)
              <input value={newCategory} onChange={(e) => setNewCategory(e.target.value)} placeholder="Pilz, Pflanze, Ding …" />
            </label>
          </div>
          <div className="form-two-col">
            <label>
              Treiber
              <select value={newDriver} onChange={(e) => setNewDriver(e.target.value as "actor" | "nature")}>
                <option value="actor">Aktor (Handlung / Rezept)</option>
                <option value="nature">Natur (Biologie / Physik)</option>
              </select>
            </label>
            <label>
              Typ
              <select value={newKind} onChange={(e) => setNewKind(e.target.value as TemplateKind)}>
                <option value="step">Einzelschritt</option>
                <option value="playbook">Playbook (Kette)</option>
                <option value="trigger">Trigger (Auslöser)</option>
                <option value="model">Modell (Decay/Bilanz)</option>
              </select>
            </label>
          </div>
          <div className="form-two-col">
            <label>
              Dauer (Tage, optional)
              <input inputMode="decimal" value={newDays} onChange={(e) => setNewDays(e.target.value)} placeholder="z. B. 10.5" />
            </label>
            <label>
              Output-Eigenschaft (optional)
              <input value={newOutProp} onChange={(e) => setNewOutProp(e.target.value)} placeholder="z. B. harvest_ready" />
            </label>
          </div>
          <div className="form-two-col">
            <label>
              Erwarteter Wert (optional)
              <input inputMode="decimal" value={newOutVal} onChange={(e) => setNewOutVal(e.target.value)} placeholder="z. B. 150" />
            </label>
            <label>
              Einheit (optional)
              <input value={newOutUnit} onChange={(e) => setNewOutUnit(e.target.value)} maxLength={20} placeholder="g, ml, %" />
            </label>
          </div>
          <div className="zg-goal-actions">
            <button type="submit" className="save-button" disabled={busy}>
              Vorlage speichern <span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>
      )}

      <div className="tpl-filters">
        <input
          type="search"
          className="tpl-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Suche nach Name, wissenschaftlichem Namen (z. B. Hericium erinaceus) oder Alias …"
          aria-label="Vorlagen durchsuchen"
        />
        <div className="tpl-filter-row">
          <div className="tpl-chip-group" role="group" aria-label="Domain-Filter">
            <button
              type="button"
              className={`tpl-chip ${domainFilter === "" ? "is-active" : ""}`}
              onClick={() => setDomainFilter("")}
            >
              Alle Domains
            </button>
            {(catalog?.facets.domains ?? []).map((dom) => (
              <button
                key={dom}
                type="button"
                className={`tpl-chip ${domainFilter === dom ? "is-active" : ""}`}
                onClick={() => setDomainFilter(dom)}
              >
                {dom}
              </button>
            ))}
          </div>
          <div className="tpl-selects">
            <select value={driverFilter} onChange={(e) => setDriverFilter(e.target.value as "" | "actor" | "nature")} aria-label="Treiber filtern">
              <option value="">Alle Treiber</option>
              <option value="actor">Nur Aktor</option>
              <option value="nature">Nur Natur</option>
            </select>
            <select value={maturityFilter} onChange={(e) => setMaturityFilter(e.target.value as "" | TemplateMaturity)} aria-label="Reifegrad filtern">
              <option value="">Alle Reifegrade</option>
              <option value="proven">Bewährt (n≥3)</option>
              <option value="calibrated">Kalibriert (n≥1)</option>
              <option value="ai_prior">KI-Prior (n=0)</option>
            </select>
            <label className="zg-toggle">
              <input type="checkbox" checked={onlyRoots} onChange={(e) => setOnlyRoots(e.target.checked)} />
              Nur Hauptvorlagen
            </label>
          </div>
        </div>
      </div>

      {loading && !catalog ? (
        <p className="empty-inline">Vorlagen-Bibliothek wird geladen …</p>
      ) : (catalog?.templates.length ?? 0) === 0 ? (
        <p className="empty-inline">Keine Vorlagen zu diesem Filter gefunden.</p>
      ) : (
        <div className="tpl-grid">{catalog?.templates.map(renderCard)}</div>
      )}
    </section>
  );
}
