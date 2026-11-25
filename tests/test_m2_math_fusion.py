"""Tests for Milestone M2: Gaussian fusion, habitat generalization, propagation, and goal probability."""

from __future__ import annotations

import math

import pytest

from metacore.math_fusion import (
    GaussianValue,
    GeoPoint,
    aggregate_series,
    calibrate_parameter,
    combine_goal_probability,
    compute_modifier_factor,
    fuse_1d,
    fuse_locations,
    generalize_habitat,
    geo_distance_meters,
    probability_in_bounds,
    probability_within_radius,
    propagate_linear_combination,
    propagate_product,
    propagate_ratio,
    propagate_split,
    propagate_sum,
)


def test_fuse_1d_independent_precision_and_same_source_dedup() -> None:
    a = GaussianValue(mean=100.0, sigma=10.0, source="measured", measurement_source="Waage_A")
    b = GaussianValue(mean=100.0, sigma=10.0, source="measured", measurement_source="Waage_B")
    fused = fuse_1d([a, b])
    assert fused.mean == pytest.approx(100.0)
    assert fused.sigma == pytest.approx(10.0 / math.sqrt(2.0))

    # Precision weighting favors tighter sigma
    tight = GaussianValue(mean=10.0, sigma=1.0, measurement_source="S1")
    loose = GaussianValue(mean=20.0, sigma=3.0, measurement_source="S2")
    weighted = fuse_1d([tight, loose])
    assert weighted.mean == pytest.approx((10.0 * 9.0 + 20.0 * 1.0) / 10.0)
    assert weighted.sigma == pytest.approx(1.0 / math.sqrt(1.0 + 1.0 / 9.0))

    # Repeated identical values from the SAME source must NOT shrink sigma
    same_src = [
        GaussianValue(mean=100.0, sigma=10.0, measurement_source="Waage_A") for _ in range(5)
    ]
    deduped = fuse_1d(same_src)
    assert deduped.mean == pytest.approx(100.0)
    assert deduped.sigma == pytest.approx(10.0)

    # Exact measurement (sigma == 0) dominates
    exact = GaussianValue(mean=42.0, sigma=0.0, source="measured", measurement_source="Exakt")
    res_exact = fuse_1d([a, exact])
    assert res_exact.mean == pytest.approx(42.0)
    assert res_exact.sigma == 0.0


def test_fuse_locations_and_habitat_generalization() -> None:
    p1 = GeoPoint(lat=50.8120, lon=12.9140, sigma_meters=12.0, measurement_source="GPS_1")
    p2 = GeoPoint(lat=50.8120, lon=12.9140, sigma_meters=12.0, measurement_source="GPS_2")
    fused = fuse_locations([p1, p2])
    assert fused.lat == pytest.approx(50.8120)
    assert fused.lon == pytest.approx(12.9140)
    assert fused.sigma_meters == pytest.approx(12.0 / math.sqrt(2.0))

    # Same phone GPS repeated 4 times must not shrink sigma
    repeated = [
        GeoPoint(lat=50.8120, lon=12.9140, sigma_meters=12.0, measurement_source="Handy_GPS")
        for _ in range(4)
    ]
    deduped = fuse_locations(repeated)
    assert deduped.sigma_meters == pytest.approx(12.0)

    # Two Riesenschirmling finds ~180 m apart -> broad habitat area (100..150 m)
    find1 = GeoPoint(lat=50.8120, lon=12.9140, sigma_meters=12.0, measurement_source="Foto_1")
    # ~180 m east at lat 50.812 (1 deg lon ≈ 70_275 m -> 180 m ≈ 0.00256 deg)
    find2 = GeoPoint(lat=50.8120, lon=12.91656, sigma_meters=15.0, measurement_source="Foto_2")
    dist = geo_distance_meters(find1.lat, find1.lon, find2.lat, find2.lon)
    assert 175.0 <= dist <= 185.0

    habitat = generalize_habitat([find1, find2])
    assert find1.lon < habitat.lon < find2.lon
    assert 100.0 <= habitat.sigma_meters <= 150.0


def test_error_propagation_koernerbrut_400g() -> None:
    dry = GaussianValue(mean=400.0, sigma=0.0, source="measured", measurement_source="Waage")
    swell_factor = GaussianValue(mean=1.75, sigma=0.10, source="ai_prior")

    hydrated = propagate_product(dry, swell_factor)
    assert hydrated.mean == pytest.approx(700.0)
    assert hydrated.sigma == pytest.approx(40.0)
    assert hydrated.source == "derived"

    bag = propagate_split(hydrated, parts=2, split_sigma=6.0)
    assert bag.mean == pytest.approx(350.0)
    assert bag.sigma == pytest.approx(math.hypot(20.0, 6.0))

    total_sum = propagate_sum([bag, bag])
    assert total_sum.mean == pytest.approx(700.0)
    assert total_sum.sigma == pytest.approx(bag.sigma * math.sqrt(2.0))


