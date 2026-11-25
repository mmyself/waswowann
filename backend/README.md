# WasWoWann Backend

Lokales **FastAPI**-Backend (`backend/`) für Foto-Ingest, EXIF-/Zeit-/Orts-Extraktion, 2D-Kalman-Ortsfusion, Optionale Gemini-/Google-Photos-Anbindung sowie die Datenbrücke und REST-API zur **MetaCore**-Engine.

---

## 1. Struktur & Aufgaben

```text
backend/
├── pyproject.toml
├── .env.example
└── app/
    ├── main.py                      # FastAPI-App, CORS, BrowserMutationGuard, Router-Mount
    ├── config.py                    # Settings (DATABASE_PATH, METACORE_DB_PATH, Zeitzone, OAuth/AI)
    ├── db.py                        # SQLite-Verbindung & Migrationen 001..005 (waswowann.sqlite3)
    ├── cli.py                       # CLI: migrate, seed-demo, import-local, sync-metacore
    ├── schemas.py                   # Pydantic-v2-Schemas
    ├── routes/
    │   ├── observations.py          # CRUD, Detailansicht, Merge & Kandidatensuche
    │   ├── media.py                 # Einzelbilder, Thumbnails, manuelle Messwert-Patches
    │   ├── uploads.py               # Multipart-Foto-/ZIP-Upload + Ingest
    │   ├── imports.py               # Demo- & lokaler Ordner-Import als Hintergrundjobs
    │   ├── google.py                # Google Photos OAuth + Picker-Flow
    │   ├── analysis.py              # Optionale Gemini-/Antigravity-Bildanalyse
    │   └── zukunftsgraph.py         # MetaCore REST-Endpunkte & Perioden-Analyse
    └── services/
        ├── gaussian_fusion.py       # Robuste IRLS-2D-Kalman-Ortsfusion + Indoor-σ-Schutz
        └── metacore_bridge.py       # Idempotente Sync-Brücke nach metacore.sqlite3
```

---

## 2. Datenbrücke: `waswowann.sqlite3` → `metacore.sqlite3`

[`metacore_bridge.py`](app/services/metacore_bridge.py) synchronisiert das bestehende Foto-Archiv (`observations`, `media`, `space_time_gauss`) deterministisch und idempotent in MetaCore-Weltlinien (`obs:<id>`):

* **Indoor-Korrelationsschutz:** Mehrfachfotos derselben Indoor-Beobachtung erhalten eine gemeinsame `measurement_source` (`indoor:<obs_id>`), sodass ihre räumliche Unsicherheit `σ` bei wiederholten Fotos derselben Wohnung **nicht** künstlich schrumpft. Unabhängige Outdoor-Fotos erhalten eigene Quellen und schärfen die Position per Kalman-Fusion.
* **Angebundene Zukunftsgraph-Pläne:** Bei Erst-Synchronisation (`seed_plans=True`) werden reale Funde idempotent mit einem Indoor-Klimaplan (*Igel-Stachelbart*), einer generalisierten Outdoor-Habitat-Prognose (inkl. `year_solar`-Phasenbedingung + Regen/Temperatur) und einer 3-stufigen Pilzzucht-Kette mit verschachtelten `parent_id`-Unterschritten & `CHOICE_OR`-Alternativzweig verknüpft — ohne spätere Nutzermutationen zu überschreiben.

---

## 3. REST-Endpunkte

### Klassisches Archiv & Medien
* `GET /api/health`, `GET /api/config`
* `GET / POST / PATCH /api/observations`, `GET /api/observations/{id}/merge-candidates`, `POST /api/observations/{id}/merge`
* `PATCH / DELETE /api/media/{id}`, `GET /api/media/{id}/image`, `GET /api/media/{id}/thumbnail`
* `POST /api/uploads`, `POST /api/imports/demo`, `POST /api/imports/local`, `GET /api/imports/{job_id}`
* `POST /api/observations/{id}/analysis` *(optional)* & `/api/google/*` *(optional)*

### Zukunftsgraph & Perioden (`/api/zukunftsgraph/*`)
| Methode & Pfad | Funktion |
|---|---|
| `GET /api/zukunftsgraph/snapshot` | Vollständiger Live-Snapshot inkl. `goal_evaluations`, `spatial_predictions`, `seasonal_series`, `current_clocks` und `periodicity` |
| `POST /api/zukunftsgraph/sync` | Manuelle Synchronisation von `waswowann.sqlite3` nach `metacore.sqlite3` |
| `GET /api/zukunftsgraph/worldlines/{item_id}` | Lokale Kausal-Linse inkl. `periodicity` (akzeptiert `obs_id` oder `entity_id`) |
| `POST /api/zukunftsgraph/observations` | Live-Messwert erfassen (stößt Trigger, Kalibrierung & Modifikatoren an) |
| `POST /api/zukunftsgraph/goals` | Neues `desired`-Ziel auf einer Weltlinie anlegen & bewerten |
| `POST /api/zukunftsgraph/escalations/{goal_id}/resolve` | Eskalation lösen (`switch_branch`, `widen_tolerance`, `abort_goal`) |
| `GET /api/zukunftsgraph/periods` | Takt-Analyse auf Archiv, Kategorie oder Weltlinie |
| `POST /api/zukunftsgraph/periods/analyze` | Beliebige `timestamps_ms`-Reihe gegen alle 8 Uhren prüfen |

---

## 4. Konfiguration, CLI & Sicherheit

* Einstellungen in `.env` (Vorlage [`.env.example`](.env.example)), relative Pfade werden immer relativ zu `backend/` aufgelöst (`../data/waswowann.sqlite3`, `../data/metacore.sqlite3`).
* `BrowserMutationGuard` ([`main.py`](app/main.py)) verlangt bei allen schreibenden `/api/*`-Anfragen eine erlaubte `Origin` und den erwarteten `Content-Type`. Nur für lokalen Betrieb (`127.0.0.1`) gedacht.

### CLI-Kommandos
```sh
.venv/bin/python -m app.cli migrate
.venv/bin/python -m app.cli seed-demo
.venv/bin/python -m app.cli import-local
.venv/bin/python -m app.cli sync-metacore
```

---

## 5. Installation & Tests

```sh
cd backend
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install --no-build-isolation --no-deps -e ../metacore

.venv/bin/pytest -v
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
