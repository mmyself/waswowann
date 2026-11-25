# WasWoWann MetaCore

Eigenständiger, deterministischer Raum-Zeit-**MetaCore** und semantischer **MCP-Server** (`waswowann-mcp`) unter `metacore/`. Er bildet den neuro-symbolischen Rechen-, Graph-, Uhren- und Planungskern von WasWoWann in reinem Python (`pydantic` + `sqlite3` + stdlib) ab.

---

## 1. Architektur & Paketstruktur

```text
metacore/
├── pyproject.toml                  # Paket "waswowann-metacore", Script "waswowann-mcp"
├── src/metacore/
│   ├── config.py                   # Settings (METACORE_DB_PATH, Default: data/metacore.sqlite3)
│   ├── db.py                       # WAL-SQLite-Verbindung (PRAGMA foreign_keys = ON) & Migrationen
│   ├── migrations/001_initial.sql  # 4 Kerntabellen + Indizes + Check-Invariants
│   ├── models.py                   # Strikte Pydantic-v2-Verträge (extra='forbid', finite floats)
│   ├── repository.py               # Typisierter SQLite-Datenzugriff (Entities, States, Txs, Edges)
│   ├── math_fusion.py              # 1D-Gauß, 2D-ENU-Ortsfusion, Habitat-Wolken, Fehlerfortpflanzung
│   ├── clocks.py                   # 7 globale Uhren + Eigen-Intervalle (Δt), Rayleigh-Test, Phasen-Wächter
│   ├── engine.py                   # Trigger, Recipe-Mix, Ratio-Guards, Modifier, parent_id-Rollup, Eskalation
│   ├── visualizer.py               # Live-SQLite-Snapshot-Export & Standalone-HTML-Visualizer
│   ├── mcp_server.py               # FastMCP-Server (stdio) mit 8 semantischen Werkzeugen
│   └── seeds/                      # 6 Referenz-Szenarien (Körnerbrut, Riesenschirmling, Roboter, …)
└── tests/                          # 36 Tests (M1–M5, Visualizer, Clocks/Periodik)
```

---

## 2. Vier-Tabellen-Datenmodell (`001_initial.sql`)

Alle Fakten, Pläne, Rezepte, Umweltbedingungen und Ressourcenbelegungen liegen ohne Sondertabellen auf genau vier Tabellen:

1. **`entities`**: `id`, `name`, `kind` (`worldline` | `type`), `category`, `source` (`observed` | `derived`), `parent_entity_id` *(Spaltungen)*, `archived`, `meta_json`.
2. **`states`**: Eigenschafts-Zustände auf einer Entität:
   * Modalität: `observed` | `predicted` | `desired` | `cancelled`
   * Herkunft: `measured` | `ai_prior` | `derived` | `user_goal` (`measurement_source` zur Korrelations-Deduplizierung)
   * Wert (`value_num ± value_sigma`, `value_unit`, `value_text`), Ort (`lat`, `lon`, `sigma_meters > 0`), Zeit (`time_ms`, `sigma_seconds > 0`)
   * Ziel-Grenzen bei `desired`: `target_min`, `target_max`, `target_preferred`, `target_earliest_ms`, `target_latest_ms`, `target_radius_meters`, `threshold_ok`, `threshold_fail` (strikt getrennt von `sigma_*`!)
3. **`transformations`**: Vorgänge und Vorlagen:
   * Modalität: `happened` | `planned` | `template` | `cancelled`; Treiber: `actor` | `nature`
   * `template_id` *(Kalibrierung)*, `parent_id` *(hierarchische Verschachtelung / HTN)*
   * `duration_seconds ± duration_sigma_seconds`, `duration_source`, `cost`, `success_prob`, `formula_json`
4. **`edges`**: Gerichtete Verknüpfung zwischen `states` und `transformations`:
   * `direction` (`in` | `out`), `role` (`consumes` | `uses` | `triggers` | `produces`)
   * `logic_group` (`AND` | `CHOICE_OR` | `WORLD_OR`), `branch_key`, `is_selected`, `outcome`, `probability`, `resource_released`

---

## 3. Mathematik, Engine & Periodik

