"""Deterministic Gaussian fusion, error propagation, calibration, and goal probability math."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from metacore.models import ValueSource
from metacore.units import convert_gaussian, normalize_unit_symbol

SeriesAggregation = Literal["latest", "sum", "mean", "min", "max", "delta"]

EARTH_RADIUS_M = 6_371_008.8


def _require_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} muss endlich sein")


def make_context_key(ctx: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Return a canonical sorted tuple representation of a context dictionary."""
    if not ctx:
        return ()
    return tuple(sorted((str(k).strip(), str(v).strip()) for k, v in ctx.items()))


@dataclass(frozen=True, slots=True)
class GaussianValue:
    """1D quantity or scalar parameter with mean, standard deviation, unit, and context."""

    mean: float
    sigma: float
    source: ValueSource = "derived"
    measurement_source: str = ""
    unit: str = ""
    context: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_finite(self.mean, "mean")
        _require_finite(self.sigma, "sigma")
        if self.sigma < 0.0:
            raise ValueError("sigma darf nicht negativ sein")


def convert_gaussian_value(value: GaussianValue, target_unit: str | None) -> GaussianValue:
    """Convert a GaussianValue to target_unit (no-op if either unit is empty)."""
    norm_target = normalize_unit_symbol(target_unit)
    norm_src = normalize_unit_symbol(value.unit)
    if not norm_target:
        return value
    if not norm_src or norm_src == norm_target:
        return GaussianValue(
            mean=value.mean,
            sigma=value.sigma,
            source=value.source,
            measurement_source=value.measurement_source,
            unit=norm_target,
            context=value.context,
        )
    new_mean, new_sigma = convert_gaussian(value.mean, value.sigma, norm_src, norm_target)
    assert new_sigma is not None
    return GaussianValue(
        mean=new_mean,
        sigma=new_sigma,
        source=value.source,
        measurement_source=value.measurement_source,
        unit=norm_target,
        context=value.context,
    )


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """2D WGS84 point with isotropic per-axis spatial uncertainty in meters."""

    lat: float
    lon: float
    sigma_meters: float
    measurement_source: str = ""

    def __post_init__(self) -> None:
        _require_finite(self.lat, "lat")
        _require_finite(self.lon, "lon")
        _require_finite(self.sigma_meters, "sigma_meters")
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError("lat muss zwischen -90 und 90 liegen")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError("lon muss zwischen -180 und 180 liegen")
        if self.sigma_meters <= 0.0:
            raise ValueError("sigma_meters muss > 0 sein")


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Result of calibrating an ai_prior parameter with real measurements."""

    mean: float
    sigma: float
    source: ValueSource
    observation_count: int


def _precision_weights(sigmas: Sequence[float]) -> tuple[list[float], float]:
    """Compute normalized inverse-variance weights and fused sigma safely without overflow."""
    scale = min(sigmas)
    weights = [(scale / sigma) ** 2 for sigma in sigmas]
    total = math.fsum(weights)
    return [w / total for w in weights], scale / math.sqrt(total)


def _collapse_same_source_1d(values: Sequence[GaussianValue]) -> list[GaussianValue]:
    """Group repeated measurements from the same non-empty source without shrinking sigma."""
    independent: list[GaussianValue] = []
    by_source: dict[str, list[GaussianValue]] = {}
    for item in values:
        key = item.measurement_source.strip()
        if not key:
            independent.append(item)
        else:
            by_source.setdefault(key, []).append(item)

    for src, group in by_source.items():
        if len(group) == 1:
            independent.append(group[0])
            continue
        exact = [g for g in group if g.sigma == 0.0]
        if exact:
            mu = math.fsum(g.mean for g in exact) / len(exact)
        else:
            weights, _ = _precision_weights([g.sigma for g in group])
            mu = math.fsum(w * g.mean for w, g in zip(weights, group))
        spread = math.sqrt(math.fsum((g.mean - mu) ** 2 for g in group) / len(group))
        sigma = max(min(g.sigma for g in group), spread)
        independent.append(
            GaussianValue(
                mean=mu,
                sigma=sigma,
                source=group[0].source,
                measurement_source=src,
                unit=group[0].unit,
                context=group[0].context,
            )
        )
    return independent


def _resolve_common_unit(units: Sequence[str], target_unit: str | None = None) -> str:
    if target_unit is not None:
        norm_target = normalize_unit_symbol(target_unit)
        if norm_target:
            return norm_target
    for unit in units:
        norm = normalize_unit_symbol(unit)
        if norm:
            return norm
    return ""


def fuse_1d(
    values: Sequence[GaussianValue],
    *,
    deduplicate_sources: bool = True,
    target_unit: str | None = None,
    require_same_context: bool = True,
) -> GaussianValue:
    """Fuse 1D Gaussian estimates using inverse-variance precision weighting with unit/context guards."""
    if not values:
        raise ValueError("fuse_1d erfordert mindestens einen Wert")

    contexts = {v.context for v in values}
    if require_same_context and len(contexts) > 1:
        raise ValueError(
            "Kontext-Konflikt in fuse_1d: Messungen mit unterschiedlichen Kontexten dürfen nicht vermischt werden."
        )
    common_context = values[0].context if len(contexts) == 1 else ()
    common_unit = _resolve_common_unit([v.unit for v in values], target_unit=target_unit)
    normalized = [convert_gaussian_value(v, common_unit) for v in values]

    items = _collapse_same_source_1d(normalized) if deduplicate_sources else list(normalized)
    if len(items) == 1:
        only = items[0]
        return GaussianValue(
            mean=only.mean,
            sigma=only.sigma,
            source=only.source,
            measurement_source=only.measurement_source,
            unit=only.unit,
            context=common_context,
        )

    exact = [v for v in items if v.sigma == 0.0]
    if exact:
        mu = math.fsum(v.mean for v in exact) / len(exact)
        return GaussianValue(mean=mu, sigma=0.0, source="derived", unit=common_unit, context=common_context)

    weights, fused_sigma = _precision_weights([v.sigma for v in items])
    fused_mean = math.fsum(w * v.mean for w, v in zip(weights, items))
    return GaussianValue(
        mean=fused_mean,
        sigma=fused_sigma,
        source="derived",
        unit=common_unit,
        context=common_context,
    )


def _to_enu(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    delta = (lon - lon0 + 180.0) % 360.0 - 180.0
    east = math.radians(delta) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    north = math.radians(lat - lat0) * EARTH_RADIUS_M
    return east, north


def _from_enu(east: float, north: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = max(-90.0, min(90.0, lat0 + math.degrees(north / EARTH_RADIUS_M)))
    cos_lat0 = max(1e-12, math.cos(math.radians(lat0)))
    lon = ((lon0 + math.degrees(east / (EARTH_RADIUS_M * cos_lat0)) + 180.0) % 360.0) - 180.0
    return lat, lon


def geo_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute local metric distance between two WGS84 coordinates in meters."""
    mid_lat = 0.5 * (lat1 + lat2)
    mid_lon = 0.5 * (lon1 + lon2)
    e1, n1 = _to_enu(lat1, lon1, mid_lat, mid_lon)
    e2, n2 = _to_enu(lat2, lon2, mid_lat, mid_lon)
    return math.hypot(e2 - e1, n2 - n1)


