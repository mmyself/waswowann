"""Deterministic astronomical, calendar, and relative-interval clocks + periodicity detection."""

from __future__ import annotations

import calendar
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from metacore.math_fusion import GaussianValue, normal_cdf, probability_in_bounds
from metacore.models import StrictModel

ClockId = Literal[
    "day_local",
    "solar_day",
    "week_local",
    "month_calendar",
    "lunar_synodic",
    "tidal_semidiurnal",
    "year_solar",
    "relative_interval",
]
PeriodStatus = Literal["detected", "weak", "none", "insufficient_data"]

DAY_SECONDS = 86_400.0
WEEK_SECONDS = 7.0 * DAY_SECONDS
MONTH_AVG_SECONDS = 30.436875 * DAY_SECONDS
LUNAR_SYNODIC_SECONDS = 29.530588853 * DAY_SECONDS
LUNAR_NEW_MOON_EPOCH_MS = 947_182_440_000  # 2000-01-06T18:14:00Z (J2000 New Moon)
TIDAL_M2_SECONDS = 12.4206012 * 3600.0  # Principal lunar semidiurnal constituent M2 (~12h25m)
TIDAL_HIGH_TIDE_EPOCH_MS = 947_182_440_000
YEAR_SOLAR_SECONDS = 365.2425 * DAY_SECONDS

CLOCK_PERIOD_SECONDS: dict[ClockId, float] = {
    "day_local": DAY_SECONDS,
    "solar_day": DAY_SECONDS,
    "week_local": WEEK_SECONDS,
    "month_calendar": MONTH_AVG_SECONDS,
    "lunar_synodic": LUNAR_SYNODIC_SECONDS,
    "tidal_semidiurnal": TIDAL_M2_SECONDS,
    "year_solar": YEAR_SOLAR_SECONDS,
    "relative_interval": 0.0,
}

GLOBAL_CLOCKS: tuple[ClockId, ...] = (
    "day_local",
    "solar_day",
    "week_local",
    "month_calendar",
    "lunar_synodic",
    "tidal_semidiurnal",
    "year_solar",
)

CLOCK_LABELS: dict[ClockId, str] = {
    "day_local": "Tageszeit (24 h)",
    "solar_day": "Sonnenstand / Tageslicht",
    "week_local": "Wochenperiode (7 d)",
    "month_calendar": "Kalendermonat",
    "lunar_synodic": "Mondzyklus (29,53 d)",
    "tidal_semidiurnal": "Gezeiten M2 (12,42 h)",
    "year_solar": "Sonnenjahr (365,24 d)",
    "relative_interval": "Eigen-Intervall (Δt)",
}

WEEKDAY_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTHS_DE = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")

WEEKDAY_ALIASES: dict[str, int] = {
    "0": 0,
    "mo": 0,
    "mon": 0,
    "montag": 0,
    "monday": 0,
    "1": 1,
    "di": 1,
    "die": 1,
    "tue": 1,
    "dienstag": 1,
    "tuesday": 1,
    "2": 2,
    "mi": 2,
    "mit": 2,
    "wed": 2,
    "mittwoch": 2,
    "wednesday": 2,
    "3": 3,
    "do": 3,
    "don": 3,
    "thu": 3,
    "donnerstag": 3,
    "thursday": 3,
    "4": 4,
    "fr": 4,
    "fre": 4,
    "fri": 4,
    "freitag": 4,
    "friday": 4,
    "5": 5,
    "sa": 5,
    "sam": 5,
    "sat": 5,
    "samstag": 5,
    "saturday": 5,
    "6": 6,
    "so": 6,
    "son": 6,
    "sun": 6,
    "sonntag": 6,
    "sunday": 6,
}


class TimeSample(StrictModel):
    """Timestamp observation with optional uncertainty and coordinates."""

    time_ms: int
    sigma_seconds: float | None = Field(default=None, ge=0.0)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)


class ClockPhaseSnapshot(StrictModel):
    """Projection of a single timestamp onto one periodic clock."""

    clock_id: ClockId
    clock_name: str
    phase: float = Field(ge=0.0, lt=1.0)
    cycle_index: int
    period_seconds: float
    label: str
    detail: str = ""
    solar_elevation_deg: float | None = None
    is_daylight: bool | None = None
    lunar_illumination: float | None = None
    is_spring_tide: bool | None = None


class PeriodCandidate(StrictModel):
    """Evaluation of one clock or endogenous cadence across a timestamp series."""

    clock_id: ClockId
    clock_name: str
    status: PeriodStatus
    period_seconds: float
    sample_count: int
    covered_cycles: int
    concentration: float = Field(ge=0.0, le=1.0)
    p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    mean_phase: float | None = Field(default=None, ge=0.0, lt=1.0)
    sigma_phase: float | None = Field(default=None, ge=0.0)
    mean_interval_seconds: float | None = None
    sigma_interval_seconds: float | None = None
    mean_label: str = ""
    next_window_start_ms: int | None = None
    next_window_center_ms: int | None = None
    next_window_end_ms: int | None = None
    explanation: str = ""


class PeriodicityReport(StrictModel):
    """Complete multi-clock periodicity and live-phase report for a worldline or series."""

    entity_id: str | None = None
    label: str = ""
    now_ms: int
    sample_count: int
    distinct_event_count: int
    current_clocks: list[ClockPhaseSnapshot] = Field(default_factory=list)
    candidates: list[PeriodCandidate] = Field(default_factory=list)
    best_clock_id: ClockId | None = None
    summary: str = ""


def _resolve_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _solar_position(time_ms: int, lat: float, lon: float) -> tuple[float, float]:
    """Return (solar_phase in [0,1), elevation_deg) using NOAA low-precision equations."""
    utc_dt = datetime.fromtimestamp(time_ms / 1000.0, tz=UTC)
    doy = utc_dt.timetuple().tm_yday
    hour_frac = utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
    gamma = math.tau / 365.0 * (doy - 1 + (hour_frac - 12.0) / 24.0)

    eqtime_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    decl_rad = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.001480 * math.sin(3.0 * gamma)
    )

    utc_minutes = utc_dt.hour * 60.0 + utc_dt.minute + utc_dt.second / 60.0
    true_solar_min = (utc_minutes + eqtime_min + 4.0 * lon) % 1440.0
    hour_angle_rad = math.radians(true_solar_min / 4.0 - 180.0)
    lat_rad = math.radians(lat)
    cos_zenith = max(
        -1.0,
        min(
            1.0,
            math.sin(lat_rad) * math.sin(decl_rad)
            + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_angle_rad),
        ),
    )
    elev_deg = 90.0 - math.degrees(math.acos(cos_zenith))
    return (true_solar_min / 1440.0) % 1.0, elev_deg


def format_phase_label(clock_id: ClockId, phase: float, *, extra: str = "") -> str:
    """Format a normalized phase [0, 1) cleanly for German UI/debug output."""
    p = phase % 1.0
    if clock_id == "day_local":
        total_min = round(p * 1440.0) % 1440
        h, m = divmod(total_min, 60)
        return f"{h:02d}:{m:02d}"
    if clock_id == "solar_day":
        total_min = round(p * 1440.0) % 1440
        h, m = divmod(total_min, 60)
        return extra or f"Sonnenzeit {h:02d}:{m:02d}"
    if clock_id == "week_local":
        total_min = round(p * 7.0 * 1440.0) % (7 * 1440)
        wd, rem_min = divmod(total_min, 1440)
        h, m = divmod(rem_min, 60)
        return f"{WEEKDAY_DE[wd]} {h:02d}:{m:02d}"
    if clock_id == "month_calendar":
        day = min(31, max(1, int(p * 30.436875) + 1))
        return f"{day}. im Monat"
    if clock_id == "lunar_synodic":
        illum = round(50.0 * (1.0 - math.cos(math.tau * p)))
        if p < 0.06 or p >= 0.94:
            name = "Neumond"
        elif p < 0.44:
            name = "Zunehmend"
        elif p < 0.56:
            name = "Vollmond"
        else:
            name = "Abnehmend"
        return f"{name} ({illum} %)"
    if clock_id == "tidal_semidiurnal":
        hours = p * (TIDAL_M2_SECONDS / 3600.0)
        if p < 0.12 or p >= 0.88:
            state = "Hochwasser"
        elif p < 0.38:
            state = "Ebbe (ablaufend)"
        elif p < 0.62:
            state = "Niedrigwasser"
        else:
            state = "Flut (auflaufend)"
        return f"{state} (+{hours:.1f} h)"
    if clock_id == "year_solar":
        ordinal = min(365, max(0, int(p * 366.0 + 1e-9)))
        dt = datetime.fromordinal(datetime(2000, 1, 1).toordinal() + ordinal)
        return f"{dt.day}. {MONTHS_DE[dt.month - 1]}"
    return f"Phase {p:.2f}"


