# WoWannWas – Technischer Umsetzungsplan für den Test-Prototyp

Stand: 2026-11-14
Grundlage: `PROJEKTPLAN.md`

## 1. Ziel und Abgrenzung

Ein lokal startbarer Single-User-Prototyp zeigt Beobachtungen mit Foto, Ort und Zeit auf einer Karte. Die Oberfläche ist ohne Google-Zugang sofort mit reproduzierbaren Demodaten nutzbar. Ein lokaler Fotoordner und ein optional konfigurierter Google Photos Picker liefern weitere Beobachtungen.

**Der erste Durchstich ist fertig, wenn:**

- FastAPI, SQLite und Next.js gemeinsam lokal laufen.
- Ein Demo-Import mit einem Befehl Fotos und Beobachtungen erzeugt.
- Links die Karte, unten der Zeitfilter und rechts eine unabhängig scrollbare Detailansicht funktionieren.
- Auswahl auf Karte oder in einer Liste Foto, Metadaten, Gemini-Platzhalter und Wetter-Platzhalter aktualisiert.
- Ein konfigurierter Google-Zugang die Auswahl, Statusabfrage und den tatsächlichen Bildimport ermöglicht; ohne Zugang bleibt die restliche App voll nutzbar.
- Fehlende Orte und Zeiten sichtbar bleiben und manuell ergänzt werden können.

**Nicht Teil dieses Prototyps:** echte Gemini-Aufrufe, Gemini-Takeout-Import, echte Wetterabfragen, Gauß-/Markow-Fusion, Heatmaps, 3D-Gelände, Benutzerverwaltung, Cloud-Deployment, Hintergrundabgleich der gesamten Fotobibliothek. Unsicherheitsfelder werden gespeichert und angezeigt, aber nicht für Berechnungen oder Filter verwendet.

## 2. Festgelegte Architektur

### 2.1 Komponenten

| Komponente | Entscheidung |
| --- | --- |
| Backend | Python 3.12+, FastAPI, Uvicorn, Pydantic v2 |
| Persistenz | SQLite über Python `sqlite3`; SQL-Migrationen, kein ORM nötig |
| Bildverarbeitung | Pillow mit EXIF-Unterstützung; JPEG, PNG und WebP |
| Google-Zugriff | `httpx`, OAuth Authorization Code Flow über Backend |
| Frontend | Next.js, App Router, TypeScript, React |
| Karte | `maplibre-gl` mit OpenFreeMap-Style |
| Backend-Tests | pytest, httpx/TestClient |
| Frontend-Tests | Vitest/Testing Library für Zustandslogik, Playwright für Browser-Smoke-Tests |

Bei der Implementierung aktuelle stabile, miteinander kompatible Versionen wählen und Python-/npm-Abhängigkeiten in Lockfiles festhalten. Kein `numpy`/`scipy` im ersten Prototyp erforderlich.

### 2.2 Laufzeit und Grenzen

- Frontend: `http://localhost:3000`.
- Backend: `http://localhost:8000`, REST unter `/api`, OpenAPI unter `/docs`.
- Next.js greift im Browser direkt auf FastAPI zu; Aufrufe mit OAuth-Sitzung nutzen `credentials: "include"`.
- CORS erlaubt ausschließlich den konfigurierten Frontend-Origin und Credentials; kein Wildcard-Origin.
- Backend bindet standardmäßig an `127.0.0.1`. Keine LAN- oder Internetfreigabe: Beobachtungs- und Foto-Endpunkte haben im Prototyp keine Benutzer-Authentifizierung.
- Nur ein Uvicorn-Worker: OAuth-Tokens liegen absichtlich ausschließlich im Arbeitsspeicher dieses Prozesses.
- Beide Server immer über `localhost` öffnen, nicht wechselnd über `127.0.0.1`; andernfalls passen Cookies und OAuth-Redirects nicht zusammen.
- Fotos bleiben im Backend-Datenverzeichnis. Next.js benötigt keinen Zugriff auf dessen Dateisystem.
- Die Demo benötigt weder Google noch Gemini noch einen Wetterdienst. Kartenkacheln/Style benötigen weiterhin Internet; bei Kartenausfall bleiben Liste und Details bedienbar.

### 2.3 Projektstruktur, die anschließend angelegt wird

```text
WoWannWas/
  PROJEKTPLAN.md
  UMSETZUNGSPLAN.md
  backend/
    pyproject.toml
    .env.example
    app/
      main.py
      config.py
      db.py
      schemas.py
      routes/
        observations.py
        media.py
        imports.py
        google.py
      services/
        photo_ingest.py
        demo_data.py
        local_import.py
        google_oauth.py
        google_picker.py
        import_jobs.py
      migrations/
        001_initial.sql
      cli.py
    tests/
  frontend/
    package.json
    .env.local.example
    src/
      app/
        layout.tsx
        page.tsx
        globals.css
      components/
        Workspace.tsx
        ObservationMap.tsx
        TimeFilter.tsx
        ObservationList.tsx
        ObservationDetails.tsx
        ImportControls.tsx
        WeatherPlaceholder.tsx
      lib/
        api.ts
        types.ts
        time-filter.ts
    tests/
  data/                         # ignoriert: SQLite, Fotos, temporäre Downloads
    photos/
    incoming/
    tmp/
```

Die Aufteilung trennt HTTP-Routen von gemeinsamem Fotoimport. Keine generische Repository-/Provider-Abstraktion für diesen Durchstich. Lokaler Import und Google-Import rufen denselben Foto-Ingest-Service auf.

## 3. Datenmodell und SQLite-Schema

### 3.1 Konventionen

- IDs: UUID als `TEXT`, im Backend erzeugt.
- Datenbankzeiten: UTC-Unix-Millisekunden als `INTEGER`; API-Zeiten: RFC-3339 mit `Z` oder explizitem Offset.
- `created_at_ms` ist der Import-/Anlagezeitpunkt, niemals Ersatz für eine unbekannte Aufnahmezeit.
- `observed_at_ms`, Ort und Unsicherheiten dürfen unbekannt sein. Fehlende Werte sind `NULL`, nicht `0`, nicht `(0, 0)` und nicht die aktuelle Zeit.
- `sigma_meters` und `sigma_seconds` sind optionale positive Standardabweichungen. Keine künstlich präzisen EXIF- oder Demo-Unsicherheiten erfinden.
- Kategorien sind im Prototyp `Pilz`, `Pflanze`, `Person`, `Ding`, `Ereignis`, `Unbekannt`.
- Jede Beobachtung erhält eine `space_time_gauss`-Zeile; deren Messwerte dürfen fehlen.
- Ein importiertes Foto erzeugt eine Beobachtung. Mehrere Fotos pro Beobachtung sind im Schema möglich, aber noch nicht Teil der Bearbeitungsoberfläche.
- JSON-Felder speichern serialisiertes JSON; Datentypen werden an der API-/Service-Grenze mit Pydantic geprüft.