def _collapse_same_source_geo(
    points: Sequence[GeoPoint], lat0: float, lon0: float
) -> list[tuple[float, float, float, str]]:
    independent: list[tuple[float, float, float, str]] = []
    by_source: dict[str, list[GeoPoint]] = {}
    for pt in points:
        key = pt.measurement_source.strip()
        if not key:
            e, n = _to_enu(pt.lat, pt.lon, lat0, lon0)
            independent.append((e, n, pt.sigma_meters, ""))
        else:
            by_source.setdefault(key, []).append(pt)

    for src, group in by_source.items():
        enu = [_to_enu(p.lat, p.lon, lat0, lon0) for p in group]
        if len(group) == 1:
            independent.append((enu[0][0], enu[0][1], group[0].sigma_meters, src))
            continue
        weights, _ = _precision_weights([p.sigma_meters for p in group])
        ce = math.fsum(w * e for w, (e, _) in zip(weights, enu))
        cn = math.fsum(w * n for w, (_, n) in zip(weights, enu))
        spread = math.sqrt(
            0.5 * math.fsum((e - ce) ** 2 + (n - cn) ** 2 for e, n in enu) / len(enu)
        )
        sigma = max(min(p.sigma_meters for p in group), spread)
        independent.append((ce, cn, sigma, src))
    return independent


