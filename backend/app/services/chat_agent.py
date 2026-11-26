"""Conversational WasWoWann assistant with MetaCore & archive tool actions and voice transcription."""

from __future__ import annotations

import base64
from datetime import datetime
import json
import math
from typing import Any, Literal
import uuid
from zoneinfo import ZoneInfo

import httpx

import app.config as config_module
from app.config import Settings
from app.db import db, now_ms
from app.routes.observations import _detail as get_observation_detail
from app.services.gaussian_fusion import recompute_observation_kalman
from app.services.metacore_bridge import (
    build_live_snapshot,
    inspect_worldline,
    open_metacore,
    resolve_entity_id,
)
from metacore.db import now_ms as mc_now_ms
from metacore.models import StateCreate
from metacore.templates import search_template_catalog

ChatScopeType = Literal["global", "entity"]

VALID_CATEGORIES = ("Pilz", "Pflanze", "Person", "Ding", "Ereignis", "Unbekannt")
VALID_ENVIRONMENTS = ("unknown", "indoor", "outdoor")
VALID_ESCALATION_ACTIONS = ("widen_tolerance", "switch_branch", "abort_goal")
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_ACTIONS_PER_TURN = 5


class AudioTranscriptionUnavailableError(RuntimeError):
    """Raised when server-side audio transcription is unavailable or unsupported."""


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config_module.settings.import_timezone)
    except Exception:
        return ZoneInfo("UTC")


def _format_ts(ms: int | None) -> str:
    if ms is None:
        return "unbekannt"
    return datetime.fromtimestamp(ms / 1000.0, tz=_tz()).strftime("%Y-%m-%d %H:%M")


