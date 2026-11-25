# WasWoWann Frontend

Web-Oberfläche (`frontend/`) auf Basis von **Next.js 15**, **React 19**, **TypeScript** und **MapLibre GL JS** für Karte, Foto-Zeitleiste, Zukunftsgraph, Perioden-Detektor und Jahresansicht.

---

## 1. Ansichten & Komponenten

```text
frontend/src/
├── app/
│   ├── layout.tsx                   # Metadaten & MapLibre-Stylesheet
│   ├── globals.css                  # Editorial Light Theme (Desktop & Mobile ab 390 px)
│   ├── page.tsx                     # Hauptarbeitsbereich
│   └── upload/page.tsx              # Upload-Ansicht inkl. optionalem Geräte-GPS
├── components/
│   ├── Workspace.tsx                # Lädt Beobachtungen & Zukunftsgraph-Snapshot; Tab-Umschalter
│   ├── ObservationMap.tsx           # MapLibre: Fundpunkte, 68/95 %-Kalman- & Prognose-Ellipsen
│   ├── TimeFilter.tsx               # Zeitraumfilter ("Alle zeigen" / "Zeitfenster") + JETZT-Horizont
│   ├── ObservationList.tsx          # Scrollbare Fundliste inkl. Einträgen ohne Koordinaten
│   ├── ObservationDetails.tsx       # Detail-Sidebar, Foto-Zeitleiste, Kausal-Weltlinie & Zielanlage
│   ├── ZukunftsgraphPanel.tsx       # Kausal-DAG (aufklappbare parent_id-Schritte), Uhren, Perioden & Jahr
│   └── AnalysisPanel.tsx            # Optionale Gemini-/Bildanalyse-Historie
└── lib/
    ├── api.ts                       # Typisierter Fetch-Client für alle Backend-Routen
    ├── types.ts                     # Gemeinsame Domain- & MetaCore-Interfaces
    ├── contours.ts                  # GeoJSON-Ellipsen (68 % / 95 %) für Ist- & Prognose-Unsicherheit
    ├── time-filter.ts               # UTC/Luxon-Zeitfensterfilter inkl. Zukunfts-Extent
    └── zukunftsgraph.ts             # Hierarchie-Gruppierung (parent_id), DAG-Ordnung & Status-Labels
```

### Drei integrierte Hauptansichten in `Workspace.tsx`
1. **Karte & Archiv** *(Standard)*: MapLibre-Karte mit OpenFreeMap-Kacheln, zuschaltbaren 68-%-/95-%-Kalman-Konturen, gestrichelten Zukunfts-Prognosen/Habitaten in Ampelfarben sowie Zeitraum-Regler.
2. **Zukunftsgraph**: 4-Stufen-Zielampel mit Dimensionsbalken (`wann`, `wo`, `wie_viel`, `vorbedingung`), Eskalations-Aktionen (`switch_branch`, `widen_tolerance`, `abort_goal`), interaktive Prozessketten mit auf-/zuklappbaren `parent_id`-Unterschritten, Kausal-Linse und Live-Messwerterfassung.
3. **Perioden & Jahr**: Aktuelle Uhren im `JETZT` (`Tag`, `Sonnenstand`, `Woche`, `Monat`, `Mond`, `Gezeiten M2`, `Jahr`), interaktiver Takt-Detektor (inkl. Test-Presets für Wochen-, Gezeiten- und 14-Tage-Zyklus) sowie 12-Monats-Jahresansicht (`Tag 1–366`).

---

## 2. Konfiguration (`.env.local.example`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | URL des lokalen FastAPI-Backends |
| `NEXT_PUBLIC_MAP_STYLE_URL` | `https://tiles.openfreemap.org/styles/liberty` | MapLibre-Stil-URL |
| `NEXT_PUBLIC_DISPLAY_TIMEZONE` | `Europe/Berlin` | Anzeige-Zeitzone für Zeitstrahl, Uhren & Fotos |

---

## 3. Entwicklung & Tests

```sh
cd frontend
npm ci
cp .env.local.example .env.local
npm run dev

# Prüfungen
npm test -- --run     # Vitest Unit-Tests (contours, time-filter, zukunftsgraph)
npm run lint          # TypeScript strict check (tsc --noEmit)
npm run build         # Next.js Production Build
npm run test:e2e      # Playwright E2E Smoke-Tests (Desktop & 390×844 Mobile)
```
