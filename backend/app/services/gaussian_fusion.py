from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, settings

EARTH_RADIUS_M = 6_371_008.8
SECONDS_PER_DAY = 86_400.0
DAYS_PER_YEAR = 365.2425
MIN_TIMESTAMP_MS = -62_135_510_400_000
MAX_TIMESTAMP_MS = 253_402_214_399_999
INDOOR_GPS_SIGMA_FLOOR_M = 12.0
OUTLIER_Z_THRESHOLD = 2.5
MEASUREMENT_FIELDS = ('lat', 'lon', 'sigma_meters', 'location_source', 'observed_at_ms', 'sigma_seconds', 'time_source')
TIMELINE_ORDER = 'observed_at_ms IS NULL, observed_at_ms, created_at_ms, id'
MONTHS_DE = ('Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez')


def _xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    delta = lon - lon0
    if not -180 <= delta < 180:
        delta = (delta + 180) % 360 - 180
    return (
        math.radians(delta) * EARTH_RADIUS_M * math.cos(math.radians(lat0)),
        math.radians(lat - lat0) * EARTH_RADIUS_M,
    )


def _weights(sigmas: list[float]) -> tuple[list[float], float]:
    # Scaling by the best measurement avoids overflow with very small sigmas.
    scale = min(sigmas)
    weights = [(scale / sigma) ** 2 for sigma in sigmas]
    total = math.fsum(weights)
    return [w / total for w in weights], max(math.ulp(0.0), scale / math.sqrt(total))


def _circular_gaussian(values: list[float], sigmas: list[float], period: float) -> tuple[float, float, float]:
    weights, measurement_sigma = _weights(sigmas)
    angles = [math.tau * (v % period) / period for v in values]
    cos_sum = math.fsum(w * math.cos(a) for w, a in zip(weights, angles))
    sin_sum = math.fsum(w * math.sin(a) for w, a in zip(weights, angles))
    mean = (math.atan2(sin_sum, cos_sum) / math.tau * period) % period
    deltas = [(v - mean + period / 2) % period - period / 2 for v in values]
    shift = math.fsum(w * d for w, d in zip(weights, deltas))
    spread = math.sqrt(math.fsum(w * ((d - shift) ** 2) for w, d in zip(weights, deltas)))
    return (mean + shift) % period, math.hypot(measurement_sigma, spread), min(1.0, math.hypot(cos_sum, sin_sum))


def temporal_patterns(timestamps: list[tuple[int, float]], timezone_name: str) -> dict:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo('UTC')
    dates = [datetime.fromtimestamp(ts / 1000, tz=tz) for ts, _ in timestamps]
    seconds = [dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000 for dt in dates]
    # Use the same phase for each calendar date in leap and non-leap years.
    days = [(datetime(2000, dt.month, dt.day).timetuple().tm_yday - 1 + sec / SECONDS_PER_DAY)
            * DAYS_PER_YEAR / 366 for dt, sec in zip(dates, seconds)]
    tod, sigma_seconds, time_concentration = _circular_gaussian(seconds, [max(1.0, s) for _, s in timestamps], SECONDS_PER_DAY)
    season, sigma_days, season_concentration = _circular_gaussian(days, [max(0.5, s / SECONDS_PER_DAY) for _, s in timestamps], DAYS_PER_YEAR)
    hour, remainder = divmod(round(tod) % int(SECONDS_PER_DAY), 3600)
    day = datetime.fromordinal(datetime(2000, 1, 1).toordinal() + int(season * 366 / DAYS_PER_YEAR + 1e-10) % 366)
    return {
        'time_of_day': {'mean_seconds': round(tod, 1) % SECONDS_PER_DAY, 'mean_label': f'{hour:02d}:{remainder // 60:02d}',
                        'sigma_seconds': round(sigma_seconds, 1), 'concentration': round(time_concentration, 4)},
        'season': {'mean_day_of_year': round(season + 1, 2), 'mean_label': f'{day.day}. {MONTHS_DE[day.month - 1]}',
                   'sigma_days': round(sigma_days, 2), 'concentration': round(season_concentration, 4)},
    }


def _sigma(value: float | None, default: float) -> float:
    return value if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else default


