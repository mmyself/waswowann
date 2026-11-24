# WoWannWas MetaCore

Eigenständiger, deterministischer Raum-Zeit-MetaCore und semantischer MCP-Server für **WoWannWas**.

Dieses Projekt bildet den **3er-Meta-Kern** (*Entität/Weltlinie*, *Zustand mit $\mu/\sigma$*, *Transformation*) inklusive Mengenbilanz, Gauß-Fusion, `ai_prior` → `measured` Kalibrierung und 4-stufiger Eskalation in sauberem Python-Code ab – zunächst unabhängig vom bestehenden Frontend/Backend unter `/Users/jan/WoWannWas` (Phase 1).

## 3-Schichten-Architektur

1. **SQLite-Kern (`001_initial.sql`):** Einheitlicher lokaler Speicher für Weltlinien (`entities`), Zustände (`states`), Prozesse/Vorlagen (`transformations`) und kausale Verknüpfungen (`edges`).
2. **Deterministische Engine (`src/metacore/`):** Rechnet Massen-/Mengenformeln mit Fehlerfortpflanzung, Gauß-Fusion (1D-Zeit & 2D-ENU-Ort), Zustands-Propagation und Eskalationsstufen 1–4 exakt aus.
3. **Semantischer MCP-Server (`wowannwas-mcp`):** Bietet LLMs und Agenten 8 sprechende Werkzeuge mit direktem Folgen-Feedback statt roher Tabellen-CRUDs.

## Geplanter Schnellstart (nach Umsetzung von M1–M4)

```sh
cd /Users/jan/WoWannWas/metacore
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
pytest
wowannwas-mcp
```

Der vollständige technische Umsetzungsplan, das Datenbankschema, alle 8 MCP-Tools und die Abnahmekriterien stehen in [UMSETZUNGSPLAN.md](UMSETZUNGSPLAN.md).