def test_calibrate_parameter_ai_prior_to_measured() -> None:
    prior = GaussianValue(mean=1.75, sigma=0.10, source="ai_prior")

    unchanged = calibrate_parameter(prior, [])
    assert unchanged.source == "ai_prior"
    assert unchanged.mean == pytest.approx(1.75)
    assert unchanged.sigma == pytest.approx(0.10)
    assert unchanged.observation_count == 0

    real_obs = [
        GaussianValue(mean=1.70, sigma=0.03, source="measured", measurement_source="Waage_Kueche")
    ]
    calibrated = calibrate_parameter(prior, real_obs)
    assert calibrated.source == "measured"
    assert calibrated.observation_count == 1
    assert 1.70 < calibrated.mean < 1.71
    assert calibrated.sigma < 0.03


def test_goal_probabilities_1d_2d_and_combined() -> None:
    pred = GaussianValue(mean=100.0, sigma=10.0, source="derived")
    assert probability_in_bounds(pred, target_min=90.0, target_max=110.0) == pytest.approx(
        0.682689, abs=1e-4
    )
    assert probability_in_bounds(pred, target_min=80.0, target_max=120.0) == pytest.approx(
        0.954499, abs=1e-4
    )
    assert probability_in_bounds(pred, target_max=100.0) == pytest.approx(0.5, abs=1e-6)

    exact = GaussianValue(mean=700.0, sigma=0.0, source="measured")
    assert probability_in_bounds(exact, target_min=600.0, target_max=900.0) == 1.0
    assert probability_in_bounds(exact, target_min=800.0) == 0.0

    # 2D isotropic Gaussian at distance 0: R = 1.515 * sigma ≈ 68.3 %
    center = GeoPoint(lat=50.812, lon=12.914, sigma_meters=20.0)
    p_zero = probability_within_radius(center, 50.812, 12.914, 1.515 * 20.0)
    assert p_zero == pytest.approx(0.6826, abs=2e-3)

    # Shifted target reduces probability monotonically
    p_near = probability_within_radius(center, 50.8122, 12.914, 30.0)
    p_far = probability_within_radius(center, 50.8130, 12.914, 30.0)
    assert p_zero > p_near > p_far > 0.0

    combined = combine_goal_probability([0.9, 0.8], path_success_prob=0.85)
    assert combined == pytest.approx(0.612)


def test_linear_combination_ratio_series_and_modifiers() -> None:
    terms = [
        (GaussianValue(500.0, 5.0, source="measured"), GaussianValue(1.0, 0.0)),
        (GaussianValue(750.0, 10.0, source="measured"), GaussianValue(1.0, 0.0)),
        (GaussianValue(150.0, 5.0, source="measured"), GaussianValue(1.0, 0.0)),
    ]
    mix = propagate_linear_combination(terms)
    assert mix.mean == pytest.approx(1400.0)
    assert mix.sigma == pytest.approx(math.sqrt(25.0 + 100.0 + 25.0))
    assert mix.source == "derived"

    # Ratio propagation A / B
    ratio = propagate_ratio(
        GaussianValue(750.0, 10.0, source="measured"),
        GaussianValue(500.0, 5.0, source="measured"),
    )
    assert ratio.mean == pytest.approx(1.5)
    expected_ratio_sigma = math.hypot(10.0 / 500.0, 750.0 * 5.0 / (500.0**2))
    assert ratio.sigma == pytest.approx(expected_ratio_sigma)

    with pytest.raises(ValueError):
        propagate_ratio(GaussianValue(10.0, 1.0), GaussianValue(0.0, 1.0))

    # Time-series aggregations
    series = [
        GaussianValue(17.0, 0.5, source="measured"),
        GaussianValue(15.0, 0.5, source="measured"),
        GaussianValue(11.0, 0.5, source="measured"),
    ]
    assert aggregate_series(series, "latest").mean == pytest.approx(11.0)
    assert aggregate_series(series, "min").mean == pytest.approx(11.0)
    assert aggregate_series(series, "max").mean == pytest.approx(17.0)
    assert aggregate_series(series, "sum").mean == pytest.approx(43.0)
    assert aggregate_series(series, "mean").mean == pytest.approx(43.0 / 3.0)
    delta = aggregate_series(series, "delta")
    assert delta.mean == pytest.approx(-6.0)
    assert delta.sigma == pytest.approx(math.hypot(0.5, 0.5))

    # Modifier factors: optimal corridor vs cool penalty
    opt = compute_modifier_factor(
        GaussianValue(21.0, 0.5),
        optimal_min=18.0,
        optimal_max=24.0,
        falloff_per_unit=0.05,
        min_factor=0.5,
        sigma_penalty_per_unit=0.01,
    )
    assert opt.mean == pytest.approx(1.0)
    cool = compute_modifier_factor(
        GaussianValue(14.0, 0.5),
        optimal_min=18.0,
        optimal_max=24.0,
        falloff_per_unit=0.05,
        min_factor=0.5,
        sigma_penalty_per_unit=0.01,
    )
    assert cool.mean == pytest.approx(0.8)
    assert cool.sigma == pytest.approx(0.01 * 4.0 + 0.05 * 0.5)