### 3.2 Initialmigration `001_initial.sql`

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'Unbekannt'
        CHECK (category IN ('Pilz', 'Pflanze', 'Person', 'Ding', 'Ereignis', 'Unbekannt')),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE space_time_gauss (
    obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    lat REAL CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
    lon REAL CHECK (lon IS NULL OR lon BETWEEN -180 AND 180),
    sigma_meters REAL CHECK (sigma_meters IS NULL OR sigma_meters > 0),
    observed_at_ms INTEGER,
    sigma_seconds REAL CHECK (sigma_seconds IS NULL OR sigma_seconds > 0),
    location_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (location_source IN ('unknown', 'exif', 'manual', 'demo')),
    time_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (time_source IN ('unknown', 'exif', 'google', 'manual', 'demo')),
    fused_group_id TEXT,
    CHECK ((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)),
    CHECK (sigma_meters IS NULL OR lat IS NOT NULL),
    CHECK (sigma_seconds IS NULL OR observed_at_ms IS NOT NULL)
);

CREATE TABLE media (
    id TEXT PRIMARY KEY,
    obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('demo', 'local', 'google_photos')),
    external_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    local_path TEXT NOT NULL,
    thumbnail_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    sha256 TEXT NOT NULL,
    exif_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX idx_media_obs ON media(obs_id);

CREATE TABLE gemini_notes (
    id TEXT PRIMARY KEY,
    obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('placeholder', 'takeout_import', 'gemini_api')),
    status TEXT NOT NULL CHECK (status IN ('not_requested', 'ready', 'error')),
    raw_text TEXT NOT NULL DEFAULT '',
    structured_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_gemini_obs ON gemini_notes(obs_id);

CREATE TABLE weather_slices (
    obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (status IN ('not_requested', 'ready', 'error')),
    anchor_at_ms INTEGER,
    slice_24h_json TEXT NOT NULL DEFAULT '[]',
    slice_7d_json TEXT NOT NULL DEFAULT '[]',
    slice_12w_json TEXT NOT NULL DEFAULT '[]',
    fetched_at_ms INTEGER
);

CREATE TABLE picker_sessions (
    id TEXT PRIMARY KEY,
    google_session_id TEXT NOT NULL UNIQUE,
    owner_session_hash TEXT NOT NULL,
    picker_uri TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'waiting', 'ready', 'importing', 'completed', 'cancelled', 'expired', 'failed'
    )),
    poll_interval_ms INTEGER NOT NULL,
    polling_deadline_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    next_poll_at_ms INTEGER,
    last_error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE import_jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('demo', 'local', 'google_photos')),
    picker_session_id TEXT REFERENCES picker_sessions(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial', 'failed')),
    total INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER
);
CREATE UNIQUE INDEX idx_one_job_per_picker_session
    ON import_jobs(picker_session_id) WHERE picker_session_id IS NOT NULL;
