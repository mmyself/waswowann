"""Export real MetaCore SQLite/engine snapshots and render interactive Zukunftsgraph HTML."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any

from metacore.config import Settings
from metacore.db import migrate
from metacore.engine import MetaCoreEngine, MutationFeedback
from metacore.math_fusion import GeoPoint, fuse_locations
from metacore.models import StateCreate, Transformation
from metacore.repository import MetaCoreRepository
from metacore.seeds import (
    seed_igel_stachelbart,
    seed_koernerbrut,
    seed_pilz_lebenszyklus,
    seed_riesenschirmling,
    seed_roboter_wasser,
    seed_substrat_rezept,
)


def _serialize_template(tpl: Transformation) -> dict[str, Any]:
    factor = tpl.formula.get("factor") if isinstance(tpl.formula, dict) else None
    return {
        "id": tpl.id,
        "name": tpl.name,
        "driver": tpl.driver,
        "duration_seconds": tpl.duration_seconds,
        "duration_sigma_seconds": tpl.duration_sigma_seconds,
        "duration_source": tpl.duration_source,
        "factor": factor,
        "formula": tpl.formula,
    }


def _template_has_open_ai_prior(tpl: Transformation) -> bool:
    if tpl.duration_seconds is not None and tpl.duration_source == "ai_prior":
        return True
    if isinstance(tpl.formula, dict):
        factor = tpl.formula.get("factor")
        if isinstance(factor, dict) and factor.get("source") == "ai_prior":
            return True
    return False


def _compute_entity_geo_fusions(repo: MetaCoreRepository) -> list[dict[str, Any]]:
    fusions: list[dict[str, Any]] = []
    for entity in sorted(repo.list_entities(include_archived=True), key=lambda e: (e.created_at_ms, e.id)):
        loc_states = [
            st
            for st in repo.list_states(entity_id=entity.id)
            if st.modality == "observed"
            and st.lat is not None
            and st.lon is not None
            and st.sigma_meters is not None
            and st.property_name in {"location", "habitat_observation"}
        ]
        if not loc_states:
            continue
        fused = fuse_locations(
            [
                GeoPoint(
                    lat=st.lat,
                    lon=st.lon,
                    sigma_meters=st.sigma_meters,
                    measurement_source=st.measurement_source,
                )
                for st in loc_states
                if st.lat is not None and st.lon is not None and st.sigma_meters is not None
            ]
        )
        fusions.append(
            {
                "entity_id": entity.id,
                "entity_name": entity.name,
                "observation_count": len(loc_states),
                "sources": sorted({st.measurement_source for st in loc_states if st.measurement_source}),
                "lat": fused.lat,
                "lon": fused.lon,
                "sigma_meters": fused.sigma_meters,
            }
        )
    return fusions


def export_graph_snapshot(
    repo: MetaCoreRepository,
    engine: MetaCoreEngine,
    *,
    title: str,
    subtitle: str,
    last_feedback: MutationFeedback | None = None,
) -> dict[str, Any]:
    """Serialize the complete live SQLite graph and dynamic engine evaluations."""
    entities = sorted(repo.list_entities(include_archived=True), key=lambda e: (e.created_at_ms, e.id))
    states = sorted(repo.list_states(), key=lambda s: (s.created_at_ms, s.id))
    transformations = sorted(repo.list_transformations(), key=lambda t: (t.created_at_ms, t.id))
    edges = sorted(repo.list_edges(), key=lambda e: (e.created_at_ms, e.id))
    templates = [t for t in transformations if t.modality == "template"]

    open_ai_priors = [
        st.id for st in states if st.modality == "predicted" and st.source == "ai_prior"
    ]
    open_ai_prior_templates = [tpl.id for tpl in templates if _template_has_open_ai_prior(tpl)]

    feedback_payload: dict[str, Any] | None = None
    if last_feedback is not None:
        feedback_payload = {
            "summary": last_feedback.summary,
            "created_state_ids": list(last_feedback.created_state_ids),
            "updated_state_ids": list(last_feedback.updated_state_ids),
            "triggered_template_ids": list(last_feedback.triggered_template_ids),
            "calibrated_template_ids": list(last_feedback.calibrated_template_ids),
        }

    return {
        "title": title,
        "subtitle": subtitle,
        "entities": [e.model_dump() for e in entities],
        "states": [s.model_dump() for s in states],
        "transformations": [t.model_dump() for t in transformations],
        "templates": [_serialize_template(t) for t in templates],
        "edges": [e.model_dump() for e in edges],
        "goal_evaluations": [g.model_dump() for g in engine.evaluate_all_goals()],
        "uses_conflicts": [c.model_dump() for c in engine.detect_uses_conflicts()],
        "open_ai_priors": open_ai_priors,
        "open_ai_prior_templates": open_ai_prior_templates,
        "entity_geo_fusions": _compute_entity_geo_fusions(repo),
        "last_feedback": feedback_payload,
    }


@contextmanager
def _temp_engine(db_filename: str) -> Iterator[tuple[MetaCoreRepository, MetaCoreEngine]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = Settings(database_path=Path(tmpdir) / db_filename)
        migrate(cfg)
        repo = MetaCoreRepository(cfg)
        engine = MetaCoreEngine(repo)
        yield repo, engine


def _run_koernerbrut_scenario() -> dict[str, Any]:
    with _temp_engine("koernerbrut.sqlite3") as (repo, engine):
        t0 = 1_795_000_000_000
        ids = seed_koernerbrut(
            repo,
            engine,
            dry_grams=400.0,
            bags=2,
            species="Igel-Stachelbart",
            start_ms=t0,
        )
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="Start (AI-Prior 700 ± 40 g)",
            subtitle="400 g Trockengewicht eingeweicht; Beutel-Vorhersage 350 ± 20,9 g auf Basis AI-Prior.",
        )

        fb_weigh = engine.record_observation(
            StateCreate(
                entity_id=ids["charge_id"],
                modality="observed",
                source="measured",
                measurement_source="Waage_Kueche",
                property_name="mass",
                label="Abgetropft gewogen: 680 g",
                value_num=680.0,
                value_sigma=10.0,
                value_unit="g",
                time_ms=t0 + 3 * 86_400_000,
            )
        )
        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Waage 680 g (Kalibriert)",
            subtitle="Reale Messung kalibriert Template-Faktor auf 'measured' und verengt Beutel-σ.",
            last_feedback=fb_weigh,
        )

        fb_abort = engine.resolve_escalation(ids["goal_id"], "abort_goal")
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Sauberer Abbruch (Freigabe)",
            subtitle="Ziel abgebrochen, geplante Schritte storniert und Inkubator-Belegung freigegeben.",
            last_feedback=fb_abort,
        )

    return {
        "id": "koernerbrut",
        "title": "1. Körnerbrut & Beutelteilung",
        "description": (
            "Igel-Stachelbart-Körnerbrut (400 g trocken → 2 Beutel): AI-Prior-Unsicherheit, "
            "Kalibrierung über Küchenwaage und saubere Ressourcenfreigabe."
        ),
        "phases": [phase_0, phase_1, phase_2],
    }


def _run_riesenschirmling_scenario() -> dict[str, Any]:
    with _temp_engine("riesenschirmling.sqlite3") as (repo, engine):
        t0 = 1_795_000_000_000
        ids = seed_riesenschirmling(repo, engine, now_oct_ms=t0)
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="2 Funde → Abgeleitetes Habitat",
            subtitle="Geo-Fusion aus 2 Funden bildet Habitat (σ ≈ 114 m) plus passives Natur-Template.",
        )

        fb_rain = engine.record_observation(
            StateCreate(
                entity_id=ids["weather_id"],
                modality="observed",
                source="measured",
                measurement_source="Wetterstation",
                property_name="rain_mm",
                label="Herbstregen 40 mm",
                value_num=40.0,
                value_sigma=2.0,
                value_unit="mm",
                time_ms=t0 + 100_000_000,
            )
        )
        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Regen 40 mm (Multi-Condition-Trigger)",
            subtitle=(
                "14-Tage-Regensumme (55 mm ≥ 35 mm) plus Temperaturabfall (-6 °C) "
                "und Frostfreiheit lösen den Saison-Trigger aus."
            ),
            last_feedback=fb_rain,
        )

        fb_frost = engine.record_observation(
            StateCreate(
                entity_id=ids["weather_id"],
                modality="observed",
                source="measured",
                measurement_source="Wetterstation",
                property_name="temperature_c",
                label="Bodenfrost -4 °C",
                value_num=-4.0,
                value_sigma=0.5,
                value_unit="°C",
                time_ms=t0 + 200_000_000,
            )
        )
        fb_zero = engine.record_observation(
            StateCreate(
                entity_id=ids["habitat_id"],
                modality="observed",
                source="measured",
                measurement_source="Kontrollgang",
                property_name="presence",
                label="Kontrollgang ohne Fund (0)",
                value_num=0.0,
                value_sigma=0.0,
                value_unit="stk",
                lat=50.812,
                lon=12.915,
                sigma_meters=140.0,
                time_ms=t0 + 300_000_000,
            )
        )
        combined_feedback = MutationFeedback(
            created_state_ids=[*fb_frost.created_state_ids, *fb_zero.created_state_ids],
            updated_state_ids=list(dict.fromkeys([*fb_frost.updated_state_ids, *fb_zero.updated_state_ids])),
            triggered_template_ids=list(
                dict.fromkeys([*fb_frost.triggered_template_ids, *fb_zero.triggered_template_ids])
            ),
            calibrated_template_ids=list(
                dict.fromkeys([*fb_frost.calibrated_template_ids, *fb_zero.calibrated_template_ids])
            ),
            goal_evaluations=fb_zero.goal_evaluations,
            uses_conflicts=fb_zero.uses_conflicts,
            open_ai_prior_state_ids=fb_zero.open_ai_prior_state_ids,
            summary=f"{fb_frost.summary} | {fb_zero.summary}",
        )
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Bodenfrost −4 °C & Null-Fund",
            subtitle="Frost bricht Vorbedingung (Stufe 4); Null-Beobachtung (0 Stk) wird explizit gespeichert.",
            last_feedback=combined_feedback,
        )

    return {
        "id": "riesenschirmling",
        "title": "2. Riesenschirmling-Habitat",
        "description": (
            "Abgeleitetes Pilz-Habitat aus 2 Fund-Weltlinien, wettergetriggerte Natur-Vorhersage "
            "(Stufe 1), Frost-Eskalation (Stufe 4) und explizite Null-Beobachtung."
        ),
        "phases": [phase_0, phase_1, phase_2],
    }


def _run_roboter_wasser_scenario() -> dict[str, Any]:
    with _temp_engine("roboter_wasser.sqlite3") as (repo, engine):
        t0 = 1_795_000_000_000
        ids = seed_roboter_wasser(repo, engine, t_now_ms=t0)
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="Greifer-Konflikt (arm2 aktiv)",
            subtitle="Aktiver CHOICE_OR-Ast 'arm2' kollidiert zeitlich auf Greifer 1 mit Kaffeekochen.",
        )

        fb_switch = engine.resolve_escalation(ids["goal_id"], "switch_branch", {"branch_key": "sideboard"})
        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Astwechsel → Sideboard",
            subtitle="Auswahl wechselt auf 'sideboard'; Kanten-Outcomes zeigen chosen vs. not_chosen.",
            last_feedback=fb_switch,
        )

        fb_lock = engine.record_observation(
            StateCreate(
                entity_id=ids["door_id"],
                modality="observed",
                source="measured",
                measurement_source="R1_Kraftsensor",
                property_name="door_state",
                label="Tür T1 verriegelt",
                value_text="verriegelt",
                distribution_kind="discrete",
                time_ms=t0 + 5_000,
            )
        )
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Tür T1 verriegelt (Stufe 4)",
            subtitle="Gemeinsame Tür-Vorbedingung bricht alle Zweige → Ziel wechselt auf Stufe 4.",
            last_feedback=fb_lock,
        )

        fb_abort = engine.resolve_escalation(ids["goal_id"], "abort_goal")
        phase_3 = export_graph_snapshot(
            repo,
            engine,
            title="Abbruch & Greifer-Freigabe",
            subtitle="Nach Zielabbruch ist Greifer 1 wieder frei und der uses-Konflikt aufgelöst.",
            last_feedback=fb_abort,
        )

    return {
        "id": "roboter_wasser",
        "title": "3. Roboter R1 & Ressourcenkonflikt",
        "description": (
            "Roboter-Handlungsplan mit exklusivem Greifer-1-Konflikt, CHOICE_OR-Alternative "
            "(Arm 2 vs. Sideboard), Tür-Vorbedingung und sauberem Abbruch."
        ),
        "phases": [phase_0, phase_1, phase_2, phase_3],
    }


def _run_igel_stachelbart_scenario() -> dict[str, Any]:
    with _temp_engine("igel_stachelbart.sqlite3") as (repo, engine):
        t0 = 1_794_767_346_000
        ids = seed_igel_stachelbart(repo, engine, t0_ms=t0, include_warmup_phase=False)
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="Tag 0–3: Start bei 16 °C & 80 ± 10 % rF",
            subtitle=(
                "Kühler Start (16 °C) dehnt die AI-Prior-Dauer leicht auf 8,33 d; "
                "Luftfeuchtigkeit 80 ± 10 % liegt im Korridor [60; 100] % (Stufe 2)."
            ),
        )

        fb_climate = None
        for day in (4, 6):
            engine.record_observation(
                StateCreate(
                    entity_id=ids["hericium_id"],
                    modality="observed",
                    source="measured",
                    measurement_source="Handy_Indoor",
                    property_name="location",
                    label=f"Indoor-Foto Tag {day}",
                    lat=53.55828,
                    lon=9.80831,
                    sigma_meters=12.0,
                    location_label="Indoor Fruchtungsbox",
                    time_ms=t0 + day * 86_400_000,
                    sigma_seconds=30.0,
                )
            )
            engine.record_observation(
                StateCreate(
                    entity_id=ids["box_id"],
                    modality="observed",
                    source="measured",
                    measurement_source="Hygrometer_Box",
                    property_name="humidity_pct",
                    label=f"Luftfeuchtigkeit Tag {day} (80 ± 10 %)",
                    value_num=80.0,
                    value_sigma=10.0,
                    value_unit="%",
                    location_label="Indoor Fruchtungsbox",
                    time_ms=t0 + day * 86_400_000,
                    sigma_seconds=30.0,
                )
            )
            fb_climate = engine.record_observation(
                StateCreate(
                    entity_id=ids["box_id"],
                    modality="observed",
                    source="measured",
                    measurement_source="Thermometer_Box",
                    property_name="temperature_c",
                    label=f"Box-Temperatur Tag {day} (18 °C)",
                    value_num=18.0,
                    value_sigma=0.5,
                    value_unit="°C",
                    location_label="Indoor Fruchtungsbox",
                    time_ms=t0 + day * 86_400_000,
                    sigma_seconds=30.0,
                )
            )

        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Tag 4–6: Temperatur steigt auf 18 °C",
            subtitle=(
                "4 Indoor-Fotos halten σ = 12 m ohne Scheinpräzision; "
                "18 °C ziehen die Live-Fruchtungsprognose zurück auf 8,0 d (Stufe 2)."
            ),
            last_feedback=fb_climate,
        )

        fb_harvest = engine.record_observation(
            StateCreate(
                entity_id=ids["hericium_id"],
                modality="observed",
                source="measured",
                measurement_source="Waage_Kueche",
                property_name="harvest_ready",
                label="Igel-Stachelbart geerntet (265 g)",
                value_num=265.0,
                value_sigma=5.0,
                value_unit="g",
                location_label="Indoor Fruchtungsbox",
                time_ms=1_795_538_193_000,
                sigma_seconds=60.0,
            )
        )
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Tag 8,92: Ernte 265 g (Kalibrierung)",
            subtitle=(
                "Realer WasWoWann-Zeitstempel (8,92 d) kalibriert das Fruchtungs-Template auf 'measured'."
            ),
            last_feedback=fb_harvest,
        )

    return {
        "id": "igel_stachelbart",
        "title": "4. Igel-Stachelbart (Reale WasWoWann-Daten)",
        "description": (
            "Reale Timelapse-Spanne aus WasWoWann plus Klima-Tracking der Indoor Fruchtungsbox "
            "(16 °C → 18 °C, 80 ± 10 % rF), Handy_Indoor-Deduplizierung (σ = 12 m) und "
            "automatische Dauer-Kalibrierung nach 8,92 Tagen."
        ),
        "phases": [phase_0, phase_1, phase_2],
    }


def _run_substrat_rezept_scenario() -> dict[str, Any]:
    with _temp_engine("substrat_rezept.sqlite3") as (repo, engine):
        t0 = 1_795_000_000_000
        ids = seed_substrat_rezept(repo, engine, start_ms=t0)
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="Optimale Mischung (1400 ± 12,2 g)",
            subtitle=(
                "500 g Sägespäne + 750 g Wasser + 150 g Brut bei 21 °C; "
                "Wasser:Späne = 1,50 im Korridor [1,35; 1,65] (Stufe 2)."
            ),
        )

        fb_dry = engine.record_observation(
            StateCreate(
                entity_id=ids["water_ent_id"],
                modality="observed",
                source="measured",
                measurement_source="Messbecher",
                property_name="water_mass",
                label="Nur 500 g Wasser abgemessen",
                value_num=500.0,
                value_sigma=10.0,
                value_unit="g",
                time_ms=t0 + 600_000,
            )
        )
        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Zu trocken: Wasser 500 g (Stufe 4)",
            subtitle="Verhältnis Wasser:Späne fällt auf 1,00 (< 1,35) und bricht die Vorbedingung.",
            last_feedback=fb_dry,
        )

        fb_ok = engine.record_observation(
            StateCreate(
                entity_id=ids["water_ent_id"],
                modality="observed",
                source="measured",
                measurement_source="Messbecher",
                property_name="water_mass",
                label="Wasser auf 760 g aufgefüllt",
                value_num=760.0,
                value_sigma=10.0,
                value_unit="g",
                time_ms=t0 + 1_200_000,
            )
        )
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Aufgefüllt: Wasser 760 g (Stufe 2)",
            subtitle="Verhältnis Wasser:Späne steigt auf 1,52 — Rezept wieder im grünen Bereich.",
            last_feedback=fb_ok,
        )

    return {
        "id": "substrat_rezept",
        "title": "5. Substrat-Rezept (Mengenrelation)",
        "description": (
            "Multi-Input-Rezept (Späne + Wasser + Körnerbrut) mit Verhältnis-Guard "
            "und Temperatur-Modulator."
        ),
        "phases": [phase_0, phase_1, phase_2],
    }


def _run_pilz_lebenszyklus_scenario() -> dict[str, Any]:
    with _temp_engine("pilz_lebenszyklus.sqlite3") as (repo, engine):
        t0 = 1_795_000_000_000
        ids = seed_pilz_lebenszyklus(repo, engine, t0_ms=t0)
        phase_0 = export_graph_snapshot(
            repo,
            engine,
            title="Übersicht: 3 Makro-Schritte (Stufe 2)",
            subtitle=(
                "Erwartete Ernte Tag 33,5 ± 2,1 d (380 g ≥ 250 g bis Tag 38). "
                "Makro-Schritt 1 kapselt 3 Unterschritte (aufklappbar)."
            ),
        )

        fb_delay = engine.record_observation(
            StateCreate(
                entity_id=ids["charge_id"],
                modality="observed",
                source="measured",
                measurement_source="Waage_Kueche",
                property_name="spawn_mass",
                label="Körnerbrut verzögert (650 g nach 20,5 d)",
                value_num=650.0,
                value_sigma=10.0,
                value_unit="g",
                time_ms=t0 + int(20.5 * 86_400_000),
                sigma_seconds=3600.0,
            )
        )
        phase_1 = export_graph_snapshot(
            repo,
            engine,
            title="Verzögerung +5 d in Schritt 1 → Kaskade (Stufe 3)",
            subtitle=(
                "+5 d in Körnerbrut schiebt Substrat (Tag 30,5) & Ernte (Tag 38,5) über Frist Tag 38 → Stufe 3."
            ),
            last_feedback=fb_delay,
        )

        fb_fail = engine.record_observation(
            StateCreate(
                entity_id=ids["autoclave_id"],
                modality="observed",
                source="measured",
                measurement_source="Autoklav_Sensor",
                property_name="steril_temp_c",
                label="Autoklav nur 105 °C (< 121 °C!)",
                value_num=105.0,
                value_sigma=1.0,
                value_unit="°C",
                time_ms=t0 + 21 * 86_400_000,
            )
        )
        phase_2 = export_graph_snapshot(
            repo,
            engine,
            title="Fehler im Detail-Unterschritt: Autoklav 105 °C (Stufe 4)",
            subtitle=(
                "Unterschritt 1.2 (Sterilisieren) bricht im Detail und rollt automatisch bis zum Ziel hoch → Stufe 4."
            ),
            last_feedback=fb_fail,
        )

        engine.record_observation(
            StateCreate(
                entity_id=ids["autoclave_id"],
                modality="observed",
                source="measured",
                measurement_source="Autoklav_Sensor",
                property_name="steril_temp_c",
                label="Nachsterilisation 122 °C",
                value_num=122.0,
                value_sigma=0.3,
                value_unit="°C",
                time_ms=t0 + 21 * 86_400_000 + 3_600_000,
            )
        )
        fb_resolved = engine.resolve_escalation(
            ids["goal_id"],
            "widen_tolerance",
            {"target_latest_ms": t0 + 42 * 86_400_000},
        )
        phase_3 = export_graph_snapshot(
            repo,
            engine,
            title="Behoben: 122 °C + Frist Tag 42 (Stufe 2)",
            subtitle="Sterilisation ok (122 °C) und Erntefrist auf Tag 42 erweitert → gesamte Kette wieder grün.",
            last_feedback=fb_resolved,
        )

    return {
        "id": "pilz_lebenszyklus",
        "title": "6. Pilz-Lebenszyklus (Kette & Detail-Zoom)",
        "description": (
            "Durchgehende Kette (Körnerbrut → Substrat → Fruchtung → Ernte) mit verschachtelten "
            "Unterschritten (Einweichen/Autoklav/Inkubator), Kaskaden-Propagation und Detail-Zoom."
        ),
        "phases": [phase_0, phase_1, phase_2, phase_3],
    }


def build_demo_scenarios_payload() -> dict[str, Any]:
    """Execute all 6 reference scenarios against real temp SQLite DBs and export snapshots."""
    koernerbrut = _run_koernerbrut_scenario()
    riesenschirmling = _run_riesenschirmling_scenario()
    roboter_wasser = _run_roboter_wasser_scenario()
    igel_stachelbart = _run_igel_stachelbart_scenario()
    substrat_rezept = _run_substrat_rezept_scenario()
    pilz_lebenszyklus = _run_pilz_lebenszyklus_scenario()
    return {
        "title": "WasWoWann · Zukunftsgraph (Live MetaCore-Engine)",
        "generated_by": "metacore.visualizer",
        "system_names": {
            "system": "WasWoWann",
            "view": "Zukunftsgraph",
            "engine": "MetaCore",
        },
        "scenario_order": [
            "koernerbrut",
            "riesenschirmling",
            "roboter_wasser",
            "igel_stachelbart",
            "substrat_rezept",
            "pilz_lebenszyklus",
        ],
        "scenarios": {
            "koernerbrut": koernerbrut,
            "riesenschirmling": riesenschirmling,
            "roboter_wasser": roboter_wasser,
            "igel_stachelbart": igel_stachelbart,
            "substrat_rezept": substrat_rezept,
            "pilz_lebenszyklus": pilz_lebenszyklus,
        },
    }


_HTML_CSS = """
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-alt: #f1f5f9;
  --border: #cbd5e1;
  --text: #0f172a;
  --muted: #475569;
  --obs: #2563eb;
  --obs-bg: #eff6ff;
  --ok: #059669;
  --ok-bg: #ecfdf5;
  --warn: #d97706;
  --warn-bg: #fffbeb;
  --fail: #dc2626;
  --fail-bg: #fef2f2;
  --cancel: #64748b;
  --cancel-bg: #f1f5f9;
  --tpl: #7c3aed;
  --tpl-bg: #f5f3ff;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.45;
}
.shell {
  max-width: 1480px;
  margin: 0 auto;
  padding: 16px 20px 36px;
}
.header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 18px;
  margin-bottom: 14px;
}
.header h1 { margin: 0; font-size: 1.25rem; font-weight: 700; }
.header-sub { margin: 3px 0 0; color: var(--muted); font-size: 0.86rem; }
.pill-row { display: flex; flex-wrap: wrap; gap: 8px; }
.pill {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  border-radius: 999px;
  padding: 7px 14px;
  font-size: 0.86rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.12s ease;
}
.pill:hover { border-color: var(--obs); background: var(--obs-bg); }
.pill.active { background: #0f172a; color: #ffffff; border-color: #0f172a; }
.phase-bar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 14px;
}
.phase-steps { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.phase-btn {
  border: 1px solid var(--border);
  background: var(--surface-alt);
  color: var(--text);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 0.84rem;
  font-weight: 600;
  cursor: pointer;
}
.phase-btn.active { background: var(--obs); color: #fff; border-color: var(--obs); }
.phase-nav { display: flex; gap: 6px; }
.nav-btn {
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: 8px;
  padding: 6px 11px;
  cursor: pointer;
  font-weight: 600;
}
.nav-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.feedback-banner {
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-left: 4px solid #0284c7;
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 14px;
  font-size: 0.86rem;
}
.main-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.75fr) minmax(320px, 1fr);
  gap: 14px;
  align-items: start;
}
@media (max-width: 1080px) {
  .main-grid { grid-template-columns: 1fr; }
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  margin-bottom: 14px;
}
.card h2 {
  margin: 0 0 10px;
  font-size: 0.96rem;
  font-weight: 700;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.svg-wrap {
  width: 100%;
  overflow-x: auto;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
}
svg { display: block; width: 100%; height: auto; min-width: 780px; }
.zoom-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 8px 10px;
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 9px;
}
.zoom-btn {
  border: 1px solid var(--tpl);
  background: #ffffff;
  color: var(--tpl);
  border-radius: 7px;
  padding: 4px 10px;
  font-size: 0.78rem;
  font-weight: 700;
  cursor: pointer;
}
.zoom-btn.expanded {
  background: var(--tpl);
  color: #ffffff;
}
.zoom-chip { cursor: pointer; }
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 18px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #e2e8f0;
  font-size: 0.78rem;
  color: var(--muted);
}
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 18px; height: 11px; border-radius: 3px; display: inline-block; }
.goal-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.goal-card.on_track { border-left: 4px solid var(--ok); background: var(--ok-bg); }
.goal-card.at_risk { border-left: 4px solid var(--warn); background: var(--warn-bg); }
.goal-card.broken { border-left: 4px solid var(--fail); background: var(--fail-bg); }
.goal-card.cancelled { border-left: 4px solid var(--cancel); background: var(--cancel-bg); }
.badge {
  display: inline-block;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 0.74rem;
  font-weight: 700;
  border: 1px solid currentColor;
}
.badge.on_track { color: var(--ok); background: #fff; }
.badge.at_risk { color: var(--warn); background: #fff; }
.badge.broken { color: var(--fail); background: #fff; }
.badge.cancelled { color: var(--cancel); background: #fff; }
.badge.info { color: var(--obs); background: var(--obs-bg); }
.badge.tpl { color: var(--tpl); background: var(--tpl-bg); }
.prob-bar-bg {
  height: 8px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.1);
  overflow: hidden;
  margin: 6px 0 8px;
}
.prob-bar-fill { height: 100%; border-radius: 999px; transition: width 0.2s ease; }
.dim-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  font-size: 0.78rem;
}
.dim-chip {
  background: rgba(255, 255, 255, 0.85);
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 4px 7px;
  display: flex;
  justify-content: space-between;
}
.dim-chip.broken { border-color: var(--fail); color: var(--fail); font-weight: 700; }
.conflict-box {
  border: 1px solid #fecaca;
  background: var(--fail-bg);
  color: #7f1d1d;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
  font-size: 0.82rem;
}
.ok-box {
  border: 1px solid #a7f3d0;
  background: var(--ok-bg);
  color: #065f46;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
  font-size: 0.82rem;
}
.kv-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.kv-table th, .kv-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid #e2e8f0;
  vertical-align: top;
}
.kv-table th { color: var(--muted); font-weight: 600; background: var(--surface-alt); }
.mono { font-family: var(--mono); font-size: 0.77rem; }
.tab-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.tab-btn {
  border: 1px solid var(--border);
  background: var(--surface-alt);
  border-radius: 7px;
  padding: 5px 10px;
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
}
.tab-btn.active { background: #0f172a; color: #fff; border-color: #0f172a; }
.node-g { cursor: pointer; }
.node-g:hover rect.node-card { filter: brightness(0.97); }
"""

_HTML_SCRIPT = r"""
(() => {
  const esc = (val) => String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

  const fmtNum = (val, digits = 2) => {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
    const num = Number(val);
    return Number.isInteger(num) ? String(num) : num.toFixed(digits);
  };

  const fmtPct = (prob) => `${Math.round(Number(prob || 0) * 100)}%`;

  const fmtTime = (ms) => {
    if (ms === null || ms === undefined) return "—";
    const d = new Date(Number(ms));
    return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
  };

  const fmtDuration = (sec, sigmaSec) => {
    if (sec === null || sec === undefined) return "—";
    const s = Number(sec);
    const sig = sigmaSec !== null && sigmaSec !== undefined ? Number(sigmaSec) : null;
    if (s >= 86400) {
      const base = `${(s / 86400).toFixed(2)} d`;
      return sig !== null ? `${base} ± ${(sig / 86400).toFixed(2)} d` : base;
    }
    const base = `${Math.round(s)} s`;
    return sig !== null ? `${base} ± ${Math.round(sig)} s` : base;
  };

  const statusColor = (status) => ({
    on_track: { stroke: "#059669", fill: "#ecfdf5", text: "#065f46" },
    at_risk: { stroke: "#d97706", fill: "#fffbeb", text: "#92400e" },
    broken: { stroke: "#dc2626", fill: "#fef2f2", text: "#991b1b" },
    cancelled: { stroke: "#64748b", fill: "#f1f5f9", text: "#475569" },
    observed: { stroke: "#2563eb", fill: "#eff6ff", text: "#1e40af" },
    template: { stroke: "#7c3aed", fill: "#f5f3ff", text: "#5b21b6" },
    happened: { stroke: "#0f766e", fill: "#f0fdfa", text: "#115e59" },
  }[status] || { stroke: "#475569", fill: "#f8fafc", text: "#0f172a" });

  let activeScenarioId = PAYLOAD.scenario_order[0];
  let activePhaseIdx = 0;
  let activeTableTab = "states";
  let selectedItem = null; // { kind: "state" | "tx", id: string }
  const expandedParentIds = new Set();

  const getScenario = () => PAYLOAD.scenarios[activeScenarioId];
  const getSnap = () => getScenario().phases[activePhaseIdx];

  function getChildCountByParent(snap) {
    const counts = {};
    for (const t of snap.transformations || []) {
      if (t.parent_id) {
        counts[t.parent_id] = (counts[t.parent_id] || 0) + 1;
      }
    }
    return counts;
  }

  function latestObsState(snap, entityId, propertyName) {
    const matches = (snap.states || []).filter(
      (s) => s.modality === "observed" && (!entityId || s.entity_id === entityId) && (!propertyName || s.property_name === propertyName)
    );
    if (!matches.length) return null;
    matches.sort((a, b) => (a.time_ms ?? a.created_at_ms) - (b.time_ms ?? b.created_at_ms));
    return matches[matches.length - 1];
  }

  function evaluateTxGuardsOk(tx, snap) {
    const guards = (tx.formula && tx.formula.guards) || [];
    if (!guards.length) return true;
    const logic = String((tx.formula && tx.formula.guard_logic) || "AND").toUpperCase();
    const checks = guards.map((g) => {
      if (g.kind === "ratio") {
        const num = latestObsState(snap, g.numerator_entity_id, g.numerator_property);
        const den = latestObsState(snap, g.denominator_entity_id, g.denominator_property);
        if (!num || !den || num.value_num === null || den.value_num === null || Math.abs(den.value_num) < 1e-12) return false;
        const r = num.value_num / den.value_num;
        if (g.min_ratio !== undefined && g.min_ratio !== null && r < Number(g.min_ratio)) return false;
        if (g.max_ratio !== undefined && g.max_ratio !== null && r > Number(g.max_ratio)) return false;
        return true;
      }
      const st = latestObsState(snap, g.entity_id, g.property_name);
      if (!st) return false;
      if (g.forbidden_texts && st.value_text && g.forbidden_texts.includes(st.value_text)) return false;
      if (g.required_text && st.value_text !== g.required_text) return false;
      if (st.value_num !== null && st.value_num !== undefined) {
        if (g.min_value !== undefined && g.min_value !== null && st.value_num < Number(g.min_value)) return false;
        if (g.max_value !== undefined && g.max_value !== null && st.value_num > Number(g.max_value)) return false;
      }
      return true;
    });
    return logic === "OR" ? checks.some(Boolean) : checks.every(Boolean);
  }

  function reachesState(startTxId, targetStateId, snap) {
    if (!targetStateId) return false;
    const q = [startTxId];
    const seen = new Set();
    while (q.length > 0) {
      const tid = q.shift();
      if (seen.has(tid)) continue;
      seen.add(tid);
      const outEdges = (snap.edges || []).filter((e) => e.transformation_id === tid && e.direction === "out");
      for (const oe of outEdges) {
        if (oe.state_id === targetStateId) return true;
        for (const ie of (snap.edges || []).filter((e) => e.state_id === oe.state_id && e.direction === "in")) {
          if (!seen.has(ie.transformation_id)) q.push(ie.transformation_id);
        }
      }
    }
    return false;
  }

  function resolveTxStatus(tx, snap) {
    if (tx.modality === "cancelled") return "cancelled";
    if (tx.modality === "template") return "template";
    if (tx.modality === "happened") return "happened";
    const conflicts = snap.uses_conflicts || [];
    if (conflicts.some((c) => c.transformation_a_id === tx.id || c.transformation_b_id === tx.id)) {
      return "at_risk";
    }
    const outEdges = (snap.edges || []).filter((e) => e.transformation_id === tx.id && e.direction === "out");
    if (outEdges.length > 0 && outEdges.every((e) => e.outcome === "not_chosen" || !e.is_selected)) {
      return "cancelled";
    }
    const children = (snap.transformations || []).filter((c) => c.parent_id === tx.id && c.modality === "planned");
    if (children.some((c) => !evaluateTxGuardsOk(c, snap))) {
      return "broken";
    }
    if (!evaluateTxGuardsOk(tx, snap)) {
      return "broken";
    }
    for (const ev of snap.goal_evaluations || []) {
      const alt = (ev.alternative_branches || []).find((b) => b.transformation_id === tx.id);
      if (alt) {
        if (alt.broken_dimensions && alt.broken_dimensions.length > 0) return "broken";
        return alt.is_selected ? ev.status : "cancelled";
      }
      if (outEdges.some((e) => e.state_id === ev.predicted_state_id)) return ev.status;
    }
    for (const ev of snap.goal_evaluations || []) {
      if (ev.predicted_state_id && reachesState(tx.id, ev.predicted_state_id, snap)) {
        if (ev.status === "at_risk") return "at_risk";
        if (ev.status === "broken" && (ev.broken_dimensions || []).includes("wann") && !(ev.broken_dimensions || []).includes("vorbedingung")) {
          return "broken";
        }
      }
    }
    return "on_track";
  }

  function resolveStateStatus(st, snap, txMap) {
    if (st.modality === "cancelled") return "cancelled";
    if (st.modality === "observed") return "observed";
    const evals = snap.goal_evaluations || [];
    if (st.modality === "desired") {
      const ev = evals.find((g) => g.goal_id === st.id);
      return ev ? ev.status : "on_track";
    }
    // Predicted state: use linked goal evaluation if present; mark unselected branches muted.
    const incoming = (snap.edges || []).filter((e) => e.direction === "out" && e.state_id === st.id);
    if (incoming.length > 0 && incoming.every((e) => e.outcome === "not_chosen" || !e.is_selected)) {
      return "cancelled";
    }
    const directGoal = evals.find((g) => g.predicted_state_id === st.id);
    if (directGoal) return directGoal.status;
    for (const ev of evals) {
      if ((ev.alternative_branches || []).some((b) => {
        const bEdges = (snap.edges || []).filter((e) => e.transformation_id === b.transformation_id && e.direction === "out");
        return bEdges.some((e) => e.state_id === st.id);
      })) {
        const alt = ev.alternative_branches.find((b) =>
          (snap.edges || []).some((e) => e.transformation_id === b.transformation_id && e.state_id === st.id)
        );
        if (alt) {
          if (alt.broken_dimensions && alt.broken_dimensions.length > 0) return "broken";
          return alt.goal_probability >= 0.8 ? "on_track" : "at_risk";
        }
      }
    }
    const entityGoal = evals.find((g) => g.entity_id === st.entity_id && g.property_name === st.property_name);
    if (entityGoal) return entityGoal.status;
    for (const ie of incoming) {
      const ptx = txMap[ie.transformation_id];
      if (ptx) {
        const pStatus = resolveTxStatus(ptx, snap);
        if (pStatus === "broken" || pStatus === "at_risk") return pStatus;
      }
    }
    return "on_track";
  }

  function describeStateValue(st) {
    if (st.value_num !== null && st.value_num !== undefined) {
      const sig = st.value_sigma !== null && st.value_sigma !== undefined ? ` ± ${fmtNum(st.value_sigma)}` : "";
      return `${fmtNum(st.value_num)}${sig} ${st.value_unit || ""}`.trim();
    }
    if (st.value_text) return st.value_text;
    if (st.lat !== null && st.lat !== undefined) {
      return `${Number(st.lat).toFixed(4)}, ${Number(st.lon).toFixed(4)} (±${fmtNum(st.sigma_meters, 1)} m)`;
    }
    const bounds = [];
    if (st.target_min !== null && st.target_min !== undefined) bounds.push(`≥ ${fmtNum(st.target_min)} ${st.value_unit || ""}`.trim());
    if (st.target_max !== null && st.target_max !== undefined) bounds.push(`≤ ${fmtNum(st.target_max)} ${st.value_unit || ""}`.trim());
    if (st.target_value_text) bounds.push(`= ${st.target_value_text}`);
    if (st.target_radius_meters !== null && st.target_radius_meters !== undefined) bounds.push(`r ≤ ${fmtNum(st.target_radius_meters, 0)} m`);
    return bounds.join(" · ") || "—";
  }

  function renderGraphSvg(snap) {
    const allStates = snap.states || [];
    const allTxs = snap.transformations || [];
    const allEdges = snap.edges || [];
    const txMap = Object.fromEntries(allTxs.map((t) => [t.id, t]));
    const entMap = Object.fromEntries((snap.entities || []).map((e) => [e.id, e]));
    const childCounts = getChildCountByParent(snap);

    const visibleTxs = allTxs.filter((tx) => {
      if (tx.parent_id && !expandedParentIds.has(tx.parent_id)) return false;
      if (tx.modality === "planned" && childCounts[tx.id] > 0 && expandedParentIds.has(tx.id)) return false;
      return true;
    });
    const visibleTxIds = new Set(visibleTxs.map((t) => t.id));

    const visibleStates = allStates.filter((st) => {
      const owner = st.meta && st.meta.internal_to_parent_tx_id;
      if (owner && !expandedParentIds.has(owner)) return false;
      return true;
    });
    const visibleStateIds = new Set(visibleStates.map((s) => s.id));

    const visibleEdges = allEdges.filter(
      (e) => visibleTxIds.has(e.transformation_id) && visibleStateIds.has(e.state_id)
    );

    const isChainGraph = visibleStates.some(
      (s) => (s.modality === "predicted" || s.modality === "cancelled") && visibleEdges.some((e) => e.direction === "in" && e.state_id === s.id)
    );

    const pos = new Map();
    const rowGap = 86;
    const topPad = 58;
    let width = 980;
    let height = 360;
    let nowX = 252;
    let columnHeadersHtml = "";

    if (!isChainGraph) {
      const colObs = visibleStates.filter((s) => s.modality === "observed");
      const colTx = visibleTxs;
      const colPred = visibleStates.filter((s) => s.modality === "predicted" || (s.modality === "cancelled" && s.source !== "user_goal"));
      const colGoal = visibleStates.filter((s) => s.modality === "desired" || (s.modality === "cancelled" && s.source === "user_goal"));

      const maxRows = Math.max(colObs.length, colTx.length, colPred.length, colGoal.length, 1);
      height = Math.max(360, topPad + maxRows * rowGap + 36);

      const layoutCol = (items, xCenter, kind) => {
        const n = items.length;
        if (n === 0) return;
        const totalH = (n - 1) * rowGap;
        const startY = topPad + (height - topPad - 36 - totalH) / 2;
        items.forEach((item, idx) => {
          pos.set(`${kind}:${item.id}`, { x: xCenter, y: startY + idx * rowGap });
        });
      };

      layoutCol(colObs, 120, "state");
      layoutCol(colTx, 395, "tx");
      layoutCol(colPred, 650, "state");
      layoutCol(colGoal, 875, "state");

      columnHeadersHtml =
        `<text x="120" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">OBSERVED (Fakten / Vergangen)</text>` +
        `<text x="395" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">TRANSFORMATIONEN / TEMPLATES</text>` +
        `<text x="650" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">PREDICTED (Vorhersagen)</text>` +
        `<text x="875" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">DESIRED (Ziele)</text>`;
    } else {
      const txCol = {};
      const stateCol = {};
      visibleStates.forEach((s) => {
        stateCol[s.id] = s.modality === "observed" ? 0 : 2;
      });
      visibleTxs.forEach((tx) => {
        if (tx.modality !== "template") txCol[tx.id] = 1;
      });

      const plannedVisible = visibleTxs.filter((tx) => tx.modality !== "template");
      for (let iter = 0; iter < plannedVisible.length + 4; iter++) {
        for (const tx of plannedVisible) {
          const inPredCols = visibleEdges
            .filter((e) => e.transformation_id === tx.id && e.direction === "in")
            .map((e) => visibleStates.find((s) => s.id === e.state_id))
            .filter((s) => s && s.modality !== "observed")
            .map((s) => stateCol[s.id] ?? 0);
          if (inPredCols.length > 0) {
            txCol[tx.id] = Math.max(txCol[tx.id], Math.max(...inPredCols) + 1);
          }
          for (const oe of visibleEdges.filter((e) => e.transformation_id === tx.id && e.direction === "out")) {
            const os = visibleStates.find((s) => s.id === oe.state_id);
            if (os && os.modality !== "desired") {
              stateCol[os.id] = Math.max(stateCol[os.id] || 0, txCol[tx.id] + 1);
            }
          }
        }
      }

      // Place each observed state immediately left of its earliest consuming/triggering tx
      for (const s of visibleStates) {
        if (s.modality !== "observed") continue;
        const linkedCols = visibleEdges
          .filter((e) => e.state_id === s.id && e.direction === "in" && txCol[e.transformation_id] !== undefined)
          .map((e) => txCol[e.transformation_id]);
        stateCol[s.id] = linkedCols.length > 0 ? Math.max(0, Math.min(...linkedCols) - 1) : 0;
      }

      // Align template boxes under their instantiated planned transformation column
      for (const tx of visibleTxs) {
        if (tx.modality !== "template") continue;
        const matchPlanned = visibleTxs.find((p) => p.modality !== "template" && p.template_id === tx.id);
        txCol[tx.id] = matchPlanned ? txCol[matchPlanned.id] : 1;
      }

      let maxChainCol = 2;
      for (const c of Object.values(txCol)) maxChainCol = Math.max(maxChainCol, c);
      for (const s of visibleStates) {
        if (s.modality !== "desired" && !(s.modality === "cancelled" && s.source === "user_goal")) {
          maxChainCol = Math.max(maxChainCol, stateCol[s.id] || 0);
        }
      }
      const goalCol = maxChainCol + 1;
      for (const s of visibleStates) {
        if (s.modality === "desired" || (s.modality === "cancelled" && s.source === "user_goal")) {
          stateCol[s.id] = goalCol;
        }
      }

      const colStep = 230;
      width = Math.max(980, 240 + goalCol * colStep);
      nowX = 120 + Math.round(colStep * 0.5);

      const buckets = {};
      for (const s of visibleStates) {
        const c = stateCol[s.id] ?? 0;
        (buckets[c] = buckets[c] || []).push({ kind: "state", item: s, rank: s.modality === "predicted" ? 0 : 1 });
      }
      for (const tx of visibleTxs) {
        const c = txCol[tx.id] ?? 1;
        (buckets[c] = buckets[c] || []).push({ kind: "tx", item: tx, rank: tx.modality === "template" ? 3 : 2 });
      }
      for (const c of Object.keys(buckets)) {
        buckets[c].sort((a, b) => a.rank - b.rank);
      }

      const maxRows = Math.max(1, ...Object.values(buckets).map((b) => b.length));
      height = Math.max(400, topPad + maxRows * rowGap + 40);

      for (const [cStr, items] of Object.entries(buckets)) {
        const xCenter = 120 + Number(cStr) * colStep;
        const n = items.length;
        const totalH = (n - 1) * rowGap;
        const startY = topPad + (height - topPad - 36 - totalH) / 2;
        items.forEach((entry, idx) => {
          pos.set(`${entry.kind}:${entry.item.id}`, { x: xCenter, y: startY + idx * rowGap });
        });
      }

      columnHeadersHtml =
        `<text x="120" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">OBSERVED (Start)</text>` +
        `<text x="${120 + Math.floor(goalCol / 2) * colStep}" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">ZUKUNFTSKETTE (Schritte &amp; Prognosen)</text>` +
        `<text x="${120 + goalCol * colStep}" y="24" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">DESIRED (Ziel)</text>`;
    }

    const parts = [];
    const minSvgWidth = isChainGraph ? width : 780;
    parts.push(`<svg viewBox="0 0 ${width} ${height}" style="min-width:${minSvgWidth}px" xmlns="http://www.w3.org/2000/svg">`);
    parts.push(`
      <defs>
        <marker id="arr-slate" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,1 L7,4 L0,7 Z" fill="#64748b"/>
        </marker>
        <marker id="arr-red" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,1 L7,4 L0,7 Z" fill="#dc2626"/>
        </marker>
        <marker id="arr-tpl" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,1 L7,4 L0,7 Z" fill="#7c3aed"/>
        </marker>
      </defs>
    `);

    // Column headers & JETZT axis
    parts.push(columnHeadersHtml);
    parts.push(`<line x1="${nowX}" y1="16" x2="${nowX}" y2="${height - 14}" stroke="#0f172a" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.35"/>`);
    parts.push(`<rect x="${nowX - 26}" y="10" width="52" height="18" rx="9" fill="#0f172a"/>`);
    parts.push(`<text x="${nowX}" y="22" text-anchor="middle" font-size="10" font-weight="700" fill="#ffffff">JETZT</text>`);

    // Subtle group box around any expanded composite parent's visible children & internal states
    for (const parentId of expandedParentIds) {
      const parentTx = txMap[parentId];
      if (!parentTx) continue;
      const boxPts = [];
      for (const tx of visibleTxs) {
        if (tx.parent_id === parentId) {
          const pt = pos.get(`tx:${tx.id}`);
          if (pt) boxPts.push(pt);
        }
      }
      for (const st of visibleStates) {
        if (st.meta && st.meta.internal_to_parent_tx_id === parentId) {
          const pt = pos.get(`state:${st.id}`);
          if (pt) boxPts.push(pt);
        }
      }
      if (boxPts.length > 0) {
        const minX = Math.min(...boxPts.map((p) => p.x)) - 104;
        const maxX = Math.max(...boxPts.map((p) => p.x)) + 104;
        const minY = Math.min(...boxPts.map((p) => p.y)) - 44;
        const maxY = Math.max(...boxPts.map((p) => p.y)) + 42;
        parts.push(
          `<rect x="${minX}" y="${minY}" width="${maxX - minX}" height="${maxY - minY}" rx="14" ` +
          `fill="#f5f3ff" fill-opacity="0.45" stroke="#7c3aed" stroke-width="1.6" stroke-dasharray="6 4"/>`
        );
        parts.push(
          `<text x="${minX + 12}" y="${minY + 15}" font-size="10.5" font-weight="700" fill="#5b21b6">` +
          `▼ ${esc(parentTx.name)} (Detailansicht)</text>`
        );
      }
    }

    // Goal -> Predicted alignment connectors (dashed target matching link)
    for (const ev of snap.goal_evaluations || []) {
      const gPos = pos.get(`state:${ev.goal_id}`);
      const pPos = ev.predicted_state_id ? pos.get(`state:${ev.predicted_state_id}`) : null;
      if (gPos && pPos) {
        const col = statusColor(ev.status).stroke;
        parts.push(
          `<path d="M ${pPos.x + 88} ${pPos.y} C ${pPos.x + 125} ${pPos.y}, ${gPos.x - 125} ${gPos.y}, ${gPos.x - 84} ${gPos.y}" ` +
          `fill="none" stroke="${col}" stroke-width="2" stroke-dasharray="3 3" opacity="0.8"/>`
        );
      }
    }

    // Explicit DB edges
    const conflicts = snap.uses_conflicts || [];
    for (const e of visibleEdges) {
      const sPos = pos.get(`state:${e.state_id}`);
      const tPos = pos.get(`tx:${e.transformation_id}`);
      if (!sPos || !tPos) continue;
      const x1 = e.direction === "in" ? sPos.x + 84 : tPos.x + 86;
      const y1 = e.direction === "in" ? sPos.y : tPos.y;
      const x2 = e.direction === "in" ? tPos.x - 86 : sPos.x - 84;
      const y2 = e.direction === "in" ? tPos.y : sPos.y;
      const dx = Math.max(36, Math.abs(x2 - x1) * 0.45);
      const inConflict = e.role === "uses" && conflicts.some(
        (c) => c.transformation_a_id === e.transformation_id || c.transformation_b_id === e.transformation_id
      );

      let stroke = "#64748b";
      let dash = "";
      let marker = "url(#arr-slate)";
      if (e.role === "triggers") {
        stroke = "#7c3aed";
        dash = "3 3";
        marker = "url(#arr-tpl)";
      } else if (e.role === "uses") {
        if (e.resource_released) {
          stroke = "#0d9488";
          dash = "5 4";
        } else if (inConflict) {
          stroke = "#dc2626";
          marker = "url(#arr-red)";
        } else {
          stroke = "#d97706";
        }
      } else if (e.outcome === "not_chosen" || !e.is_selected) {
        stroke = "#94a3b8";
        dash = "5 5";
      }

      const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      parts.push(
        `<path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.8" ` +
        `${dash ? `stroke-dasharray="${dash}"` : ""} marker-end="${marker}" opacity="0.88"/>`
      );
      const mx = (x1 + x2) / 2;
      const my = (y1 + y2) / 2 - 5;
      let label = e.role;
      if (e.logic_group !== "AND") label += ` [${e.logic_group}:${e.branch_key}]`;
      if (e.role === "uses" && e.resource_released) label += " (frei)";
      parts.push(
        `<text x="${mx}" y="${my}" text-anchor="middle" font-size="9.5" font-weight="600" fill="${stroke}">${esc(label)}</text>`
      );
    }

    // Draw Transformations
    for (const tx of visibleTxs) {
      const p = pos.get(`tx:${tx.id}`);
      if (!p) continue;
      const st = resolveTxStatus(tx, snap);
      const pal = statusColor(st);
      const isSelected = selectedItem && selectedItem.kind === "tx" && selectedItem.id === tx.id;
      const dash = tx.modality === "planned" ? 'stroke-dasharray="6 3"' : (tx.modality === "template" ? 'stroke-dasharray="2 3"' : "");
      const bw = isSelected ? 3 : 1.8;
      const nestedCount = tx.modality === "planned" ? (childCounts[tx.id] || 0) : 0;
      parts.push(`<g class="node-g" data-kind="tx" data-id="${esc(tx.id)}">`);
      parts.push(`<rect class="node-card" x="${p.x - 86}" y="${p.y - 26}" width="172" height="52" rx="9" fill="${pal.fill}" stroke="${pal.stroke}" stroke-width="${bw}" ${dash}/>`);
      const nameShort = tx.name.length > 25 ? tx.name.slice(0, 24) + "…" : tx.name;
      parts.push(`<text x="${p.x}" y="${p.y - 5}" text-anchor="middle" font-size="11" font-weight="700" fill="${pal.text}">${esc(nameShort)}</text>`);
      const sub = `${tx.modality} · ${tx.driver}` + (tx.duration_seconds !== null && tx.duration_seconds !== undefined ? ` · ${fmtDuration(tx.duration_seconds)}` : "");
      parts.push(`<text x="${p.x}" y="${p.y + 13}" text-anchor="middle" font-size="9.5" fill="#475569">${esc(sub)}</text>`);
      if (nestedCount > 0) {
        parts.push(`<g class="zoom-chip" data-toggle-parent="${esc(tx.id)}">`);
        parts.push(`<rect x="${p.x - 52}" y="${p.y - 36}" width="104" height="16" rx="8" fill="#7c3aed"/>`);
        parts.push(`<text x="${p.x}" y="${p.y - 25}" text-anchor="middle" font-size="9" font-weight="700" fill="#ffffff">▶ +${nestedCount} Unterschritte</text>`);
        parts.push(`</g>`);
      }
      parts.push(`</g>`);
    }

    // Draw States
    for (const s of visibleStates) {
      const p = pos.get(`state:${s.id}`);
      if (!p) continue;
      const st = resolveStateStatus(s, snap, txMap);
      const pal = statusColor(st);
      const isSelected = selectedItem && selectedItem.kind === "state" && selectedItem.id === s.id;
      const dash = s.modality === "predicted" ? 'stroke-dasharray="6 3"' : "";
      const bw = isSelected ? 3 : 1.8;
      const ent = entMap[s.entity_id];
      const valLine = describeStateValue(s);
      const titleShort = s.label.length > 26 ? s.label.slice(0, 25) + "…" : s.label;
      const subShort = valLine.length > 28 ? valLine.slice(0, 27) + "…" : valLine;

      parts.push(`<g class="node-g" data-kind="state" data-id="${esc(s.id)}">`);
      if (s.modality === "desired") {
        parts.push(`<rect x="${p.x - 88}" y="${p.y - 31}" width="176" height="62" rx="13" fill="none" stroke="${pal.stroke}" stroke-width="1.4" opacity="0.55"/>`);
      }
      parts.push(`<rect class="node-card" x="${p.x - 82}" y="${p.y - 26}" width="164" height="52" rx="9" fill="${pal.fill}" stroke="${pal.stroke}" stroke-width="${bw}" ${dash}/>`);
      parts.push(`<text x="${p.x}" y="${p.y - 8}" text-anchor="middle" font-size="10.5" font-weight="700" fill="${pal.text}">${esc(titleShort)}</text>`);
      parts.push(`<text x="${p.x}" y="${p.y + 7}" text-anchor="middle" font-size="9.5" fill="#0f172a">${esc(subShort)}</text>`);
      const badge = `${ent ? ent.name.split(" ")[0] : s.entity_id} · ${s.source}`;
      const badgeShort = badge.length > 28 ? badge.slice(0, 27) + "…" : badge;
      parts.push(`<text x="${p.x}" y="${p.y + 20}" text-anchor="middle" font-size="8.5" fill="#64748b">${esc(badgeShort)}</text>`);
      parts.push(`</g>`);
    }

    parts.push(`</svg>`);
    return parts.join("");
  }

  function renderGoals(snap) {
    const evals = snap.goal_evaluations || [];
    if (evals.length === 0) return `<p class="muted">Keine Ziele in diesem Snapshot.</p>`;
    return evals.map((g) => {
      const col = statusColor(g.status).stroke;
      const pct = fmtPct(g.goal_probability);
      const dims = Object.entries(g.dimension_probs || {}).map(([dim, val]) => {
        const isBroken = (g.broken_dimensions || []).includes(dim);
        return `<div class="dim-chip ${isBroken ? "broken" : ""}"><span>${esc(dim)}</span><span>${fmtPct(val)}</span></div>`;
      }).join("");
      const branches = (g.alternative_branches || []).length > 0
        ? `<div style="margin-top:8px;font-size:0.78rem;">
            <strong>Zweige:</strong>
            ${g.alternative_branches.map((b) =>
              `<div style="display:flex;justify-content:space-between;padding:2px 0;">
                <span>${b.is_selected ? "●" : "○"} ${esc(b.branch_key)} (${esc(b.transformation_name)})</span>
                <span>P=${fmtPct(b.goal_probability)}${b.broken_dimensions.length ? " · gebrochen: " + esc(b.broken_dimensions.join(",")) : ""}</span>
              </div>`
            ).join("")}
          </div>`
        : "";
      return `
        <div class="goal-card ${esc(g.status)}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <strong style="font-size:0.88rem;">${esc(g.label)}</strong>
            <span class="badge ${esc(g.status)}">Stufe ${esc(g.stage)} · ${esc(g.status)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:0.8rem;margin-top:4px;">
            <span>Ziel-Wahrscheinlichkeit</span>
            <strong>${pct}</strong>
          </div>
          <div class="prob-bar-bg"><div class="prob-bar-fill" style="width:${pct};background:${col};"></div></div>
          <div class="dim-grid">${dims}</div>
          ${branches}
          <div style="margin-top:6px;font-size:0.78rem;color:#334155;">${esc(g.summary)}</div>
        </div>
      `;
    }).join("");
  }

  function renderConflictsAndPriors(snap) {
    const conflicts = snap.uses_conflicts || [];
    const conflictHtml = conflicts.length > 0
      ? conflicts.map((c) => `
          <div class="conflict-box">
            <strong>Ressourcen-Konflikt auf ${esc(c.entity_name)}</strong><br/>
            ${esc(c.transformation_a_name)} ↔ ${esc(c.transformation_b_name)}<br/>
            <span class="mono">${fmtTime(c.overlap_start_ms)} – ${fmtTime(c.overlap_end_ms)}</span>
          </div>
        `).join("")
      : `<div class="ok-box">Keine aktiven <code>uses</code>-Ressourcenkonflikte.</div>`;

    const openStates = snap.open_ai_priors || [];
    const openTpls = snap.open_ai_prior_templates || [];
    const geoFusions = snap.entity_geo_fusions || [];

    const geoHtml = geoFusions.length > 0
      ? geoFusions.map((gf) => `
          <div style="font-size:0.8rem;padding:4px 0;border-top:1px solid #e2e8f0;">
            <strong>Geo-Fusion ${esc(gf.entity_name)}:</strong>
            σ = <strong>${fmtNum(gf.sigma_meters, 1)} m</strong>
            (${gf.observation_count} Beobachtungen, Quellen: ${esc((gf.sources || []).join(", ") || "—")})
          </div>
        `).join("")
      : "";

    return `
      ${conflictHtml}
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;">
        <span class="badge info">Offene AI-Prior-Zustände: ${openStates.length}</span>
        <span class="badge tpl">Offene AI-Prior-Templates: ${openTpls.length}</span>
      </div>
      ${geoHtml}
    `;
  }

  function renderSelectionDetails(snap) {
    if (!selectedItem) {
      return `<p style="font-size:0.82rem;color:#475569;margin:0;">Klicke einen Knoten oder eine Transformation im Zukunftsgraph an, um alle Felder anzuzeigen.</p>`;
    }
    if (selectedItem.kind === "state") {
      const s = (snap.states || []).find((x) => x.id === selectedItem.id);
      if (!s) return `<p style="font-size:0.82rem;">Zustand nicht mehr vorhanden.</p>`;
      return `
        <table class="kv-table">
          <tr><th>ID / Property</th><td class="mono">${esc(s.id)} · ${esc(s.property_name)}</td></tr>
          <tr><th>Modalität / Quelle</th><td>${esc(s.modality)} / <strong>${esc(s.source)}</strong> (${esc(s.measurement_source || "—")})</td></tr>
          <tr><th>Wert / Grenzen</th><td>${esc(describeStateValue(s))}</td></tr>
          <tr><th>Zeitpunkt</th><td class="mono">${fmtTime(s.time_ms)}</td></tr>
          <tr><th>Ort</th><td>${esc(s.location_label || "—")}</td></tr>
        </table>
      `;
    }
    const tx = (snap.transformations || []).find((x) => x.id === selectedItem.id);
    if (!tx) return `<p style="font-size:0.82rem;">Transformation nicht mehr vorhanden.</p>`;
    return `
      <table class="kv-table">
        <tr><th>ID</th><td class="mono">${esc(tx.id)}</td></tr>
        <tr><th>Name</th><td><strong>${esc(tx.name)}</strong></td></tr>
        <tr><th>Modalität / Driver</th><td>${esc(tx.modality)} / ${esc(tx.driver)}</td></tr>
        <tr><th>Dauer / Quelle</th><td>${fmtDuration(tx.duration_seconds, tx.duration_sigma_seconds)} (<strong>${esc(tx.duration_source)}</strong>)</td></tr>
        <tr><th>Formel</th><td class="mono">${esc(JSON.stringify(tx.formula))}</td></tr>
      </table>
    `;
  }

  function renderBottomTable(snap) {
    const entMap = Object.fromEntries((snap.entities || []).map((e) => [e.id, e.name]));
    if (activeTableTab === "states") {
      const rows = (snap.states || []).map((s) => `
        <tr>
          <td class="mono">${esc(s.id)}</td>
          <td>${esc(entMap[s.entity_id] || s.entity_id)}</td>
          <td>${esc(s.label)}<br/><span class="mono" style="color:#64748b;">${esc(s.property_name)}</span></td>
          <td>${esc(s.modality)}</td>
          <td><strong>${esc(s.source)}</strong><br/><span style="color:#64748b;">${esc(s.measurement_source || "—")}</span></td>
          <td>${esc(describeStateValue(s))}</td>
        </tr>
      `).join("");
      return `<table class="kv-table"><thead><tr><th>ID</th><th>Entität</th><th>Label / Property</th><th>Modalität</th><th>Quelle / Sensor</th><th>Wert / Ziel</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    if (activeTableTab === "templates") {
      const rows = (snap.transformations || []).map((t) => {
        const fac = t.formula && t.formula.factor ? `${fmtNum(t.formula.factor.mean)} ± ${fmtNum(t.formula.factor.sigma)} (${t.formula.factor.source})` : "—";
        return `
          <tr>
            <td class="mono">${esc(t.id)}</td>
            <td><strong>${esc(t.name)}</strong></td>
            <td>${esc(t.modality)} / ${esc(t.driver)}</td>
            <td>${fmtDuration(t.duration_seconds, t.duration_sigma_seconds)} (<strong>${esc(t.duration_source)}</strong>)</td>
            <td class="mono">${esc(fac)}</td>
          </tr>
        `;
      }).join("");
      return `<table class="kv-table"><thead><tr><th>ID</th><th>Name</th><th>Modalität / Driver</th><th>Dauer / Quelle</th><th>Faktor-Kalibrierung</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    if (activeTableTab === "entities") {
      const rows = (snap.entities || []).map((e) => `
        <tr>
          <td class="mono">${esc(e.id)}</td>
          <td><strong>${esc(e.name)}</strong></td>
          <td>${esc(e.kind)} / ${esc(e.category)}</td>
          <td>${esc(e.source)}</td>
          <td class="mono">${esc(JSON.stringify(e.meta))}</td>
        </tr>
      `).join("");
      return `<table class="kv-table"><thead><tr><th>ID</th><th>Name</th><th>Typ / Kategorie</th><th>Herkunft</th><th>Meta</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    const rows = (snap.edges || []).map((e) => `
      <tr>
        <td class="mono">${esc(e.id)}</td>
        <td class="mono">${esc(e.transformation_id)}</td>
        <td class="mono">${esc(e.state_id)}</td>
        <td>${esc(e.direction)} · <strong>${esc(e.role)}</strong></td>
        <td>${esc(e.logic_group)}:${esc(e.branch_key)} (${e.is_selected ? "aktiv" : "inaktiv"} · ${esc(e.outcome)}${e.resource_released ? " · frei" : ""})</td>
      </tr>
    `).join("");
    return `<table class="kv-table"><thead><tr><th>ID</th><th>Transformation</th><th>Zustand</th><th>Richtung / Rolle</th><th>Verzweigung / Status</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function render() {
    const scen = getScenario();
    const snap = getSnap();
    if (!selectedItem && (snap.goal_evaluations || []).length > 0) {
      selectedItem = { kind: "state", id: snap.goal_evaluations[0].goal_id };
    }

    const root = document.getElementById("app");
    const scenarioPills = PAYLOAD.scenario_order.map((sid) => {
      const s = PAYLOAD.scenarios[sid];
      return `<button class="pill ${sid === activeScenarioId ? "active" : ""}" data-scenario="${esc(sid)}">${esc(s.title)}</button>`;
    }).join("");

    const phaseBtns = scen.phases.map((p, idx) =>
      `<button class="phase-btn ${idx === activePhaseIdx ? "active" : ""}" data-phase="${idx}">${idx + 1}. ${esc(p.title)}</button>`
    ).join("");

    const fb = snap.last_feedback;
    const fbHtml = fb ? `
      <div class="feedback-banner">
        <strong>Letzte Engine-Rückmeldung:</strong> ${esc(fb.summary)}
        ${fb.triggered_template_ids.length ? ` · <span class="badge tpl">Trigger: ${esc(fb.triggered_template_ids.join(", "))}</span>` : ""}
        ${fb.calibrated_template_ids.length ? ` · <span class="badge on_track">Kalibriert: ${esc(fb.calibrated_template_ids.join(", "))}</span>` : ""}
      </div>
    ` : "";

    const childCounts = getChildCountByParent(snap);
    const compositeParents = (snap.transformations || []).filter(
      (t) => t.modality === "planned" && childCounts[t.id] > 0
    );
    const zoomHtml = compositeParents.length > 0 ? `
      <div class="zoom-bar">
        <span style="font-size:0.78rem;color:#475569;font-weight:600;">Verschachtelte Schritte (Zoom):</span>
        ${compositeParents.map((p) => {
          const isOpen = expandedParentIds.has(p.id);
          const count = childCounts[p.id];
          return `<button class="zoom-btn ${isOpen ? "expanded" : ""}" data-toggle-parent="${esc(p.id)}">${isOpen ? "▼" : "▶"} ${esc(p.name)} (${isOpen ? "einklappen" : "+" + count + " Unterschritte"})</button>`;
        }).join("")}
      </div>
    ` : "";

    root.innerHTML = `
      <div class="shell">
        <div class="header">
          <div>
            <h1>${esc(PAYLOAD.title)}</h1>
            <p class="header-sub">
              Begriffe: <strong>WasWoWann</strong> (Gesamtsystem) · <strong>Zukunftsgraph</strong> (Zukunftssicht) · <strong>MetaCore</strong> (SQLite/Python-Engine) — Live-Snapshot ohne statische Demo-Werte.
            </p>
          </div>
          <div class="pill-row">${scenarioPills}</div>
        </div>

        <div class="phase-bar">
          <div>
            <div style="font-weight:700;font-size:0.95rem;">${esc(scen.title)} — ${esc(snap.title)}</div>
            <div style="color:#475569;font-size:0.84rem;">${esc(snap.subtitle)}</div>
          </div>
          <div class="phase-steps">
            ${phaseBtns}
            <div class="phase-nav">
              <button class="nav-btn" id="prev-phase" ${activePhaseIdx === 0 ? "disabled" : ""}>← Zurück</button>
              <button class="nav-btn" id="next-phase" ${activePhaseIdx >= scen.phases.length - 1 ? "disabled" : ""}>Weiter →</button>
            </div>
          </div>
        </div>

        ${fbHtml}

        <div class="main-grid">
          <div>
            <div class="card">
              <h2>
                <span>Zukunftsgraph (v2: Linienstil = Modalität, Farbe = Status)</span>
                <span class="mono" style="font-weight:500;color:#475569;">${snap.states.length} Zustände · ${snap.transformations.length} Transformationen · ${snap.edges.length} Kanten</span>
              </h2>
              ${zoomHtml}
              <div class="svg-wrap">${renderGraphSvg(snap)}</div>
              <div class="legend">
                <span class="legend-item"><span class="swatch" style="border:2px solid #2563eb;background:#eff6ff;"></span>observed / happened (durchgezogen)</span>
                <span class="legend-item"><span class="swatch" style="border:2px dashed #059669;background:#ecfdf5;"></span>predicted / planned (gestrichelt)</span>
                <span class="legend-item"><span class="swatch" style="border:2px dotted #7c3aed;background:#f5f3ff;"></span>template / triggers</span>
                <span class="legend-item"><span class="swatch" style="border:2px solid #059669;background:#ecfdf5;"></span>Stufe 2 on_track</span>
                <span class="legend-item"><span class="swatch" style="border:2px solid #d97706;background:#fffbeb;"></span>Stufe 3 / Konflikt at_risk</span>
                <span class="legend-item"><span class="swatch" style="border:2px solid #dc2626;background:#fef2f2;"></span>Stufe 4 broken</span>
                <span class="legend-item"><span class="swatch" style="border:2px dashed #64748b;background:#f1f5f9;"></span>cancelled / not_chosen</span>
              </div>
            </div>

            <div class="card">
              <h2><span>SQLite-Tabellen & Kalibrierung</span></h2>
              <div class="tab-row">
                <button class="tab-btn ${activeTableTab === "states" ? "active" : ""}" data-tab="states">Zustände (${snap.states.length})</button>
                <button class="tab-btn ${activeTableTab === "templates" ? "active" : ""}" data-tab="templates">Transformationen & Templates (${snap.transformations.length})</button>
                <button class="tab-btn ${activeTableTab === "entities" ? "active" : ""}" data-tab="entities">Entitäten (${snap.entities.length})</button>
                <button class="tab-btn ${activeTableTab === "edges" ? "active" : ""}" data-tab="edges">Kanten (${snap.edges.length})</button>
              </div>
              <div style="overflow-x:auto;">${renderBottomTable(snap)}</div>
            </div>
          </div>

          <div>
            <div class="card">
              <h2><span>Ziele & 4-Stufen-Eskalation</span></h2>
              ${renderGoals(snap)}
            </div>
            <div class="card">
              <h2><span>Ressourcen-Konflikte, AI-Priors & Geo-Fusion</span></h2>
              ${renderConflictsAndPriors(snap)}
            </div>
            <div class="card">
              <h2><span>Ausgewähltes Element</span></h2>
              ${renderSelectionDetails(snap)}
            </div>
          </div>
        </div>
      </div>
    `;

    root.querySelectorAll("[data-scenario]").forEach((btn) => {
      btn.addEventListener("click", () => {
        activeScenarioId = btn.getAttribute("data-scenario");
        activePhaseIdx = 0;
        selectedItem = null;
        expandedParentIds.clear();
        render();
      });
    });

    root.querySelectorAll("[data-toggle-parent]").forEach((btn) => {
      btn.addEventListener("click", (evt) => {
        evt.stopPropagation();
        const pid = btn.getAttribute("data-toggle-parent");
        if (expandedParentIds.has(pid)) {
          expandedParentIds.delete(pid);
        } else {
          expandedParentIds.add(pid);
        }
        render();
      });
    });

    root.querySelectorAll("[data-phase]").forEach((btn) => {
      btn.addEventListener("click", () => {
        activePhaseIdx = Number(btn.getAttribute("data-phase"));
        selectedItem = null;
        render();
      });
    });

    root.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        activeTableTab = btn.getAttribute("data-tab");
        render();
      });
    });

    root.querySelectorAll(".node-g").forEach((g) => {
      g.addEventListener("click", () => {
        selectedItem = {
          kind: g.getAttribute("data-kind"),
          id: g.getAttribute("data-id"),
        };
        render();
      });
    });

    const prev = document.getElementById("prev-phase");
    if (prev) prev.addEventListener("click", () => { if (activePhaseIdx > 0) { activePhaseIdx -= 1; selectedItem = null; render(); } });
    const next = document.getElementById("next-phase");
    if (next) next.addEventListener("click", () => { if (activePhaseIdx < scen.phases.length - 1) { activePhaseIdx += 1; selectedItem = null; render(); } });
  }

  render();
})();
"""


def _serialize_payload_for_js(payload: dict[str, Any]) -> str:
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    meta_only = {k: v for k, v in payload.items() if k != "scenarios"}
    lines = ["{"]
    for key, val in meta_only.items():
        dumped_val = json.dumps(val, ensure_ascii=False).replace("</", "<\\/")
        lines.append(f"      {json.dumps(key)}: {dumped_val},")
    lines.append('      "scenarios": {')
    scenario_keys = list(scenarios.keys())
    for idx, skey in enumerate(scenario_keys):
        comma = "," if idx < len(scenario_keys) - 1 else ""
        dumped_scen = json.dumps(scenarios[skey], ensure_ascii=False).replace("</", "<\\/")
        lines.append(f"        {json.dumps(skey)}: {dumped_scen}{comma}")
    lines.append("      }")
    lines.append("    }")
    return "\n".join(lines)


def render_html(payload: dict[str, Any]) -> str:
    """Render a standalone interactive Zukunftsgraph HTML page from an engine payload."""
    safe_json = _serialize_payload_for_js(payload)
    page_title = escape(str(payload.get("title", "WasWoWann · Zukunftsgraph")))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"  <title>{page_title}</title>\n"
        f"  <style>{_HTML_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '  <div id="app"></div>\n'
        "  <script>\n"
        f"    const PAYLOAD = {safe_json};\n"
        f"{_HTML_SCRIPT}\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )
