# WasWoWann / MetaCore Arbeitsregeln

1. **Automatische Ontologie & Nomenklatur (`embl-ebi-ols` / `NCBITaxon`):**
   - Beim Anlegen neuer biologischer Funde oder Vorlagen (Pilze, Pflanzen, Tiere) immer `ontology_id` (z. B. `NCBITaxon:91752`), den wissenschaftlichen Namen (`scientific_name`, Binomen), `taxon_path` und gefundene Synonyme als `aliases` mitpflegen bzw. per `embl-ebi-ols` automatisch auflösen.
2. **Duplikat- & Literatur-Check vor `ai_prior`-Templates:**
   - Vor dem Erstellen einer neuen Vorlage zuerst `search_templates` (über wissenschaftlichen Namen und Trivialnamen/Aliase) auf bestehende Vorlagen prüfen, um Duplikate zu vermeiden.
   - Wenn Startwerte als `ai_prior` gesetzt werden, gezielt `literature-search-openalex` (oder `literature-search-europepmc` als Fallback) befragen und gefundene DOIs/URLs direkt als `evidence_refs` hinterlegen.
3. **Architektur & Grenzen:**
   - `metacore/` bleibt 100 % domänenneutral (keine hardcodierten Einzel-Domänen in der Engine).
   - `metacore/src/metacore/mcp_server.py` behält genau die 8 bestehenden MCP-Tools bei.
   - Nach verifizierten Änderungen im Repo automatisch einen sauberen Git-Commit + Push auf `origin/main` ausführen (niemals `.sqlite3`-Dateien oder `.env`/Secrets committen).
