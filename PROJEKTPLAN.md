# WasWoWann – Projektplan & Architektur-Übersicht

Stand: 2026-11-14

## 1. Projektidee
**WasWoWann** sammelt Beobachtungen (Pflanzen, Pilze, Personen, Dinge, Ereignisse) als Weltlinien mit **Was** (Objekt/Foto/Bestimmung), **Wo** (Ort) und **Wann** (Zeit) auf einer interaktiven Karte.

* **Karte + Zeitschieber:** Zeitpunkte oder Zeitfenster lassen sich über einen Zeitschieber ein- und ausblenden (oder alle Funde gleichzeitig anzeigen).
* **Rechte Seitenleiste:** Scrollbare Detailansicht mit vergrößertem Foto, Beschreibung, Gemini-Analyse, Unsicherheitswerten und Wetterverlauf.
* **Gaußsche Unsicherheit & Fusion:** Jeder Orts- und Zeitwert trägt eine Gaußsche Glockenkurve ($\mu, \sigma$). Mehrere Beobachtungen am selben Ort/Objekt verschmelzen über Bayessche / Markow-basierte Fusion zu schärferen, verlässlicheren Werten.
* **Historisches Wetter in Zeitscheiben:** Zu jedem Fund wird automatisch das lokale Wetter vor dem Ereignis gespeichert:
  * Letzte **24 Stunden** (stündlich)
  * Letzte **7 Tage** (täglich)
  * Letzte **12 Wochen** (wöchentlich aggregiert – ideal für Pilz- und Pflanzenwachstum)

---

## 2. Festgelegter Tech-Stack
* **Datenbank:** **SQLite** (lokal, leichtgewichtig, keine Server-Wartung; speichert Funde, Gauß-Parameter, Gemini-Texte und gecachte Wetter-Scheiben).
* **Frontend:** **Next.js (React)** + **MapLibre GL JS** + **OpenStreetMap / OpenFreeMap** (kostenlos, WebGL-fähig für flüssige Zeitschieber, Gauß-Heatmaps und optionales 3D-Gelände).
* **Backend & Mathematik:** **Python (FastAPI)** mit `numpy` / `scipy` (Gauß-Fusion), `Pillow` / `exifread` (EXIF-Daten) und `httpx` (Open-Meteo & Google APIs).
* **Wetter-Daten:** **Open-Meteo Historical Weather API** (für nicht-kommerzielle Nutzung kostenlos, kein API-Key nötig, Daten rückwirkend bis 1940).

---

## 3. Anbindung von Google Photos & Google Gemini (Wichtige API-Grenzen & Lösung)

### Google Photos
* **Einschränkung seit März 2025:** Google hat den breiten Lesezugriff (`photoslibrary.readonly`) abgeschaltet. Programme dürfen nicht mehr im Hintergrund die gesamte Fotobibliothek durchsuchen.
* **Lösung – Google Photos Picker API (`photospicker.mediaitems.readonly`):**
  * In der Web-App gibt es einen Button *„Aus Google Photos wählen“*.
  * Es öffnet sich direkt die Google-Photos-Auswahl (kein manuelles Herunterladen/Hochladen nötig).
  * Nach der Auswahl holt sich das Backend die Bilder inkl. Metadaten (Zeitstempel, GPS/EXIF) automatisch ab.

### Google Gemini
* **Einschränkung:** Die offizielle **Gemini Developer API** kann **keine** bestehenden privaten Chat-Threads aus der Consumer-Web-App (`gemini.google.com`) direkt auslesen – dafür existiert kein API-Endpunkt.
* **Lösung (zweigleisig):**
  1. **Bestehende Gemini-Threads (einmaliger Import):** Export über **Google Takeout** (*Gemini Apps*) oder Kopieren/Teilen des Threads. Ein Import-Skript parst die Antworten und verknüpft sie automatisch (über Zeitstempel, Pilznamen oder Bildvergleich) mit den gewählten Google-Photos-Bildern.
  2. **Neue Fotos (vollautomatisch):** Sobald ein Foto über den Google Photos Picker importiert wird, sendet das Backend es direkt an die **Gemini API**, um Artbestimmung, Beschreibung, Konfidenz und Tags automatisch zu generieren und in SQLite zu speichern.

---

## 4. Datenmodell (Konzept für SQLite)
* `observations`: `id`, `category` (Pilz, Pflanze, Person, Ding…), `title`, `created_at`
* `space_time_gauss`: `obs_id`, `lat`, `lon`, `sigma_meters`, `timestamp`, `sigma_seconds`, `fused_group_id`
* `media`: `id`, `obs_id`, `source` (`google_photos` / `local`), `external_id`, `local_path`, `exif_json`
* `gemini_notes`: `id`, `obs_id`, `source` (`takeout_import` / `gemini_api`), `raw_text`, `structured_json`
* `weather_slices`: `obs_id`, `slice_24h_json`, `slice_7d_json`, `slice_12w_json`

---

## 5. Phasenplan
1. **Phase 1 – Fundament & UI-Prototyp:** SQLite-Schema, FastAPI-Grundgerüst, Next.js + MapLibre-Karte mit Zeitschieber und rechter Detail-Sidebar.
2. **Phase 2 – Google Photos Picker & Gemini-Import:** OAuth-Anbindung für den Photos Picker + Import-Skript für deinen bestehenden Gemini-Thread (Takeout/Text) sowie automatische Gemini-API-Analyse für neue Bilder.
3. **Phase 3 – Wetter-Scheiben:** Automatischer Abruf der 24h-, 7-Tage- und 12-Wochen-Daten über Open-Meteo inkl. kleiner Verlaufsdiagramme in der Sidebar.
4. **Phase 4 – Gauß- & Markow-Fusion:** Berechnung der 2D-/1D-Gauß-Kurven, Verschmelzung naher Funde und Darstellung als Wahrscheinlichkeits-Wolken auf der Karte.