def effective_location_sigma(
    raw_sigma: float | None,
    location_source: str | None,
    environment: str = 'unknown',
    *,
    config: Settings = settings,
) -> float:
    """Apply realistic indoor GPS sigma floor while leaving explicit manual pins untouched."""
    sigma = _sigma(raw_sigma, config.fusion_default_sigma_meters)
    if environment == 'indoor' and location_source in ('exif', 'browser', 'legacy'):
        return max(sigma, INDOOR_GPS_SIGMA_FLOOR_M)
    return sigma


def _geometric_median(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Compute 2D spatial geometric median via Weiszfeld's algorithm starting at coordinate medians."""
    cx = float(median(p[0] for p in points))
    cy = float(median(p[1] for p in points))
    eps = 1e-6
    for _ in range(20):
        inv_dists = [1.0 / max(eps, math.hypot(px - cx, py - cy)) for px, py in points]
        total = math.fsum(inv_dists)
        nx = math.fsum(w * px for w, (px, _) in zip(inv_dists, points)) / total
        ny = math.fsum(w * py for w, (_, py) in zip(inv_dists, points)) / total
        if math.hypot(nx - cx, ny - cy) < 1e-5:
            return nx, ny
        cx, cy = nx, ny
    return cx, cy


def _robust_multiplier(z: float, threshold: float = OUTLIER_Z_THRESHOLD) -> float:
    """Return 1.0 inside the Gaussian core (z <= threshold) and redescending tail attenuation beyond."""
    if z <= threshold:
        return 1.0
    excess = z - threshold
    return ((threshold / z) ** 2) / (1.0 + excess * excess)


def _robust_fuse_locations(
    entries: list[tuple[float, float, float, str, int]],
) -> tuple[float, float, float, str, dict[int, dict]]:
    """Run permutation-invariant robust IRLS 2D Kalman fusion with Birge uncertainty scaling."""
    ordered = sorted(entries, key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
    sigmas = [entry[0] for entry in ordered]
    _, lat0, lon0, first_source, _ = ordered[0]
    points = [_xy(lat, lon, lat0, lon0) for _, lat, lon, _, _ in ordered]
    n = len(ordered)

    if n == 1:
        x, y = points[0]
        norm_weights = [1.0]
        sigma_final = sigmas[0]
    elif n == 2:
        norm_weights, sigma_meas = _weights(sigmas)
        x = math.fsum(w * px for w, (px, _) in zip(norm_weights, points))
        y = math.fsum(w * py for w, (_, py) in zip(norm_weights, points))
        chi2 = math.fsum((math.hypot(px - x, py - y) / s) ** 2 for (px, py), s in zip(points, sigmas))
        birge = max(1.0, math.sqrt(chi2 / 2.0))
        sigma_final = sigma_meas * birge
    else:
        x, y = _geometric_median(points)
        scale = min(sigmas)
        norm_weights = [1.0 / n] * n
        sigma_meas = sigmas[0]
        rhos = [1.0] * n
        for _ in range(15):
            dists = [math.hypot(px - x, py - y) for px, py in points]
            rhos = [_robust_multiplier(d / s) for d, s in zip(dists, sigmas)]
            raw_weights = [rho * ((scale / s) ** 2) for rho, s in zip(rhos, sigmas)]
            total_w = math.fsum(raw_weights)
            if total_w <= 0:
                break
            norm_weights = [w / total_w for w in raw_weights]
            nx = math.fsum(w * px for w, (px, _) in zip(norm_weights, points))
            ny = math.fsum(w * py for w, (_, py) in zip(norm_weights, points))
            sigma_meas = max(math.ulp(0.0), scale / math.sqrt(total_w))
            if math.hypot(nx - x, ny - y) < 1e-5:
                x, y = nx, ny
                break
            x, y = nx, ny

        dists = [math.hypot(px - x, py - y) for px, py in points]
        rhos = [_robust_multiplier(d / s) for d, s in zip(dists, sigmas)]
        raw_weights = [rho * ((scale / s) ** 2) for rho, s in zip(rhos, sigmas)]
        total_w = math.fsum(raw_weights)
        norm_weights = [w / total_w for w in raw_weights]
        sigma_meas = max(math.ulp(0.0), scale / math.sqrt(total_w))
        dof_2d = 2.0 * (math.fsum(rhos) - 1.0)
        if dof_2d > 0.5:
            chi2 = math.fsum(rho * ((d / s) ** 2) for rho, d, s in zip(rhos, dists, sigmas))
            birge = max(1.0, math.sqrt(chi2 / dof_2d))
        else:
            birge = 1.0
        sigma_final = sigma_meas * birge

    lat = max(-90.0, min(90.0, lat0 + math.degrees(y / EARTH_RADIUS_M)))
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    if not -180 <= lon <= 180:
        lon = (lon + 180) % 360 - 180
    source = first_source if all(entry[3] == first_source for entry in ordered) else 'kalman'

    diagnostics: dict[int, dict] = {}
    for (sigma_eff, _, _, _, orig_idx), (px, py), weight in zip(ordered, points, norm_weights):
        dist_m = math.hypot(px - x, py - y)
        z_score = dist_m / sigma_eff
        diagnostics[orig_idx] = {
            'effective_sigma_meters': round(sigma_eff, 2),
            'kalman_distance_meters': round(dist_m, 2),
            'kalman_z_score': round(z_score, 2),
            'kalman_weight': round(weight, 4),
            'kalman_outlier': bool(n >= 3 and z_score > OUTLIER_Z_THRESHOLD),
        }
    return lat, lon, sigma_final, source, diagnostics


def diagnose_media_locations(
    rows,
    *,
    environment: str = 'unknown',
    config: Settings = settings,
) -> tuple[dict, dict[int, dict]]:
    """Fuse media coordinates/timestamps and return both summary metrics and per-row diagnostics."""
    row_list = list(rows)
    locations, timestamps = [], []
    for idx, row in enumerate(row_list):
        lat, lon, ts = row['lat'], row['lon'], row['observed_at_ms']
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            sigma_eff = effective_location_sigma(row['sigma_meters'], row['location_source'], environment, config=config)
            locations.append((sigma_eff, lat, lon, row['location_source'], idx))
        if isinstance(ts, int) and MIN_TIMESTAMP_MS <= ts <= MAX_TIMESTAMP_MS:
            timestamps.append((ts, _sigma(row['sigma_seconds'], config.fusion_default_sigma_seconds), row['time_source']))
    result = dict(lat=None, lon=None, sigma_meters=None, location_source='unknown',
                  location_measurement_count=len(locations), observed_at_ms=None, sigma_seconds=None, time_source='unknown',
                  first_observed_at_ms=None, last_observed_at_ms=None, time_of_day=None, season=None)
    diagnostics: dict[int, dict] = {}
    if locations:
        lat, lon, sigma, source, diagnostics = _robust_fuse_locations(locations)
        result.update(lat=lat, lon=lon, sigma_meters=sigma, location_source=source)
    if timestamps:
        timestamps.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        last, sigma, source = timestamps[0]
        result.update(observed_at_ms=last, sigma_seconds=sigma, time_source=source,
                      first_observed_at_ms=timestamps[-1][0], last_observed_at_ms=last,
                      **temporal_patterns([(ts, s) for ts, s, _ in timestamps], config.import_timezone))
    return result, diagnostics


def fuse_media_measurements(rows, *, environment: str = 'unknown', config: Settings = settings) -> dict:
    result, _ = diagnose_media_locations(rows, environment=environment, config=config)
    return result


def recompute_observation_kalman(conn: sqlite3.Connection, obs_id: str, *, config: Settings = settings) -> dict:
    env_row = conn.execute('SELECT environment FROM observations WHERE id=?', (obs_id,)).fetchone()
    environment = env_row['environment'] if env_row else 'unknown'
    fields = ', '.join(MEASUREMENT_FIELDS)
    result = fuse_media_measurements(
        conn.execute(f'SELECT {fields} FROM media WHERE obs_id=?', (obs_id,)),
        environment=environment,
        config=config,
    )
    values = {**result, 'obs_id': obs_id, 'time_of_day': json.dumps(result['time_of_day']), 'season': json.dumps(result['season'])}
    conn.execute(f"""INSERT INTO space_time_gauss ({', '.join(values)})
        VALUES ({', '.join(':' + key for key in values)})
        ON CONFLICT(obs_id) DO UPDATE SET {', '.join(f'{key}=excluded.{key}' for key in result)}""", values)
    return result