def project_timestamp(
    time_ms: int,
    clock_id: ClockId,
    *,
    tz_name: str = "Europe/Berlin",
    lat: float = 53.55,
    lon: float = 9.99,
    phase_offset_hours: float = 0.0,
) -> ClockPhaseSnapshot:
    """Project a single UTC timestamp onto a periodic clock."""
    if clock_id == "relative_interval":
        raise ValueError("relative_interval erfordert mindestens zwei Zeitpunkte auf einer Weltlinie")

    tz = _resolve_tz(tz_name)
    dt = datetime.fromtimestamp(time_ms / 1000.0, tz=tz)
    sec_of_day = dt.hour * 3600.0 + dt.minute * 60.0 + dt.second + (dt.microsecond / 1_000_000.0)

    if clock_id == "day_local":
        phase = (sec_of_day / DAY_SECONDS) % 1.0
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=dt.date().toordinal(),
            period_seconds=DAY_SECONDS,
            label=format_phase_label(clock_id, phase),
            detail=f"{dt.day}. {MONTHS_DE[dt.month - 1]}",
        )

    if clock_id == "solar_day":
        phase, elev = _solar_position(time_ms, lat, lon)
        is_daylight = elev > -0.833
        if is_daylight:
            tag = f"Tageslicht ({elev:+.0f}°)"
        elif elev >= -6.0:
            tag = f"Dämmerung ({elev:+.0f}°)"
        else:
            tag = f"Nacht ({elev:+.0f}°)"
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=dt.date().toordinal(),
            period_seconds=DAY_SECONDS,
            label=tag,
            detail=f"Sonnenhöhe {elev:.1f}°",
            solar_elevation_deg=round(elev, 2),
            is_daylight=is_daylight,
        )

    if clock_id == "week_local":
        phase = ((dt.weekday() * DAY_SECONDS + sec_of_day) / WEEK_SECONDS) % 1.0
        cycle_idx = (dt.date().toordinal() - 1) // 7
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=cycle_idx,
            period_seconds=WEEK_SECONDS,
            label=format_phase_label(clock_id, phase),
            detail="Wochenende" if dt.weekday() >= 5 else "Werktag",
        )

    if clock_id == "month_calendar":
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        phase = ((dt.day - 1 + sec_of_day / DAY_SECONDS) / days_in_month) % 1.0
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=dt.year * 12 + dt.month,
            period_seconds=days_in_month * DAY_SECONDS,
            label=f"{dt.day}. {MONTHS_DE[dt.month - 1]}",
            detail=f"Tag {dt.day}/{days_in_month}",
        )

    if clock_id == "lunar_synodic":
        raw_cycles = (time_ms - LUNAR_NEW_MOON_EPOCH_MS) / (LUNAR_SYNODIC_SECONDS * 1000.0)
        cycle_idx = math.floor(raw_cycles)
        phase = (raw_cycles - cycle_idx) % 1.0
        illum = round(0.5 * (1.0 - math.cos(math.tau * phase)), 3)
        dist_syzygy = min(phase, 1.0 - phase, abs(phase - 0.5))
        spring = dist_syzygy <= 0.08
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=cycle_idx,
            period_seconds=LUNAR_SYNODIC_SECONDS,
            label=format_phase_label(clock_id, phase),
            detail="Springtide-Bereich" if spring else "Mittlere / Nipptide",
            lunar_illumination=illum,
            is_spring_tide=spring,
        )

    if clock_id == "tidal_semidiurnal":
        elapsed_s = (time_ms - TIDAL_HIGH_TIDE_EPOCH_MS) / 1000.0 - phase_offset_hours * 3600.0
        raw_cycles = elapsed_s / TIDAL_M2_SECONDS
        cycle_idx = math.floor(raw_cycles)
        phase = (raw_cycles - cycle_idx) % 1.0
        return ClockPhaseSnapshot(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            phase=phase,
            cycle_index=cycle_idx,
            period_seconds=TIDAL_M2_SECONDS,
            label=format_phase_label(clock_id, phase),
            detail="M2-Tide (12,42 h)",
        )

    # year_solar: leap-year-aligned phase [0, 1)
    yday_2000 = datetime(2000, dt.month, dt.day).timetuple().tm_yday
    phase = ((yday_2000 - 1 + sec_of_day / DAY_SECONDS) / 366.0) % 1.0
    return ClockPhaseSnapshot(
        clock_id="year_solar",
        clock_name=CLOCK_LABELS["year_solar"],
        phase=phase,
        cycle_index=dt.year,
        period_seconds=YEAR_SOLAR_SECONDS,
        label=f"{dt.day}. {MONTHS_DE[dt.month - 1]}",
        detail=f"{dt.year}",
    )