CREATE INDEX idx_space_time_observed ON space_time_gauss(observed_at_ms);
CREATE INDEX idx_space_time_location ON space_time_gauss(lat, lon);
```

Migrationen beim Backend-Start vor Annahme von Requests ausführen; `PRAGMA user_version` hält die angewandte Versionsnummer fest. Zusätzlich bei **jeder Verbindung** `foreign_keys=ON`, `busy_timeout=5000` setzen; Datenbank einmal auf WAL-Modus stellen. Pro Request/Job-Arbeitsschritt eine eigene Verbindung, keine quer über Threads geteilte Connection. SQLite-Schreibtransaktionen kurz halten; kein HTTP-Download innerhalb einer offenen Transaktion.

Beim Anlegen einer Beobachtung auch einen Gemini-Platzhalter (`status=not_requested`) und Wetter-Platzhalter anlegen. `fused_group_id` bleibt ungenutzt und `NULL`.

## 4. Backend-Verträge

### 4.1 Allgemeine Regeln

- Alle fachlichen Routen beginnen mit `/api`.
- Pydantic prüft Koordinaten, Kategorien, Textlängen und RFC-3339-Zeiten. Naive API-Zeitangaben ohne Offset ablehnen.
- Ein leerer Titel ist unzulässig; Titel maximal 200, Beschreibung maximal 10.000 Zeichen.
- Fehlerformat für eigene Fachfehler: `{"error":{"code":"...","message":"..."}}`; FastAPI-Validierungsfehler bleiben dokumentierte `422`-Antworten.
- Unbekannte IDs: `404`; ungültiger Sessionzustand/aktiver Import: `409`; Google nicht konfiguriert: `503`; fehlende/abgelaufene Google-Anmeldung: `401`.
- Mutierende Browser-Aufrufe nur als JSON und mit exakt erlaubtem `Origin`; dadurch bleiben lokale Import- und OAuth-Aktionen vor fremden Webseiten geschützt. OAuth-Callback ist davon ausgenommen und wird über `state` geschützt. CLI-Import läuft direkt über den Service, nicht über einen Origin-losen HTTP-Sonderweg.
- Keine absoluten Dateipfade, Google-Tokens oder privaten Download-URLs in fachlichen API-Antworten oder Logs.

### 4.2 Endpunktübersicht

| Methode und Pfad | Zweck / Antwort |
| --- | --- |
| `GET /api/health` | Prozess und DB erreichbar; `200 {"status":"ok"}` |
| `GET /api/config` | Öffentliche Fähigkeiten: `google_photos_enabled`, `local_import_enabled`, `demo_enabled`; keine Secrets/Pfade |
| `GET /api/observations` | Leichte Liste aller Beobachtungen plus Zeitbereich und Zähler |
| `GET /api/observations/{id}` | Details mit Medien, Unsicherheiten, Gemini- und Wetterzustand |
| `POST /api/observations` | Manuelle Beobachtung auch ohne Foto anlegen; `201` mit Detailobjekt |
| `PATCH /api/observations/{id}` | Titel, Kategorie, Beschreibung, Ort, Zeit und Unsicherheiten ändern |
| `GET /api/media/{id}/image` | Lokale Anzeigedatei mit korrektem Content-Type |
| `GET /api/media/{id}/thumbnail` | Lokales Vorschaubild |
| `POST /api/imports/demo` | Idempotenten Demo-Import starten; `202 {job_id}` |
| `POST /api/imports/local` | Konfigurierten lokalen Ordner importieren; `202 {job_id}` |
| `GET /api/imports/{job_id}` | Fortschritt, Ergebniszähler und bereinigte Einzelfehler |
| `GET /api/google/auth/status` | `enabled`, `authenticated`, optional `expires_at` |
| `GET /api/google/oauth/start` | Browsernavigation zum Google-OAuth-Consent umleiten |
| `GET /api/google/oauth/callback` | Code/State prüfen, Token tauschen, zum Frontend umleiten |
| `POST /api/google/picker/sessions` | Echte Google-Auswahlsitzung erstellen; `201` |
| `GET /api/google/picker/sessions/{id}` | Gedrosselt Google-Status abfragen und lokalen Status liefern |
| `POST /api/google/picker/sessions/{id}/import` | Nur bei `ready` Importjob starten; `202 {job_id}` |
| `DELETE /api/google/picker/sessions/{id}` | Auswahl abbrechen und Google-Session bestmöglich aufräumen; `204` |

Keine Löschfunktion für Beobachtungen im ersten Durchstich; so entfällt ein unnötiger destruktiver UI-Pfad. Keine beliebigen Upload- oder Ordnerpfad-Endpunkte.

### 4.3 Beobachtungs-DTOs

Die Liste enthält für maximal etwa 1.000 Prototyp-Beobachtungen alle leichten Datensätze, **nicht** Bildbytes, EXIF-Blöcke oder lange Notizen. Filtern erfolgt für diese Größe im Browser; dadurch gibt es keine Request-Flut beim Schieben.

```json
{
  "items": [
    {
      "id": "uuid",
      "title": "Beispiel-Fund",
      "category": "Pilz",
      "lat": 52.52,
      "lon": 13.405,
      "observed_at": "2026-10-18T09:30:00Z",
      "thumbnail_url": "/api/media/media-uuid/thumbnail",
      "source": "demo"
    }
  ],
  "total": 1,
  "located_count": 1,
  "undated_count": 0,
  "time_extent": {
    "min": "2026-10-18T09:30:00Z",
    "max": "2026-10-18T09:30:00Z"
  }
}
```

`lat`, `lon`, `observed_at`, `thumbnail_url` und bei manuellen Beobachtungen `source` sind nullable. `time_extent` ist `null`, wenn keine Beobachtung datiert ist. Relative Medien-URLs löst das Frontend gegen den Backend-Origin auf.

Das Detailobjekt ergänzt:

- `description`, `created_at`, `updated_at`.
- `space_time`: Koordinaten, `observed_at`, `sigma_meters`, `sigma_seconds`, `location_source`, `time_source`.
- `media[]`: ID, Dateiname, Quelle, Abmessungen, `image_url`, `thumbnail_url`, ausgewählte lesbare EXIF-Felder.
- `gemini`: `status`, `source`, `text`, `confidence` (vorerst `null`), `tags` (vorerst `[]`).
- `weather`: `status`, `anchor_at`, `slices: {"24h": [], "7d": [], "12w": []}`.

`POST` akzeptiert `title`, optionale Kategorie/Beschreibung und ein optionales `space_time`-Objekt. `PATCH` aktualisiert nur übergebene Felder; explizites `null` löscht einen Wert. Ort wird stets als Paar übergeben/gelöscht. Beim Entfernen von Ort/Zeit zugehörige Sigma-Werte mit löschen. Bei Änderungen des Orts oder Zeitpunkts Wettercache leeren und `status=not_requested` setzen. Nach jeder Änderung ein vollständiges Detailobjekt zurückgeben und die Liste neu laden.

### 4.4 Foto-Ingest und Auslieferung

Ein gemeinsamer Ingest-Service erhält Quelle, externe ID, Dateibytes/-pfad und vorhandene Metadaten:

1. Deduplizierung prüfen: Google über Picker-Medien-ID, lokal über SHA-256 des Originals, Demo über feste Fixture-ID. Dieselben Bytes aus unterschiedlichen Quellen dürfen im Prototyp getrennte Beobachtungen ergeben.
2. Datei maximal 25 MiB; Pixelzahl maximal 40 Megapixel. Dateiendung allein nicht vertrauen: tatsächlichen Bildinhalt mit Pillow prüfen. Videos, HEIC und RAW zunächst mit verständlichem Einzelfehler überspringen.
3. EXIF vor Orientierungskorrektur auslesen; GPS nur verwenden, wenn vollständig und plausibel. Orientierung mit `ImageOps.exif_transpose` korrigieren.
4. Zeit ermitteln: Google-`createTime` bevorzugen, sonst EXIF `DateTimeOriginal` plus Offset. Bei fehlendem EXIF-Offset `IMPORT_TIMEZONE=Europe/Berlin` als **explizite Importannahme** verwenden und in `exif_json` vermerken. Bei mehrdeutiger/nicht existierender Sommerzeit oder fehlendem Zeitwert Zeitpunkt offen lassen statt raten.
5. Anzeige-JPEG mit maximal 2.560 Pixeln längster Kante und Thumbnail mit maximal 480 Pixeln erzeugen; Alpha auf neutralem Hintergrund zusammensetzen. Anzeigevarianten ohne EXIF speichern. Roh-EXIF auf relevante, JSON-fähige Felder begrenzen.
6. Beide Bilddateien zunächst unter zufälligen temporären Namen schreiben und atomar in `data/photos/{media_id}/` verschieben. DB-Zeilen für Beobachtung und Platzhalter in einer Transaktion schreiben; bei Fehlschlag eigene neu erzeugte Dateien entfernen.
7. Originale im Nutzerordner niemals verändern. Google-Originaldownloads nach erfolgreicher Verarbeitung aus dem temporären Verzeichnis entfernen. Der Prototyp ist kein Originalfoto-Archiv.

Gespeicherte Pfade sind relativ zum konfigurierten Datenverzeichnis und werden ausschließlich vom Backend erzeugt. Medienrouten lösen nur bekannte DB-IDs auf und prüfen den aufgelösten Pfad gegen das Datenverzeichnis. Keine freie Pfadannahme über Queryparameter. `FileResponse`, `Cache-Control: private, max-age=3600` und `X-Content-Type-Options: nosniff` verwenden.

### 4.5 Importjobs ohne zusätzliche Infrastruktur

- Keine Redis-/Celery-Abhängigkeit. Ein prozesslokaler Worker verarbeitet persistierte Jobs nacheinander; Start über eine In-Process-Task, blockierende Pillow-/SQLite-Arbeit in einem Thread.
- Job zuerst in SQLite schreiben, dann Verarbeitung starten. Einfache globale Sperre: bei bereits aktivem Import liefert ein neuer Start `409 IMPORT_BUSY`, außer derselbe Pickerjob existiert bereits.
- Zähler nach jeder Datei persistieren. `processed = imported + skipped + failed`; `total` darf während der Google-Paginierung noch `null` sein.
- Einzelfehler stoppen nicht den gesamten Import. Erfolgreich mit Einzelfehlern: `partial`; alle Dateien gescheitert oder fataler Ablaufabbruch: `failed`; ansonsten `completed`.
- Bei Prozessstart alte `queued`/`running`-Jobs auf `failed` mit `PROCESS_RESTARTED` setzen. Kein dauerhaft zuverlässiger Hintergrundbetrieb behaupten.
- Demo-/Ordnerimport kann nach Neustart erneut gestartet werden; bereits importierte Dateien werden übersprungen. Google-Auswahl bei verlorener Sitzung erneut durchführen; persistente Medien-IDs verhindern Duplikate.

## 5. Sofort nutzbare Daten: Demo und lokaler Ordner

### 5.1 Deterministischer Demo-Modus

`python -m app.cli seed-demo` und der UI-Button „Demodaten laden“ rufen denselben Service auf.

- 16 Fixtures mit festen IDs, Titeln und Zeit-/Ortswerten ausliefern.
- 14 mit Koordinaten in drei nahe gelegenen Clustern, zwei ausdrücklich ohne Ort.
- 15 mit Aufnahmezeit über etwa zwölf Wochen verteilt; eine ohne Datum. Gleiche Zeitpunkte und gleiche Koordinaten bewusst mindestens einmal enthalten.
- Kategorien mischen; Sigma-Werte teils leer, teils als klar synthetische Demowerte hinterlegen.
- Fotos mit Pillow aus einfachen farbigen Formen und Beschriftung generieren. Sichtbarer Hinweis „Synthetisches Demobild – kein Naturfoto“. Keine externen Downloads und keine vorgetäuschte Artbestimmung.
- Feste Fixture-Zeiten verwenden, kein zeitabhängiges „heute minus …“: Tests bleiben reproduzierbar. Zeitfilter richtet sich nach Datenumfang, nicht nach Tagesdatum.
- Gemini und Wetter bleiben echte `not_requested`-Platzhalter, keine erfundenen Ergebnisse.
- Wiederholtes Seed verändert existierende, gegebenenfalls manuell bearbeitete Demo-Beobachtungen nicht und erzeugt keine Duplikate.

### 5.2 Lokaler Import

`LOCAL_IMPORT_DIR` konfiguriert serverseitig einen Ordner. UI-Button „Lokalen Ordner importieren“ zeigt nur an, ob er eingerichtet ist; kein Freitextpfad und kein System-Dateidialog mit falschem Browser-Dateisystemversprechen.

- Rekursiv unterstützte Bilddateien einlesen, Symlinks nicht verfolgen und aufgelöste Pfade auf Zugehörigkeit zum konfigurierten Ordner prüfen.
- Ohne gültige Konfiguration ist die Funktion deaktiviert; beim Aufruf `503 LOCAL_IMPORT_NOT_CONFIGURED`.
- GPS aus lokal vorhandenen EXIF-Daten übernehmen; fehlende Daten bleiben offen.
- Ohne EXIF-Zeit weder Datei-mtime noch Importzeit als Aufnahmezeit ausgeben.
- Fortschritt und Ergebnis anzeigen, beispielsweise „8 importiert, 2 bereits vorhanden, 1 nicht unterstützt“.
- Keine Beobachtungen außerhalb des Ordners lesen; Fotos im Originalordner nicht umbenennen, verschieben oder löschen.

## 6. Google Photos: OAuth, Picker und Import

### 6.1 Wichtige Korrektur gegenüber einer Annahme im Projektplan

Der Picker erlaubt nur ausdrücklich ausgewählte Medien, keinen allgemeinen Bibliothekszugriff. Sein Metadatenschema bietet **keine GPS-Felder**; der Fotodownload über `baseUrl=d` entfernt Standortmetadaten. Deshalb darf der Google-Import keinen automatischen Standort versprechen. Aufnahmezeit aus `createTime` nutzen, Ort im Regelfall offen lassen und über „Ort ergänzen“ editierbar machen. Lokale Originaldateien sind der verlässliche Testweg für vorhandenes EXIF-GPS.

Keine Google Library API und keinen alten `photoslibrary.readonly`-Scope implementieren.

### 6.2 Konfiguration und OAuth-Vorbereitung

In Google Cloud manuell vorbereiten:

1. Google Photos Picker API aktivieren.
2. OAuth-Consent konfigurieren; im Testbetrieb das verwendete Google-Konto als Testnutzer eintragen, soweit der gewählte Consent-Typ dies erfordert.
3. OAuth-Client vom Typ Webanwendung erstellen.
4. Redirect-URI exakt auf `http://localhost:8000/api/google/oauth/callback` setzen.
5. Client-ID/-Secret ausschließlich im Backend hinterlegen.
6. Scope: `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.

OAuth Authorization Code Flow über Backend implementieren:

- Beim Start zufällige opaque App-Session-ID als `HttpOnly`, host-only, `SameSite=Lax`-Cookie setzen; `Secure` bei HTTPS, lokal über HTTP ausnahmsweise aus.
- Zufälliges einmaliges `state` erzeugen und zusammen mit Ablaufzeit von zehn Minuten an die App-Session binden; zusätzlich PKCE mit S256 verwenden.
- Callback prüft `state`, Bindung und Ablauf **vor** Tokenaustausch; `state` anschließend verbrauchen. Consent-Abbruch verständlich zur UI zurückmelden.
- Tokenaustausch und PKCE-Verifier bleiben im Backend. Access-Token und ggf. erhaltene Refresh-Tokens nur im Prozessspeicher halten; in DB nur Hash der zugehörigen App-Session-ID verwenden.
- Für den Prototyp keinen dauerhaften Offline-Zugriff verlangen. Falls kein verwendbarer Token mehr vorhanden ist, `401 GOOGLE_AUTH_REQUIRED` und erneute Anmeldung anbieten; kein stiller Endlos-Retry.
- Nach Callback ausschließlich auf fest konfigurierten Frontend-Origin umleiten, z. B. `/?google=connected`; keine freien Redirect-Ziele und keine Tokens in der URL.
- Ohne Credentials liefert Auth-Status `enabled=false`; die App startet trotzdem normal. Ein Backend-Neustart beendet bewusst die Google-Anmeldung.
- Jeder Picker-Endpunkt und dessen Importstatus prüfen die Zugehörigkeit zur angemeldeten App-Session. Eine fremde lokale Picker-ID ist nicht zugänglich.

### 6.3 Google-Endpunkte und Ablauf

Echter Picker-Service: `https://photospicker.googleapis.com/v1`, **nicht** `photoslibrary.googleapis.com`.

