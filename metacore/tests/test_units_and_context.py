"""Tests for Package 1: domain-agnostic unit normalization & measurement context isolation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from metacore.config import Settings
from metacore.engine import MetaCoreEngine
from metacore.math_fusion import (
    GaussianValue,
    aggregate_series,
    fuse_1d,
    make_context_key,
    propagate_linear_combination,
    propagate_sum,
)
from metacore.mcp_server import create_server
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository
from metacore.units import (
    UnitCompatibilityError,
    are_units_compatible,
    convert_gaussian,
    convert_sigma,
    convert_to_canonical,
    convert_value,
    get_unit_dimension,
    normalize_unit_symbol,
)


def _decode_tool_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw:
        text = getattr(raw[0], "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError(f"Unexpected MCP tool payload: {type(raw)}")


def test_units_registry_and_affine_temperature_conversion() -> None:
    assert normalize_unit_symbol("mL") == "ml"
    assert normalize_unit_symbol("L") == "l"
    assert normalize_unit_symbol("degC") == "°C"
    assert get_unit_dimension("kg") == "mass"
    assert get_unit_dimension("°C") == "temperature"
    assert are_units_compatible("kg", "g")
    assert are_units_compatible("", "kg")
    assert are_units_compatible("beutel", "beutel")
    assert not are_units_compatible("kg", "°C")
    assert not are_units_compatible("kg", "beutel")

    # Mass & volume linear conversions
    assert convert_value(1.5, "kg", "g") == pytest.approx(1500.0)
    assert convert_value(250.0, "ml", "l") == pytest.approx(0.25)
    assert convert_sigma(0.05, "kg", "g") == pytest.approx(50.0)

    # Temperature affine mean vs linear sigma (NEVER add 273.15 to sigma!)
    mean_k, sig_k = convert_gaussian(20.0, 0.5, "°C", "K")
    assert mean_k == pytest.approx(293.15)
    assert sig_k == pytest.approx(0.5)

    mean_f, sig_f = convert_gaussian(20.0, 1.0, "°C", "°F")
    assert mean_f == pytest.approx(68.0)
    assert sig_f == pytest.approx(1.8)

    can_m, can_s, can_u = convert_to_canonical(2.5, 0.1, "kg")
    assert (can_m, can_s, can_u) == (pytest.approx(2500.0), pytest.approx(100.0), "g")

    with pytest.raises(UnitCompatibilityError):
        convert_value(10.0, "kg", "°C")
    with pytest.raises(UnitCompatibilityError):
        convert_value(10.0, "kg", "beutel")


def test_fuse_1d_unit_conversion_and_context_conflict() -> None:
    ctx_rest = make_context_key({"activity": "resting"})
    ctx_run = make_context_key({"activity": "running"})

    # Compatible units fuse cleanly with inverse-variance weighting
    fused = fuse_1d(
        [
            GaussianValue(1.5, 0.05, source="measured", measurement_source="scale_a", unit="kg", context=ctx_rest),
            GaussianValue(1520.0, 20.0, source="measured", measurement_source="scale_b", unit="g", context=ctx_rest),
        ],
        target_unit="g",
    )
    assert fused.unit == "g"
    assert fused.context == ctx_rest
    assert fused.mean == pytest.approx(1517.24, rel=1e-3)
    assert fused.sigma < 20.0

    # Mixed contexts raise ValueError when require_same_context=True
    with pytest.raises(ValueError, match="Kontext-Konflikt"):
        fuse_1d(
            [
                GaussianValue(42.0, 1.0, unit="bpm", context=ctx_rest),
                GaussianValue(98.0, 2.0, unit="bpm", context=ctx_run),
            ]
        )

    # Incompatible units fail explicitly
    with pytest.raises(UnitCompatibilityError):
        propagate_sum([GaussianValue(1.0, 0.1, unit="kg"), GaussianValue(20.0, 0.5, unit="°C")])

    # Linear combination & series aggregation normalize compatible units
    lin = propagate_linear_combination(
        [
            (GaussianValue(0.5, 0.01, unit="kg"), GaussianValue(1.0, 0.0)),
            (GaussianValue(500.0, 5.0, unit="g"), GaussianValue(1.0, 0.0)),
        ],
        target_unit="g",
    )
    assert lin.unit == "g"
    assert lin.mean == pytest.approx(1000.0)

    max_val = aggregate_series(
        [
            GaussianValue(500.0, 5.0, unit="g"),
            GaussianValue(0.9, 0.02, unit="kg"),
        ],
        aggregation="max",
    )
    assert max_val.unit == "g"
    assert max_val.mean == pytest.approx(900.0)


def test_engine_cross_unit_goal_recipe_ratio_and_context_isolation(
    repo: MetaCoreRepository,
) -> None:
    engine = MetaCoreEngine(repo)
    rabbit = repo.create_entity(EntityCreate(name="Häschen_Flocke", kind="worldline", category="tier"))
    t0 = 1_796_000_000_000

    # Context-separated mass measurements on the same entity
    dry_bowl = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            measurement_source="scale_1",
            property_name="bowl_mass",
            label="Napf leer",
            value_num=250.0,
            value_sigma=2.0,
            value_unit="g",
            context={"phase": "before_fill"},
            time_ms=t0,
        )
    )
    assert dry_bowl.context == {"phase": "before_fill"}

    repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            measurement_source="scale_2",
            property_name="bowl_mass",
            label="Napf voll (kg)",
            value_num=0.65,
            value_sigma=0.01,
            value_unit="kg",
            context={"phase": "after_fill"},
            time_ms=t0 + 60_000,
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            measurement_source="scale_3",
            property_name="bowl_mass",
            label="Napf voll (g)",
            value_num=660.0,
            value_sigma=10.0,
            value_unit="g",
            context={"phase": "after_fill"},
            time_ms=t0 + 120_000,
        )
    )

    # Without context_filter, fuse_property_states refuses to mix before_fill and after_fill
    with pytest.raises(ValueError, match="Kontext-Konflikt"):
        engine.fuse_property_states(rabbit.id, "bowl_mass", target_unit="g")

    # With context_filter, it fuses 0.65 kg and 660 g cleanly in grams
    fused_full = engine.fuse_property_states(
        rabbit.id,
        "bowl_mass",
        context_filter={"phase": "after_fill"},
        target_unit="g",
    )
    assert fused_full.unit == "g"
    assert fused_full.mean == pytest.approx(655.0)

    # Context-scoped goal in grams evaluated against kg observation
    goal = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="desired",
            source="user_goal",
            property_name="bowl_mass",
            label="Napf voll >= 600 g",
            value_unit="g",
            target_min=600.0,
            target_max=800.0,
            context={"phase": "after_fill"},
        )
    )
    ev = engine.evaluate_goal(goal.id)
    assert ev.stage == 2
    assert ev.goal_probability > 0.99

    # Dimension-incompatible goal fails evaluation safely with p_qty = 0
    bad_goal = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="desired",
            source="user_goal",
            property_name="bowl_mass",
            label="Temperaturziel auf Masse",
            value_unit="°C",
            target_min=18.0,
            target_max=25.0,
            context={"phase": "after_fill"},
        )
    )
    bad_ev = engine.evaluate_goal(bad_goal.id)
    assert bad_ev.stage == 4
    assert bad_ev.dimension_probs["wie_viel"] == 0.0

    # Ratio guard & recipe_mix across units + trigger context matching
    water_in = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            property_name="water_added",
            label="Wasser 0.3 l",
            value_num=0.3,
            value_sigma=0.01,
            value_unit="l",
            time_ms=t0,
        )
    )
    pellets_in = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            property_name="pellets_mass",
            label="Pellets 0.5 kg",
            value_num=0.5,
            value_sigma=0.01,
            value_unit="kg",
            time_ms=t0,
        )
    )
    mix_tpl = repo.create_transformation(
        TransformationCreate(
            name="Ration anmischen",
            modality="template",
            driver="actor",
            duration_seconds=600.0,
            duration_source="measured",
            formula={
                "kind": "recipe_mix",
                "mode": "weighted_sum",
                "output_property": "ration_mass",
                "output_unit": "g",
                "ingredients": [
                    {"property_name": "pellets_mass", "weight": 1.0, "unit": "g"},
                    {"property_name": "water_added", "weight": 1.0, "unit": "ml"},
                ],
                "ratio_guards": [
                    {
                        "numerator_property": "pellets_mass",
                        "denominator_property": "bowl_mass",
                        "denominator_context": {"phase": "after_fill"},
                        "min_ratio": 0.5,
                        "max_ratio": 1.0,
                    }
                ],
                "triggers": {
                    "logic": "AND",
                    "conditions": [
                        {
                            "property_name": "bowl_mass",
                            "match_context": {"phase": "after_fill"},
                            "unit": "g",
                            "min_value": 600.0,
                        }
                    ],
                },
            },
        )
    )
    fb = engine.instantiate_template(
        mix_tpl.id,
        input_state_ids=[pellets_in.id, water_in.id],
        target_entity_id=rabbit.id,
        start_time_ms=t0,
    )
    pred = repo.get_state(fb.created_state_ids[0])
    assert pred is not None
    assert pred.value_unit == "g"
    # 0.5 kg -> 500 g + 0.3 l -> 300 ml (weighted sum = 800)
    assert pred.value_num == pytest.approx(800.0)

    ration_goal = repo.create_state(
        StateCreate(
            entity_id=rabbit.id,
            modality="desired",
            source="user_goal",
            property_name="ration_mass",
            label="Ration 750-850 g",
            value_unit="g",
            target_min=750.0,
            target_max=850.0,
        )
    )
    ration_ev = engine.evaluate_goal(ration_goal.id)
    assert ration_ev.stage == 2
    assert ration_ev.dimension_probs["vorbedingung"] > 0.99

    # Check trigger fires ONLY for matching context (not before_fill)
    trig_before = engine.record_observation(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            property_name="bowl_mass",
            label="700 g before_fill",
            value_num=700.0,
            value_unit="g",
            context={"phase": "before_fill"},
            time_ms=t0 + 180_000,
        )
    )
    assert mix_tpl.id not in trig_before.triggered_template_ids

    trig_after = engine.record_observation(
        StateCreate(
            entity_id=rabbit.id,
            modality="observed",
            source="measured",
            property_name="bowl_mass",
            label="0.72 kg after_fill",
            value_num=0.72,
            value_unit="kg",
            context={"phase": "after_fill"},
            time_ms=t0 + 240_000,
        )
    )
    assert mix_tpl.id in trig_after.triggered_template_ids

    # Periodicity report respects context_filter
    rep_after = engine.analyze_worldline_periods(
        entity_id=rabbit.id,
        property_name="bowl_mass",
        context_filter={"phase": "after_fill"},
    )
    assert rep_after.sample_count == 3


def test_mcp_records_context_on_observation_and_goal(settings: Settings) -> None:
    server = create_server(settings)

    async def run() -> None:
        obs = _decode_tool_result(
            await server.call_tool(
                "record_observation",
                {
                    "entity_id_or_name": "Gewächshaus_Nord",
                    "property_name": "temp",
                    "label": "Schatten 21 °C",
                    "value_num": 21.0,
                    "value_sigma": 0.5,
                    "value_unit": "°C",
                    "context": {"position": "shade"},
                },
            )
        )
        assert obs["observation_id"]
        goal = _decode_tool_result(
            await server.call_tool(
                "set_goal",
                {
                    "entity_id_or_name": "Gewächshaus_Nord",
                    "property_name": "temp",
                    "label": "Ziel 290-300 K",
                    "value_unit": "K",
                    "target_min": 290.0,
                    "target_max": 300.0,
                    "context": {"position": "shade"},
                },
            )
        )
        assert goal["evaluation"]["stage"] == 2
        assert goal["goal"]["context"] == {"position": "shade"}

    asyncio.run(run())
