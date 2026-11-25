"""Domain-agnostic physical unit registry, dimension checks, and affine-safe Gaussian conversions."""

from __future__ import annotations

import math
from dataclasses import dataclass


class UnitCompatibilityError(ValueError):
    """Raised when attempting to convert or combine quantities with incompatible dimensions."""


@dataclass(frozen=True, slots=True)
class UnitSpec:
    """Definition of one unit relative to its dimension's canonical base unit."""

    symbol: str
    dimension: str
    base_unit: str
    scale: float  # base = value * scale + offset
    offset: float = 0.0


_FAHRENHEIT_OFFSET = 459.67 * (5.0 / 9.0)

_UNIT_SPECS: tuple[UnitSpec, ...] = (
    # Mass (canonical base: g)
    UnitSpec("µg", "mass", "g", 1e-6),
    UnitSpec("mg", "mass", "g", 1e-3),
    UnitSpec("g", "mass", "g", 1.0),
    UnitSpec("kg", "mass", "g", 1_000.0),
    UnitSpec("t", "mass", "g", 1_000_000.0),
    # Volume (canonical base: ml)
    UnitSpec("µl", "volume", "ml", 1e-3),
    UnitSpec("ml", "volume", "ml", 1.0),
    UnitSpec("cl", "volume", "ml", 10.0),
    UnitSpec("dl", "volume", "ml", 100.0),
    UnitSpec("l", "volume", "ml", 1_000.0),
    UnitSpec("m3", "volume", "ml", 1_000_000.0),
    # Length (canonical base: m)
    UnitSpec("mm", "length", "m", 1e-3),
    UnitSpec("cm", "length", "m", 1e-2),
    UnitSpec("dm", "length", "m", 1e-1),
    UnitSpec("m", "length", "m", 1.0),
    UnitSpec("km", "length", "m", 1_000.0),
    # Time (canonical base: s)
    UnitSpec("ms", "time", "s", 1e-3),
    UnitSpec("s", "time", "s", 1.0),
    UnitSpec("min", "time", "s", 60.0),
    UnitSpec("h", "time", "s", 3_600.0),
    UnitSpec("d", "time", "s", 86_400.0),
    UnitSpec("w", "time", "s", 604_800.0),
    # Temperature (canonical base: K; affine offset on mean, linear scale on sigma)
    UnitSpec("K", "temperature", "K", 1.0, 0.0),
    UnitSpec("°C", "temperature", "K", 1.0, 273.15),
    UnitSpec("°F", "temperature", "K", 5.0 / 9.0, _FAHRENHEIT_OFFSET),
    # Ratio / fraction (canonical base: ratio)
    UnitSpec("ratio", "ratio", "ratio", 1.0),
    UnitSpec("%", "ratio", "ratio", 0.01),
    UnitSpec("ppm", "ratio", "ratio", 1e-6),
    # Energy (canonical base: J)
    UnitSpec("J", "energy", "J", 1.0),
    UnitSpec("kJ", "energy", "J", 1_000.0),
    UnitSpec("MJ", "energy", "J", 1_000_000.0),
    UnitSpec("Wh", "energy", "J", 3_600.0),
    UnitSpec("kWh", "energy", "J", 3_600_000.0),
    UnitSpec("cal", "energy", "J", 4.184),
    UnitSpec("kcal", "energy", "J", 4_184.0),
    # Power (canonical base: W)
    UnitSpec("mW", "power", "W", 1e-3),
    UnitSpec("W", "power", "W", 1.0),
    UnitSpec("kW", "power", "W", 1_000.0),
    # Pressure (canonical base: Pa)
    UnitSpec("Pa", "pressure", "Pa", 1.0),
    UnitSpec("hPa", "pressure", "Pa", 100.0),
    UnitSpec("kPa", "pressure", "Pa", 1_000.0),
    UnitSpec("mbar", "pressure", "Pa", 100.0),
    UnitSpec("bar", "pressure", "Pa", 100_000.0),
    # Count (canonical base: stk)
    UnitSpec("stk", "count", "stk", 1.0),
    # Area (canonical base: m2)
    UnitSpec("cm2", "area", "m2", 1e-4),
    UnitSpec("m2", "area", "m2", 1.0),
    UnitSpec("ha", "area", "m2", 10_000.0),
    UnitSpec("km2", "area", "m2", 1_000_000.0),
)

_BY_SYMBOL: dict[str, UnitSpec] = {spec.symbol: spec for spec in _UNIT_SPECS}

_ALIASES: dict[str, str] = {
    "ug": "µg",
    "mcg": "µg",
    "µg": "µg",
    "mg": "mg",
    "g": "g",
    "kg": "kg",
    "t": "t",
    "ul": "µl",
    "µl": "µl",
    "ml": "ml",
    "mL": "ml",
    "cl": "cl",
    "dl": "dl",
    "l": "l",
    "L": "l",
    "m3": "m3",
    "m³": "m3",
    "mm": "mm",
    "cm": "cm",
    "dm": "dm",
    "m": "m",
    "km": "km",
    "ms": "ms",
    "s": "s",
    "sec": "s",
    "min": "min",
    "h": "h",
    "hr": "h",
    "d": "d",
    "day": "d",
    "w": "w",
    "week": "w",
    "K": "K",
    "k": "K",
    "°C": "°C",
    "°c": "°C",
    "degC": "°C",
    "degc": "°C",
    "C": "°C",
    "°F": "°F",
    "°f": "°F",
    "degF": "°F",
    "degf": "°F",
    "F": "°F",
    "ratio": "ratio",
    "%": "%",
    "pct": "%",
    "ppm": "ppm",
    "J": "J",
    "j": "J",
    "kJ": "kJ",
    "kj": "kJ",
    "MJ": "MJ",
    "mj": "MJ",
    "Wh": "Wh",
    "wh": "Wh",
    "kWh": "kWh",
    "kwh": "kWh",
    "cal": "cal",
    "kcal": "kcal",
    "mW": "mW",
    "mw": "mW",
    "W": "W",
    "kW": "kW",
    "kw": "kW",
    "Pa": "Pa",
    "pa": "Pa",
    "hPa": "hPa",
    "hpa": "hPa",
    "kPa": "kPa",
    "kpa": "kPa",
    "mbar": "mbar",
    "bar": "bar",
    "stk": "stk",
    "pcs": "stk",
    "count": "stk",
    "cm2": "cm2",
    "cm²": "cm2",
    "m2": "m2",
    "m²": "m2",
    "ha": "ha",
    "km2": "km2",
    "km²": "km2",
}