1. **Session erzeugen:** Backend ruft `POST /sessions` mit Bearer-Token auf und speichert Google-ID, `pickerUri`, `expireTime`, `pollingConfig.pollInterval` und `pollingConfig.timeoutIn` in normalisierter Form.
2. **UI-Antwort:** `{id, status: "waiting", picker_url, poll_interval_ms, polling_deadline, expires_at}`. `id` ist die lokale UUID, nicht die Google-ID. Google-Dauerstrings korrekt parsen, keine feste Pollfrequenz unterstellen.
3. **Auswahl öffnen:** Nach Sessionanlage einen sichtbaren Link/Button „Google-Auswahl öffnen“ in neuem Tab anbieten. Das vermeidet Popup-Blockierung nach einem asynchronen Request. Nicht in ein iframe einbetten; kein automatisches Schließen voraussetzen.
4. **Poll:** Browser fragt den lokalen Status frühestens nach dem vorgegebenen Intervall ab. Backend ruft `GET /sessions/{googleSessionId}` nur auf, wenn `next_poll_at_ms` erreicht ist; vorher liefert es den gespeicherten Zustand. `mediaItemsSet=true` ergibt `ready`.
5. **Warten beenden:** Bei `timeoutIn` automatische Abfragen stoppen und „Wartezeit abgelaufen – Status erneut prüfen oder neue Auswahl starten“ zeigen. Ein Poll-Timeout ist nicht automatisch der Ablauf der Google-Session. Erst `expireTime` oder entsprechender Google-Fehler setzt `expired`.
6. **Import starten:** UI zeigt „Auswahl importieren“. Backend erlaubt den Start nur bei `ready`, erstellt atomar genau einen Job und setzt Session auf `importing`. Ein wiederholter Start derselben Session gibt den vorhandenen Job zurück.
7. **Auswahl lesen:** `GET /mediaItems?sessionId={googleSessionId}`, alle Seiten über `nextPageToken` abrufen. Keine Annahme, dass alle Medien auf eine Seite passen.
8. **Bytes laden:** Für Fotoeinträge `mediaFile.baseUrl` mit Downloadsuffix `=d` und gültigem Bearer-Token abrufen; Zeit aus `createTime`, Name/MIME aus `mediaFile`. URLs erst kurz vor Verarbeitung nutzen; sie sind ungefähr 60 Minuten gültig und können früher ungültig werden.
9. **Importieren:** Streaming-Download mit Größenlimit, anschließend gemeinsamer Ingest-Service. Nicht unterstützte Medientypen als übersprungen melden. API-Name/MIME nicht als Ersatz für die Bildvalidierung behandeln.
10. **Abschluss:** Erst nach Übernahme aller benötigten Bytes `DELETE /sessions/{googleSessionId}` aufrufen. Lokale Beobachtungen bleiben erhalten. Session bei erfolgreichem/teilweisem Import `completed`, bei fatalem Abbruch `failed`.