def fuse_locations(points: Sequence[GeoPoint], *, deduplicate_sources: bool = True) -> GeoPoint:
    """Fuse multiple WGS84 measurements of the same object in local ENU meters."""
    if not points:
        raise ValueError("fuse_locations erfordert mindestens einen Punkt")
    if len(points) == 1:
        return points[0]

    lat0, lon0 = points[0].lat, points[0].lon
    if deduplicate_sources:
        items = _collapse_same_source_geo(points, lat0, lon0)
    else:
        items = [(*_to_enu(p.lat, p.lon, lat0, lon0), p.sigma_meters, p.measurement_source) for p in points]

    if len(items) == 1:
        e, n, sigma, src = items[0]
        lat, lon = _from_enu(e, n, lat0, lon0)
        return GeoPoint(lat=lat, lon=lon, sigma_meters=sigma, measurement_source=src)

    weights, fused_sigma = _precision_weights([item[2] for item in items])
    fe = math.fsum(w * item[0] for w, item in zip(weights, items))
    fn = math.fsum(w * item[1] for w, item in zip(weights, items))
    lat, lon = _from_enu(fe, fn, lat0, lon0)
    return GeoPoint(lat=lat, lon=lon, sigma_meters=fused_sigma)


def generalize_habitat(
    points: Sequence[GeoPoint],
    *,
    min_sigma_meters: float = 50.0,
    small_sample_factor: float = 1.25,
) -> GeoPoint:
    """Generalize scattered finds (e.g., 2 mushrooms 180 m apart) into a conservative habitat area."""
    if not points:
        raise ValueError("generalize_habitat erfordert mindestens einen Punkt")
    if min_sigma_meters <= 0.0:
        raise ValueError("min_sigma_meters muss > 0 sein")

    lat0, lon0 = points[0].lat, points[0].lon
    enu = [_to_enu(p.lat, p.lon, lat0, lon0) for p in points]
    n_pts = len(points)
    ce = math.fsum(e for e, _ in enu) / n_pts
    cn = math.fsum(n for _, n in enu) / n_pts

    sample_spread = math.sqrt(math.fsum((e - ce) ** 2 + (n - cn) ** 2 for e, n in enu) / n_pts)
    obs_rms = math.sqrt(math.fsum(p.sigma_meters ** 2 for p in points) / n_pts)
    factor = small_sample_factor if n_pts < 5 else 1.0
    habitat_sigma = max(min_sigma_meters, math.hypot(sample_spread, obs_rms) * factor)

    lat, lon = _from_enu(ce, cn, lat0, lon0)
    return GeoPoint(lat=lat, lon=lon, sigma_meters=habitat_sigma, measurement_source="habitat_derived")


def propagate_sum(
    values: Sequence[GaussianValue], *, target_unit: str | None = None
) -> GaussianValue:
    """Propagate mean and uncertainty for a sum of independent Gaussian values."""
    if not values:
        raise ValueError("propagate_sum erfordert mindestens einen Wert")
    common_unit = _resolve_common_unit([v.unit for v in values], target_unit=target_unit)
    norm_vals = [convert_gaussian_value(v, common_unit) for v in values]
    mean = math.fsum(v.mean for v in norm_vals)
    sigma = math.sqrt(math.fsum(v.sigma ** 2 for v in norm_vals))
    return GaussianValue(mean=mean, sigma=sigma, source="derived", unit=common_unit)


def propagate_product(a: GaussianValue, b: GaussianValue) -> GaussianValue:
    """First-order error propagation for Z = A * B."""
    mean = a.mean * b.mean
    sigma = math.hypot(b.mean * a.sigma, a.mean * b.sigma)
    return GaussianValue(mean=mean, sigma=sigma, source="derived")


def propagate_split(
    total: GaussianValue, parts: int, *, split_sigma: float = 0.0
) -> GaussianValue:
    """Propagate uncertainty when splitting a total quantity evenly across k parts."""
    if parts < 1:
        raise ValueError("parts muss >= 1 sein")
    _require_finite(split_sigma, "split_sigma")
    if split_sigma < 0.0:
        raise ValueError("split_sigma darf nicht negativ sein")
    mean = total.mean / parts
    sigma = math.hypot(total.sigma / parts, split_sigma)
    return GaussianValue(mean=mean, sigma=sigma, source="derived")