def _row_to_message(row) -> dict[str, Any]:
    try:
        actions = json.loads(row["actions_json"] or "[]")
        if not isinstance(actions, list):
            actions = []
    except (ValueError, TypeError):
        actions = []
    return {
        "id": row["id"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "role": row["role"],
        "content": row["content"],
        "actions": actions,
        "created_at_ms": row["created_at_ms"],
    }


def list_messages(scope_type: ChatScopeType, scope_id: str, limit: int = 80) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, scope_type, scope_id, role, content, actions_json, created_at_ms
            FROM chat_messages
            WHERE scope_type = ? AND scope_id = ?
            ORDER BY created_at_ms ASC, id ASC
            LIMIT ?
            """,
            (scope_type, scope_id, max(1, min(limit, 500))),
        ).fetchall()
    return [_row_to_message(row) for row in rows]


def append_message(
    *,
    scope_type: ChatScopeType,
    scope_id: str,
    role: Literal["user", "assistant"],
    content: str,
    actions: list[dict[str, Any]] | None = None,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    msg_id = str(uuid.uuid4())
    ts = created_at_ms if created_at_ms is not None else now_ms()
    actions_list = list(actions or [])
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages(id, scope_type, scope_id, role, content, actions_json, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, scope_type, scope_id, role, content, json.dumps(actions_list, ensure_ascii=False), ts),
        )
    return {
        "id": msg_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "role": role,
        "content": content,
        "actions": actions_list,
        "created_at_ms": ts,
    }


def clear_messages(scope_type: ChatScopeType, scope_id: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        )
        return cur.rowcount


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _parse_llm_json(raw_text: str) -> tuple[str, list[dict[str, Any]]]:
    cleaned = _strip_code_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        return raw_text.strip(), []

    if not isinstance(parsed, dict):
        return raw_text.strip(), []

    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = raw_text.strip()
    raw_actions = parsed.get("actions")
    actions: list[dict[str, Any]] = []
    if isinstance(raw_actions, list):
        for item in raw_actions[:MAX_ACTIONS_PER_TURN]:
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                actions.append(item)
    return reply.strip(), actions


def _build_context_snapshot(scope_type: ChatScopeType, scope_id: str) -> dict[str, Any]:
    snap = build_live_snapshot(auto_sync=True)
    repo, _ = open_metacore()
    template_catalog = search_template_catalog(repo, only_roots=True)
    with db() as conn:
        obs_rows = conn.execute(
            """
            SELECT o.id, o.title, o.category, o.environment, o.description, o.updated_at_ms,
                   s.lat, s.lon, s.first_observed_at_ms, s.last_observed_at_ms
            FROM observations o
            JOIN space_time_gauss s ON s.obs_id = o.id
            ORDER BY o.updated_at_ms DESC, o.id DESC
            LIMIT 40
            """
        ).fetchall()
        focus_obs = get_observation_detail(conn, scope_id) if scope_type == "entity" else None

    observations_compact = [
        {
            "obs_id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "environment": r["environment"],
            "description": (r["description"] or "")[:240],
            "lat": r["lat"],
            "lon": r["lon"],
            "last_observed": _format_ts(r["last_observed_at_ms"]),
        }
        for r in obs_rows
    ]

    focus_worldline: dict[str, Any] | None = None
    if scope_type == "entity":
        try:
            wl = inspect_worldline(scope_id)
            focus_worldline = {
                "entity": wl["entity"],
                "legacy_obs_id": wl.get("legacy_obs_id"),
                "history_observed": [
                    {
                        "id": s["id"],
                        "property_name": s["property_name"],
                        "label": s["label"],
                        "value_num": s["value_num"],
                        "value_unit": s["value_unit"],
                        "time": _format_ts(s.get("time_ms")),
                    }
                    for s in wl["history_observed"][-12:]
                ],
                "future_predicted": [
                    {
                        "id": s["id"],
                        "property_name": s["property_name"],
                        "label": s["label"],
                        "value_num": s["value_num"],
                        "value_sigma": s["value_sigma"],
                        "value_unit": s["value_unit"],
                        "time": _format_ts(s.get("time_ms")),
                    }
                    for s in wl["future_predicted"][-12:]
                ],
                "goal_evaluations": wl["goal_evaluations"],
                "periodicity_summary": (wl.get("periodicity") or {}).get("summary"),
            }
        except KeyError:
            focus_worldline = None

    return {
        "now_iso": _format_ts(snap.get("now_ms")),
        "now_ms": snap.get("now_ms"),
        "observations": observations_compact,
        "entities": [
            {"entity_id": e["id"], "name": e["name"], "category": e["category"], "legacy_obs_id": (e.get("meta") or {}).get("legacy_obs_id")}
            for e in snap.get("entities", [])
        ],
        "goal_evaluations": snap.get("goal_evaluations", []),
        "upcoming_predictions": [
            {
                "id": s["id"],
                "entity_id": s["entity_id"],
                "property_name": s["property_name"],
                "label": s["label"],
                "value_num": s.get("value_num"),
                "value_sigma": s.get("value_sigma"),
                "value_unit": s.get("value_unit"),
                "time": _format_ts(s.get("time_ms")),
            }
            for s in snap.get("states", [])
            if s.get("modality") == "predicted"
        ][:20],
        "uses_conflicts": snap.get("uses_conflicts", []),
        "periodicity_summary": (snap.get("periodicity") or {}).get("summary"),
        "templates": [
            {
                "id": t["template"]["id"],
                "name": t["template"]["name"],
                "domain": t["profile"]["domain"],
                "scientific_name": t["profile"]["scientific_name"],
                "aliases": t["profile"]["aliases"],
                "template_kind": t["profile"]["template_kind"],
                "maturity": t["profile"]["maturity"],
                "observations_count": t["profile"]["observations_count"],
                "children_count": len(t["children"]),
            }
            for t in template_catalog["templates"][:30]
        ],
        "focus_observation": (
            {
                "id": focus_obs["id"],
                "title": focus_obs["title"],
                "category": focus_obs["category"],
                "environment": focus_obs["environment"],
                "description": focus_obs["description"],
                "lat": focus_obs["lat"],
                "lon": focus_obs["lon"],
                "sigma_meters": focus_obs["sigma_meters"],
                "first_observed_at": focus_obs["first_observed_at"],
                "last_observed_at": focus_obs["last_observed_at"],
                "gemini_summary": ((focus_obs.get("gemini") or {}).get("result") or {}).get("description", "")[:400],
            }
            if focus_obs
            else None
        ),
        "focus_worldline": focus_worldline,
    }


def _build_system_prompt(scope_type: ChatScopeType, scope_id: str, context: dict[str, Any]) -> str:
    scope_desc = (
        f"Der Nutzer spricht gerade gezielt über EIN einzelnes Ding (scope_type='entity', scope_id='{scope_id}'). "
        "Wenn der Nutzer 'es', 'hier', 'dieses Ding', eine Notiz, einen Messwert oder ein Ziel erwähnt ohne ID, beziehe dich auf dieses Objekt."
        if scope_type == "entity"
        else "Der Nutzer befindet sich im globalen WasWoWann-Chat mit Überblick über alle Funde, Weltlinien und den Zukunftsgraphen."
    )
    return (
        "Du bist der hilfreiche, präzise Assistent von WasWoWann (Archiv für Funde, Messungen, Weltlinien und Zukunftsgraph). "
        "Antworte auf Deutsch, klar, freundlich und faktenbasiert auf Basis der übergebenen Live-Daten.\n\n"
        f"{scope_desc}\n\n"
        "Wenn der Nutzer ausdrücklich etwas festhalten, notieren, einen Messwert erfassen, ein Ziel setzen/ändern oder eine Eskalation lösen möchte, "
        "kannst du im Feld `actions` konkrete Aktionen zurückgeben, die das Backend sofort ausführt. "
        "Erfinde KEINE Aktionen, wenn der Nutzer nur eine Frage stellt.\n\n"
        "Antworte AUSSCHLIESSLICH als gültiges JSON-Objekt mit genau diesem Schema:\n"
        '{"reply": "Deine deutsche Antwort an den Nutzer", "actions": [...]}\n\n'
        "Unterstützte Aktionstypen in `actions` (max. 5):\n"
        '1. {"type": "add_note", "obs_id": "<optional, sonst aktuelles Ding>", "text": "Notiztext"}\n'
        '2. {"type": "record_observation", "entity_id": "<entity_id oder obs_id>", "property_name": "<z.B. temperature_c, humidity_pct, harvest_ready, rain_mm, height_cm>", "value_num": 18.5, "value_sigma": 0.5, "value_unit": "°C", "label": "optionaler Titel", "context": {}}\n'
        '3. {"type": "set_goal", "entity_id": "<entity_id oder obs_id>", "property_name": "<z.B. harvest_ready>", "label": "Zielbeschreibung", "value_unit": "g", "target_min": 200, "target_max": null, "target_earliest_ms": null, "target_latest_ms": null, "target_radius_meters": null}\n'
        '4. {"type": "update_goal", "goal_id": "<goal_id>", "label": "optional", "target_min": 180, "target_max": null, "value_unit": "optional", "target_earliest_ms": null, "target_latest_ms": null}\n'
        '5. {"type": "resolve_escalation", "goal_id": "<goal_id>", "action": "widen_tolerance|switch_branch|abort_goal", "params": {}}\n'
        '6. {"type": "update_observation_meta", "obs_id": "<optional, sonst aktuelles Ding>", "title": "optional", "category": "Pilz|Pflanze|Person|Ding|Ereignis|Unbekannt", "environment": "unknown|indoor|outdoor"}\n\n'
        "Aktueller Live-Kontext (JSON):\n"
        + json.dumps(context, ensure_ascii=False)
    )


def _call_llm(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_text: str,
    config: Settings,
) -> tuple[str, list[dict[str, Any]]]:
    recent = history[-14:]
    if config.gemini_provider == "antigravity":
        if not config.antigravity_bridge_url or not config.antigravity_bridge_auth:
            raise RuntimeError("ANALYSIS_NOT_CONFIGURED")
        messages = [{"role": "system", "content": system_prompt}]
        for item in recent:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": user_text})
        url = config.antigravity_bridge_url.rstrip("/")
        url = f"{url}/chat/completions" if url.endswith("/v1") else f"{url}/v1/chat/completions"
        response = httpx.post(
            url,
            json={"model": config.gemini_model, "messages": messages, "stream": False},
            headers={"Authorization": f"Bearer {config.antigravity_bridge_auth}"},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        raw = body["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            raise ValueError("INVALID_CHAT_RESPONSE")
        return _parse_llm_json(raw)

    if config.gemini_provider == "google" and config.google_api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent"
        contents = []
        for item in recent:
            role = "model" if item["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": item["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_text}]})
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
        }
        response = httpx.post(url, params={"key": config.google_api_key}, json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        raw = body["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_llm_json(raw)

    raise RuntimeError("ANALYSIS_NOT_CONFIGURED")


def _offline_fallback_reply(
    *,
    scope_type: ChatScopeType,
    context: dict[str, Any],
    reason: str,
) -> str:
    note_prefix = (
        "Der KI-Sprachdienst ist gerade nicht konfiguriert oder nicht erreichbar — "
        "hier ist der aktuelle Stand direkt aus deinen WasWoWann-Daten:\n\n"
        if reason
        else ""
    )
    if scope_type == "entity" and context.get("focus_observation"):
        obs = context["focus_observation"]
        wl = context.get("focus_worldline") or {}
        preds = wl.get("future_predicted") or []
        goals = wl.get("goal_evaluations") or []
        lines = [
            f"**{obs['title']}** ({obs['category']}, Umgebung: {obs['environment']})",
        ]
        if obs.get("description"):
            lines.append(f"Notiz: {obs['description'][:220]}")
        if preds:
            next_p = preds[0]
            lines.append(f"Nächste Prognose: {next_p['label']} ({next_p.get('time', '')})")
        if goals:
            lines.append(
                "Ziele: "
                + ", ".join(f"{g['label']} [{g['status']}, {round(g['goal_probability'] * 100)} %]" for g in goals)
            )
        return note_prefix + "\n".join(lines)

    goals = context.get("goal_evaluations") or []
    preds = context.get("upcoming_predictions") or []
    obs_count = len(context.get("observations") or [])
    ent_count = len(context.get("entities") or [])
    lines = [f"Archiv-Überblick: {obs_count} Beobachtungen, {ent_count} Weltlinien im Zukunftsgraphen."]
    if goals:
        lines.append(
            "Aktive Ziele: "
            + "; ".join(f"{g['label']} ({g['status']}, {round(g['goal_probability'] * 100)} %)" for g in goals[:4])
        )
    if preds:
        lines.append(
            "Nächste Prognosen: "
            + "; ".join(f"{p['label']} ({p.get('time', '')})" for p in preds[:4])
        )
    return note_prefix + "\n".join(lines)


def _finite_opt_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and math.isfinite(float(val)):
        return float(val)
    return None


def _opt_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and math.isfinite(float(val)):
        return int(val)
    return None


def _execute_actions(
    actions: list[dict[str, Any]],
    *,
    scope_type: ChatScopeType,
    scope_id: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not actions:
        return results

    repo, engine = open_metacore()
    default_target = scope_id if scope_type == "entity" else None

    for raw in actions[:MAX_ACTIONS_PER_TURN]:
        atype = str(raw.get("type", "")).strip()
        try:
            if atype == "add_note":
                text = str(raw.get("text") or raw.get("note") or "").strip()
                if not text:
                    raise ValueError("Notiztext fehlt.")
                obs_id = str(raw.get("obs_id") or default_target or "").strip()
                if obs_id.startswith("obs:"):
                    obs_id = obs_id[4:]
                if not obs_id:
                    raise ValueError("Keine Ziel-Beobachtung für add_note angegeben.")
                stamp = _format_ts(now_ms())
                entry = f"[{stamp}] {text}"
                with db() as conn:
                    row = conn.execute("SELECT id, title, description FROM observations WHERE id = ?", (obs_id,)).fetchone()
                    if row is None:
                        raise ValueError(f"Beobachtung '{obs_id}' nicht gefunden.")
                    existing_desc = (row["description"] or "").strip()
                    new_desc = f"{existing_desc}\n{entry}".strip() if existing_desc else entry
                    conn.execute(
                        "UPDATE observations SET description = ?, updated_at_ms = ? WHERE id = ?",
                        (new_desc[:10000], now_ms(), obs_id),
                    )
                ent_id = resolve_entity_id(repo, obs_id)
                if ent_id is not None:
                    engine.record_observation(
                        StateCreate(
                            entity_id=ent_id,
                            modality="observed",
                            source="measured",
                            measurement_source="chat_voice",
                            property_name="note",
                            label=text[:200],
                            value_num=1.0,
                            value_sigma=0.0,
                            value_unit="",
                            value_text=text[:500],
                            time_ms=mc_now_ms(),
                            sigma_seconds=60.0,
                            assertion_confidence=0.95,
                        )
                    )
                results.append(
                    {
                        "type": "add_note",
                        "ok": True,
                        "summary": f"Notiz ergänzt: {text[:80]}",
                        "details": {"obs_id": obs_id},
                    }
                )

            elif atype == "record_observation":
                target = str(raw.get("entity_id") or default_target or "").strip()
                ent_id = resolve_entity_id(repo, target) if target else None
                if ent_id is None:
                    raise ValueError(f"Unbekannte Weltlinie: {target or '(leer)'}")
                prop = str(raw.get("property_name") or "").strip()
                val_num = _finite_opt_float(raw.get("value_num"))
                if not prop or val_num is None:
                    raise ValueError("property_name und numerischer value_num erforderlich.")
                sigma_val = _finite_opt_float(raw.get("value_sigma"))
                if sigma_val is None or sigma_val < 0:
                    sigma_val = 0.0
                unit = str(raw.get("value_unit") or "").strip()[:40]
                label = str(raw.get("label") or f"{prop}: {val_num} {unit}".strip()).strip()[:240]
                ctx_raw = raw.get("context")
                ctx = (
                    {str(k): str(v) for k, v in ctx_raw.items() if k and v}
                    if isinstance(ctx_raw, dict)
                    else {}
                )
                conf = _finite_opt_float(raw.get("assertion_confidence"))
                if conf is None or not (0.0 < conf <= 1.0):
                    conf = 0.95
                fb = engine.record_observation(
                    StateCreate(
                        entity_id=ent_id,
                        modality="observed",
                        source="measured",
                        measurement_source="chat_voice",
                        property_name=prop,
                        label=label,
                        value_num=val_num,
                        value_sigma=sigma_val,
                        value_unit=unit,
                        time_ms=_opt_int(raw.get("time_ms")) or mc_now_ms(),
                        sigma_seconds=60.0,
                        context=ctx,
                        assertion_confidence=conf,
                    )
                )
                results.append(
                    {
                        "type": "record_observation",
                        "ok": True,
                        "summary": f"Messwert erfasst: {label}",
                        "details": {"entity_id": ent_id, "feedback": fb.summary},
                    }
                )

            elif atype == "set_goal":
                target = str(raw.get("entity_id") or raw.get("entity_id_or_obs_id") or default_target or "").strip()
                ent_id = resolve_entity_id(repo, target) if target else None
                if ent_id is None:
                    raise ValueError(f"Unbekannte Weltlinie für Ziel: {target or '(leer)'}")
                label = str(raw.get("label") or "").strip()[:240]
                if not label:
                    raise ValueError("Ziel-Label erforderlich.")
                prop = str(raw.get("property_name") or "harvest_ready").strip()[:120]
                unit = str(raw.get("value_unit") or "").strip()[:40]
                goal_state = repo.create_state(
                    StateCreate(
                        entity_id=ent_id,
                        modality="desired",
                        source="user_goal",
                        property_name=prop,
                        label=label,
                        value_unit=unit,
                        target_min=_finite_opt_float(raw.get("target_min")),
                        target_max=_finite_opt_float(raw.get("target_max")),
                        target_earliest_ms=_opt_int(raw.get("target_earliest_ms")),
                        target_latest_ms=_opt_int(raw.get("target_latest_ms")),
                        target_radius_meters=_finite_opt_float(raw.get("target_radius_meters")),
                        priority=float(_finite_opt_float(raw.get("priority")) or 1.0),
                    )
                )
                ev = engine.evaluate_goal(goal_state.id)
                results.append(
                    {
                        "type": "set_goal",
                        "ok": True,
                        "summary": f"Ziel gesetzt: {label} ({round(ev.goal_probability * 100)} %)",
                        "details": {"goal_id": goal_state.id, "status": ev.status},
                    }
                )

            elif atype == "update_goal":
                goal_id = str(raw.get("goal_id") or "").strip()
                current = repo.get_state(goal_id) if goal_id else None
                if current is None or current.modality != "desired":
                    raise ValueError(f"Aktives Ziel '{goal_id}' nicht gefunden.")
                updates: dict[str, Any] = {}
                if isinstance(raw.get("label"), str) and raw["label"].strip():
                    updates["label"] = raw["label"].strip()[:240]
                if isinstance(raw.get("value_unit"), str):
                    updates["value_unit"] = raw["value_unit"].strip()[:40]
                for num_field in ("target_min", "target_max", "target_radius_meters"):
                    if num_field in raw:
                        updates[num_field] = _finite_opt_float(raw[num_field])
                for int_field in ("target_earliest_ms", "target_latest_ms"):
                    if int_field in raw:
                        updates[int_field] = _opt_int(raw[int_field])
                if updates:
                    repo.update_state(goal_id, **updates)
                ev = engine.evaluate_goal(goal_id)
                results.append(
                    {
                        "type": "update_goal",
                        "ok": True,
                        "summary": f"Ziel aktualisiert: {ev.label} ({round(ev.goal_probability * 100)} %)",
                        "details": {"goal_id": goal_id, "status": ev.status},
                    }
                )

            elif atype == "resolve_escalation":
                goal_id = str(raw.get("goal_id") or "").strip()
                esc_action = str(raw.get("action") or "").strip()
                if esc_action not in VALID_ESCALATION_ACTIONS:
                    raise ValueError(f"Unbekannte Eskalations-Aktion: {esc_action}")
                params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
                fb = engine.resolve_escalation(goal_id, esc_action, params)  # type: ignore[arg-type]
                results.append(
                    {
                        "type": "resolve_escalation",
                        "ok": True,
                        "summary": f"Eskalation gelöst ({esc_action})",
                        "details": {"goal_id": goal_id, "feedback": fb.summary},
                    }
                )

            elif atype == "update_observation_meta":
                obs_id = str(raw.get("obs_id") or default_target or "").strip()
                if obs_id.startswith("obs:"):
                    obs_id = obs_id[4:]
                if not obs_id:
                    raise ValueError("Keine Ziel-Beobachtung angegeben.")
                updates: dict[str, Any] = {}
                if isinstance(raw.get("title"), str) and raw["title"].strip():
                    updates["title"] = raw["title"].strip()[:200]
                if raw.get("category") in VALID_CATEGORIES:
                    updates["category"] = raw["category"]
                if raw.get("environment") in VALID_ENVIRONMENTS:
                    updates["environment"] = raw["environment"]
                if not updates:
                    raise ValueError("Keine gültigen Metadaten zum Aktualisieren angegeben.")
                with db() as conn:
                    cur = conn.execute(
                        f"UPDATE observations SET {', '.join(f'{k}=?' for k in updates)}, updated_at_ms = ? WHERE id = ?",
                        (*updates.values(), now_ms(), obs_id),
                    )
                    if not cur.rowcount:
                        raise ValueError(f"Beobachtung '{obs_id}' nicht gefunden.")
                    if "environment" in updates:
                        recompute_observation_kalman(conn, obs_id)
                results.append(
                    {
                        "type": "update_observation_meta",
                        "ok": True,
                        "summary": f"Metadaten aktualisiert ({', '.join(updates.keys())})",
                        "details": {"obs_id": obs_id, **updates},
                    }
                )

            else:
                results.append(
                    {
                        "type": atype or "unknown",
                        "ok": False,
                        "summary": f"Unbekannter Aktionstyp: {atype}",
                    }
                )
        except Exception as exc:
            results.append(
                {
                    "type": atype or "unknown",
                    "ok": False,
                    "summary": f"Aktion fehlgeschlagen ({atype}): {exc}",
                }
            )

    return results


def handle_chat_message(
    *,
    message: str,
    scope_type: ChatScopeType = "global",
    scope_id: str = "global",
    config: Settings | None = None,
) -> dict[str, Any]:
    active_config = config or config_module.settings
    clean_scope_id = scope_id.strip() or "global"
    if scope_type == "global":
        clean_scope_id = "global"
    clean_msg = message.strip()

    history_before = list_messages(scope_type, clean_scope_id, limit=30)
    user_msg = append_message(
        scope_type=scope_type,
        scope_id=clean_scope_id,
        role="user",
        content=clean_msg,
    )

    context = _build_context_snapshot(scope_type, clean_scope_id)
    system_prompt = _build_system_prompt(scope_type, clean_scope_id, context)

    executed_actions: list[dict[str, Any]] = []
    try:
        reply_text, planned_actions = _call_llm(
            system_prompt=system_prompt,
            history=history_before,
            user_text=clean_msg,
            config=active_config,
        )
        if planned_actions:
            executed_actions = _execute_actions(
                planned_actions,
                scope_type=scope_type,
                scope_id=clean_scope_id,
            )
    except Exception as exc:
        reply_text = _offline_fallback_reply(
            scope_type=scope_type,
            context=context,
            reason=str(exc),
        )

    assistant_msg = append_message(
        scope_type=scope_type,
        scope_id=clean_scope_id,
        role="assistant",
        content=reply_text,
        actions=executed_actions,
    )

    snapshot = build_live_snapshot(auto_sync=False)
    updated_observation = None
    if scope_type == "entity":
        obs_lookup_id = clean_scope_id[4:] if clean_scope_id.startswith("obs:") else clean_scope_id
        with db() as conn:
            updated_observation = get_observation_detail(conn, obs_lookup_id)

    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "executed_actions": executed_actions,
        "snapshot": snapshot,
        "updated_observation": updated_observation,
    }


def _audio_format_from_mime(mime_type: str, filename: str | None) -> str:
    lower = mime_type.lower()
    for fmt in ("wav", "mp3", "webm", "ogg", "m4a", "mp4"):
        if fmt in lower:
            return "mp4" if fmt == "m4a" else fmt
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in {"wav", "mp3", "webm", "ogg", "m4a", "mp4"}:
            return "mp4" if ext == "m4a" else ext
    return "webm"


def transcribe_audio(
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/webm",
    filename: str | None = None,
    config: Settings | None = None,
) -> str:
    active_config = config or config_module.settings
    if not audio_bytes:
        raise ValueError("Leere Audioaufnahme.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Audioaufnahme ist größer als 12 MB.")

    b64 = base64.b64encode(audio_bytes).decode("ascii")
    prompt = "Transkribiere diese deutsche Sprachaufnahme wörtlich. Antworte ausschließlich mit dem transkribierten Text ohne Anführungszeichen oder Zusatzkommentare."

    if active_config.gemini_provider == "google" and active_config.google_api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_config.gemini_model}:generateContent"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime_type or "audio/webm", "data": b64}},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.0},
        }
        try:
            response = httpx.post(url, params={"key": active_config.google_api_key}, json=payload, timeout=60)
            response.raise_for_status()
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return text.strip()
        except Exception as exc:
            raise AudioTranscriptionUnavailableError(f"Audio-Transkription über Google Gemini fehlgeschlagen: {exc}") from exc

    if active_config.gemini_provider == "antigravity" and active_config.antigravity_bridge_url and active_config.antigravity_bridge_auth:
        url = active_config.antigravity_bridge_url.rstrip("/")
        url = f"{url}/chat/completions" if url.endswith("/v1") else f"{url}/v1/chat/completions"
        audio_fmt = _audio_format_from_mime(mime_type or "", filename)
        payload = {
            "model": active_config.gemini_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "input_audio", "input_audio": {"data": b64, "format": audio_fmt}},
                    ],
                }
            ],
            "stream": False,
        }
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {active_config.antigravity_bridge_auth}"},
                timeout=60,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            if isinstance(text, str) and text.strip():
                return text.strip()
        except Exception as exc:
            raise AudioTranscriptionUnavailableError(
                "Serverseitige Audio-Transkription wird von der aktuellen KI-Bridge nicht unterstützt. Bitte nutze die Live-Spracherkennung in Chrome."
            ) from exc

    raise AudioTranscriptionUnavailableError(
        "Kein Sprachdienst für Audio-Transkription konfiguriert. Bitte nutze die Live-Spracherkennung im Browser."
    )
