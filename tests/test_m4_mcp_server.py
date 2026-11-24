"""Tests for Milestone M4: semantic FastMCP server and all 8 tools."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from metacore.config import Settings
from metacore.mcp_server import create_server


def _decode_tool_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw:
        text = getattr(raw[0], "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError(f"Unerwartetes MCP-Tool-Ergebnisformat: {type(raw)} -> {raw!r}")


def test_mcp_server_registers_all_8_tools_and_entrypoint_exists(settings: Settings) -> None:
    server = create_server(settings)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "get_focus_briefing",
        "inspect_worldline",
        "search_templates",
        "define_template",
        "instantiate_template",
        "record_observation",
        "set_goal",
        "resolve_escalation",
    }
    script_path = Path(sys.executable).parent / "wowannwas-mcp"
    assert script_path.exists()


def test_mcp_semantic_workflow_roundtrip(settings: Settings) -> None:
    server = create_server(settings)

    async def run_flow() -> None:
        # 1. Define template
        tpl_res = _decode_tool_result(
            await server.call_tool(
                "define_template",
                {
                    "name": "Weizen einweichen & teilen",
                    "driver": "actor",
                    "category": "rezept",
                    "duration_seconds": 3 * 86_400,
                    "duration_sigma_seconds": 3600.0,
                    "formula": {
                        "kind": "product_and_split",
                        "output_property": "mass",
                        "output_unit": "g",
                        "factor": {"mean": 1.75, "sigma": 0.10, "source": "ai_prior"},
                        "split_parts": 2,
                        "split_sigma": 6.0,
                    },
                },
            )
        )
        tpl_id = tpl_res["template"]["id"]

        # 2. Search templates
        found = _decode_tool_result(
            await server.call_tool("search_templates", {"query": "Weizen", "category": "rezept"})
        )
        assert found["count"] == 1
        assert found["templates"][0]["calibration_status"]["factor_source"] == "ai_prior"

        # 3. Record initial dry grain observation (auto-creates worldline)
        t0 = 1_795_000_000_000
        dry_obs = _decode_tool_result(
            await server.call_tool(
                "record_observation",
                {
                    "entity_id_or_name": "Weizencharge_M4",
                    "property_name": "mass",
                    "label": "400 g trocken",
                    "category": "rezept",
                    "measurement_source": "Waage_Kueche",
                    "value_num": 400.0,
                    "value_sigma": 0.0,
                    "value_unit": "g",
                    "time_ms": t0,
                },
            )
        )
        dry_id = dry_obs["observation_id"]

        # 4. Instantiate template with split into 2 bags
        inst = _decode_tool_result(
            await server.call_tool(
                "instantiate_template",
                {
                    "template_id": tpl_id,
                    "input_state_ids": [dry_id],
                    "target_entity_id_or_name": "Weizencharge_M4",
                    "ai_priors": {"factor": [1.75, 0.10]},
                    "split_entities": ["Beutel_1_M4", "Beutel_2_M4"],
                },
            )
        )
        assert len(inst["created_state_ids"]) == 3
        assert len(inst["open_ai_prior_state_ids"]) == 1

        # 5. Set goal on total mass
        goal_res = _decode_tool_result(
            await server.call_tool(
                "set_goal",
                {
                    "entity_id_or_name": "Weizencharge_M4",
                    "property_name": "mass",
                    "label": "Mindestens 600 g abgetropft",
                    "value_unit": "g",
                    "target_min": 600.0,
                },
            )
        )
        goal_id = goal_res["goal"]["id"]
        assert goal_res["evaluation"]["stage"] == 2

        # 6. Briefing shows open ai_prior and goal on track
        brief = _decode_tool_result(
            await server.call_tool("get_focus_briefing", {"horizon_days": 14})
        )
        assert len(brief["open_ai_priors"]) == 1
        assert brief["goal_evaluations"][0]["goal_id"] == goal_id

        # 7. Record real drained weight (680 g) -> calibrates template & closes open ai_prior
        weighed = _decode_tool_result(
            await server.call_tool(
                "record_observation",
                {
                    "entity_id_or_name": "Weizencharge_M4",
                    "property_name": "mass",
                    "label": "Gewogen 680 g",
                    "measurement_source": "Waage_Kueche",
                    "value_num": 680.0,
                    "value_sigma": 10.0,
                    "value_unit": "g",
                    "time_ms": t0 + 3 * 86_400 * 1000,
                },
            )
        )
        assert tpl_id in weighed["calibrated_template_ids"]
        assert weighed["open_ai_prior_state_ids"] == []

        # 8. Inspect worldline & resolve via abort_goal
        world = _decode_tool_result(
            await server.call_tool("inspect_worldline", {"entity_id_or_name": "Weizencharge_M4"})
        )
        assert len(world["history_observed"]) == 2
        assert len(world["child_entities"]) == 2

        aborted = _decode_tool_result(
            await server.call_tool(
                "resolve_escalation", {"goal_id": goal_id, "action": "abort_goal"}
            )
        )
        assert aborted["goal_evaluations"][0]["status"] == "cancelled"

    asyncio.run(run_flow())


def test_mcp_entrypoint_script_importable(settings: Settings) -> None:
    from metacore import mcp_server

    assert callable(mcp_server.main)
    with pytest.raises((ValueError, ToolError)):
        asyncio.run(create_server(settings).call_tool("get_focus_briefing", {"horizon_days": -1}))