def calibrate_parameter(
    prior: GaussianValue, observations: Sequence[GaussianValue]
) -> CalibrationResult:
    """Update a template prior (often ai_prior) using deduplicated real observations."""
    if not observations:
        return CalibrationResult(
            mean=prior.mean,
            sigma=prior.sigma,
            source=prior.source,
            observation_count=0,
        )

    deduped = _collapse_same_source_1d(observations)
    prior_clean = GaussianValue(mean=prior.mean, sigma=prior.sigma, source=prior.source)
    fused = fuse_1d([prior_clean, *deduped], deduplicate_sources=False)
    return CalibrationResult(
        mean=fused.mean,
        sigma=fused.sigma,
        source="measured",
        observation_count=len(observations),
    )


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    """Cumulative distribution function of N(mean, sigma^2)."""
    _require_finite(x, "x")
    _require_finite(mean, "mean")
    _require_finite(sigma, "sigma")
    if sigma < 0.0:
        raise ValueError("sigma darf nicht negativ sein")
    if sigma == 0.0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def probability_in_bounds(
    value: GaussianValue,
    *,
    target_min: float | None = None,
    target_max: float | None = None,
) -> float:
    """Compute P(target_min <= X <= target_max) for a scalar prediction."""
    if target_min is not None and target_max is not None and target_min > target_max:
        raise ValueError("target_min darf nicht größer als target_max sein")
    if value.sigma == 0.0:
        in_low = target_min is None or value.mean >= target_min
        in_high = target_max is None or value.mean <= target_max
        return 1.0 if (in_low and in_high) else 0.0

    upper = 1.0 if target_max is None else normal_cdf(target_max, value.mean, value.sigma)
    lower = 0.0 if target_min is None else normal_cdf(target_min, value.mean, value.sigma)
    return max(0.0, min(1.0, upper - lower))


def probability_within_radius(
    point: GeoPoint, target_lat: float, target_lon: float, radius_meters: float
) -> float:
    """Compute probability that an isotropic 2D Gaussian location lies within target radius."""
    _require_finite(radius_meters, "radius_meters")
    if radius_meters <= 0.0:
        raise ValueError("radius_meters muss > 0 sein")
    if not (-90.0 <= target_lat <= 90.0 and -180.0 <= target_lon <= 180.0):
        raise ValueError("Ungültige Zielkoordinaten")

    s = point.sigma_meters
    r = radius_meters
    d = geo_distance_meters(point.lat, point.lon, target_lat, target_lon)
    if d == 0.0:
        return 1.0 - math.exp(-(r * r) / (2.0 * s * s))

    a = max(-r, d - 8.0 * s)
    b = min(r, d + 8.0 * s)
    if a >= b:
        return 0.0

    def integrand(x: float) -> float:
        chord_half = math.sqrt(max(0.0, r * r - x * x))
        pdf_x = math.exp(-((x - d) ** 2) / (2.0 * s * s)) / (math.sqrt(math.tau) * s)
        cdf_y = math.erf(chord_half / (s * math.sqrt(2.0)))
        return pdf_x * cdf_y

    steps = 200
    h = (b - a) / steps
    acc = integrand(a) + integrand(b)
    for i in range(1, steps):
        acc += (4.0 if i % 2 == 1 else 2.0) * integrand(a + i * h)
    return max(0.0, min(1.0, acc * h / 3.0))


def combine_goal_probability(
    dimension_probs: Sequence[float], *, path_success_prob: float = 1.0
) -> float:
    """Combine independent goal dimension probabilities and path reliability into [0, 1]."""
    _require_finite(path_success_prob, "path_success_prob")
    if not 0.0 <= path_success_prob <= 1.0:
        raise ValueError("path_success_prob muss in [0, 1] liegen")
    prob = path_success_prob
    for p in dimension_probs:
        _require_finite(p, "dimension_prob")
        if not 0.0 <= p <= 1.0:
            raise ValueError("dimension_probs müssen in [0, 1] liegen")
        prob *= p
    return max(0.0, min(1.0, prob))


