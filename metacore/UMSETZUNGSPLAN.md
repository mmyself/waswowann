# WasWoWann MetaCore – Umsetzungsplan (Phase 1)

Stand: 2026-11-24

## 1. Zielbild & Abgrenzung (Phase 1 vs. Phase 2)

Dieses Repository (`/Users/jan/WasWoWann/metacore`) setzt den **neuro-symbolischen Kern** von WasWoWann als eigenständiges Python-Projekt um: Ein **programmierter, deterministischer MetaCore** übernimmt Rechnen, Graph-Konsistenz, Gauß-Fusion, Weltlinien-Spaltung/Verschmelzung und 4-stufige Eskalation, während **LLMs und Agenten** über einen **semantischen MCP-Server** natürlichsprachliche Eingaben übersetzen, `ai_prior`-Schätzwerte liefern und den Zukunftsgraphen pflegen.

* **Phase 1 (Umfang dieses Plans):**
  * Eigenständige SQLite-Datenbank mit Migration `001_initial.sql` (4 Kerntabellen + Indizes).
  * Validierte Pydantic-Domänenmodelle (`src/metacore/models.py`).
  * Gauß-Fusion (1D-Zeit + 2D-ENU-Ort), Fehlerfortpflanzung für Mengenformeln und Template-Kalibrierung (`math_fusion.py`).
  * Zustands-Propagation, Trigger-Auswertung und 4-stufige Eskalations-Engine (`engine.py`).
  * Semantischer MCP-Server (`mcp_server.py`) mit 8 Werkzeugen und direktem Folgen-Feedback.
  * Referenz-Seeds und durchgängige Tests für die drei Leitbeispiele (*Körnerbrut 400 g*, *Riesenschirmling Folgejahr*, *Roboter R1*).
* **Phase 2 (bewusst ausgeklammert, folgt separat):**
  * HTTP-/FastAPI-Anbindung an das bestehende Backend (`/Users/jan/WasWoWann/backend`).
  * Visuelle Integration (Karte, Zeitstrahl, Zukunftsgraph, Kausal-Linse) in das Next.js-Frontend (`/Users/jan/WasWoWann/frontend`).
  * Brücke zwischen bestehenden `observations`/`media`-Tabellen und MetaCore-Weltlinien.

---

## 2. Architektur & Paketstruktur (`src/metacore/`)

```text
metacore/
├── .gitignore
├── pyproject.toml
├── README.md
├── UMSETZUNGSPLAN.md
├── src/
│   └── metacore/
│       ├── __init__.py
│       ├── config.py              # Pfade (METACORE_DB_PATH), Defaults, Konfigurations-Settings
│       ├── db.py                  # SQLite-Verbindung (WAL, FK=ON), atomare Migrationen
│       ├── migrations/
│       │   └── 001_initial.sql    # 4 Kerntabellen: entities, states, transformations, edges
│       ├── models.py              # Pydantic-Verträge für Entitäten, Zustände, Kanten, Feedback
│       ├── math_fusion.py         # 1D/2D-Gauß-Fusion, Fehlerfortpflanzung, Template-Kalibrierung
│       ├── engine.py              # Graph-Engine: Templates, Propagation, Eskalation (Stufe 1–4)
│       ├── mcp_server.py          # MCP-Server (stdio) mit 8 semantischen Tools
│       └── seeds/
│           ├── __init__.py
│           ├── koernerbrut.py     # Seed: Rezept + 400-g-Weizencharge (2 Beutel)
│           ├── riesenschirmling.py# Seed: 2 Vorjahresfunde + Habitat-Wolke (σ ≈ 140 m)
│           └── roboter_wasser.py  # Seed: Skill "Getränk holen" + Greifer-Belegung (uses)
└── tests/
    ├── conftest.py                # Temporäre SQLite-DB + Fixtures je Test
    ├── test_db_migrations.py      # Schema, Constraints, Foreign Keys, Idempotenz
    ├── test_math_fusion.py        # 1D-Fusion, ENU-Ortsfusion, Mengen-Fehlerfortpflanzung
    ├── test_escalation_engine.py  # Stufen 1–4, uses-Freigabe, Spaltung/Verschmelzung
    ├── test_scenarios_e2e.py      # End-to-End: Körnerbrut 400 g, Riesenschirmling, Roboter R1
    └── test_mcp_server.py         # Tool-Aufrufe, Schema-Validierung, Folgen-Feedback
```