def get_current_clock_phases(
    now_ms: int,
    *,
    tz_name: str = "Europe/Berlin",
    lat: float = 53.55,
    lon: float = 9.99,
    tidal_offset_hours: float = 0.0,
) -> list[ClockPhaseSnapshot]:
    """Return projections onto all 7 global periodic clocks at `now_ms`."""
    return [
        project_timestamp(
            now_ms,
            cid,
            tz_name=tz_name,
            lat=lat,
            lon=lon,
            phase_offset_hours=tidal_offset_hours if cid == "tidal_semidiurnal" else 0.0,
        )
        for cid in GLOBAL_CLOCKS
    ]


def _arc_probability(
    mean_phase: float, sigma_phase: float, start_phase: float, end_phase: float
) -> float:
    """Compute probability that a circular Gaussian N_wrap(mean_phase, sigma_phase^2) lies inside [start, end]."""
    a = start_phase % 1.0
    b = end_phase % 1.0
    if abs(end_phase - start_phase) >= 1.0 - 1e-9:
        return 1.0
    width = (b - a) % 1.0
    if width <= 1e-9:
        return 0.0
    center = (a + 0.5 * width) % 1.0
    delta = ((mean_phase - center + 0.5) % 1.0) - 0.5
    half_w = 0.5 * width
    if sigma_phase <= 1e-9:
        return 1.0 if abs(delta) <= half_w + 1e-9 else 0.0

    # Sum over neighboring periodic images on R for exact wrapped normal interval mass
    total = 0.0
    for shift in (-1.0, 0.0, 1.0):
        hi = normal_cdf(shift + half_w, delta, sigma_phase)
        lo = normal_cdf(shift - half_w, delta, sigma_phase)
        total += max(0.0, hi - lo)
    return max(0.0, min(1.0, total))


def _merge_day_arcs(day_indices: Sequence[int]) -> list[tuple[float, float]]:
    unique = sorted({d % 7 for d in day_indices})
    if not unique:
        return []
    if len(unique) == 7:
        return [(0.0, 1.0)]
    arcs: list[tuple[int, int]] = []
    for d in unique:
        if arcs and arcs[-1][1] == d - 1:
            arcs[-1] = (arcs[-1][0], d)
        else:
            arcs.append((d, d))
    # Wrap Sunday..Monday seamlessly if both present
    if len(arcs) > 1 and arcs[0][0] == 0 and arcs[-1][1] == 6:
        start_d = arcs[-1][0]
        end_d = 7 + arcs[0][1]
        arcs = [(start_d, end_d), *arcs[1:-1]]
    return [(s / 7.0, (e + 1) / 7.0) for s, e in arcs]


