"""Tests for multi-clock projections, periodicity detection, and phase-gated triggers/guards."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from metacore.clocks import (
    DAY_SECONDS,
    LUNAR_NEW_MOON_EPOCH_MS,
    LUNAR_SYNODIC_SECONDS,
    TIDAL_M2_SECONDS,
    TimeSample,
    analyze_periodicity,
    evaluate_phase_condition,
    get_current_clock_phases,
    project_timestamp,
)
from metacore.engine import MetaCoreEngine
from metacore.models import EdgeCreate, EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository


def _ms(year: int, month: int, day: int, hour: int = 12, minute: int = 0, tz: str = "Europe/Berlin") -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def test_astronomical_and_calendar_clocks() -> None:
    # 1. Exact New Moon & Full Moon on lunar_synodic
    new_moon = project_timestamp(LUNAR_NEW_MOON_EPOCH_MS, "lunar_synodic")
    assert new_moon.phase == pytest.approx(0.0, abs=1e-6)
    assert new_moon.is_spring_tide is True

    full_moon_ms = LUNAR_NEW_MOON_EPOCH_MS + int(0.5 * LUNAR_SYNODIC_SECONDS * 1000)
    full_moon = project_timestamp(full_moon_ms, "lunar_synodic")
    assert full_moon.phase == pytest.approx(0.5, abs=1e-4)
    assert "Vollmond" in full_moon.label

    # 2. Solar elevation in Hamburg: June midday daylight vs midnight darkness
    june_noon = _ms(2026, 6, 21, 13, 30)
    june_night = _ms(2026, 6, 21, 1, 0)
    noon_snap = project_timestamp(june_noon, "solar_day", lat=53.55, lon=9.99)
    night_snap = project_timestamp(june_night, "solar_day", lat=53.55, lon=9.99)
    assert noon_snap.is_daylight is True
    assert noon_snap.solar_elevation_deg is not None and noon_snap.solar_elevation_deg > 50.0
    assert night_snap.is_daylight is False

    # 3. Tidal M2 (~12.42h) shifts by ~50 min per 24h day + supports port offset
    t_base = _ms(2026, 11, 1, 6, 0)
    tide0 = project_timestamp(t_base, "tidal_semidiurnal")
    tide_24h = project_timestamp(t_base + int(DAY_SECONDS * 1000), "tidal_semidiurnal")
    delta_phase = (tide0.phase - tide_24h.phase) % 1.0
    # 24h is ~1.932 M2 cycles -> ~0.068 cycle (~50.5 min) earlier in the phase
    assert 0.05 < delta_phase < 0.09
    tide_offset = project_timestamp(t_base, "tidal_semidiurnal", phase_offset_hours=3.1)
    assert tide_offset.phase != pytest.approx(tide0.phase, abs=0.05)

    # 4. Week & year wrap-around evaluation
    sat_ms = _ms(2026, 11, 21, 10, 0)  # Saturday
    tue_ms = _ms(2026, 11, 24, 10, 0)  # Tuesday
    weekend_cond = {"kind": "period_phase", "clock": "week_local", "days": ["Sat", "Sun"]}
    assert evaluate_phase_condition(weekend_cond, time_ms=sat_ms) == pytest.approx(1.0)
    assert evaluate_phase_condition(weekend_cond, time_ms=tue_ms) == pytest.approx(0.0)

    winter_cond = {"kind": "period_phase", "clock": "year_solar", "day_of_year_min": 320, "day_of_year_max": 45}
    assert evaluate_phase_condition(winter_cond, time_ms=_ms(2026, 12, 20, 12)) == pytest.approx(1.0)
    assert evaluate_phase_condition(winter_cond, time_ms=_ms(2026, 1, 15, 12)) == pytest.approx(1.0)
    assert evaluate_phase_condition(winter_cond, time_ms=_ms(2026, 7, 15, 12)) == pytest.approx(0.0)

    # 5. All 7 live clocks returned
    clocks = get_current_clock_phases(t_base)
    assert len(clocks) == 7


def test_periodicity_detection_across_week_year_tidal_and_relative() -> None:
    # A. Weekly pattern (5 consecutive Saturdays at 09:00) -> week_local & day_local detected, year insufficient
    sat_start = _ms(2026, 10, 3, 9, 0)
    weekly_samples = [
        TimeSample(time_ms=sat_start + i * 7 * 86_400_000, sigma_seconds=300.0)
        for i in range(5)
    ]
    rep_week = analyze_periodicity(weekly_samples, now_ms=sat_start + 30 * 86_400_000)
    by_id = {c.clock_id: c for c in rep_week.candidates}
    assert by_id["week_local"].status == "detected"
    assert by_id["day_local"].status == "detected"
    assert by_id["year_solar"].status == "insufficient_data"
    assert by_id["week_local"].next_window_center_ms is not None
    assert by_id["week_local"].next_window_center_ms >= rep_week.now_ms

    # B. Multi-year autumn pattern on varying weekdays -> year_solar detected, week_local none
    autumn_samples = [
        TimeSample(time_ms=_ms(2023, 10, 14, 15, 0)),
        TimeSample(time_ms=_ms(2024, 10, 18, 11, 0)),
        TimeSample(time_ms=_ms(2024, 10, 25, 16, 0)),
        TimeSample(time_ms=_ms(2025, 10, 12, 9, 0)),
        TimeSample(time_ms=_ms(2026, 10, 20, 14, 0)),
        TimeSample(time_ms=_ms(2026, 10, 28, 17, 0)),
    ]
    rep_year = analyze_periodicity(autumn_samples)
    by_y = {c.clock_id: c for c in rep_year.candidates}
    assert by_y["year_solar"].status == "detected"
    assert by_y["week_local"].status == "none"

    # C. Tidal low-tide pattern (~12.42h apart across cycles) -> tidal_semidiurnal detected, day_local weaker/not locked
    tide_samples = [
        TimeSample(time_ms=sat_start + round((0.5 + i * 3.0) * TIDAL_M2_SECONDS * 1000))
        for i in range(5)
    ]
    rep_tide = analyze_periodicity(tide_samples)
    by_t = {c.clock_id: c for c in rep_tide.candidates}
    assert by_t["tidal_semidiurnal"].status == "detected"
    assert by_t["tidal_semidiurnal"].concentration > 0.99

    # D. Endogenous flush cadence (relative_interval ~14d)
    flush_days = [0, 14, 27, 42]
    flush_samples = [TimeSample(time_ms=sat_start + d * 86_400_000) for d in flush_days]
    rep_rel = analyze_periodicity(flush_samples, now_ms=sat_start + 45 * 86_400_000)
    by_r = {c.clock_id: c for c in rep_rel.candidates}
    assert by_r["relative_interval"].status == "detected"
    assert by_r["relative_interval"].mean_interval_seconds == pytest.approx(14.0 * 86_400.0, rel=0.05)

    # E. Photo burst on a single afternoon -> never hallucinates a multi-cycle period
    burst = [TimeSample(time_ms=sat_start + i * 60_000) for i in range(6)]
    rep_burst = analyze_periodicity(burst)
    assert rep_burst.distinct_event_count == 1
    assert rep_burst.best_clock_id is None


def test_engine_phase_triggers_and_guards(repo: MetaCoreRepository) -> None:
    engine = MetaCoreEngine(repo)
    weather = repo.create_entity(EntityCreate(name="Waldwetter", kind="worldline", category="umgebung"))

    # Trigger only fires if rain >= 30 mm AND timestamp is in autumn (Sep-Nov: days 244..334)
    tpl = repo.create_transformation(
        TransformationCreate(
            name="Herbst-Pilzschub",
            modality="template",
            driver="nature",
            duration_seconds=5 * 86_400.0,
            formula={
                "kind": "duration_step",
                "output_property": "flush",
                "output_value": 1.0,
                "triggers": {
                    "logic": "AND",
                    "category": "umgebung",
                    "conditions": [
                        {
                            "kind": "period_phase",
                            "clock": "year_solar",
                            "day_of_year_min": 244,
                            "day_of_year_max": 334,
                        },
                        {
                            "entity_id": weather.id,
                            "property_name": "rain_mm",
                            "aggregation": "sum",
                            "window_seconds": 7 * 86_400,
                            "min_value": 30.0,
                        },
                    ],
                },
            },
        )
    )

    # July rain does NOT trigger autumn template
    july_ms = _ms(2026, 7, 15, 14, 0)
    fb_july = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="rain_mm",
            label="Sommerregen 40 mm",
            value_num=40.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=july_ms,
        )
    )
    assert tpl.id not in fb_july.triggered_template_ids

    # October rain DOES trigger autumn template
    oct_ms = _ms(2026, 10, 15, 14, 0)
    fb_oct = engine.record_observation(
        StateCreate(
            entity_id=weather.id,
            modality="observed",
            source="measured",
            property_name="rain_mm",
            label="Herbstregen 38 mm",
            value_num=38.0,
            value_sigma=2.0,
            value_unit="mm",
            time_ms=oct_ms,
        )
    )
    assert tpl.id in fb_oct.triggered_template_ids

    # Phase guard in goal evaluation: Saturday daylight Wattwanderung passes vs Tuesday midnight fails
    trip = repo.create_entity(EntityCreate(name="Wattwanderung", kind="worldline", category="ereignis"))
    sat_noon = _ms(2026, 6, 20, 13, 0)
    tx = repo.create_transformation(
        TransformationCreate(
            name="Tour bei Tageslicht am Wochenende",
            modality="planned",
            driver="actor",
            duration_seconds=3600.0,
            formula={
                "guards": [
                    {"kind": "period_phase", "clock": "week_local", "days": ["Sat", "Sun"]},
                    {"kind": "period_phase", "clock": "solar_day", "require_daylight": True},
                ]
            },
        )
    )
    pred = repo.create_state(
        StateCreate(
            entity_id=trip.id,
            modality="predicted",
            source="derived",
            property_name="presence",
            label="Ankunft im Watt",
            value_num=1.0,
            value_sigma=0.0,
            lat=53.9,
            lon=8.7,
            sigma_meters=50.0,
            time_ms=sat_noon,
            sigma_seconds=300.0,
        )
    )
    goal = repo.create_state(
        StateCreate(
            entity_id=trip.id,
            modality="desired",
            source="user_goal",
            property_name="presence",
            label="Sichere Wochenend-Tagestour",
            target_min=1.0,
        )
    )
    repo.create_edge(EdgeCreate(transformation_id=tx.id, state_id=pred.id, direction="out", role="produces"))

    ev_ok = engine.evaluate_goal(goal.id)
    assert ev_ok.status == "on_track"
    assert ev_ok.dimension_probs["vorbedingung"] == pytest.approx(1.0)

    # Shift predicted state to Tuesday 01:00 AM -> violates both week_local & daylight guards -> broken!
    tue_night = _ms(2026, 6, 23, 1, 0)
    repo.update_state(pred.id, time_ms=tue_night)
    ev_fail = engine.evaluate_goal(goal.id)
    assert ev_fail.status == "broken"
    assert "vorbedingung" in ev_fail.broken_dimensions

    # Verify inspect_worldline periodicity output works end-to-end
    for idx in range(4):
        engine.record_observation(
            StateCreate(
                entity_id=trip.id,
                modality="observed",
                source="measured",
                property_name="presence",
                label=f"Tour #{idx}",
                value_num=1.0,
                time_ms=_ms(2026, 5, 2, 13, 0) + idx * 7 * 86_400_000,
            )
        )
    rep = engine.analyze_worldline_periods(entity_id=trip.id, property_name="presence")
    assert rep.best_clock_id in ("week_local", "day_local", "solar_day")
