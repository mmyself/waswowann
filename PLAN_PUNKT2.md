# Punkt 2 - Technischer Plan fuer Luna

## Ziel und Ist-Stand

Mobile Route `/upload` fuer Einzel-/Mehrfach-/ZIP-Upload, unmittelbare Gemini-Bestimmung und vollstaendige Ergebnisbearbeitung; erst **Auf Karte anzeigen** wechselt nach `/`. Dazu automatische Wetter-Scheiben und nachvollziehbare Gauss-Fusion.

Grundlage: `PROJEKTPLAN.md`, gezielter Einblick in `backend/app`, `frontend/src` und Tests/Konfiguration; `UMSETZUNGSPLAN.md` nicht gelesen. Vorhanden sind SQLite/FastAPI, Pillow-EXIF-Auswertung und JPEG-Ableitungen, CRUD/Importjobs sowie Next.js 15/MapLibre/Zeitfilter/Sidebar. Sigma-Spalten existieren bereits; Gemini und Wetter sind Platzhalter. Kein neuer Photos-Picker-/OAuth-/Takeout-Ausbau in diesem Auftrag.

**Integrationsfehler zuerst beheben:**
- `backend/app/db.py:migrate` unterstuetzt nur 001 und `*_initial.sql`: nummerierte Migrationen entdecken und je Migration inklusive `user_version` atomar ausfuehren.
- `backend/app/main.py:BrowserMutationGuard`: Multipart ausschliesslich fuer `POST /api/uploads` erlauben, Origin-Pruefung unveraendert beibehalten.
- `frontend/src/lib/api.ts:request`: bei FormData keinen Content-Type setzen; Browser erzeugt Boundary. Fehler sowohl aus `{error}` als auch bestehendem `{detail:{error}}` lesen.
- `frontend/src/components/ObservationDetails.tsx` sendet verschachteltes `space_time` beim PATCH, Backend erwartet flache Felder: Frontend korrigieren und Speichern testen.

## 1. Migration und Vertraege

Neue `backend/app/migrations/002_upload_weather_fusion.sql`, 001 unveraendert lassen; bestehende DB vor Migration sichern.

| Tabelle | Aenderung |
|---|---|
| `space_time_gauss` | Bestehende Sigmas/`fused_group_id` verwenden; `location_source` um `browser` erweitern. |
| `gemini_notes` | Status `not_requested|queued|running|ready|error`; zusaetzlich `parent_note_id`, `question_text`, `provider`, `model`, `error_code`. Ergebnis bleibt in `structured_json`, Antwort in `raw_text`; jede Folgeanalyse eigene Note. |
| `weather_slices` | Status `not_requested|queued|running|ready|partial|error|unavailable`; `cache_key`, `error_code` ergaenzen, drei vorhandene Slice-JSONs behalten. |
| `weather_cache` (neu) | `cache_key` PK, Koordinaten, UTC-Anker, Schema-Version, drei Slice-JSONs, Abdeckung/Quellen-JSON, Status, Abruf-/Ablaufzeit. |
| `processing_jobs` (neu) | ID, Fund-FK, Kind `gemini|weather`, optionale Note-ID, Input-Key, Status, Versuche, naechster Versuch, Fehlercode, Zeitstempel. Partieller UNIQUE-Index fuer aktive Auftraege desselben Kind/Fund/Input-Key. |
| `fusion_groups` (neu) | ID, fusionierte Koordinaten/Zeit und beide Sigmas, Aktualisierungszeit. Mitgliedschaft ueber vorhandenes `space_time_gauss.fused_group_id`, darauf Index. |

SQLite-CHECK-Erweiterungen erfordern Tabellenneuaufbau: alle Werte/Indizes/FKs erhalten, `foreign_key_check` pruefen. Unbekannte Bestandssigmas bleiben NULL. In `schemas.py` und `frontend/src/lib/types.ts` Status, Browserquelle und strukturierte Ergebnisse synchronisieren; Zahlen endlich, Sigmas >0, Zeit mit Offset/UTC. Listen-API um beide Sigmas und Gruppen-ID ergaenzen. Detail-API muss `structured_json` auslesen statt Konfidenz/Tags fest zu verdrahten.

### API

Neue Router `backend/app/routes/{uploads,analysis,weather,fusion}.py` in `main.py` registrieren.

| Endpunkt | Vertrag |
|---|---|
| `POST /api/uploads` | Multipart `files[]` (Bilder/ZIP), optional `browser_location` als JSON (`lat,lon,accuracy_meters,captured_at`) und IANA-`timezone`. `202 {items:[{filename,observation_id,media_id,result:imported|duplicate|error,error_code}]}` nach Vorschau-Speicherung und Jobanlage; nicht auf KI warten. Teilfehler erhalten Erfolge; ungueltiger Gesamtrequest 413/415/422. |
| `GET /api/observations/{id}` | Bestehenden Endpunkt um volle Bestimmung, Rueckfragenverlauf, Wetterstatus/Abdeckung und Fusion erweitern; Upload-UI pollt nur bei aktiven Jobs. |
| `PATCH /api/observations/{id}` | Bestehender flacher Vertrag; Titel/Kategorie/Beschreibung, Ort/Zeit/Sigmas. Wetter bei Ort/Zeit-Aenderung invalidieren, Fusion auch bei Sigma-Aenderung neu rechnen. |
| `POST /api/observations/{id}/analysis` | JSON `{question?,parent_note_id?}`: Erst-/Wiederholungsanalyse oder Rueckfrage, `202 {note_id,status}`. Parent muss zum Fund gehoeren; identischen aktiven Auftrag wiederverwenden. |
| `POST /api/observations/{id}/weather/refresh` | `{}` -> 202; Cache beruecksichtigen, keine ungebremste Force-Option; ohne Ort/Zeit 422. |
| `GET /api/observations/{id}/fusion-candidates` | Nahe kompatible Funde mit Abstaenden; noch keine Fusion. |
| `POST /api/fusion-groups` | `{observation_ids,confirm_same_object:true}`; >=2 unterschiedliche kompatible Messungen, atomar gruppieren/rechnen. Fremde bestehende Mitgliedschaft: 409. |
| `PATCH /api/fusion-groups/{id}` / `DELETE /api/fusion-groups/{id}` | Explizite Mitgliederliste aendern / Gruppe aufloesen; Originalmessungen erhalten. Gruppe unter zwei Mitgliedern aufloesen. |
| `GET /api/fusion-groups?from=...&to=...` | Ohne Grenzen ganze Gruppen; mit Zeitfilter nur sichtbare Mitglieder fusionieren (>=2), verwendete IDs mitliefern. Gefilterte Posterioren nicht global speichern. |

## 2. Upload und Bildaufbereitung

**Dateien:** `services/photo_ingest.py` erweitern, `services/upload_ingest.py` neu; `backend/pyproject.toml` um `python-multipart` ergaenzen. Bestehende lokale/Google-Importe nutzen weiterhin die gemeinsame Aufbereitung.

1. JPEG/PNG/WebP nach Inhalt mit Pillow validieren. HEIC/HEIF vorerst explizit ablehnen. Grenzen: 25 MiB/Bild, 100 MiB/Request, 100 Bilder, ZIP entpackt 500 MiB gesamt, Kompressionsverhaeltnis <=100:1, 40 Megapixel/Bild. Bytes selbst zaehlen, nicht nur Headern vertrauen; Decode-Arbeit begrenzt ausserhalb Event-Loop.
2. **Originale ausschliesslich im RAM, auch keine temporaere Originalablage.** Begrenzten Streaming-Multipart-Parser einsetzen; Standard-`UploadFile` spoolt ggf. auf Disk und erfuellt dies nicht automatisch. ZIP begrenzt im RAM oeffnen und Eintraege einzeln lesen; kein `extractall`. Traversal/absolute Pfade/Symlinks, verschachtelte/verschluesselte ZIPs ablehnen, Verzeichnisse/`__MACOSX` ignorieren.
3. EXIF vor Komprimierung lesen: GPS, DateTimeOriginal aus Exif-SubIFD samt OffsetTimeOriginal (0x9011), danach passende Digitized-/DateTime-Fallbacks. Bestehende falsche Offset-Tag-Zuordnung in `_metadata` korrigieren. Ohne Offset ausgewaehlte IANA-Zone samt sichtbarer Annahme nutzen; DST-Luecke/-Mehrdeutigkeit bleibt unbekannt bis Korrektur.
4. EXIF-GPS hat Vorrang. Browser-GPS nur nach Aktion/Erlaubnis und Bestaetigung pro betroffenem Bild: aktueller Geraetestandort ist kein historischer Fotostandort. HTTPS/localhost voraussetzen; Verweigerung/Timeout blockiert keinen Upload. Fehlende Aufnahmezeit nicht durch Uploadzeit ersetzen.
5. Orientierung anwenden, Transparenz aufhellen, EXIF aus Ableitungen entfernen. JPEG-Vorschau max. 1600 px, Qualitaet und danach Dimensionen iterativ reduzieren: Ziel **100-150 KiB, harte Obergrenze 150 KiB**; kleine Bilder nicht aufblasen. Thumbnail <=480 px / <=40 KiB. Nur diese Dateien atomar unter UUID-Pfaden speichern; `local_path` bezeichnet Vorschau, kein Original-Endpunkt.
6. Original-SHA-256 aus RAM fuer vorhandene lokale Deduplizierung nutzen, `source='local'` behalten. Duplikate liefern bestehende IDs ohne erneute KI-Kosten. Originalpuffer freigeben, nie in Logs/DB/Base64 persistieren; bei Fehlern keine verwaisten Ableitungen. Nach Commit Gemini sofort, Wetter bei Ort+Zeit einreihen.

