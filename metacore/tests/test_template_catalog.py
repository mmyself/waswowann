"""Tests for scientific nomenclature indexing, aliases, playbook hierarchy & context matching."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from metacore.config import Settings
from metacore.db import migrate
from metacore.mcp_server import create_server
from metacore.models import EntityCreate, StateCreate, TransformationCreate
from metacore.repository import MetaCoreRepository
from metacore.templates import search_template_catalog


def _decode_tool(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw:
        text = getattr(raw[0], "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError(f"Unexpected MCP output: {raw!r}")


def test_nomenclature_aliases_and_umlaut_search(settings: Settings) -> None:
    migrate(settings)
    repo = MetaCoreRepository(settings)
    repo.create_transformation(
        TransformationCreate(
            name="Igel-Stachelbart Fruchtung",
            modality="template",
            driver="nature",
            duration_seconds=900_000.0,
            duration_sigma_seconds=86_400.0,
            duration_source="measured",
            formula={"output_property": "harvest_ready", "output_value": 136.0, "output_unit": "g"},
            meta={
                "domain": "pilzzucht",
                "category": "Pilz",
                "scientific_name": "Hericium erinaceus",
                "taxon_path": ["Fungi", "Basidiomycota", "Hericium", "Hericium erinaceus"],
                "aliases": ["Igelstachelbart", "Löwenmähne", "Lion's Mane"],
            },
        )
    )
    repo.create_transformation(
        TransformationCreate(
            name="Wolf-Fährten-Monitor",
            modality="template",
            driver="nature",
            meta={
                "domain": "tierwelt",
                "category": "Tier",
                "scientific_name": "Canis lupus",
                "taxon_path": ["Animalia", "Chordata", "Mammalia", "Canis", "Canis lupus"],
                "aliases": ["Wolf", "Grauwolf"],
            },
        )
    )

    # Folded umlauts & hyphen tolerance
    for q in ("Igelstachelbart", "loewenmaehne", "Hericium erinaceus", "Fungi"):
        res = search_template_catalog(repo, query=q)
        assert res["count"] == 1
        assert res["templates"][0]["profile"]["scientific_name"] == "Hericium erinaceus"

    # Scientific filter on zoology
    wolf = search_template_catalog(repo, scientific_name="Canis lupus")
    assert wolf["count"] == 1
    assert wolf["templates"][0]["profile"]["domain"] == "tierwelt"


def test_playbook_hierarchy_maturity_ranking_and_entity_matching(settings: Settings) -> None:
    migrate(settings)
    repo = MetaCoreRepository(settings)

    # Playbook root + 2 steps
    pb = repo.create_transformation(
        TransformationCreate(
            id="tpl:pb_hericium",
            name="Igel-Stachelbart 2. Schub Playbook",
            modality="template",
            driver="actor",
            meta={
                "domain": "pilzzucht",
                "category": "Pilz",
                "scientific_name": "Hericium erinaceus",
                "aliases": ["Igelstachelbart", "Löwenmähne"],
                "template_kind": "playbook",
                "observations_count": 3,
                "applies_to": {
                    "entity_categories": ["Pilz"],
                    "scientific_names": ["Hericium erinaceus"],
                    "required_properties": ["block_phase"],
                },
            },
        )
    )
    repo.create_transformation(
        TransformationCreate(
            id="tpl:pb_step1",
            name="Kälteschock im Kühlschrank",
            modality="template",
            driver="actor",
            parent_id=pb.id,
            duration_seconds=86_400.0,
            meta={"domain": "pilzzucht", "category": "Pilz", "scientific_name": "Hericium erinaceus"},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            id="tpl:pb_step2",
            name="2. Schub Fruchtung",
            modality="template",
            driver="nature",
            parent_id=pb.id,
            duration_seconds=900_000.0,
            duration_source="measured",
            formula={"output_property": "harvest_ready", "output_value": 85.0, "output_unit": "g"},
            meta={"domain": "pilzzucht", "category": "Pilz", "scientific_name": "Hericium erinaceus"},
        )
    )

    # Lower maturity & archived templates
    repo.create_transformation(
        TransformationCreate(
            id="tpl:generic_mushroom",
            name="Allgemeiner Pilz-Tipp",
            modality="template",
            driver="nature",
            meta={"domain": "natur", "category": "Pilz"},
        )
    )
    repo.create_transformation(
        TransformationCreate(
            id="tpl:old_archived",
            name="Veraltetes Hericium-Setup",
            modality="template",
            driver="actor",
            meta={"domain": "pilzzucht", "category": "Pilz", "scientific_name": "Hericium erinaceus", "archived": True},
        )
    )

    # Root-only search finds playbook by child step name and inherits expected output
    by_child = search_template_catalog(repo, query="Kälteschock", only_roots=True)
    assert by_child["count"] == 1
    assert by_child["templates"][0]["template"]["id"] == "tpl:pb_hericium"
    assert len(by_child["templates"][0]["children"]) == 2
    assert by_child["templates"][0]["profile"]["maturity"] == "proven"
    assert by_child["templates"][0]["profile"]["produces_property"] == "harvest_ready"
    assert by_child["templates"][0]["profile"]["expected_output_num"] == 85.0

    # Entity matching prioritizes exact binomial species match over generic category match
    ent = repo.create_entity(
        EntityCreate(
            id="ent:my_block",
            name="Mein Kulturblock (Hericium erinaceus)",
            kind="worldline",
            category="Pilz",
        )
    )
    repo.create_state(
        StateCreate(
            entity_id=ent.id,
            modality="observed",
            source="measured",
            property_name="block_phase",
            label="Abgeerntet",
            value_num=1.0,
        )
    )

    matched = search_template_catalog(repo, only_roots=True, applies_to_entity_id=ent.id)
    ids = [item["template"]["id"] for item in matched["templates"]]
    assert ids == ["tpl:pb_hericium", "tpl:generic_mushroom"]
    assert matched["templates"][0]["match_score"] > matched["templates"][1]["match_score"]
    assert any("Wissenschaftlicher Name" in r for r in matched["templates"][0]["match_reasons"])

    # Archived excluded by default, included when requested
    with_arch = search_template_catalog(repo, only_roots=True, include_archived=True)
    assert any(item["template"]["id"] == "tpl:old_archived" for item in with_arch["templates"])


def test_mcp_define_and_search_with_scientific_name(settings: Settings) -> None:
    server = create_server(settings)

    async def flow() -> None:
        defined = _decode_tool(
            await server.call_tool(
                "define_template",
                {
                    "name": "Austernseitling Stroh-Beutel",
                    "driver": "actor",
                    "category": "Pilz",
                    "domain": "pilzzucht",
                    "scientific_name": "Pleurotus ostreatus",
                    "aliases": ["Austernpilz", "Oyster Mushroom"],
                    "template_kind": "step",
                    "duration_seconds": 14 * 86_400,
                },
            )
        )
        assert defined["profile"]["scientific_name"] == "Pleurotus ostreatus"

        found = _decode_tool(
            await server.call_tool(
                "search_templates",
                {"scientific_name": "Pleurotus", "domain": "pilzzucht"},
            )
        )
        assert found["count"] == 1
        assert "Oyster Mushroom" in found["templates"][0]["profile"]["aliases"]

    asyncio.run(flow())