def evaluate_phase_condition(
    cond: dict[str, Any],
    *,
    time_ms: int,
    sigma_seconds: float | None = None,
    tz_name: str = "Europe/Berlin",
    lat: float | None = None,
    lon: float | None = None,
) -> float:
    """Evaluate a declarative `period_phase` trigger/guard condition against a timestamp."""
    clock_id: ClockId = str(cond.get("clock", "year_solar"))  # type: ignore[assignment]
    if clock_id == "relative_interval":
        return 1.0

    use_lat = float(lat) if lat is not None else float(cond.get("lat", 53.55))
    use_lon = float(lon) if lon is not None else float(cond.get("lon", 9.99))
    tz = str(cond.get("timezone", tz_name))
    offset_h = float(cond.get("phase_offset_hours", 0.0))

    snap = project_timestamp(
        time_ms,
        clock_id,
        tz_name=tz,
        lat=use_lat,
        lon=use_lon,
        phase_offset_hours=offset_h,
    )
    sigma_phase = (
        min(0.45, float(sigma_seconds) / snap.period_seconds)
        if sigma_seconds and sigma_seconds > 0 and snap.period_seconds > 0
        else 0.0
    )

    if clock_id == "solar_day" and (
        "require_daylight" in cond or "min_elevation_deg" in cond or "max_elevation_deg" in cond
    ):
        elev = snap.solar_elevation_deg if snap.solar_elevation_deg is not None else 0.0
        min_el = cond.get("min_elevation_deg")
        max_el = cond.get("max_elevation_deg")
        if cond.get("require_daylight") is True and min_el is None:
            min_el = -0.833
        # Approximate elevation rate ~ 15 deg/hour -> ~1/4 deg per minute
        sigma_deg = min(20.0, (float(sigma_seconds) / 3600.0) * 15.0) if sigma_seconds and sigma_seconds > 0 else 0.0
        return probability_in_bounds(
            GaussianValue(elev, sigma_deg),
            target_min=float(min_el) if min_el is not None else None,
            target_max=float(max_el) if max_el is not None else None,
        )

    if clock_id == "week_local" and isinstance(cond.get("days"), list):
        day_indices: list[int] = []
        for raw in cond["days"]:
            key = str(raw).strip().lower()
            if key in WEEKDAY_ALIASES:
                day_indices.append(WEEKDAY_ALIASES[key])
        if not day_indices:
            return 0.0
        arcs = _merge_day_arcs(day_indices)
        return max(
            0.0,
            min(1.0, sum(_arc_probability(snap.phase, sigma_phase, s, e) for s, e in arcs)),
        )

    # Interval bounds expressed in clock-native units or normalized phase [0, 1)
    if "hours_min" in cond or "hours_max" in cond:
        period_hours = snap.period_seconds / 3600.0
        h_min = float(cond.get("hours_min", 0.0)) % period_hours
        h_max = float(cond.get("hours_max", period_hours))
        if abs(h_max - period_hours) < 1e-6 and float(cond.get("hours_min", 0.0)) == 0.0:
            return 1.0
        return _arc_probability(
            snap.phase,
            sigma_phase,
            h_min / period_hours,
            (h_max % period_hours) / period_hours if h_max != h_min else (h_min / period_hours + 1e-6),
        )

    if clock_id == "year_solar" and ("day_of_year_min" in cond or "day_of_year_max" in cond):
        d_min = max(1.0, min(366.0, float(cond.get("day_of_year_min", 1.0))))
        d_max = max(1.0, min(366.0, float(cond.get("day_of_year_max", 366.0))))
        return _arc_probability(snap.phase, sigma_phase, (d_min - 1.0) / 366.0, d_max / 366.0)

    if clock_id == "month_calendar" and ("day_of_month_min" in cond or "day_of_month_max" in cond):
        d_min = max(1.0, min(31.0, float(cond.get("day_of_month_min", 1.0))))
        d_max = max(1.0, min(31.0, float(cond.get("day_of_month_max", 31.0))))
        denom = snap.period_seconds / DAY_SECONDS
        return _arc_probability(snap.phase, sigma_phase, (d_min - 1.0) / denom, min(denom, d_max) / denom)

    if "phase_min" in cond or "phase_max" in cond:
        p_min = float(cond.get("phase_min", 0.0))
        p_max = float(cond.get("phase_max", 1.0))
        return _arc_probability(snap.phase, sigma_phase, p_min, p_max)

    return 1.0


def _rayleigh_p_value(n: int, r: float) -> float:
    """Finite-sample Rayleigh test p-value for uniformity on a circle (Zar / Mardia)."""
    if n < 2:
        return 1.0
    big_r = n * r
    exponent = math.sqrt(max(0.0, 1.0 + 4.0 * n + 4.0 * (n * n - big_r * big_r))) - (1.0 + 2.0 * n)
    return max(0.0, min(1.0, math.exp(exponent)))


def _deduplicate_bursts(samples: Sequence[TimeSample], *, min_gap_ms: int = 600_000) -> list[TimeSample]:
    """Collapse bursts of photos taken within `min_gap_ms` of each other into single event timestamps."""
    ordered = sorted(samples, key=lambda s: s.time_ms)
    if not ordered:
        return []
    distinct: list[TimeSample] = [ordered[0]]
    for sample in ordered[1:]:
        if sample.time_ms - distinct[-1].time_ms >= min_gap_ms:
            distinct.append(sample)
    return distinct