def propagate_linear_combination(
    terms: Sequence[tuple[GaussianValue, GaussianValue]],
    *,
    target_unit: str | None = None,
) -> GaussianValue:
    """Propagate mean and RSS sigma for Y = sum_i c_i * X_i over uncertain inputs/weights."""
    if not terms:
        raise ValueError("terms darf nicht leer sein")
    common_unit = _resolve_common_unit([value.unit for value, _ in terms], target_unit=target_unit)
    norm_terms = [(convert_gaussian_value(value, common_unit), weight) for value, weight in terms]
    mean = sum(weight.mean * value.mean for value, weight in norm_terms)
    sigma = math.sqrt(
        sum(
            (weight.mean * value.sigma) ** 2 + (value.mean * weight.sigma) ** 2
            for value, weight in norm_terms
        )
    )
    source: ValueSource = (
        "ai_prior"
        if any("ai_prior" in (value.source, weight.source) for value, weight in norm_terms)
        else "derived"
    )
    return GaussianValue(mean=mean, sigma=sigma, source=source, unit=common_unit)


def propagate_ratio(numerator: GaussianValue, denominator: GaussianValue) -> GaussianValue:
    """Propagate Gaussian uncertainty for ratio R = numerator / denominator."""
    if abs(denominator.mean) < 1e-12:
        raise ValueError("denominator.mean darf nicht 0 sein")
    mean = numerator.mean / denominator.mean
    sigma = math.hypot(
        numerator.sigma / abs(denominator.mean),
        abs(numerator.mean) * denominator.sigma / (denominator.mean**2),
    )
    source: ValueSource = (
        "ai_prior" if "ai_prior" in (numerator.source, denominator.source) else "derived"
    )
    return GaussianValue(mean=mean, sigma=sigma, source=source)


def aggregate_series(
    samples: Sequence[GaussianValue],
    aggregation: SeriesAggregation = "latest",
    *,
    target_unit: str | None = None,
) -> GaussianValue:
    """Aggregate an ordered time series of Gaussian samples into a single summary value."""
    if not samples:
        raise ValueError("samples darf nicht leer sein")
    common_unit = _resolve_common_unit([s.unit for s in samples], target_unit=target_unit)
    norm_samples = [convert_gaussian_value(s, common_unit) for s in samples]
    if aggregation == "latest":
        return norm_samples[-1]
    if aggregation == "min":
        return min(norm_samples, key=lambda s: s.mean)
    if aggregation == "max":
        return max(norm_samples, key=lambda s: s.mean)

    source: ValueSource = (
        "ai_prior" if any(s.source == "ai_prior" for s in norm_samples) else "derived"
    )
    if aggregation == "sum":
        return GaussianValue(
            mean=sum(s.mean for s in norm_samples),
            sigma=math.sqrt(sum(s.sigma**2 for s in norm_samples)),
            source=source,
            unit=common_unit,
        )
    if aggregation == "mean":
        n = len(norm_samples)
        return GaussianValue(
            mean=sum(s.mean for s in norm_samples) / n,
            sigma=math.sqrt(sum(s.sigma**2 for s in norm_samples)) / n,
            source=source,
            unit=common_unit,
        )
    if aggregation == "delta":
        if len(norm_samples) == 1:
            return GaussianValue(mean=0.0, sigma=0.0, source=source, unit=common_unit)
        first, last = norm_samples[0], norm_samples[-1]
        return GaussianValue(
            mean=last.mean - first.mean,
            sigma=math.hypot(last.sigma, first.sigma),
            source=source,
            unit=common_unit,
        )
    raise ValueError(f"Unbekannte Aggregation: {aggregation}")


def compute_modifier_factor(
    observed: GaussianValue,
    *,
    optimal_min: float,
    optimal_max: float,
    falloff_per_unit: float,
    min_factor: float = 0.0,
    sigma_penalty_per_unit: float = 0.0,
) -> GaussianValue:
    """Compute a [min_factor, 1.0] environmental scaling factor outside an optimal corridor."""
    _require_finite(optimal_min, "optimal_min")
    _require_finite(optimal_max, "optimal_max")
    _require_finite(falloff_per_unit, "falloff_per_unit")
    _require_finite(min_factor, "min_factor")
    _require_finite(sigma_penalty_per_unit, "sigma_penalty_per_unit")
    if optimal_min > optimal_max:
        raise ValueError("optimal_min darf nicht größer als optimal_max sein")
    if falloff_per_unit < 0.0:
        raise ValueError("falloff_per_unit darf nicht negativ sein")
    if not 0.0 <= min_factor <= 1.0:
        raise ValueError("min_factor muss in [0, 1] liegen")
    if sigma_penalty_per_unit < 0.0:
        raise ValueError("sigma_penalty_per_unit darf nicht negativ sein")

    if observed.mean < optimal_min:
        dist = optimal_min - observed.mean
    elif observed.mean > optimal_max:
        dist = observed.mean - optimal_max
    else:
        dist = 0.0

    factor = max(min_factor, 1.0 - falloff_per_unit * dist)
    sigma = sigma_penalty_per_unit * dist + falloff_per_unit * observed.sigma
    return GaussianValue(mean=factor, sigma=sigma, source="derived")


