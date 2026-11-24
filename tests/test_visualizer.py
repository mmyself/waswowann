"""Tests for real engine snapshot export and Zukunftsgraph HTML rendering."""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

import pytest

from metacore.visualizer import build_demo_scenarios_payload, render_html


def test_build_demo_scenarios_payload_real_engine_metrics() -> None:
    payload = build_demo_scenarios_payload()
    assert payload["scenario_order"] == [
        "koernerbrut",
        "riesenschirmling",
        "roboter_wasser",
        "igel_stachelbart",
    ]
    scenarios = payload["scenarios"]

    # 1. Körnerbrut calibration & incubator release
    kb = scenarios["koernerbrut"]["phases"]
    assert len(kb) == 3
    ent_map0 = {e["id"]: e for e in kb[0]["entities"]}
    kb_totals0 = [
        s
        for s in kb[0]["states"]
        if s["property_name"] == "mass"
        and s["modality"] == "predicted"
        and "Beutel" not in ent_map0[s["entity_id"]]["name"]
    ]
    assert len(kb_totals0) == 1
    assert kb_totals0[0]["value_num"] == pytest.approx(700.0)
    assert kb_totals0[0]["value_sigma"] == pytest.approx(40.0)
    assert kb[0]["goal_evaluations"][0]["stage"] == 2
    assert kb[0]["templates"][0]["factor"]["source"] == "ai_prior"

    ent_map1 = {e["id"]: e for e in kb[1]["entities"]}
    kb_bags1 = [
        s
        for s in kb[1]["states"]
        if s["property_name"] == "mass"
        and s["modality"] == "predicted"
        and "Beutel" in ent_map1[s["entity_id"]]["name"]
    ]
    assert len(kb_bags1) == 2
    assert all(b["value_num"] == pytest.approx(340.0) for b in kb_bags1)
    assert all(b["value_sigma"] == pytest.approx(math.hypot(5.0, 6.0)) for b in kb_bags1)
    assert kb[1]["templates"][0]["factor"]["source"] == "measured"
    assert len(kb[1]["open_ai_prior_templates"]) < len(kb[0]["open_ai_prior_templates"])

    assert kb[2]["goal_evaluations"][0]["status"] == "cancelled"
    kb_uses = [e for e in kb[2]["edges"] if e["role"] == "uses"]
    assert kb_uses and all(e["resource_released"] for e in kb_uses)

    # 2. Riesenschirmling derived habitat, Stage-1 trigger, frost Stage 4, zero-observation
    rs = scenarios["riesenschirmling"]["phases"]
    assert len(rs) == 3
    habitats = [e for e in rs[0]["entities"] if e["source"] == "derived"]
    assert len(habitats) == 1
    assert 100.0 <= habitats[0]["meta"]["habitat_sigma_meters"] <= 150.0
    assert rs[0]["goal_evaluations"][0]["stage"] == 2
    assert rs[1]["last_feedback"] is not None
    assert len(rs[1]["last_feedback"]["triggered_template_ids"]) >= 1
    assert rs[2]["goal_evaluations"][0]["stage"] == 4
    assert "vorbedingung" in rs[2]["goal_evaluations"][0]["broken_dimensions"]
    zero_obs = [
        s
        for s in rs[2]["states"]
        if s["property_name"] == "presence" and s["modality"] == "observed" and s["value_num"] == 0.0
    ]
    assert len(zero_obs) == 1

    # 3. Roboter R1 conflict, branch switch, locked door Stage 4, clean abort
    rw = scenarios["roboter_wasser"]["phases"]
    assert len(rw) == 4
    assert len(rw[0]["uses_conflicts"]) == 1
    assert rw[0]["goal_evaluations"][0]["stage"] == 2
    sideboard_edges = [e for e in rw[1]["edges"] if e["branch_key"] == "sideboard"]
    arm2_edges = [e for e in rw[1]["edges"] if e["branch_key"] == "arm2"]
    assert any(e["outcome"] == "chosen" for e in sideboard_edges)
    assert any(e["outcome"] == "not_chosen" for e in arm2_edges)
    assert rw[2]["goal_evaluations"][0]["stage"] == 4
    assert "vorbedingung" in rw[2]["goal_evaluations"][0]["broken_dimensions"]
    assert rw[3]["goal_evaluations"][0]["status"] == "cancelled"
    assert rw[3]["uses_conflicts"] == []

    # 4. Igel-Stachelbart indoor deduplication & real harvest duration calibration
    ig = scenarios["igel_stachelbart"]["phases"]
    assert len(ig) == 2
    assert len(ig[0]["entity_geo_fusions"]) == 1
    assert ig[0]["entity_geo_fusions"][0]["observation_count"] == 4
    assert ig[0]["entity_geo_fusions"][0]["sigma_meters"] == pytest.approx(12.0)
    assert ig[0]["templates"][0]["duration_source"] == "ai_prior"
    assert ig[1]["templates"][0]["duration_source"] == "measured"
    dur = ig[1]["templates"][0]["duration_seconds"]
    assert dur is not None and 8.5 * 86_400 <= dur <= 9.2 * 86_400


def test_render_html_valid_and_js_syntax_passes_node_check(tmp_path: Path) -> None:
    payload = build_demo_scenarios_payload()
    html = render_html(payload)

    assert html.startswith("<!DOCTYPE html>")
    assert "WoWannWas" in html
    assert "Zukunftsgraph" in html
    assert "MetaCore" in html
    assert "Igel-Stachelbart" in html
    assert "Riesenschirmling" in html
    assert "Greifer 1" in html

    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert len(scripts) == 1
    js_file = tmp_path / "zukunftsgraph_bundle.js"
    js_file.write_text(scripts[0], encoding="utf-8")

    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
