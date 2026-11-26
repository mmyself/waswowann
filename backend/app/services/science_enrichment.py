"""Automatic EMBL-EBI OLS4 taxonomy lookup & OpenAlex literature/duplicate check for templates."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from metacore.repository import MetaCoreRepository
from metacore.templates import search_template_catalog

OLS_BASE = "https://www.ebi.ac.uk/ols4/api"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_BIO_HINT_WORDS = {
    "pilz",
    "pilzzucht",
    "pflanze",
    "pflanzen",
    "tier",
    "tiere",
    "tierwelt",
    "natur",
    "garten",
    "fungi",
    "plantae",
    "animalia",
    "mushroom",
}
_GENERIC_ANCESTORS = {"root", "cellular organisms"}
_PAREN_BINOMIAL_RE = re.compile(r"\(([A-Z][a-z]+(?:\s+[a-z][a-z-]+){1,2})\)")


def _looks_biological(
    *,
    name: str,
    scientific_name: str | None,
    category: str,
    domain: str,
) -> bool:
    if scientific_name and scientific_name.strip():
        return True
    if _PAREN_BINOMIAL_RE.search(name):
        return True
    cat_low = category.strip().lower()
    dom_low = domain.strip().lower()
    return cat_low in _BIO_HINT_WORDS or dom_low in _BIO_HINT_WORDS


def _clean_query_for_taxon(name: str, scientific_name: str | None) -> str:
    if scientific_name and scientific_name.strip():
        return scientific_name.strip()
    m = _PAREN_BINOMIAL_RE.search(name)
    if m:
        return m.group(1).strip()
    cleaned = re.split(r"\s*[—–\-:]\s*", name.strip(), maxsplit=1)[0].strip()
    return cleaned or name.strip()


def lookup_ols_taxon(query: str, *, timeout: float = 6.0) -> dict[str, Any] | None:
    """Resolve an organism/fungus/plant/animal name via EMBL-EBI OLS4 (NCBITaxon)."""
    q = query.strip()
    if not q:
        return None
    try:
        resp = httpx.get(
            f"{OLS_BASE}/search",
            params={"q": q, "ontology": "ncbitaxon", "rows": 5},
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        docs = (resp.json().get("response") or {}).get("docs") or []
    except Exception:
        return None

    if not docs:
        return None

    q_low = q.lower()
    chosen = next(
        (d for d in docs if str(d.get("label") or "").strip().lower() == q_low),
        docs[0],
    )
    obo_id = str(chosen.get("obo_id") or "").strip()
    iri = str(chosen.get("iri") or "").strip()
    label = str(chosen.get("label") or "").strip()
    if not obo_id or not label:
        return None

    synonyms: list[str] = []
    taxon_path: list[str] = []

    if iri:
        try:
            encoded_iri = quote(quote(iri, safe=""), safe="")
            term_resp = httpx.get(
                f"{OLS_BASE}/ontologies/ncbitaxon/terms/{encoded_iri}",
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            if term_resp.status_code == 200:
                t_body = term_resp.json()
                for syn in t_body.get("synonyms") or []:
                    s_clean = str(syn).strip()
                    if s_clean and s_clean.lower() != label.lower() and s_clean not in synonyms:
                        synonyms.append(s_clean)

            anc_resp = httpx.get(
                f"{OLS_BASE}/ontologies/ncbitaxon/terms/{encoded_iri}/ancestors",
                params={"size": 30},
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            if anc_resp.status_code == 200:
                anc_terms = ((anc_resp.json().get("_embedded") or {}).get("terms")) or []
                for anc in reversed(anc_terms):
                    a_lbl = str(anc.get("label") or "").strip()
                    if (
                        a_lbl
                        and a_lbl.lower() not in _GENERIC_ANCESTORS
                        and "incertae sedis" not in a_lbl.lower()
                        and a_lbl not in taxon_path
                    ):
                        taxon_path.append(a_lbl)
        except Exception:
            pass

    if label not in taxon_path:
        taxon_path.append(label)

    return {
        "ontology_id": obo_id,
        "scientific_name": label,
        "synonyms": synonyms[:10],
        "taxon_path": taxon_path,
        "iri": iri,
    }


def lookup_openalex_references(
    query: str,
    *,
    scientific_name: str | None = None,
    limit: int = 3,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Query OpenAlex for concise peer-reviewed references supporting an ai_prior template."""
    parts = [p.strip() for p in (scientific_name or "", query) if p and p.strip()]
    search_str = " ".join(parts).strip()
    if not search_str:
        return []

    params: dict[str, Any] = {
        "search": search_str,
        "sort": "cited_by_count:desc",
        "per-page": max(1, min(limit, 5)),
        "select": "id,doi,title,publication_year,cited_by_count,primary_location",
    }
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key

    try:
        resp = httpx.get(
            OPENALEX_WORKS_URL,
            params=params,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        works = resp.json().get("results") or []
    except Exception:
        return []

    refs: list[dict[str, Any]] = []
    for w in works[:limit]:
        title = str(w.get("title") or "").strip()
        doi = w.get("doi")
        wid = w.get("id")
        ref_url = str(doi or wid or "").strip()
        if not title or not ref_url:
            continue
        year = w.get("publication_year")
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "OpenAlex"
        cited = w.get("cited_by_count")
        label = f"{title} ({year}, {src})" if year else f"{title} ({src})"
        refs.append(
            {
                "kind": "doi" if doi else "openalex",
                "ref": ref_url,
                "label": label[:240],
                "year": year,
                "cited_by_count": cited,
            }
        )
    return refs


def find_existing_template_matches(
    repo: MetaCoreRepository,
    *,
    name: str,
    scientific_name: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return existing root templates sharing the same scientific name or name tokens."""
    seen: dict[str, dict[str, Any]] = {}
    if scientific_name and scientific_name.strip():
        by_sci = search_template_catalog(
            repo,
            scientific_name=scientific_name.strip(),
            only_roots=True,
        )["templates"]
        for item in by_sci:
            tid = item["template"]["id"]
            seen[tid] = {
                "id": tid,
                "name": item["template"]["name"],
                "scientific_name": item["profile"]["scientific_name"],
                "ontology_id": item["profile"].get("ontology_id"),
                "maturity": item["profile"]["maturity"],
                "observations_count": item["profile"]["observations_count"],
                "match_reason": "Gleicher wissenschaftlicher Name",
            }

    clean_q = _clean_query_for_taxon(name, None)
    if clean_q and len(clean_q) >= 3:
        by_name = search_template_catalog(
            repo,
            query=clean_q,
            only_roots=True,
        )["templates"]
        for item in by_name:
            tid = item["template"]["id"]
            if tid not in seen:
                seen[tid] = {
                    "id": tid,
                    "name": item["template"]["name"],
                    "scientific_name": item["profile"]["scientific_name"],
                    "ontology_id": item["profile"].get("ontology_id"),
                    "maturity": item["profile"]["maturity"],
                    "observations_count": item["profile"]["observations_count"],
                    "match_reason": "Ähnlicher Name / Alias",
                }
    return list(seen.values())[:limit]


def preview_template_enrichment(
    repo: MetaCoreRepository,
    *,
    name: str,
    scientific_name: str | None = None,
    category: str = "general",
    domain: str = "general",
    duration_source: str = "ai_prior",
) -> dict[str, Any]:
    """Run duplicate detection, OLS taxon resolution, and OpenAlex reference lookup."""
    clean_sci = scientific_name.strip() if scientific_name and scientific_name.strip() else None
    taxonomy: dict[str, Any] | None = None
    if _looks_biological(name=name, scientific_name=clean_sci, category=category, domain=domain):
        taxonomy = lookup_ols_taxon(_clean_query_for_taxon(name, clean_sci))

    effective_sci = clean_sci or (taxonomy["scientific_name"] if taxonomy else None)
    existing_matches = find_existing_template_matches(
        repo,
        name=name,
        scientific_name=effective_sci,
    )
    literature_refs: list[dict[str, Any]] = []
    if duration_source == "ai_prior":
        literature_refs = lookup_openalex_references(
            _clean_query_for_taxon(name, None) or name,
            scientific_name=effective_sci,
        )

    return {
        "existing_matches": existing_matches,
        "taxonomy": taxonomy,
        "literature_refs": literature_refs,
    }