def _evaluate_global_clock_series(
    clock_id: ClockId,
    events: Sequence[TimeSample],
    *,
    tz_name: str,
    now_ms: int,
    tidal_offset_hours: float,
) -> PeriodCandidate:
    period_s = CLOCK_PERIOD_SECONDS[clock_id]
    projections = [
        project_timestamp(
            ev.time_ms,
            clock_id,
            tz_name=tz_name,
            lat=ev.lat if ev.lat is not None else 53.55,
            lon=ev.lon if ev.lon is not None else 9.99,
            phase_offset_hours=tidal_offset_hours if clock_id == "tidal_semidiurnal" else 0.0,
        )
        for ev in events
    ]
    n = len(projections)
    covered_cycles = len({p.cycle_index for p in projections})

    if n < 3 or covered_cycles < 2:
        return PeriodCandidate(
            clock_id=clock_id,
            clock_name=CLOCK_LABELS[clock_id],
            status="insufficient_data",
            period_seconds=period_s,
            sample_count=n,
            covered_cycles=covered_cycles,
            concentration=0.0,
            explanation=f"Erst {n} Ereignisse in {covered_cycles} Zyklus/Zyklen (mind. 3 Ereignisse über ≥ 2 Zyklen nötig).",
        )

    angles = [math.tau * p.phase for p in projections]
    cos_m = math.fsum(math.cos(a) for a in angles) / n
    sin_m = math.fsum(math.sin(a) for a in angles) / n
    r = min(1.0, math.hypot(cos_m, sin_m))
    mean_phase = (math.atan2(sin_m, cos_m) / math.tau) % 1.0
    deltas = [((p.phase - mean_phase + 0.5) % 1.0) - 0.5 for p in projections]
    sigma_phase = max(0.005, math.sqrt(math.fsum(d * d for d in deltas) / n))
    p_val = _rayleigh_p_value(n, r)

    if p_val < 0.05 and r >= 0.70:
        status: PeriodStatus = "detected"
    elif p_val < 0.15 and r >= 0.50:
        status = "weak"
    else:
        status = "none"

    now_snap = project_timestamp(
        now_ms,
        clock_id,
        tz_name=tz_name,
        lat=events[-1].lat if events[-1].lat is not None else 53.55,
        lon=events[-1].lon if events[-1].lon is not None else 9.99,
        phase_offset_hours=tidal_offset_hours if clock_id == "tidal_semidiurnal" else 0.0,
    )
    delta_phase = (mean_phase - now_snap.phase) % 1.0
    next_center_ms = now_ms + round(delta_phase * period_s * 1000.0)
    half_win_ms = max(60_000, round(1.5 * sigma_phase * period_s * 1000.0))
    mean_label = format_phase_label(clock_id, mean_phase)

    if status == "detected":
        expl = f"Stabiles Muster um {mean_label} (R = {r:.0%}, p = {p_val:.3f}, {covered_cycles} Zyklen)."
    elif status == "weak":
        expl = f"Schwache Häufung um {mean_label} (R = {r:.0%}, p = {p_val:.3f})."
    else:
        expl = f"Gleichmäßig verteilt / kein Takt auf dieser Uhr (R = {r:.0%}, p = {p_val:.2f})."

    return PeriodCandidate(
        clock_id=clock_id,
        clock_name=CLOCK_LABELS[clock_id],
        status=status,
        period_seconds=period_s,
        sample_count=n,
        covered_cycles=covered_cycles,
        concentration=round(r, 4),
        p_value=round(p_val, 4),
        mean_phase=round(mean_phase, 4),
        sigma_phase=round(sigma_phase, 4),
        mean_label=mean_label,
        next_window_start_ms=next_center_ms - half_win_ms,
        next_window_center_ms=next_center_ms,
        next_window_end_ms=next_center_ms + half_win_ms,
        explanation=expl,
    )