### Gauß-Mathematik & Kalibrierung (`math_fusion.py`)
* **1D-Fusion & Quellen-Deduplizierung:** Wiederholte Messungen mit derselben `measurement_source` (z. B. `Handy_Indoor`) schrumpfen `σ` nicht künstlich; unabhängige Quellen fusionieren präzisionsgewichtet (`1 / σ²`).
* **2D-ENU-Ortsfusion & Habitat-Generalisierung:** `fuse_locations` rechnet in lokal-metrischen ENU-Koordinaten; `generalize_habitat` bildet aus verstreuten Einzelfunden einen konservativen Suchradius (`σ_Gebiet`).
* **Fehlerfortpflanzung:** Summen, Linearkombinationen (`recipe_mix`), Produkte (`propagate_product`), Teilungen (`propagate_split`) und Quotienten (`propagate_ratio`).
* **Vorlagen-Lernen (`calibrate_parameter`):** `ai_prior`-Faktoren/Dauern werden ab der ersten echten `measured`-Messung bayessch geschärft und auf `measured` umgestellt.

### Kausale Engine (`engine.py`)
* **Multi-Condition-Trigger (`AND` / `OR`):** Zeitfenster-Aggregation (`latest`, `sum`, `mean`, `min`, `max`, `delta`) plus `period_phase`-Bedingungen.
* **`recipe_mix`, `ratio_guards` & `modifiers`:** Rezept-Massenbilanzen, Verhältniswächter (z. B. Wasser/Holz-Verhältnis) und Umwelt-Modifikatoren (z. B. Temperatur-/Luftfeuchte-Korridor verschiebt Fruchtungsdauer dynamisch über `_reapply_modifiers_for_observation`).
* **Mehrstufige Ketten & verschachtelte Unterschritte (`parent_id`):** Vorwärtspropagation (`propagate_downstream`) aktualisiert nachgelagerte Kettenschritte; Makro-Transformationen aggregieren Dauer/Kosten/Erfolg aus ihren Kindern (`_rollup_composite_tx`); `abort_goal` kaskadiert sauber nach unten (Kinder) und oben (verwaiste Vorstufen) inklusive `uses`-Freigabe.
* **4-Stufen-Zielbewertung (`evaluate_goal`):** Bewertet dynamisch `wann`, `wo`, `wie_viel` und `vorbedingung` (`stage` 2 = `on_track`, 3 = `at_risk`, 4 = `broken`).

### Uhren & Perioden-Detektor (`clocks.py`)
* **7 globale Uhren:** `day_local`, `solar_day` (NOAA-Sonnenhöhe aus `lat, lon`), `week_local`, `month_calendar`, `lunar_synodic` (`29,530589 d`), `tidal_semidiurnal` (`12,4206 h` M2-Tide als 2. Schwebungs-Harmonische aus `f_Tag - f_Mond` mit lokalem `phase_offset_hours`) und `year_solar` (`365,2425 d`).
* **8. Uhr:** `relative_interval` (Abstand `Δt ± σ` direkt auf der Weltlinie mit 10-Minuten-Burst-Deduplizierung).
* **Takt-Analyse (`analyze_periodicity`):** Zirkuläre Konzentration `R`, abgedeckte Zyklen (`≥ 2`), endlicher Rayleigh-Test `p`, `status` (`detected` | `weak` | `none` | `insufficient_data`) und Prognose des nächsten Fensters nach `JETZT`.

---

## 4. Semantischer MCP-Server (`waswowann-mcp`)

Der `stdio`-Server ([`mcp_server.py`](src/metacore/mcp_server.py)) bietet genau 8 Tools mit Pydantic-Validierung und Folgen-Feedback (`MutationFeedback`):

| Tool | Aufgabe |
|---|---|
| `get_focus_briefing` | Lage rund um *Jetzt*: Ziele (Stufe 2–4), `uses`-Konflikte, anstehende Prognosen, offene `ai_prior`s |
| `inspect_worldline` | Historie (`observed`), Zukunft (`predicted`/`desired`), Transformationen, Kanten & `periodicity` |
| `search_templates` | Durchsucht gespeicherte Vorlagen inkl. Kalibrierungsstatus |
| `define_template` | Legt wiederverwendbare `template`-Vorlagen (auch mit `parent_id`-Kindschritten) an |
| `instantiate_template` | Bindet Vorlage auf Entitäten, Startzeit, Mengen & Split |
| `record_observation` | Erfasst Messung/Null-Fund, prüft Trigger, kalibriert Vorlagen, rechnet Folgen neu |
| `set_goal` | Setzt `desired`-Ziel mit Grenzen und bewertet sofort die Machbarkeit |
| `resolve_escalation` | Löst Konflikte per `widen_tolerance`, `switch_branch` oder `abort_goal` |

**MCP-Client-Konfiguration (Beispiel):**
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

## 5. Entwicklung & Tests

```sh
cd metacore
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

.venv/bin/pytest -v
.venv/bin/ruff check src tests
```