def normalize_unit_symbol(unit: str | None) -> str:
    """Normalize known unit aliases to canonical symbols while preserving custom unit strings."""
    if not unit:
        return ""
    stripped = unit.strip()
    if not stripped:
        return ""
    if stripped in _ALIASES:
        return _ALIASES[stripped]
    return stripped


def get_unit_spec(unit: str | None) -> UnitSpec | None:
    """Return UnitSpec for a known unit or None if empty or custom."""
    sym = normalize_unit_symbol(unit)
    if not sym:
        return None
    return _BY_SYMBOL.get(sym)


def get_unit_dimension(unit: str | None) -> str | None:
    """Return the physical dimension name of a known unit, or None if unknown/empty."""
    spec = get_unit_spec(unit)
    return spec.dimension if spec is not None else None


def are_units_compatible(unit_a: str | None, unit_b: str | None) -> bool:
    """Return True if units can be converted between each other (empty unit is wildcard)."""
    norm_a = normalize_unit_symbol(unit_a)
    norm_b = normalize_unit_symbol(unit_b)
    if not norm_a or not norm_b:
        return True
    if norm_a == norm_b:
        return True
    spec_a = _BY_SYMBOL.get(norm_a)
    spec_b = _BY_SYMBOL.get(norm_b)
    if spec_a is None or spec_b is None:
        return False
    return spec_a.dimension == spec_b.dimension


def _require_compatible_specs(from_unit: str | None, to_unit: str | None) -> tuple[UnitSpec | None, UnitSpec | None]:
    norm_from = normalize_unit_symbol(from_unit)
    norm_to = normalize_unit_symbol(to_unit)
    if not norm_from or not norm_to or norm_from == norm_to:
        return _BY_SYMBOL.get(norm_from), _BY_SYMBOL.get(norm_to)
    spec_from = _BY_SYMBOL.get(norm_from)
    spec_to = _BY_SYMBOL.get(norm_to)
    if spec_from is None or spec_to is None:
        raise UnitCompatibilityError(
            f"Inkompatible Einheiten: '{from_unit}' vs. '{to_unit}' (unbekannte/custom Einheit)."
        )
    if spec_from.dimension != spec_to.dimension:
        raise UnitCompatibilityError(
            f"Dimensionskonflikt: '{from_unit}' ({spec_from.dimension}) vs. "
            f"'{to_unit}' ({spec_to.dimension})."
        )
    return spec_from, spec_to


def convert_value(value: float, from_unit: str | None, to_unit: str | None) -> float:
    """Convert a scalar measurement or bound between compatible units (supports affine °C/°F/K)."""
    if not math.isfinite(value):
        raise ValueError("value muss endlich sein")
    norm_from = normalize_unit_symbol(from_unit)
    norm_to = normalize_unit_symbol(to_unit)
    if not norm_from or not norm_to or norm_from == norm_to:
        return value
    spec_from, spec_to = _require_compatible_specs(norm_from, norm_to)
    assert spec_from is not None and spec_to is not None
    base_val = value * spec_from.scale + spec_from.offset
    return (base_val - spec_to.offset) / spec_to.scale


def convert_sigma(sigma: float, from_unit: str | None, to_unit: str | None) -> float:
    """Convert a standard deviation linearly between compatible units (never adds affine offsets)."""
    if not math.isfinite(sigma):
        raise ValueError("sigma muss endlich sein")
    if sigma < 0.0:
        raise ValueError("sigma darf nicht negativ sein")
    norm_from = normalize_unit_symbol(from_unit)
    norm_to = normalize_unit_symbol(to_unit)
    if not norm_from or not norm_to or norm_from == norm_to:
        return sigma
    spec_from, spec_to = _require_compatible_specs(norm_from, norm_to)
    assert spec_from is not None and spec_to is not None
    return sigma * (spec_from.scale / spec_to.scale)


def convert_gaussian(
    mean: float,
    sigma: float | None,
    from_unit: str | None,
    to_unit: str | None,
) -> tuple[float, float | None]:
    """Convert (mean, sigma) between compatible units while handling affine offsets safely."""
    new_mean = convert_value(mean, from_unit, to_unit)
    new_sigma = convert_sigma(sigma, from_unit, to_unit) if sigma is not None else None
    return new_mean, new_sigma


def convert_to_canonical(
    mean: float,
    sigma: float | None,
    unit: str | None,
) -> tuple[float, float | None, str]:
    """Convert a value and sigma into the canonical base unit of its dimension."""
    norm = normalize_unit_symbol(unit)
    spec = _BY_SYMBOL.get(norm)
    if spec is None:
        return mean, sigma, norm
    if spec.symbol == spec.base_unit:
        return mean, sigma, spec.base_unit
    new_mean, new_sigma = convert_gaussian(mean, sigma, spec.symbol, spec.base_unit)
    return new_mean, new_sigma, spec.base_unit
