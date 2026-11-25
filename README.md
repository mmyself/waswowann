# WoWannWas

Lokaler Single-User-Prototyp für Beobachtungen mit Foto, Ort und Aufnahmezeit. Die Anwendung kombiniert FastAPI, SQLite und Next.js mit einer MapLibre-Karte. Die Demo funktioniert ohne Google-Zugang, Gemini oder Wetterdienst; Kartenstil und Kacheln benötigen Internetzugriff.

## Voraussetzungen

- Python 3.12 oder neuer
- Node.js 20 oder neuer und npm
- Internetzugriff für OpenFreeMap-Kartenressourcen

Die Server sind ausschließlich für lokalen Betrieb vorgesehen. Das Backend bindet standardmäßig an `127.0.0.1` und bietet keine Benutzeranmeldung für Beobachtungs- und Foto-Endpunkte. Nicht an LAN oder Internet freigeben.

## Schnellstart

In zwei Terminals aus dem Projektverzeichnis starten.

### 1. Backend

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python -m app.cli migrate
python -m app.cli seed-demo
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 2. Frontend

```sh
cd frontend
npm ci
cp .env.local.example .env.local
npm run dev
```

Öffne anschließend `http://localhost:3000`. Frontend und Backend müssen beide über `localhost` aufgerufen werden, nicht abwechselnd über `127.0.0.1`; das ist für die OAuth-Cookie- und Redirect-Zuordnung wichtig. Die API-Dokumentation ist unter `http://localhost:8000/docs` erreichbar.

`python -m app.cli seed-demo` kann beliebig oft ausgeführt werden: Es legt 16 reproduzierbare synthetische Beobachtungen an und verändert bereits vorhandene Einträge nicht. In der Oberfläche gibt es außerdem einen Demo-Importknopf. Zwei der Demobeobachtungen haben keinen Ort und eine hat keine Aufnahmezeit, damit fehlende Werte direkt ausprobiert werden können.

## Tests und Checks

Backend (im aktivierten Backend-Virtualenv):

```sh
cd backend
pytest
```

Frontend:

```sh
cd frontend
npm test -- --run
npm run lint
npm run build
npx playwright install chromium  # einmalig fuer Browser-Smoke-Tests
npm run test:e2e
```

## Konfiguration

Backend-Einstellungen stehen in `backend/.env.example`; relative Pfade werden vom Backend-Projektverzeichnis aus aufgelöst. Die lokale Importfunktion scannt nur den konfigurierten `LOCAL_IMPORT_DIR`-Ordner und ist deaktiviert, falls der Ordner nicht eingerichtet ist. Unterstützte Bilddateien bleiben im Originalordner unverändert. Importierte Anzeige- und Thumbnailbilder sowie die SQLite-Datenbank werden unter `data/` gespeichert.

Frontend-Einstellungen stehen in `frontend/.env.local.example`. Der öffentliche API-Origin, Kartenstil und die Anzeigezeitzone lassen sich dort anpassen. Tokens und andere Geheimnisse gehören ausschließlich ins Backend, nicht in `NEXT_PUBLIC_`-Variablen.

## Google Photos Picker (optional)

Ohne Google-Anmeldedaten bleibt die Anwendung vollständig für Demo- und lokale Tests nutzbar. Für den echten Picker-Flow:

1. Aktiviere in einem Google-Cloud-Projekt die Google Photos Picker API und richte den OAuth-Consent-Bildschirm ein. Füge das zu testende Konto, falls erforderlich, als Testnutzer hinzu.
2. Erstelle einen OAuth-Client vom Typ „Webanwendung“ mit der Redirect-URI `http://localhost:8000/api/google/oauth/callback`.
3. Trage Client-ID und Client-Secret ausschließlich in `backend/.env` als `GOOGLE_CLIENT_ID` und `GOOGLE_CLIENT_SECRET` ein. Der Scope ist `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.
4. Starte das Backend neu und melde dich über die Google Photos-Steuerung in der Anwendung an.

Der Picker importiert nur ausdrücklich ausgewählte Medien; er bietet keinen Zugriff auf die ganze Bibliothek. Google stellt in diesem Ablauf keine GPS-Metadaten bereit, und der Download entfernt Standortmetadaten. Google-importierte Beobachtungen können daher einen Ort benötigen, der anschließend manuell ergänzt wird. Ein Backend-Neustart beendet die im Arbeitsspeicher gehaltene Google-Anmeldung. Ohne echte Cloud-Zugangsdaten ist der Live-OAuth-/Picker-Ablauf nicht testbar. Der Picker-Code ist vorbereitet; automatisierte Google-HTTP-Mocks sind noch nicht enthalten.

## Daten und Grenzen

- `data/` enthält lokale Datenbank-, Foto-, Import- und temporäre Dateien und ist nicht für Versionskontrolle vorgesehen.
- Gemini- und Wetterbereiche sind ausdrücklich Platzhalter; es finden keine echten Analysen oder Wetterabfragen statt.
- Unsicherheitswerte werden gespeichert und angezeigt, jedoch nicht zur Filterung oder Berechnung verwendet.
- Vor einem Betrieb außerhalb des lokalen Rechners wären unter anderem Authentifizierung, dauerhafte verschlüsselte Tokenhaltung, CSRF-/Session-Härtung und zuverlässige Jobverarbeitung erforderlich.

Weitere Architektur- und Abnahmeentscheidungen stehen in `UMSETZUNGSPLAN.md` und `PROJEKTPLAN.md`.