Kein Google-Bild direkt per `<img src=baseUrl>` laden: Authorization, Ablauf und Datenschutz gehören ins Backend. Keine vom Browser frei gelieferten Download-URLs akzeptieren. Bei HTTP-Redirects Tokens niemals an einen anderen Host weiterreichen; Redirects ohne Authorization behandeln und den Google-Ablauf im Integrationstest verifizieren.

### 6.4 Fehler- und Aufräumverhalten

- Google-`401`: Anmeldung erneuern; Import gegebenenfalls mit verständlichem Fehler abschließen.
- Google-`403`: fehlenden Scope, Testnutzer-/API-Freigabe oder verweigerte Berechtigung anzeigen.
- Google-`429`: `Retry-After` beachten; nur begrenzte Wiederholungen, keine Poll-Schleife ohne Grenze.
- Netzwerk-/`5xx`-Fehler: maximal drei Versuche mit Backoff; Einzelbildfehler protokollieren und andere Bilder weiterverarbeiten.
- Nutzerabbruch: lokales Polling stoppen und Session löschen. Bei aktivem Import keine Löschung zulassen (`409`), damit benötigte Download-URLs nicht vorzeitig ungültig werden.
- Fehlgeschlagenes Google-Cleanup darf bereits importierte Fotos nicht zurückrollen. Bereinigten Fehlercode speichern; beim nächsten angemeldeten Zugriff vor Sessionablauf noch einmal versuchen.
- Abgelaufene oder nach Backend-Neustart nicht mehr nutzbare Sitzungen sichtbar beenden; ohne Token kann die App keine Remote-Löschung garantieren. Keine automatische Fortsetzung versprechen.

Für automatisierte Tests Google-HTTP-Antworten mocken; der UI-Demo-Modus benötigt **keine** vorgetäuschten OAuth-Tokens und keine Fake-Google-Session.

## 7. Frontend: Layout und Verhalten

### 7.1 Desktop-Grundlayout

```text
+-----------------------------------------------------------+
| WoWannWas     Demo / Lokaler Ordner / Google Photos         |
+--------------------------------------+--------------------+
|                                      | Detail-Sidebar     |
| Karte                                | großes Foto        |
|                                      | Titel / Metadaten  |
| Marker                               | Ort/Zeit ergänzen  |
|                                      | Gemini-Platzhalter |
|                                      | Wetter 24h/7d/12w  |
+--------------------------------------+                    |
| Alle anzeigen / Zeitfenster           | separat scrollbar  |
| Start [--------- Zeit --------] Ende  |                    |
| sichtbare Funde / Datumsbereich       |                    |
+--------------------------------------+--------------------+
```

