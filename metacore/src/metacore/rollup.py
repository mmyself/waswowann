"""High-frequency raw time-series bucketing and roll-up into causal graph States."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from metacore.clocks import ClockId, project_timestamp
from metacore.math_fusion import make_context_key
from metacore.models import EvidenceRef, RawSample, StateCreate, StrictModel
from metacore.units import convert_gaussian, normalize_unit_symbol

RollupWindowKind = Literal["fixed_seconds", "hour", "day", "clock_phase"]


class RollupBucket(StrictModel):
    """Summary statistics and provenance produced for one time/context bucket."""

    bucket_key: str
    window_kind: RollupWindowKind
    entity_id: str
    property_name: str
    sample_ids: list[str] = Field(default_factory=list)
    sample_count: int
    start_ms: int
    end_ms: int
    mean: float
    sigma: float
    min_value: float
    max_value: float
    slope_per_hour: float | None = None
    unit: str = ""
    context: dict[str, str] = Field(default_factory=dict)
    measurement_source: str = ""
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


def _resolve_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _bucket_key_for_sample(
    sample: RawSample,
    *,
    window_kind: RollupWindowKind,
    window_seconds: float,
    tz: ZoneInfo,
    clock_id: ClockId,
    phase_bins: int | None,
    lat: float,
    lon: float,
) -> tuple[str, str]:
    """Return (machine_bucket_key, human_label_suffix) for a sample timestamp."""
    if window_kind == "fixed_seconds":
        if window_seconds <= 0.0:
            raise ValueError("window_seconds muss > 0 sein")
        win_ms = round(window_seconds * 1000.0)
        if win_ms <= 0:
            raise ValueError("window_seconds ist zu klein")
        idx = sample.time_ms // win_ms
        return f"fixed:{win_ms}:{idx}", f"Fenster {int(window_seconds)}s"

    dt = datetime.fromtimestamp(sample.time_ms / 1000.0, tz=tz)
    if window_kind == "hour":
        key = dt.strftime("%Y-%m-%dT%H")
        return f"hour:{key}", dt.strftime("%d.%m. %H:00")
    if window_kind == "day":
        key = dt.strftime("%Y-%m-%d")
        return f"day:{key}", dt.strftime("%Y-%m-%d")
    if window_kind == "clock_phase":
        snap = project_timestamp(
            sample.time_ms,
            clock_id,
            tz_name=tz.key,
            lat=lat,
            lon=lon,
        )
        if clock_id == "solar_day" and phase_bins is None:
            sub = "day" if snap.is_daylight else "night"
            label = "Tag" if snap.is_daylight else "Nacht"
            return f"clock:{clock_id}:{snap.cycle_index}:{sub}", f"Zyklus {snap.cycle_index} ({label})"
        bins = max(1, phase_bins or 1)
        sub_idx = min(bins - 1, int(snap.phase * bins))
        return f"clock:{clock_id}:{snap.cycle_index}:{sub_idx}", f"{snap.clock_name} #{snap.cycle_index}/{sub_idx}"

    raise ValueError(f"Unbekannte window_kind: {window_kind}")


def compute_rollup_buckets(
    samples: Sequence[RawSample],
    *,
    window_kind: RollupWindowKind = "day",
    window_seconds: float = 3_600.0,
    tz_name: str = "Europe/Berlin",
    clock_id: ClockId = "solar_day",
    phase_bins: int | None = None,
    lat: float = 51.0,
    lon: float = 10.0,
    target_unit: str | None = None,
) -> list[tuple[RollupBucket, StateCreate]]:
    """Group raw samples by (entity, property, context, window) and build rolled-up StateCreate payloads."""
    if not samples:
        return []

    tz = _resolve_tz(tz_name)
    groups: dict[
        tuple[str, str, tuple[tuple[str, str], ...], str],
        tuple[str, list[RawSample]],
    ] = {}

    for sample in sorted(samples, key=lambda s: (s.time_ms, s.id)):
        b_key, b_label = _bucket_key_for_sample(
            sample,
            window_kind=window_kind,
            window_seconds=window_seconds,
            tz=tz,
            clock_id=clock_id,
            phase_bins=phase_bins,
            lat=lat,
            lon=lon,
        )
        ctx_key = make_context_key(sample.context)
        g_key = (sample.entity_id, sample.property_name, ctx_key, b_key)
        if g_key not in groups:
            groups[g_key] = (b_label, [])
        groups[g_key][1].append(sample)

    results: list[tuple[RollupBucket, StateCreate]] = []
    for (ent_id, prop_name, ctx_key, b_key), (b_label, items) in groups.items():
        ctx_dict = dict(ctx_key)
        out_unit = normalize_unit_symbol(target_unit)
        if not out_unit:
            for item in items:
                u = normalize_unit_symbol(item.value_unit)
                if u:
                    out_unit = u
                    break

        vals: list[float] = []
        sigmas: list[float] = []
        times: list[int] = []
        sources: list[str] = []
        raw_ev_refs: list[EvidenceRef] = []
        seen_raw_refs: set[str] = set()

        for item in items:
            v_mean, v_sig = convert_gaussian(item.value_num, item.value_sigma, item.value_unit, out_unit)
            vals.append(v_mean)
            sigmas.append(float(v_sig or 0.0))
            times.append(item.time_ms)
            if item.measurement_source and item.measurement_source not in sources:
                sources.append(item.measurement_source)
            if item.evidence_ref and item.evidence_ref not in seen_raw_refs:
                seen_raw_refs.add(item.evidence_ref)
                raw_ev_refs.append(
                    EvidenceRef(kind="raw_source", ref=item.evidence_ref, label=f"Rohbeleg {b_label}")
                )

        n = len(vals)
        mean_val = math.fsum(vals) / n
        if n > 1:
            sample_std = math.sqrt(math.fsum((v - mean_val) ** 2 for v in vals) / (n - 1))
        else:
            sample_std = 0.0
        rms_sensor_sig = math.sqrt(math.fsum(s * s for s in sigmas) / n)
        combined_sigma = math.hypot(sample_std, rms_sensor_sig)

        min_val = min(vals)
        max_val = max(vals)
        start_ms = min(times)
        end_ms = max(times)
        mid_ms = (start_ms + end_ms) // 2
        half_span_s = max(0.0, (end_ms - start_ms) / 2000.0)

        slope_per_hour: float | None = None
        if n >= 2 and end_ms > start_ms:
            t_hours = [(t - mid_ms) / 3_600_000.0 for t in times]
            mean_th = math.fsum(t_hours) / n
            denom = math.fsum((th - mean_th) ** 2 for th in t_hours)
            if denom > 1e-12:
                numer = math.fsum((th - mean_th) * (v - mean_val) for th, v in zip(t_hours, vals))
                slope_per_hour = numer / denom

        sample_ids = [item.id for item in items]
        batch_ref = EvidenceRef(
            kind="raw_samples",
            ref=f"{ent_id}:{prop_name}:{b_key}",
            label=f"Roll-Up ({n} Rohwerte, {b_label})",
        )
        all_refs = [batch_ref, *raw_ev_refs]
        meas_src = ",".join(sources) if sources else "raw_rollup"

        bucket = RollupBucket(
            bucket_key=b_key,
            window_kind=window_kind,
            entity_id=ent_id,
            property_name=prop_name,
            sample_ids=sample_ids,
            sample_count=n,
            start_ms=start_ms,
            end_ms=end_ms,
            mean=mean_val,
            sigma=combined_sigma,
            min_value=min_val,
            max_value=max_val,
            slope_per_hour=slope_per_hour,
            unit=out_unit,
            context=ctx_dict,
            measurement_source=meas_src,
            evidence_refs=all_refs,
        )

        unit_suffix = f" {out_unit}" if out_unit else ""
        rollup_meta: dict[str, Any] = {
            "rollup": {
                "bucket_key": b_key,
                "window_kind": window_kind,
                "n": n,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "min": min_val,
                "max": max_val,
                "slope_per_hour": slope_per_hour,
                "sample_ids": sample_ids,
            }
        }
        state_payload = StateCreate(
            entity_id=ent_id,
            modality="observed",
            source="measured",
            measurement_source=meas_src,
            property_name=prop_name,
            label=f"{prop_name} ({b_label}: {mean_val:.2f}{unit_suffix}, n={n})",
            value_num=mean_val,
            value_sigma=combined_sigma,
            value_unit=out_unit,
            time_ms=mid_ms,
            sigma_seconds=half_span_s if half_span_s > 0.0 else None,
            context=ctx_dict,
            evidence_refs=all_refs,
            meta=rollup_meta,
        )
        results.append((bucket, state_payload))

    return results