def propagate_exponential_decay(
    initial: GaussianValue,
    equilibrium: GaussianValue,
    dt_seconds: float,
    tau: GaussianValue,
    *,
    dt_sigma_seconds: float = 0.0,
) -> GaussianValue:
    """Propagate x(t) = x_inf + (x_0 - x_inf) * exp(-dt / tau) with Gaussian error propagation."""
    _require_finite(dt_seconds, "dt_seconds")
    _require_finite(dt_sigma_seconds, "dt_sigma_seconds")
    if dt_seconds < 0.0:
        raise ValueError("dt_seconds darf nicht negativ sein")
    if dt_sigma_seconds < 0.0:
        raise ValueError("dt_sigma_seconds darf nicht negativ sein")
    if tau.mean <= 0.0:
        raise ValueError("tau.mean muss > 0 sein")

    out_unit = _resolve_common_unit([initial.unit, equilibrium.unit])
    init_norm = convert_gaussian_value(initial, out_unit)
    eq_norm = convert_gaussian_value(equilibrium, out_unit)

    decay = math.exp(-dt_seconds / tau.mean)
    delta = init_norm.mean - eq_norm.mean
    mean = eq_norm.mean + delta * decay

    d_x0 = decay
    d_xinf = 1.0 - decay
    d_tau = delta * decay * (dt_seconds / (tau.mean**2))
    d_dt = -delta * decay / tau.mean

    sigma = math.sqrt(
        (d_x0 * init_norm.sigma) ** 2
        + (d_xinf * eq_norm.sigma) ** 2
        + (d_tau * tau.sigma) ** 2
        + (d_dt * dt_sigma_seconds) ** 2
    )
    src: ValueSource = (
        "ai_prior"
        if "ai_prior" in (init_norm.source, eq_norm.source, tau.source)
        else "derived"
    )
    return GaussianValue(
        mean=mean,
        sigma=sigma,
        source=src,
        unit=out_unit,
        context=init_norm.context,
    )


def propagate_cumulative_balance(
    flows: Sequence[tuple[GaussianValue, float]],
    *,
    initial: GaussianValue | None = None,
    min_clamp: float | None = None,
    max_clamp: float | None = None,
    target_unit: str | None = None,
) -> GaussianValue:
    """Accumulate signed inflows/outflows over an optional initial storage level with RSS uncertainty."""
    if initial is None and not flows:
        raise ValueError("propagate_cumulative_balance erfordert initial oder mindestens einen Flow")
    if min_clamp is not None:
        _require_finite(min_clamp, "min_clamp")
    if max_clamp is not None:
        _require_finite(max_clamp, "max_clamp")
    if min_clamp is not None and max_clamp is not None and min_clamp > max_clamp:
        raise ValueError("min_clamp darf nicht größer als max_clamp sein")

    unit_pool = ([initial.unit] if initial is not None else []) + [v.unit for v, _ in flows]
    common_unit = _resolve_common_unit(unit_pool, target_unit=target_unit)

    terms: list[tuple[GaussianValue, GaussianValue]] = []
    if initial is not None:
        terms.append((convert_gaussian_value(initial, common_unit), GaussianValue(1.0, 0.0, source="derived")))
    for val, signed_weight in flows:
        _require_finite(signed_weight, "signed_weight")
        terms.append(
            (
                convert_gaussian_value(val, common_unit),
                GaussianValue(float(signed_weight), 0.0, source="derived"),
            )
        )

    combined = propagate_linear_combination(terms, target_unit=common_unit or None)
    mean = combined.mean
    if min_clamp is not None:
        mean = max(min_clamp, mean)
    if max_clamp is not None:
        mean = min(max_clamp, mean)
    return GaussianValue(
        mean=mean,
        sigma=combined.sigma,
        source=combined.source,
        unit=common_unit,
        context=initial.context if initial is not None else (),
    )