- Workspace als Grid: `minmax(0, 1fr) 380px`, Inhaltsbereich mit `min-height: 0`.
- Linke Spalte: Karte flexibel, Zeitfilter darunter; rechte Spalte spannt die volle Inhaltshöhe.
- Sidebar: `overflow-y: auto`; Scrollen verändert nicht Karte oder Zeitfilter.
- Kopfzeile kompakt. Unterhalb von etwa 900 px einspaltig: Karte mit ca. 45–50 vh, Zeitfilter, danach Details; keine unlesbar schmale Seitenspalte.
- Eindeutige Lade-, Leer- und Fehlerzustände; fehlende Daten als „Unbekannt“ statt leere Werte zeigen.

### 7.2 Karte

- `ObservationMap.tsx` als Client-Komponente, per `dynamic(..., {ssr:false})` laden. MapLibre niemals während Server Rendering initialisieren.
- MapLibre-CSS einbinden und Karteninstanz einmal je Mount erzeugen; beim Unmount entfernen. Größenänderungen mit `ResizeObserver`/`map.resize()` berücksichtigen.
- OpenFreeMap-Style zunächst `https://tiles.openfreemap.org/styles/liberty`. URL konfigurierbar halten; Anbieter-/OpenStreetMap-Attribution sichtbar lassen.
- Beobachtungen in einer GeoJSON-Source mit Circle-Layern darstellen, nicht als hunderte React-DOM-Marker. Feature-ID entspricht Beobachtungs-ID; Geometrie immer `[lon, lat]`.
- Nur Beobachtungen mit gültigem Ort und passendem Zeitfilter in die Source schreiben. Kartenprojektion kann Polnähe nicht vollständig darstellen: Punkte jenseits des darstellbaren Mercator-Bereichs aus der Karte ausnehmen, aber in der Liste behalten und kennzeichnen.
- Ausgewählten Marker durch eigene Kontur/Größe hervorheben. Keine Heatmap und kein Clustering nötig für 16–1.000 Testbeobachtungen.
- Initial auf alle kartierbaren Beobachtungen zoomen; bei genau einem Punkt sinnvollen Maximalzoom begrenzen. Bei null Punkten Standardausschnitt Mitteleuropa.
- Filterbewegungen setzen GeoJSON neu, aber verändern nicht ständig Zoom/Ausschnitt. „Auf sichtbare Funde zoomen“ als explizite Aktion anbieten.
- Bei gleichem Ort zeigt ein Klick eine kleine Auswahlliste der getroffenen Features statt immer nur denselben Fund.
- Kartenfehler in einem Hinweis anzeigen; Beobachtungsliste bleibt der alternative Auswahlweg.

### 7.3 Zeitfilter

**Zwei explizite Modi:**

- **Alle anzeigen:** Standard nach dem ersten Laden; alle datierten und undatierten Beobachtungen sichtbar, Zeitfensterregler deaktiviert.
- **Zeitfenster:** zwei Grenzen `start` und `end`, beide inklusive. Initial auf gesamten bekannten Datenzeitraum gesetzt. Zwei beschriftete native Range-Inputs plus exakte Datum-/Zeitfelder genügen; kein komplexes Timeline-Paket.

Regeln:

- Zeitfilter arbeitet auf `observed_at`, niemals auf `created_at`.
- Regel: `start <= observed_at <= end`; Sigma-Werte verändern diese Regel im Prototyp nicht.
- Eingaben und Anzeige verwenden eine klar ausgewiesene UI-Zeitzone (`Europe/Berlin`); Filtervergleiche erfolgen ausschließlich in UTC-Millisekunden. Lokale Datumseingaben mit einer Zeitzonenbibliothek, z. B. Luxon, umrechnen; ungültige/mehrdeutige DST-Zeiten nicht stillschweigend verschieben.
- UI verhindert `start > end` und zeigt den aktiven Zeitraum als Text. Range-Schritt für den groben Prototyp eine Stunde; exakte Zeitfelder unterstützen Minuten und Endpunkte einschließlich Datenmaximum.
- Undatierte Beobachtungen im Fenstermodus ausschließen und Anzahl mit Hinweis „ohne Datum – im Zeitfenster ausgeblendet“ anzeigen. Im Alle-Modus gehören sie dazu.
- Bei leerer/komplett undatierter Datenmenge ist Fenstermodus deaktiviert. Bei genau einem bekannten Zeitpunkt diesen als einziges Fenster zulassen; keine Division durch null.
- API einmal laden, sichtbare IDs aus State mit `useMemo` ableiten. Map und Liste nutzen dieselbe gefilterte Menge; die Karte zusätzlich nur kartierbare Punkte.
- Nach einem Import Liste und Zeitumfang neu laden. Im Alle-Modus alles einschließen; ein bewusst gesetztes Fenster nicht ungefragt erweitern. Falls sein Datenbezug entfällt, Grenzen nachvollziehbar auf den neuen Umfang begrenzen.

### 7.4 Auswahl, Liste und Sidebar

`Workspace.tsx` hält `observations`, `filterMode`, `timeWindow`, `selectedObservationId`, `selectedDetail` und Importzustand. Keine globale State-Library erforderlich.

- Eine kompakte aufklappbare Beobachtungsliste unter/über der Karte erlaubt Auswahl auch ohne Koordinaten. Zeigt Titel, Datum und Hinweis „Ort fehlt“.
- Marker-/Listenklick setzt dieselbe Auswahl-ID und lädt das Detailobjekt. Überholte Detailrequests mit `AbortController` abbrechen, damit schnelles Klicken nicht die falschen Details zeigt.
- Nach Filterung unsichtbare Auswahl zurücksetzen und Auswahlhinweis zeigen; keine Details eines ausgeblendeten Fundes unbemerkt stehen lassen.
- Ohne Auswahl: „Fund auf der Karte oder in der Liste auswählen“.
- Foto groß, Seitenverhältnis erhalten, `object-fit: contain`, maximal etwa 420 px Höhe. Original-Anzeigevariante bei Bedarf in neuem Tab öffnen. Kein Foto: neutraler Platzhalter.
- Metadaten: Titel, Kategorie, Beschreibung, Aufnahmezeit mit Zeitzone, Koordinaten, Herkunft, Bildmaße, Sigma-Werte samt Einheit. `NULL`-Sigma als „nicht angegeben“, nicht als null Meter Unsicherheit darstellen.
- Einfache Bearbeitungsmaske für Titel, Kategorie, Beschreibung, Koordinaten, Datum/Zeit und optionale Sigma-Werte. Koordinaten zunächst numerisch editieren; kein Karten-Pin-Editor notwendig.
- „Gemini-Analyse – noch nicht angebunden“ mit Platzhaltertext anzeigen; Konfidenz und Tags explizit „noch nicht vorhanden“. Keine falsche KI-Aussage über das Bild.