## 3. Gemini und Hintergrundverarbeitung

**Neue Dateien:** `services/gemini_client.py`, `services/analysis.py`, `services/processing_jobs.py`. `config.py`/`backend/.env.example` um `GEMINI_PROVIDER=google|antigravity`, `GEMINI_MODEL`, `GOOGLE_API_KEY` bzw. Bridge-URL/Auth ergaenzen. `/api/config` verrät nur Verfuegbarkeit/Provider, keine Secrets.

- Einziger Providerzugang: `gemini_client.analyze(image_bytes,mime_type,context,question) -> AnalysisResult`. Google ueber offizielle Gemini-API mit Inline-Bild; nur sanitierte Vorschau senden, keine Files-API/Originalablage. Bildfaehiges Modell konfigurierbar; GPS nicht ungefragt an KI senden.
- **Bridge-Vertrag unbekannt:** Luna prueft reale Antigravity-Doku/Konfiguration und verifiziert Request/Auth/Response per Testaufruf. Keine Endpunkte, OpenAI-Kompatibilitaet oder privaten Gemini-Webchat-Zugriff erfinden. Google-Key-Modus muss unabhaengig funktionieren; fehlen beide, `ANALYSIS_NOT_CONFIGURED`, Fund bleibt nutzbar. Kein stiller Providerwechsel.
- Ergebnis per Pydantic validieren: Titel, Kategorie, Beschreibung, deutscher/wissenschaftlicher Name, Konfidenz 0..1 (Modell-Selbsteinschaetzung), Tags, Alternativen, Rueckfragen und Warnungen. Unbekannt zulassen; Pilzbestimmung ist keine Verzehrfreigabe. Als Text rendern, kein ungesichertes HTML.
- Rueckfragen verwenden gespeicherte Vorschau und bisherigen Note-Kontext. Vorschlaege nicht automatisch ueber manuelle Felder schreiben: **Bestimmung uebernehmen** speichert explizit per PATCH.
- Persistente Jobs im FastAPI-Lifespan starten/stoppen; zwei begrenzte Worker, zunaechst ein Backend-Prozess. Kurze DB-Transaktionen, keine HTTP-Aufrufe unter Schreiblock. Unterbrochene Jobs beim Neustart neu einreihen; maximal drei Versuche mit Timeout/Backoff/Retry-After bei 429/5xx/Transportfehlern. Auth-/Schemafehler direkt melden. Externe KI-Aufrufe sind nach Absturz nicht garantiert exactly-once.
- Status pro Bild getrennt von Wetter/Import anzeigen; Detail-Polling ca. 1-2 s mit Abbruch bei Navigation/Endstatus. Ein externer Fehler verwirft keinen Fund und blockiert keine anderen Ergebnisse.

## 4. Wetter-Scheiben und Cache

**Neue Datei:** `services/weather.py`; automatischer Trigger nach Ingest, Orts-/Zeit-PATCH und gedrosselter einmaliger Bestands-Backfill. Ohne Ort/Zeit kein Abruf; geloeschte Werte leeren Slices. Sigma-only-Aenderung invalidiert Wetter nicht.