def _evaluate_relative_interval(
    events: Sequence[TimeSample],
    *,
    now_ms: int,
) -> PeriodCandidate:
    if len(events) < 3:
        return PeriodCandidate(
            clock_id="relative_interval",
            clock_name=CLOCK_LABELS["relative_interval"],
            status="insufficient_data",
            period_seconds=0.0,
            sample_count=len(events),
            covered_cycles=max(0, len(events) - 1),
            concentration=0.0,
            explanation="Mindestens 3 zeitlich getrennte Ereignisse (2 Abstände) für Eigen-Intervall nötig.",
        )

    deltas_s = [(events[i].time_ms - events[i - 1].time_ms) / 1000.0 for i in range(1, len(events))]
    mean_s = math.fsum(deltas_s) / len(deltas_s)
    sigma_s = math.sqrt(math.fsum((d - mean_s) ** 2 for d in deltas_s) / len(deltas_s))
    cv = sigma_s / max(1e-6, mean_s)
    conc = max(0.0, min(1.0, 1.0 - cv))

    if cv <= 0.35:
        status: PeriodStatus = "detected"
    elif cv <= 0.60:
        status = "weak"
    else:
        status = "none"

    period_ms = max(60_000, round(mean_s * 1000.0))
    next_center_ms = events[-1].time_ms + period_ms
    while next_center_ms <= now_ms:
        next_center_ms += period_ms
    half_win_ms = max(60_000, round(1.5 * sigma_s * 1000.0))

    days_mean = mean_s / DAY_SECONDS
    days_sig = sigma_s / DAY_SECONDS
    lbl = f"alle {days_mean:.1f} ± {days_sig:.1f} d" if days_mean >= 1.0 else f"alle {mean_s / 3600.0:.1f} h"
    return PeriodCandidate(
        clock_id="relative_interval",
        clock_name=CLOCK_LABELS["relative_interval"],
        status=status,
        period_seconds=round(mean_s, 1),
        sample_count=len(events),
        covered_cycles=len(deltas_s),
        concentration=round(conc, 4),
        mean_interval_seconds=round(mean_s, 1),
        sigma_interval_seconds=round(sigma_s, 1),
        mean_label=lbl,
        next_window_start_ms=next_center_ms - half_win_ms,
        next_window_center_ms=next_center_ms,
        next_window_end_ms=next_center_ms + half_win_ms,
        explanation=f"Abstände zwischen Ereignissen: {lbl} (Stabilität {conc:.0%}).",
    )


def analyze_periodicity(
    samples: Sequence[TimeSample],
    *,
    tz_name: str = "Europe/Berlin",
    now_ms: int | None = None,
    tidal_offset_hours: float = 0.0,
    entity_id: str | None = None,
    label: str = "",
) -> PeriodicityReport:
    """Project a sequence of timestamps onto all clocks and identify significant cycles."""
    distinct = _deduplicate_bursts(samples)
    ref_now = now_ms if now_ms is not None else (distinct[-1].time_ms if distinct else 0)
    ref_lat = next((s.lat for s in reversed(distinct) if s.lat is not None), 53.55)
    ref_lon = next((s.lon for s in reversed(distinct) if s.lon is not None), 9.99)
    current = get_current_clock_phases(
        ref_now,
        tz_name=tz_name,
        lat=ref_lat,
        lon=ref_lon,
        tidal_offset_hours=tidal_offset_hours,
    )

    candidates: list[PeriodCandidate] = [
        _evaluate_global_clock_series(
            cid,
            distinct,
            tz_name=tz_name,
            now_ms=ref_now,
            tidal_offset_hours=tidal_offset_hours,
        )
        for cid in GLOBAL_CLOCKS
    ]
    candidates.append(_evaluate_relative_interval(distinct, now_ms=ref_now))

    priority_rank: dict[PeriodStatus, int] = {
        "detected": 0,
        "weak": 1,
        "none": 2,
        "insufficient_data": 3,
    }
    candidates.sort(key=lambda c: (priority_rank[c.status], -c.concentration, -(c.covered_cycles)))

    detected = [c for c in candidates if c.status == "detected"]
    best_id = detected[0].clock_id if detected else None
    if detected:
        top_names = ", ".join(f"{c.clock_name} (R={c.concentration:.0%})" for c in detected[:2])
        summary = f"Erkannte Taktung: {top_names}."
    elif len(distinct) < 3:
        summary = "Noch zu wenige zeitlich getrennte Beobachtungen für eine Takt-Erkennung."
    else:
        summary = "Kein stabiler Einzel-Takt erkannt (Abstände/Zyklen noch zu schwach oder gemischt)."

    return PeriodicityReport(
        entity_id=entity_id,
        label=label,
        now_ms=ref_now,
        sample_count=len(samples),
        distinct_event_count=len(distinct),
        current_clocks=current,
        candidates=candidates,
        best_clock_id=best_id,
        summary=summary,
    )