### 7.5 Wetter-Scheiben-Platzhalter

Tabs/Segmentauswahl `24 h`, `7 Tage`, `12 Wochen`. Für alle drei denselben leeren Zustand zeigen: „Wetterdaten werden in einer späteren Ausbaustufe geladen“.

Die späteren Zeiträume bereits eindeutig definieren:

- 24 h: 24 Intervalle à eine Stunde.
- 7 Tage: sieben Intervalle à 24 Stunden.
- 12 Wochen: zwölf Intervalle à sieben Tage.
- Alle relativ zum Beobachtungszeitpunkt `T`, insgesamt `[T - Dauer, T)`, nicht relativ zum heutigen Tag. Tages-/Wochenwerte sind damit rollierende Zeitabschnitte, keine Kalenderwochen.

Ohne Ort oder Zeit zusätzlich „Ort und Aufnahmezeit erforderlich“. Noch keine Diagramme, keine leeren Achsen und keine erfundenen Wetterwerte. Für die spätere Befüllung einen Bucket-Vertrag vorsehen: `start`, `end`, `temperature_mean_c`, `precipitation_sum_mm`, `humidity_mean_pct`; unbekannte Messwerte bleiben `null`.

## 8. Konfiguration und Startablauf

### 8.1 Backend `.env.example`

```dotenv
HOST=127.0.0.1
PORT=8000
FRONTEND_ORIGIN=http://localhost:3000
DATABASE_PATH=../data/wowannwas.sqlite3
PHOTO_STORAGE_DIR=../data/photos
TEMP_STORAGE_DIR=../data/tmp
LOCAL_IMPORT_DIR=../data/incoming
IMPORT_TIMEZONE=Europe/Berlin
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8000/api/google/oauth/callback
```

Relative Dateipfade immer gegen das feste Backend-Projektverzeichnis auflösen, nicht gegen das zufällige aktuelle Arbeitsverzeichnis. `.env` explizit aus diesem Verzeichnis laden. Die angegebenen Datenpfade zeigen damit in das gemeinsame `WoWannWas/data/`.

### 8.2 Frontend `.env.local.example`

```dotenv
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_MAP_STYLE_URL=https://tiles.openfreemap.org/styles/liberty
NEXT_PUBLIC_DISPLAY_TIMEZONE=Europe/Berlin
```

Keine Secrets mit `NEXT_PUBLIC_` präfixieren. `.env`, `.env.local`, `data/`, Python-Virtualenv und `node_modules/` später in `.gitignore` aufnehmen.

### 8.3 Geplanter erster Start

Die folgenden Befehle werden durch die Implementierung bereitgestellt; aktuell existieren noch keine ausführbaren App-Dateien.

```sh
# Terminal 1, aus dem Projektverzeichnis:
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python -m app.cli migrate
python -m app.cli seed-demo
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2, aus dem Projektverzeichnis:
cd frontend
npm ci
cp .env.local.example .env.local
npm run dev
```

`npm ci` setzt das bei der Initialisierung erzeugte `package-lock.json` voraus. App unter `http://localhost:3000` öffnen. Ohne Google-Konfiguration muss sie unmittelbar Karte, Demofunde und Details anzeigen. Ein leerer Datenbestand zeigt stattdessen den gut sichtbaren Demo-Importbutton. Keine automatischen Datenimporte bei jedem Backend-Start.

## 9. Konkrete Implementierungsreihenfolge

### Schritt 1 – Backend-Grundlage und Schema

Anlegen: Backend-Paket, Konfiguration, DB-Verbindungen/Migrationsrunner, Initialmigration, Health- und Config-Endpunkte.

**Abnahme:** Leere SQLite-Datei lässt sich initialisieren; erneuter Start ist migrationssicher; Healthcheck und `/docs` funktionieren ohne Google-Credentials. Foreign-Key- und Koordinatenconstraints sind getestet.

### Schritt 2 – Beobachtungen, Bilder und Demo-Durchstich

Implementieren: Pydantic-DTOs, Listen-/Detail-/Anlage-/Änderungsendpunkte, Bild-Ingest, Fotoauslieferung, Demo-Generator und CLI. Danach einfacher Importjob-Runner und Demo-Start über HTTP.

**Abnahme:** 16 Demo-Beobachtungen erscheinen; Thumbnail und großes Bild sind abrufbar; zweimaliges Seed erzeugt keine Duplikate; unbekannte Orte/Zeiten bleiben `null`.

### Schritt 3 – Next.js-Grundlayout, Karte und Detailauswahl

Implementieren: API-Client/Typen, Workspace-Grid, MapLibre/OpenFreeMap, Beobachtungsliste, Auswahl und Sidebar mit echten Backendbildern und Platzhaltern.

**Abnahme:** Marker und Listeneinträge öffnen dieselben Details; Bilder bleiben in der Sidebar sichtbar und scrollbar; Desktop und schmales Fenster funktionieren; Kartenfehler blockiert Liste nicht.

### Schritt 4 – Zeitfilter und Metadatenbearbeitung

Implementieren: Alle-Modus, Fenstergrenzen, UTC-/Zeitzonenlogik, konsistente Listen-/Kartenfilterung, Auswahlreset und Bearbeitungsmaske.

**Abnahme:** Zeitgrenzen inklusive; undatierte Funde nur im Alle-Modus; Start/Ende vertauschbar nicht zulässig; nach Ortskorrektur erscheint der Fund an der richtigen Stelle. Ein-Punkt-Zeitumfang funktioniert.

### Schritt 5 – Lokalen Import vollständig machen