- UTC-Anker `T = Aufnahmezeit auf volle Stunde abgerundet`, sichtbar ausgeben. Fenster strikt vor T: `[T-24h,T)` in 24 Stunden, `[T-7d,T)` in sieben rollierenden 24h-Buckets, `[T-84d,T)` in zwoelf rollierenden 7d-Buckets. Unvollstaendige letzte Stunde nicht verwenden; UTC-Berechnung bleibt DST-stabil.
- Stuendliche `temperature_2m`, `relative_humidity_2m`, `precipitation` ueber maximal 84 Tage holen, lokal aggregieren; keinen nicht verfuegbaren taeglichen Feuchteparameter annehmen. Archive API fuer Historie, Forecast API mit `past_days` fuer juengste Archivluecke. Aktuelle Verfuegbarkeit/Verzoegerung beider APIs vor Umsetzung pruefen; UTC-Stunden deduplizieren, Herkunft kennzeichnen, nie Werte nach T verwenden.
- Zeitlabels/Akkumulationsintervalle normalisieren: Niederschlag fuer die vorangegangene Stunde entsprechend zuordnen. Bestehendes Bucketformat `start,end,temperature_mean_c,precipitation_sum_mm,humidity_mean_pct` plus Abdeckung je Messgroesse. Unvollstaendige Aggregate NULL/partial, Luecken niemals 0 mm; Zukunft/fehlende Historie als unavailable melden.
- Cache-Key aus exakten normalisierten Koordinaten, T und Variablen-/Aggregationsversion; keine versteckte Ortsrundung. TTL vollstaendige Historie 30 Tage, juengste/partielle Daten 1 Stunde, Fehler-Retry fruehestens nach 5 Minuten. Gleiche aktive Cache-Anfragen zusammenfassen.
- Wetterjob traegt Input-Key; Ergebnis nur an Fund anhaengen, wenn Ort/Zeit weiterhin denselben Key liefern. Andernfalls nur Cache fuellen: kein alter Response darf einen inzwischen korrigierten Fund ueberschreiben.
- `WeatherPlaceholder.tsx` durch `WeatherPanel.tsx` ersetzen: drei Tabs, kleine Temperatur-/Feuchteverlaeufe und Niederschlagsdarstellung, Einheiten, Zeitraumgrenzen, UTC-Anker, Abdeckung, Quelle/Open-Meteo-Attribution und Retry-Zustaende. SVG plus zugaengliche Wertetabelle genuegt.

## 5. Gauss-Fusion und MapLibre

**Neue Datei:** `services/gaussian_fusion.py`; `numpy` in `backend/pyproject.toml` ergaenzen. Keine Markow-Kette in diesem Schritt.

- Isotrope 2D-Ortsnormalverteilung in lokalen metrischen Ost/Nord-Koordinaten und unabhaengige 1D-Zeitnormalverteilung. `sigma_meters` = Standardabweichung je Raumachse, `sigma_seconds` = zeitliches 1-sigma; UI eindeutig beschriften.
- Fuer neue EXIF-Werte ohne Genauigkeit konfigurierbare, sichtbare Annahmen z. B. 20 m / 60 s anbieten. Browser-`accuracy` ist 95%-Radius: unter isotrop-gaussscher Annahme `sigma=accuracy/sqrt(-2*ln(0.05))`. Bestehendes NULL nicht erfinden; unbekannte Sigmas nicht fusionieren.
- Kandidaten brauchen Ort, Zeit und beide Sigmas: Abstand <=100 m, Zeitdifferenz <=1 h sowie `d^2/(sigma_i^2+sigma_j^2)<=9` und `dt^2/(tau_i^2+tau_j^2)<=9`. Gleiche Kategorie, keine widersprechenden bekannten Taxa; Grenzen konfigurierbar. Gruppenmitglieder paarweise pruefen, keine transitive Kettenbildung.
- **Naehe allein beweist keine Objektidentitaet:** automatisch vorschlagen, erst nach Bestaetigung desselben Objekts/Ereignisses fusionieren. SHA-Duplikate und bekannte abgeleitete/gemeinsam verortete Messungen nicht als unabhaengig zaehlen. Unabhaengigkeitsannahme/GPS-Korrelation in UI kenntlich machen.
- WGS84 nach lokalem ENU transformieren, keine Laengengradmittelung. Je Ortsachse `w_i=1/sigma_i^2`, `mu_f=sum(w_i*mu_i)/sum(w_i)`, `sigma_f=sqrt(1/sum(w_i))`; Zeit analog in Sekunden relativ zu nahem Referenzzeitpunkt. Flacher Prior, bedingt unabhaengige Messungen. Zurueck nach WGS84/UTC; Originalmessungen unveraendert.
- Messwert-/Mitgliedschaftsaenderungen rechnen Gruppen atomar neu. Unvollstaendige/inkompatible Mitglieder entfernen und melden, Gruppen <2 aufloesen.
- `ObservationMap.tsx`: getrennte GeoJSON-Quellen/Layers fuer Einzel-/Fusionskonturen und Zentren, Overlay-Schalter/Legende. Metrische abgestufte Konturen statt fester Pixelkreise: 68%-Radius `1.51*sigma`, 95% `2.45*sigma`; Mitglieder bleiben anklickbar. Datumsgrenze/Mercator-Grenzen sicher behandeln bzw. dort explizit auslassen.
- Nur sichtbare Messungen darstellen, gefilterte Fusion aus sichtbaren Mitgliedern berechnen. Bestehende Mittelpunkt-Zeitfiltersemantik erhalten; Sidebar zeigt zeitliches 95%-Intervall `mu +/- 1.96*sigma_seconds`.

