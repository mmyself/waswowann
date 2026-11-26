"""Domain-agnostic template catalog, scientific nomenclature indexing, playbook hierarchy & context matching."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from metacore.models import Entity, Transformation, TransformationDriver
from metacore.repository import MetaCoreRepository

TemplateKind = Literal["playbook", "step", "trigger", "model"]
TemplateMaturity = Literal["ai_prior", "calibrated", "proven", "archived"]

_MATURITY_RANK: dict[TemplateMaturity, int] = {
    "proven": 3,
    "calibrated": 2,
    "ai_prior": 1,
    "archived": 0,
}

_PAREN_SCI_RE = re.compile(r"\(([A-Z][a-z]+(?:\s+[a-z][a-z-]+){1,2})\)")


def normalize_search_text(text: str) -> str:
    """Fold German umlauts/eszett and punctuation into space-separated ASCII-friendly tokens."""
    folded = text.strip().lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        folded = folded.replace(src, dst)
    folded = re.sub(r"[-_/.,:;()\[\]{}'\"`´’]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _normalize_variants(text: str) -> tuple[str, str, str]:
    """Return (folded_spaced, compact_folded, raw_spaced) for fuzzy umlaut/hyphen matching."""
    raw_spaced = re.sub(r"[-_/.,:;()\[\]{}'\"`´’]+", " ", text.strip().lower())
    raw_spaced = re.sub(r"\s+", " ", raw_spaced).strip()
    folded = normalize_search_text(text)
    compact = folded.replace(" ", "")
    return folded, compact, raw_spaced


def _text_matches_needle(haystack: str, needle: str) -> bool:
    if not needle:
        return True
    n_fold, n_comp, n_raw = _normalize_variants(needle)
    h_fold, h_comp, h_raw = _normalize_variants(haystack)
    if n_fold and (n_fold in h_fold or (n_comp and n_comp in h_comp)):
        return True
    if n_raw and n_raw in h_raw:
        return True
    # Also match if user typed un-umlauted single letters ("lovenmahne" / "lwm")
    plain_n = n_raw.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    plain_h = h_raw.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    return bool(plain_n and plain_n in plain_h)


def _str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in val:
        cleaned = str(item).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _infer_template_kind(tpl: Transformation, child_templates: list[Transformation]) -> TemplateKind:
    explicit = tpl.meta.get("template_kind")
    if explicit in ("playbook", "step", "trigger", "model"):
        return explicit  # type: ignore[return-value]
    if child_templates:
        return "playbook"
    if "triggers" in tpl.formula:
        return "trigger"
    if tpl.formula.get("kind") in ("decay_step", "cumulative_balance", "linear_combination"):
        return "model"
    return "step"


def _infer_observations_count(tpl: Transformation) -> int:
    raw_count = tpl.meta.get("observations_count")
    if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
        return raw_count
    history = tpl.meta.get("calibration_history")
    if isinstance(history, list) and history:
        return len(history)
    factor_src = tpl.formula["factor"].get("source") if isinstance(tpl.formula.get("factor"), dict) else None
    output_src = tpl.formula.get("output_source")
    if tpl.duration_source == "measured" or factor_src == "measured" or output_src == "measured":
        return 1
    return 0


def _infer_maturity(tpl: Transformation, obs_count: int) -> TemplateMaturity:
    if tpl.meta.get("archived") is True or tpl.modality == "cancelled":
        return "archived"
    if obs_count >= 3:
        return "proven"
    if obs_count >= 1:
        return "calibrated"
    return "ai_prior"


def _normalize_applies_to(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "entity_categories": [],
            "scientific_names": [],
            "required_properties": [],
            "context": {},
        }
    ctx = raw.get("context")
    return {
        "entity_categories": _str_list(raw.get("entity_categories")),
        "scientific_names": _str_list(raw.get("scientific_names")),
        "required_properties": _str_list(raw.get("required_properties")),
        "context": {str(k): str(v) for k, v in ctx.items() if k and v} if isinstance(ctx, dict) else {},
    }


def extract_template_profile(
    tpl: Transformation,
    child_templates: list[Transformation] | tuple[Transformation, ...] = (),
) -> dict[str, Any]:
    """Build normalized taxonomy, nomenclature, maturity & I/O metadata for a template."""
    children = list(child_templates)
    meta = tpl.meta or {}
    formula = tpl.formula or {}

    category = str(meta.get("category") or "general").strip() or "general"
    domain = str(meta.get("domain") or category or "general").strip().lower() or "general"

    sci_raw = meta.get("scientific_name") or meta.get("species")
    scientific_name = str(sci_raw).strip() if isinstance(sci_raw, str) and sci_raw.strip() else None

    canon_raw = meta.get("canonical_name")
    canonical_name = (
        str(canon_raw).strip()
        if isinstance(canon_raw, str) and canon_raw.strip()
        else scientific_name
    )

    taxon_path = _str_list(meta.get("taxon_path"))
    aliases = _str_list(meta.get("aliases"))
    template_kind = _infer_template_kind(tpl, children)

    applies_to = _normalize_applies_to(meta.get("applies_to"))
    if scientific_name and scientific_name not in applies_to["scientific_names"]:
        applies_to["scientific_names"] = [scientific_name, *applies_to["scientific_names"]]

    produces_property = formula.get("output_property")
    produces_unit = str(formula.get("output_unit") or "")
    expected_output_num = formula.get("output_value")
    expected_output_sigma = formula.get("output_sigma")

    if not produces_property and children:
        for child in reversed(children):
            cf = child.formula or {}
            if cf.get("output_property"):
                produces_property = cf.get("output_property")
                produces_unit = str(cf.get("output_unit") or "")
                expected_output_num = cf.get("output_value")
                expected_output_sigma = cf.get("output_sigma")
                break

    total_duration_seconds = tpl.duration_seconds
    if total_duration_seconds is None and children:
        child_durs = [c.duration_seconds for c in children if c.duration_seconds is not None]
        if child_durs:
            total_duration_seconds = float(sum(child_durs))

    obs_count = _infer_observations_count(tpl)
    if obs_count == 0 and children:
        obs_count = max((_infer_observations_count(c) for c in children), default=0)
    maturity = _infer_maturity(tpl, obs_count)
    history = meta.get("calibration_history") if isinstance(meta.get("calibration_history"), list) else []
    onto_raw = meta.get("ontology_id")
    ontology_id = str(onto_raw).strip() if isinstance(onto_raw, str) and onto_raw.strip() else None
    raw_refs = meta.get("evidence_refs")
    evidence_refs = (
        [dict(r) for r in raw_refs if isinstance(r, dict) and r.get("ref")]
        if isinstance(raw_refs, list)
        else []
    )

    return {
        "domain": domain,
        "category": category,
        "scientific_name": scientific_name,
        "ontology_id": ontology_id,
        "canonical_name": canonical_name,
        "taxon_path": taxon_path,
        "aliases": aliases,
        "template_kind": template_kind,
        "applies_to": applies_to,
        "produces_property": str(produces_property) if produces_property else None,
        "produces_unit": produces_unit,
        "expected_output_num": expected_output_num,
        "expected_output_sigma": expected_output_sigma,
        "total_duration_seconds": total_duration_seconds,
        "observations_count": obs_count,
        "maturity": maturity,
        "calibration_history": history,
        "evidence_refs": evidence_refs,
        "child_count": len(children),
    }


def _entity_scientific_names(entity: Entity) -> list[str]:
    names: list[str] = []
    meta = entity.meta or {}
    for key in ("scientific_name", "species", "canonical_name"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            names.append(val.strip())
    for match in _PAREN_SCI_RE.findall(entity.name):
        if match.strip() not in names:
            names.append(match.strip())
    return names


def _score_entity_match(
    tpl: Transformation,
    profile: dict[str, Any],
    *,
    entity: Entity,
    entity_props: set[str],
    entity_contexts: dict[str, set[str]],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    ent_sci_names = _entity_scientific_names(entity)
    tpl_sci_names = list(profile["applies_to"]["scientific_names"])
    if profile["scientific_name"] and profile["scientific_name"] not in tpl_sci_names:
        tpl_sci_names.append(profile["scientific_name"])

    # 1. Scientific nomenclature match (botanical / zoological / canonical)
    matched_sci = False
    for e_sci in ent_sci_names:
        for t_sci in tpl_sci_names:
            if _text_matches_needle(e_sci, t_sci) or _text_matches_needle(t_sci, e_sci):
                score += 60.0
                reasons.append(f"Wissenschaftlicher Name ({t_sci})")
                matched_sci = True
                break
        if matched_sci:
            break

    # 2. Alias / name match against entity name
    for alias in [tpl.name, *profile["aliases"]]:
        if alias and (_text_matches_needle(entity.name, alias) or _text_matches_needle(alias, entity.name)):
            score += 35.0
            reasons.append(f"Name/Alias ({alias})")
            break

    # 3. Category match (via applies_to.entity_categories or meta.category)
    allowed_cats = list(profile["applies_to"]["entity_categories"])
    if not allowed_cats and profile["category"] and profile["category"] != "general":
        allowed_cats = [profile["category"]]
    if any(_text_matches_needle(entity.category, cat) for cat in allowed_cats):
        score += 20.0
        reasons.append(f"Kategorie ({entity.category})")

    # 4. Required property presence match
    req_props = profile["applies_to"]["required_properties"]
    matched_props = [p for p in req_props if p in entity_props]
    if matched_props:
        score += 15.0
        reasons.append(f"Passende Eigenschaft ({', '.join(matched_props)})")

    # 5. Context key=value match (e.g. environment=indoor/outdoor)
    req_ctx: dict[str, str] = profile["applies_to"]["context"]
    if req_ctx:
        all_match = True
        for ck, cv in req_ctx.items():
            ent_vals = set(entity_contexts.get(ck, set()))
            meta_val = (entity.meta or {}).get(ck)
            if isinstance(meta_val, str) and meta_val.strip():
                ent_vals.add(meta_val.strip().lower())
            if cv.lower() not in ent_vals:
                all_match = False
                break
        if all_match:
            score += 10.0
            reasons.append("Kontext passt")
        else:
            score = max(0.0, score - 25.0)

    return score, reasons


def _serialize_entry(
    tpl: Transformation,
    children: list[Transformation],
    *,
    match_score: float = 0.0,
    match_reasons: list[str] | None = None,
) -> dict[str, Any]:
    factor_src = (
        tpl.formula["factor"].get("source")
        if isinstance(tpl.formula.get("factor"), dict)
        else None
    )
    profile = extract_template_profile(tpl, children)
    return {
        "template": tpl.model_dump(),
        "calibration_status": {
            "factor_source": factor_src,
            "duration_source": tpl.duration_source,
        },
        "profile": profile,
        "children": [
            _serialize_entry(child, [])
            for child in sorted(children, key=lambda c: (c.name, c.created_at_ms))
        ],
        "match_score": match_score,
        "match_reasons": list(match_reasons or []),
    }


def _template_haystack(tpl: Transformation, children: list[Transformation]) -> str:
    parts = [tpl.name, json.dumps(tpl.formula, ensure_ascii=False), json.dumps(tpl.meta, ensure_ascii=False)]
    for child in children:
        parts.extend([
            child.name,
            json.dumps(child.formula, ensure_ascii=False),
            json.dumps(child.meta, ensure_ascii=False),
        ])
    return " ".join(parts)


def search_template_catalog(
    repo: MetaCoreRepository,
    *,
    query: str = "",
    category: str | None = None,
    domain: str | None = None,
    scientific_name: str | None = None,
    driver: TransformationDriver | None = None,
    template_kind: TemplateKind | None = None,
    maturity: TemplateMaturity | None = None,
    only_roots: bool = False,
    include_archived: bool = False,
    applies_to_entity_id: str | None = None,
) -> dict[str, Any]:
    """Search and rank templates by nomenclature, aliases, taxonomy facets, maturity & entity context."""
    all_templates = repo.list_transformations(modality="template")
    children_by_parent: dict[str, list[Transformation]] = {}
    for tpl in all_templates:
        if tpl.parent_id:
            children_by_parent.setdefault(tpl.parent_id, []).append(tpl)

    entity: Entity | None = None
    entity_props: set[str] = set()
    entity_contexts: dict[str, set[str]] = {}
    if applies_to_entity_id is not None:
        entity = repo.get_entity(applies_to_entity_id)
        if entity is None:
            return {
                "templates": [],
                "count": 0,
                "facets": {"domains": [], "scientific_names": [], "maturities": {}},
            }
        for st in repo.list_states(entity_id=entity.id):
            entity_props.add(st.property_name)
            for ck, cv in st.context.items():
                entity_contexts.setdefault(ck, set()).add(cv.lower())

    needle = query.strip()
    sci_needle = scientific_name.strip() if scientific_name else ""
    domain_needle = domain.strip().lower() if domain else ""

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    all_domains: set[str] = set()
    all_sci: set[str] = set()
    mat_counts: dict[str, int] = {}

    for tpl in all_templates:
        children = children_by_parent.get(tpl.id, [])
        profile = extract_template_profile(tpl, children)

        if profile["maturity"] == "archived" and not include_archived:
            continue

        # Collect facets across non-archived root-or-matching catalog
        if not only_roots or tpl.parent_id is None:
            all_domains.add(profile["domain"])
            if profile["scientific_name"]:
                all_sci.add(profile["scientific_name"])
            m_key = str(profile["maturity"])
            mat_counts[m_key] = mat_counts.get(m_key, 0) + 1

        if only_roots and tpl.parent_id is not None:
            continue

        tpl_cat = str(tpl.meta.get("category", ""))
        if category is not None and tpl_cat != category:
            continue
        if domain_needle and profile["domain"] != domain_needle:
            continue
        if driver is not None and tpl.driver != driver:
            continue
        if template_kind is not None and profile["template_kind"] != template_kind:
            continue
        if maturity is not None and profile["maturity"] != maturity:
            continue

        if sci_needle:
            sci_candidates = [
                profile["scientific_name"] or "",
                profile["ontology_id"] or "",
                profile["canonical_name"] or "",
                *profile["taxon_path"],
                *(str(c.meta.get("scientific_name") or c.meta.get("species") or "") for c in children),
            ]
            if not any(c and _text_matches_needle(c, sci_needle) for c in sci_candidates):
                continue

        haystack = _template_haystack(tpl, children)
        if needle and not _text_matches_needle(haystack, needle):
            continue

        query_score = 0.0
        if needle:
            if _text_matches_needle(tpl.name, needle):
                query_score += 30.0
            if profile["scientific_name"] and _text_matches_needle(profile["scientific_name"], needle):
                query_score += 25.0
            if any(_text_matches_needle(a, needle) for a in profile["aliases"]):
                query_score += 20.0
            query_score += 5.0

        match_score = 0.0
        match_reasons: list[str] = []
        if entity is not None:
            match_score, match_reasons = _score_entity_match(
                tpl,
                profile,
                entity=entity,
                entity_props=entity_props,
                entity_contexts=entity_contexts,
            )
            if match_score <= 0.0:
                continue

        entry = _serialize_entry(
            tpl,
            children,
            match_score=match_score,
            match_reasons=match_reasons,
        )
        sort_key = (
            -match_score,
            -query_score,
            -_MATURITY_RANK.get(profile["maturity"], 0),
            -profile["observations_count"],
            -(1 if profile["template_kind"] == "playbook" else 0),
            tpl.name.lower(),
        )
        ranked.append((sort_key, entry))

    ranked.sort(key=lambda pair: pair[0])
    items = [pair[1] for pair in ranked]
    return {
        "templates": items,
        "count": len(items),
        "facets": {
            "domains": sorted(all_domains),
            "scientific_names": sorted(all_sci),
            "maturities": mat_counts,
        },
    }