Implementieren: Ordnerscan, EXIF-Auswertung, Größen-/Formatgrenzen, Deduplizierung, Jobstatusanzeige und übersichtliche Fehlerliste.

**Abnahme:** JPEG mit GPS landet korrekt auf der Karte; Foto ohne GPS bleibt auswählbar; beschädigtes/HEIC-Bild verhindert nicht den Restimport; erneuter Import überspringt bestehende Dateien; Originaldateien bleiben unverändert.

### Schritt 6 – OAuth und echter Google-Picker-Flow

Implementieren: Auth-Status, Session-Cookie, State/PKCE, Callback, Sessionanlage/Polling, paginiertes Listing, authentifizierten Download und Cleanup. UI um Login-, Auswahl-, Warte-, Import- und Fehlerzustände ergänzen.

**Abnahme:** Ohne Konfiguration keine Fehlerspirale; mit Testkonto lassen sich ausgewählte Fotos lokal anzeigen. Fehlender Google-Standort wird offen ausgewiesen; Wiederholung erzeugt keine Duplikate. Consent-Abbruch, Tokenablauf und Sessionablauf sind nachvollziehbar.

### Schritt 7 – Integration und Browser-Abnahme

Automatisierte Tests ausführen, Frontend-Produktionsbuild prüfen, beide Server starten und den kompletten Demo-/Ordner-/Picker-Ablauf im Browser testen. Browserkonsole und Backendlogs auf Fehler prüfen. Noch fehlende echte Google-Zugangsdaten als offene externe Voraussetzung benennen, nicht durch einen erfolgreichen Mocktest als erledigt darstellen.

**Abnahme:** Alle nachfolgenden Tests sind bestanden oder ein externer Google-Livetest ist ausdrücklich als noch ausstehend dokumentiert. Für den UI-/Demo-Prototyp darf dieser Livetest kein Startblocker sein.

## 10. Testplan und Abnahmeszenarien

### 10.1 Automatisiert: Backend

- Migration auf leerer DB, Wiederholungsstart, Foreign Keys, koordinatenweises `NULL` und ungültige Sigma-Werte.
- Anlage, Liste, Details, partielle Änderung, `404`, `422`; UTC-Konvertierung und naive API-Zeiten.
- Idempotentes Demo-Seed und wiederholter lokaler/Google-Import mit stabilen externen IDs.
- Bild mit EXIF-Rotation, GPS N/S/E/W, Offset, fehlenden Metadaten, DST-Grenzfall, beschädigtem Inhalt und überschrittenem Limit.
- Medienpfad kann nicht aus dem Datenverzeichnis ausbrechen; lokale Symlinks werden nicht importiert.
- Importjobs: Einzelfehler, Doppelstart, Prozessneustart und bereinigte Fehlermeldungen ohne Secrets.
- OAuth: falscher/fehlender/abgelaufener/recycelter State, falsche Sessionbindung, Consent-Abbruch und Tokenablauf.
- Picker-HTTP-Mocks: `mediaItemsSet=false/true`, gedrosseltes Polling, Poll-Timeout ungleich Sessionablauf, mehrere Listenseiten, authentifizierter Download, `401`/`403`/`429`, Cleanup erst nach Download.
- Origin-/JSON-Prüfung für mutierende Browserendpunkte und Eigentümerprüfung der Google-Sitzungen.

### 10.2 Automatisiert: Frontend

- Zeitfilter: inklusive Grenzen, Alle-Modus, unbekannte Zeiten, leerer Umfang, identischer Min-/Max-Zeitpunkt und Zeitzonenumrechnung.
- Auswahl verschwindet bei Filterausschluss; veraltete Detailantwort überschreibt keine neuere Auswahl.
- Karten-GeoJSON enthält nur gültige Punkte und Koordinaten in der Reihenfolge `[lon, lat]`.
- Sidebar zeigt fehlende Fotos/Metadaten und echte Platzhalterzustände ohne erfundene Analysewerte.
- Playwright: Demo laden → Listeneintrag wählen → großes Foto prüfen → Zeitfenster ändern → Metadaten ergänzen → Liste/Karte aktualisiert.

### 10.3 Manuelle Browser-Abnahme

1. Beide Server ohne Google-Secrets starten; Demodaten laden und Karte/Details bedienen.
2. Karte und Sidebar mit Maus/Touchpad unabhängig bedienen; kleinen Viewport und Tastaturbedienung prüfen.
3. Alle-Modus und enge Zeitfenster vergleichen; Funde ohne Ort und Datum bewusst auswählen.
4. Zwei Funde an derselben Position getrennt auswählen.
5. Foto ohne GPS/Datum lokal importieren, Werte ergänzen und Ergebnis auf Karte/Zeitleiste prüfen.
6. OpenFreeMap-Netzwerkzugriff blockieren: verständlicher Hinweis, Liste/Details weiterhin nutzbar.
7. Mit Google-Testkonto anmelden, Auswahl öffnen, Fotos auswählen, importieren und große Bilder prüfen; Auswahlabbruch und abgelaufene Sitzung testen.
8. Backend während eines Testimports neu starten: kein hängender Dauerstatus und keine behauptete Google-Anmeldung danach.

## 11. Bewusste Folgeschritte nach dem Prototyp

Erst nach erfolgreicher UI-/Importabnahme echte Gemini-Analyse und Wetterabfragen ergänzen. Dafür bestehen bereits getrennte Notiz-/Wetterspeicher und Zustände. Danach können Herkunft/Unsicherheit fachlich präzisiert, mehrere Fotos einem Fund zugeordnet und Gauß-Fusion samt geeigneter Kartenvisualisierung implementiert werden. Für einen öffentlichen Betrieb sind vor allem Authentifizierung, persistente verschlüsselte Tokenhaltung, CSRF-/Session-Härtung, dauerhafte Jobverarbeitung und Datenschutz neu zu planen.

## 12. Referenzen für die Google-Implementierung

Die REST-Referenz ist für Hostnamen und Methoden maßgeblich; allgemeine Beispiele nicht mit der Library API vermischen.

- Picker REST-Referenz: https://developers.google.com/photos/picker/reference/rest
- Picker-Sessions und Polling: https://developers.google.com/photos/picker/guides/sessions
- Medienauswahl, Download und URL-Lebensdauer: https://developers.google.com/photos/picker/guides/media-items
- Medienfelder, insbesondere fehlende GPS-Felder: https://developers.google.com/photos/picker/reference/rest/v1/mediaItems