## 6. Frontend und Reihenfolge

1. Migration, Typen, PATCH-/FormData-/Fehlerkorrekturen samt Tests.
2. Sichere Upload-API und gemeinsame Bildoptimierung.
3. Gemini-Kapsel/Jobs, dann `frontend/src/app/upload/page.tsx` + `components/UploadWorkspace.tsx`: separate Kamera- und Mehrfach-/ZIP-Auswahl, GPS-Bestaetigung, Status pro Bild. Objekt-URLs freigeben, Originale nie in localStorage/IndexedDB speichern.
4. Wiederverwendbare `AnalysisPanel.tsx`/`ObservationEditor.tsx` in Upload und `ObservationDetails.tsx`. Auf `/upload` Foto, Metadatenquellen, volle Bestimmung/Alternativen/Konfidenz/Tags/Warnungen, Bearbeitung und Rueckfrage anzeigen. Bestehende Ergebnisse per `/upload?observation=<id>` wieder laden; kein automatischer Redirect.
5. Topbar-Link zu Upload; **Auf Karte anzeigen** -> `/?observation=<id>`. `Workspace.tsx` liest Parameter nach Listenladen, waehlt Fund, oeffnet Sidebar und fokussiert Karte; Zeitfilter darf Auswahl nicht sofort verwerfen. Ohne Ort Sidebar trotzdem zeigen, keine Koordinate erfinden.
6. Wetterservice/Backfill/Sidebar, danach Fusion/API/Overlay; bestehende Gestaltung in `globals.css` bewahren, abschliessend mobile und Desktop-Browserprobe.

## Abnahme

- **DB:** belegte 001-DB auf 002 migriert, zweiter Start unveraendert; IDs/Medien/Notizen/Werte/FKs erhalten. Bestehende Tests mit positionalen `Settings(...)` und `user_version==1` aktualisieren.
- **Upload/Metadaten:** Einzel/Mehrfach/ZIP, Duplikate/Teilfehler, MIME-Taeuschung, Traversal/Symlink/Bombe, Byte-/Pixelgrenzen und Abbruch getestet. Auch >1-MiB-Eingaben erzeugen keine Originaldateien; detailreiche Vorschau <=150 KiB, Orientierung stimmt, kein EXIF-GPS im JPEG. EXIF-SubIFD/Offset/DST, GPS 0/0/S/W und verweigerter Browserstandort getestet; Korrekturen ueberstehen Reload.
- **KI:** Erstbestimmung startet ohne Zusatzklick auf `/upload`; Rueckfrage nutzt gleichen Fund/Bildkontext. Provider-Mocks testen Schemafehler/fehlende Config/429/Timeout/Neustart; manuelle Felder bleiben erhalten. Reale Bridge/Google-Smoke-Tests nur bewusst mit bereitgestellter Konfiguration.
- **Wetter:** deterministische 24/7/12 Buckets, Mittelwerte/Summen, Niederschlagsintervalle, DST, Archiv-/Forecast-Uebergang, Luecken/Zukunft und Cache-Hit ohne HTTP getestet. Gleichzeitiger Orts-PATCH kann nicht mit altem Wetter ueberschrieben werden.
- **Fusion:** zwei identische unabhaengige Messungen ergeben `sigma/sqrt(2)`; Praezisionsgewichtung und Sekunden/Millisekunden stimmen. Duplikate/NULL/Ausreisser/Kettenbildung/Gruppenaenderung/Zeitfilter-Subset getestet; Originalwerte bleiben unveraendert.
- **Browser:** `frontend/e2e/upload.spec.ts`, 390x844 und Desktop: Upload -> volle Analyse bleibt auf `/upload` -> Bearbeiten/Speichern -> Rueckfrage -> Kartenbutton mit korrekter Auswahl. GPS-Ablehnung, ZIP-Teilfehler, KI-/Wetterausfall, Reload, Wettertabs, Overlay bei mehreren Zoomstufen und Zeitfilter pruefen; keine Ueberlaeufe/Konsolenfehler. Veraltete Ueberschrift in bestehender `e2e/smoke.spec.ts` korrigieren.
- **Prueflauf:** neue Backendtests `test_uploads.py`, `test_analysis.py`, `test_weather.py`, `test_fusion.py`; `pytest` sowie frontendseitig `npm run lint`, `npm test`, `npm run build`, `npm run test:e2e` mit gestarteten Diensten. Standardtests ohne Live-Netzwerk; finale reale mobile HTTPS-/GPS-Probe nicht durch Build ersetzen.