---

## 3. SQLite-Datenmodell (`001_initial.sql`)

Das gesamte Weltmodell wird ohne Sonder-Tabellen für „Rezepte“, „Pläne“ oder „Ressourcen“ auf genau **vier Kerntabellen** abgebildet (`PRAGMA foreign_keys = ON`, `journal_mode = WAL`, Zeitstempel als UTC-Millisekunden `INTEGER`, Unsicherheiten strikt `> 0` oder `NULL`):

### 3.1 `entities` (Weltlinien & austauschbare Typen)
| Spalte | Typ & Constraints | Bedeutung |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | UUID / stabiler Schlüssel |
| `name` | `TEXT NOT NULL` | Sprechender Name (z. B. `Myzelgebiet Nordwiese`, `Weizen`) |
| `kind` | `TEXT NOT NULL CHECK(kind IN ('worldline','type'))` | `worldline` (Individuum/Ort/Phänomen) vs. `type` (austauschbarer Stoff) |
| `category` | `TEXT NOT NULL DEFAULT 'general'` | Domänen-Tag (`pilz`, `rezept`, `robotik`, `wetter`, …) |
| `parent_entity_id` | `TEXT REFERENCES entities(id) ON DELETE SET NULL` | Optional für Spaltungen (z. B. Beutel #1 stammt von Charge X) |
| `archived` | `INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))` | Weiche Archivierung abgeschlossener Weltlinien |
| `meta_json` | `TEXT NOT NULL DEFAULT '{}'` | Zusatzattribute |
| `created_at_ms`, `updated_at_ms` | `INTEGER NOT NULL` | Zeitstempel |

### 3.2 `states` (Zustände in Raum, Zeit, Menge & Modalität)
| Spalte | Typ & Constraints | Bedeutung |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | Zustands-ID |
| `entity_id` | `TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE` | Zugehörige Weltlinie oder Typ |
| `modality` | `TEXT NOT NULL CHECK(modality IN ('observed','predicted','desired','cancelled'))` | Ist, Prognose, Soll-Ziel oder sauber abgebrochen |
| `source` | `TEXT NOT NULL CHECK(source IN ('measured','ai_prior','derived','user_goal'))` | Ehrliche Herkunft (`ai_prior` vs. `measured` vs. Formel-Ergebnis) |
| `label` | `TEXT NOT NULL` | Kurzbeschreibung des Zustands |
| `lat`, `lon` | `REAL` (beide `NULL` oder beide gesetzt; gültige WGS84-Bereiche) | Ort (`Wo`) |
| `sigma_meters` | `REAL CHECK(sigma_meters IS NULL OR (sigma_meters > 0 AND lat IS NOT NULL))` | Isotrope 1-$\sigma$-Ortsunsicherheit |
| `location_label` | `TEXT NOT NULL DEFAULT ''` | Lesbarer Ortsname (z. B. `Küche`, `Nordwiese`) |
| `observed_at_ms` | `INTEGER` | Zeitpunkt (`Wann`, UTC-ms; bei Zielen Ziel-Mitte/Frist) |
| `sigma_seconds` | `REAL CHECK(sigma_seconds IS NULL OR (sigma_seconds > 0 AND observed_at_ms IS NOT NULL))` | Zeitliche 1-$\sigma$-Unsicherheit / Toleranz |
| `quantity_mean` | `REAL` | Menge (`Wie viel`), z. B. `400.0` |
| `quantity_sigma` | `REAL CHECK(quantity_sigma IS NULL OR quantity_sigma >= 0)` | Mengenunsicherheit (z. B. `20.0`) |
| `quantity_unit` | `TEXT NOT NULL DEFAULT ''` | Einheit (`g`, `ml`, `stk`, `°C`, `mm`) |
| `state_status` | `TEXT NOT NULL DEFAULT 'active' CHECK(state_status IN ('active','satisfied','at_risk','broken','released'))` | Aktueller Bewertungsstatus |
| `escalation_stage` | `INTEGER NOT NULL DEFAULT 2 CHECK(escalation_stage BETWEEN 1 AND 4)` | Zugeordnete Eskalationsstufe |
| `broken_dimensions_json` | `TEXT NOT NULL DEFAULT '[]'` | z. B. `["wann","wie_viel"]` bei Stufe 4 |
| `meta_json` | `TEXT NOT NULL DEFAULT '{}'` | z. B. Temperatur, Feuchte, Kontext |
| `created_at_ms`, `updated_at_ms` | `INTEGER NOT NULL` | Zeitstempel |

### 3.3 `transformations` (Aktionen, Naturprozesse, Hierarchie & Vorlagen)
| Spalte | Typ & Constraints | Bedeutung |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | Transformations-ID |
| `name` | `TEXT NOT NULL` | Name (z. B. `Einweichen & Abtrocknen`, `Schub im Myzelgebiet`) |
| `modality` | `TEXT NOT NULL CHECK(modality IN ('happened','planned','template','cancelled'))` | Geschehen, eingeplant, wiederverwendbare Vorlage oder verworfen |
| `driver` | `TEXT NOT NULL CHECK(driver IN ('actor','nature'))` | Gesteuerte Akteurs-Aktion vs. passiver Natur-/Weltprozess |
| `template_id` | `TEXT REFERENCES transformations(id) ON DELETE SET NULL` | Verweis auf Ursprungs-Vorlage (für `ai_prior` → `measured` Kalibrierung) |
| `parent_id` | `TEXT REFERENCES transformations(id) ON DELETE CASCADE` | Hierarchie / Einkapselung von Teilgraphen (HTN-Prinzip) |
| `duration_seconds` | `REAL CHECK(duration_seconds IS NULL OR duration_seconds >= 0)` | Erwartete/gemessene Dauer $\Delta t$ |
| `duration_sigma_seconds` | `REAL CHECK(duration_sigma_seconds IS NULL OR duration_sigma_seconds > 0)` | Unsicherheit $\sigma_{\Delta t}$ |
| `cost` | `REAL NOT NULL DEFAULT 0.0` | Aufwand / Kosten für Pfadwahl (`Wahl-ODER`) |
| `success_prob` | `REAL NOT NULL DEFAULT 1.0 CHECK(success_prob > 0 AND success_prob <= 1.0)` | Erfolgswahrscheinlichkeit |
| `formula_json` | `TEXT NOT NULL DEFAULT '{}'` | Deklarative Parameter/Faktoren (z. B. Quellfaktor `1.75 ± 0.10`, Temp-Kurve) |
| `created_at_ms`, `updated_at_ms` | `INTEGER NOT NULL` | Zeitstempel |

### 3.4 `edges` (Kantenrollen, UND vs. ODER, Ressourcenbelegung)
| Spalte | Typ & Constraints | Bedeutung |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | Kanten-ID |
| `transformation_id` | `TEXT NOT NULL REFERENCES transformations(id) ON DELETE CASCADE` | Verknüpfte Transformation |
| `state_id` | `TEXT NOT NULL REFERENCES states(id) ON DELETE CASCADE` | Verknüpfter Zustand |
| `direction` | `TEXT NOT NULL CHECK(direction IN ('in','out'))` | Input (`state → transformation`) oder Output (`transformation → state`) |
| `role` | `TEXT NOT NULL CHECK(role IN ('consumes','uses','triggers','produces'))` | Verbrauch, zeitliche Belegung, Auslöser oder Ergebnis |
| `logic_group` | `TEXT NOT NULL DEFAULT 'AND' CHECK(logic_group IN ('AND','CHOICE_OR','WORLD_OR'))` | Trennung von Parallelität, Wahlalternative und Risikoast |
| `branch_key` | `TEXT NOT NULL DEFAULT 'default'` | Identifiziert Zweig innerhalb einer `CHOICE_OR`-/`WORLD_OR`-Gruppe |
| `is_selected` | `INTEGER NOT NULL DEFAULT 1 CHECK(is_selected IN (0,1))` | Markiert den aktuell gewählten Zweig bei `CHOICE_OR` |
| `probability` | `REAL CHECK(probability IS NULL OR (probability > 0 AND probability <= 1.0))` | Wahrscheinlichkeit bei `WORLD_OR` (z. B. `0.65` vs. `0.35`) |
| `ratio_mean`, `ratio_sigma` | `REAL` (optional) | Mengenanteil / Split-Faktor (z. B. `1/2` auf 2 Beutel) |

Indizes entstehen gezielt auf `states(entity_id, modality, observed_at_ms)`, `states(state_status, escalation_stage)`, `transformations(modality, template_id, parent_id)` und `edges(transformation_id, state_id)`.

---

## 4. Mathematik, Fusion & `ai_prior` → `measured` Lernzyklus (`math_fusion.py`)

Alle Berechnungen arbeiten rein deterministisch in Python (`math`, ohne komplexe Fremdabhängigkeiten):

1. **1D-Gauß-Fusion (Zeit, Menge & skalare Faktoren):**
   Für unabhängige Messungen $(\mu_i, \sigma_i)$ mit Präzision $w_i = 1 / \sigma_i^2$:
   $$\mu_f = \frac{\sum w_i \mu_i}{\sum w_i}, \qquad \sigma_f = \sqrt{\frac{1}{\sum w_i}}$$
2. **2D-Ortsfusion / Habitat-Generalisierung:**
   * *Konvergenz (dasselbe Objekt mehrfach gemessen):* Übernahme der lokalen metrischen ENU-Transformation aus WasWoWann (`gaussian_fusion.py`-Mathematik) zur präzisionsgewichteten Fusion.
   * *Streuung (z. B. 2 Schirmling-Funde im Abstand ~180 m → Suchgebiet):* Berechnung des Schwerpunkts plus kombinierter Gebietsstreuung $\sigma_{\text{Gebiet}} = \sqrt{\sigma_{\text{sample}}^2 + \bar{\sigma}_{\text{obs}}^2}$ mit konservativem Mindest-$\sigma$ bei $n < 5$ Funden.
3. **Analytische Fehlerfortpflanzung für Mengenformeln:**
   * Produkt $Z = A \cdot B$ (z. B. `400 g × (1,75 ± 0,10)`):
     $$\mu_Z = \mu_A \mu_B, \qquad \sigma_Z = \sqrt{(\mu_B \sigma_A)^2 + (\mu_A \sigma_B)^2}$$
   * Aufteilung auf $k$ Beutel ($Z / k$ + Abfüll-Unsicherheit $\sigma_{\text{split}}$):
     $$\mu_{\text{Beutel}} = \frac{\mu_Z}{k}, \qquad \sigma_{\text{Beutel}} = \sqrt{\left(\frac{\sigma_Z}{k}\right)^2 + \sigma_{\text{split}}^2}$$
4. **`ai_prior` → `measured` Kalibrierung von Vorlagen:**
   * Beim Instanziieren einer neuen Vorlage ohne lokale Historie darf das LLM einen Start-Prior (`source='ai_prior'`, bewusst breites $\sigma_{\text{prior}}$) in `formula_json` hinterlegen.
   * Sobald echte `measured`-Beobachtungen vorliegen (z. B. real gewogene `680 g` statt `700 g`), aktualisiert die Engine den Vorlagen-Faktor per Bayesschem Update (mit abnehmendem Gewicht des `ai_prior`, sobald echte Messungen vorliegen) und speichert die kalibrierte Vorlage zurück. Die Herkunft wechselt ab der ersten realen Messung sichtbar auf `measured` (oder `calibrated`).

---

## 5. Eskalations-Engine (Stufen 1–4) (`engine.py`)

Bei jeder Zustandsänderung (`record_observation`, `set_goal`, `instantiate_template`, Parameter-Update) läuft eine deterministische Neubewertung des betroffenen Teilgraphen:

1. **Stufe 1 – Ereignis-Trigger (Reflex):**
   Prüft registrierte `template`-Transformationen mit `triggers`-Vorbedingungen gegen neue `observed`-Zustände (z. B. *„Regen $\ge 35\text{ mm}$ im Habitat Nordwiese“* → instanziiert Herbst-Prognose automatisch).
2. **Stufe 2 – Toleranz-Nachjustierung:**
   Propagiert geänderte Zeit-, Orts- oder Mengenwerte vorwärts durch abhängige `predicted`-Knoten. Liegen alle aktiven `desired`-Ziele innerhalb ihrer Toleranzen (z. B. $|\mu_{\text{pred}} - \mu_{\text{goal}}| \le 2\sigma_{\text{total}}$ und benötigte Menge erreichbar), bleibt Stufe 2 aktiv (`state_status = 'satisfied'` bzw. `'active'`).
3. **Stufe 3 – Plan-Anpassung (Fallback → Re-Planning):**
   Wird ein aktives Ziel gefährdet (z. B. Beutel #2 kontaminiert, Oktober zu trocken, Flurtür geschlossen):
   * Prüft zuerst vorhandene `WORLD_OR`-Fallback-Zweige oder alternative `CHOICE_OR`-Pfade (minimale Kosten, kein Ressourcenkonflikt bei `uses`).
   * Wählt den besten gültigen Ersatzpfad (`is_selected = 1`), aktualisiert betroffene `predicted`-Zeiten/Mengen und meldet Stufe 3 (`at_risk` / `adjusted`).
4. **Stufe 4 – Ziel-Eskalation (Unmachbarkeit):**
   Existiert kein gültiger Pfad mehr (alle Zweige verletzen Vorbedingungen oder Toleranzen), markiert die Engine das `desired`-Ziel als `broken` (`escalation_stage = 4`) und berechnet exakt `broken_dimensions_json`:
   * `wann`: prognostizierter Endzeitpunkt liegt hinter Ziel-Deadline $+ 2\sigma$.
   * `wo`: Zielort nicht erreichbar / Blockade auf allen Pfaden.
   * `wie_viel`: erreichbare Menge kleiner als Ziel-Soll (z. B. `0 g` Brut nach Nassfäule).
   * `vorbedingung`: harte Umwelt-/Zustandsbedingung verletzt (z. B. Bodenfrost `-4 °C`).

   Über `resolve_escalation` unterstützt die Engine vier klare Aktionen: `widen_tolerance` (Toleranz/Frist aufweiten), `switch_goal` (Fallback-Ziel aktivieren), `replan` (Ersatz-Template starten) und `abort_goal` (Ziel auf `cancelled`, zugehörige `uses`-Ressourcen sofort auf `released`).

---

## 6. Semantischer MCP-Server (8 Tools + Folgen-Feedback) (`mcp_server.py`)

Der Server läuft lokal über `stdio` (`mcp.server.fastmcp` bzw. `Server` aus dem offiziellen `mcp`-SDK) und gibt bei **jedem** Schreib-Tool ein strukturiertes `MutationResult` zurück (geänderte Knoten, kalibrierte Priors, neue Eskalationsstufe, aktive Warnungen).

| # | Tool-Name | Zweck für das LLM / den Agenten |
|---|---|---|
| 1 | `get_focus_briefing` | Liefert kompakte Lage rund um *Jetzt*: anstehende Aktionen, offene `ai_prior`-Stellen zum Nachfragen und aktive Stufe-3/4-Spannungen. |
| 2 | `inspect_worldline` | Zeigt komplette Weltlinie einer Entität (Vergangenheit `observed` + Zukunft `predicted`/`desired` + Kausal-Nachbarschaft). |
| 3 | `search_templates` | Durchsucht gespeicherte Rezepte/Skills/Natur-Templates nach Stichwort, Kategorie oder Auslöser. |
| 4 | `define_template` | Legt ein neues zeit-/ortloses `template` (inkl. Teilgraph, Kantenrollen, Formeln und optionaler `ai_prior`-Werte) an. |
| 5 | `instantiate_template` | Bindet eine Vorlage an konkrete `entities`, Startzeit, Ort und Mengen; berechnet alle `predicted`-Knoten sofort voraus. |
| 6 | `record_observation` | Erfasst reale `observed`-Messungen (Waage, Foto/EXIF, Temperatur, erstes Myzel, Null-Fund), aktualisiert Vorlagen-Priors und berechnet Folgen + Eskalation neu. |
| 7 | `set_goal` | Legt ein `desired`-Ziel (`Wo, Wann ± σ, Wie viel ± σ`) an oder passt es an und prüft sofort die Machbarkeit. |
| 8 | `resolve_escalation` | Löst einen Stufe-3/4-Konflikt sauber auf (`widen_tolerance`, `switch_goal`, `replan`, `abort_goal`) inkl. automatischer `uses`-Ressourcen-Freigabe. |

### Beispiel-Einbindung in MCP-Clients (z. B. Antigravity / Claude Desktop)

```json
{
  "mcpServers": {
    "waswowann-metacore": {
      "command": "/Users/jan/WasWoWann/metacore/.venv/bin/waswowann-mcp",
      "env": {
        "METACORE_DB_PATH": "/Users/jan/WasWoWann/data/metacore.sqlite3"
      }
    }
  }
}
```

---

## 7. Meilensteine & Abnahmekriterien

### M1 – Projektfundament, DB-Schicht & Modelle
* **Inhalt:** `config.py`, `db.py`, `migrations/001_initial.sql`, `models.py`, `tests/test_db_migrations.py`.
* **Abnahme:** `migrate()` legt frische SQLite-DB mit allen 4 Tabellen, Constraints (`σ > 0`, gültige Koordinaten) und Indizes an; wiederholter Aufruf ist idempotent; fehlerhafte Kanten/Sigmas werden sauber abgewiesen.

### M2 – Gauß-Mathematik, Mengen-Fehlerfortpflanzung & Kalibrierung
* **Inhalt:** `math_fusion.py`, `tests/test_math_fusion.py`.
* **Abnahme:** 1D-Fusion zweier identischer Messungen ergibt exakt $\sigma / \sqrt{2}$; ENU-Ortsfusion arbeitet metrisch exakt; `400 g × (1,75 ± 0,10) ÷ 2` liefert reproduzierbar `700 g ± 40 g` gesamt und `~350 g ± 21 g` pro Beutel; echte `measured`-Werte schärfen `ai_prior`-Parameter nachweisbar.

### M3 – Graph-Propagation & 4-stufige Eskalations-Engine
* **Inhalt:** `engine.py`, `tests/test_escalation_engine.py`.
* **Abnahme:** Hierarchische `template`-Instanziierung, `UND` vs. `CHOICE_OR` vs. `WORLD_OR`, automatische `uses`-Konfliktprüfung, Wechsel über Stufen 1–4 mit exakter `broken_dimensions_json`-Diagnose und verifizierter `uses`-Freigabe bei `abort_goal`.

### M4 – Semantischer MCP-Server & Folgen-Feedback
* **Inhalt:** `mcp_server.py`, `tests/test_mcp_server.py`.
* **Abnahme:** Alle 8 MCP-Tools sind über das offizielle MCP-SDK aufrufbar, validieren per Pydantic, liefern konsistentes Folgen-Feedback (inkl. Stufe & gebrochener Dimensionen) und starten sauber über `waswowann-mcp`.

### M5 – Drei Referenz-Szenarien (Seeds & End-to-End-Tests)
* **Inhalt:** `seeds/{koernerbrut,riesenschirmling,roboter_wasser}.py`, `tests/test_scenarios_e2e.py`.
* **Abnahme:**
  1. **Körnerbrut 400 g:** Start mit `ai_prior` (`1,75 ± 0,10`), reale Messung `680 g` kalibriert Vorlage, Spaltung in 2 Beutel, Ausfall Beutel #2 löst Stufe 3 aus, Nassfäule löst Stufe 4 mit Freigabe des Inkubators aus.
  2. **Riesenschirmling Folgejahr:** 2 scharfe Funde + Vorwetter erzeugen Habitat-Wolke ($\sigma \approx 140\text{ m}$), Trockenheit schiebt Schub nach hinten (Stufe 3), Frost `-4 °C` eskaliert Stufe 4 (Fortschreibung Jahr 3 / Null-Fund).
  3. **Roboter R1:** `uses`-Belegung an Greifer 1 verhindert naives Türöffnen, Fallback über Greifer 2 / Sideboard (Stufe 3), abgeschlossene Tür triggert Stufe 4 (`wo`, `vorbedingung`).
* **Gesamtcheck:** `pytest` und `ruff check` laufen grün durch.

---

## 8. Ausblick Phase 2 (Integration in WasWoWann)

Sobald M1–M5 im Testbetrieb über MCP überzeugen, wird MetaCore in das bestehende `/Users/jan/WasWoWann`-System eingehängt:
1. Einbindung von `metacore` als lokales Python-Paket im `backend/` oder gemeinsamer Zugriff auf dieselbe SQLite-Datei.
2. REST-Endpunkte im FastAPI-Backend für Karte, Zukunftsgraph und lokale Kausal-Linse.
3. Erweiterung der bestehenden Next.js-Oberfläche (Zeitschieber über `JETZT` hinaus, σ-Trichter auf der MapLibre-Karte, Kausal-Linse in der rechten Sidebar) nach dem Design der Prototypen.
