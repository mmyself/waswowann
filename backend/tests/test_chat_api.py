from __future__ import annotations

import json
from fastapi.testclient import TestClient

from app.db import db
from app.services import chat_agent
from app.services.gaussian_fusion import recompute_observation_kalman

HEADERS = {"Origin": "http://localhost:3000", "Content-Type": "application/json"}


def _seed_single_observation(config, *, obs_id: str = "hericium-chat-1", title: str = "Igel-Stachelbart") -> None:
    now = 1_795_500_000_000
    with db(config) as conn:
        conn.execute(
            """
            INSERT INTO observations(id, category, title, description, environment, created_at_ms, updated_at_ms)
            VALUES (?, 'Pilz', ?, 'Erster Schub auf Substratblock', 'indoor', ?, ?)
            """,
            (obs_id, title, now, now),
        )
        conn.execute(
            """
            INSERT INTO media(
                id, obs_id, source, external_id, original_name, local_path, thumbnail_path,
                mime_type, width, height, sha256, exif_json, created_at_ms,
                observed_at_ms, sigma_seconds, time_source, lat, lon, sigma_meters, location_source
            ) VALUES ('m-chat-1', ?, 'local', 'ext-chat-1', 'chat1.jpg', 'img.jpg', 'thumb.jpg',
                      'image/jpeg', 100, 100, 'sha-chat-1', '{}', ?, ?, 60.0, 'exif', 53.55828, 9.80831, 10.0, 'exif')
            """,
            (obs_id, now, now),
        )
        recompute_observation_kalman(conn, obs_id, config=config)


def test_chat_scopes_isolated_and_offline_fallback(client: TestClient, config):
    _seed_single_observation(config)

    g_res = client.post(
        "/api/chat/messages",
        headers=HEADERS,
        json={"message": "Was steht an?", "scope_type": "global", "scope_id": "global"},
    )
    assert g_res.status_code == 200
    g_body = g_res.json()
    assert g_body["user_message"]["scope_type"] == "global"
    assert g_body["assistant_message"]["content"]
    assert g_body["updated_observation"] is None

    e_res = client.post(
        "/api/chat/messages",
        headers=HEADERS,
        json={"message": "Wie geht es dem Pilz?", "scope_type": "entity", "scope_id": "hericium-chat-1"},
    )
    assert e_res.status_code == 200
    e_body = e_res.json()
    assert e_body["user_message"]["scope_type"] == "entity"
    assert e_body["updated_observation"]["id"] == "hericium-chat-1"
    assert "Igel-Stachelbart" in e_body["assistant_message"]["content"]

    g_hist = client.get("/api/chat/messages?scope_type=global&scope_id=global").json()
    e_hist = client.get("/api/chat/messages?scope_type=entity&scope_id=hericium-chat-1").json()
    assert len(g_hist["messages"]) == 2
    assert len(e_hist["messages"]) == 2

    deleted = client.delete("/api/chat/messages?scope_type=global&scope_id=global", headers={"Origin": "http://localhost:3000"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 2
    assert len(client.get("/api/chat/messages?scope_type=global&scope_id=global").json()["messages"]) == 0
    assert len(client.get("/api/chat/messages?scope_type=entity&scope_id=hericium-chat-1").json()["messages"]) == 2


def test_chat_executes_note_observation_set_and_update_goal(client: TestClient, config, monkeypatch):
    _seed_single_observation(config)

    def fake_call_llm(*, system_prompt, history, user_text, config):
        return (
            "Alles klar! Ich habe die Notiz ergänzt, 19.2 °C erfasst und dein Ernte-Ziel angelegt.",
            [
                {"type": "add_note", "text": "Kleine Primordien sichtbar"},
                {
                    "type": "record_observation",
                    "property_name": "temperature_c",
                    "value_num": 19.2,
                    "value_sigma": 0.3,
                    "value_unit": "°C",
                    "label": "Box-Temperatur 19,2 °C",
                },
                {
                    "type": "set_goal",
                    "property_name": "harvest_ready",
                    "label": "Erntegewicht Ziel",
                    "value_unit": "g",
                    "target_min": 220.0,
                },
            ],
        )

    monkeypatch.setattr(chat_agent, "_call_llm", fake_call_llm)

    res = client.post(
        "/api/chat/messages",
        headers=HEADERS,
        json={
            "message": "Notiere Primordien, 19.2 Grad und Ziel 220 g.",
            "scope_type": "entity",
            "scope_id": "hericium-chat-1",
        },
    )
    assert res.status_code == 200
    body = res.json()
    actions = body["executed_actions"]
    assert len(actions) == 3
    assert all(a["ok"] for a in actions)

    updated_obs = body["updated_observation"]
    assert "Kleine Primordien sichtbar" in updated_obs["description"]

    goal_id = actions[2]["details"]["goal_id"]
    states = body["snapshot"]["states"]
    assert any(s["property_name"] == "temperature_c" and s["value_num"] == 19.2 for s in states)
    assert any(s["id"] == goal_id and s["target_min"] == 220.0 for s in states)

    def fake_update_llm(*, system_prompt, history, user_text, config):
        return (
            "Zielmenge auf 180 g angepasst.",
            [{"type": "update_goal", "goal_id": goal_id, "target_min": 180.0}],
        )

    monkeypatch.setattr(chat_agent, "_call_llm", fake_update_llm)
    res2 = client.post(
        "/api/chat/messages",
        headers=HEADERS,
        json={
            "message": "Setze das Ziel lieber auf 180 g.",
            "scope_type": "entity",
            "scope_id": "hericium-chat-1",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["executed_actions"][0]["ok"] is True
    updated_states = res2.json()["snapshot"]["states"]
    assert any(s["id"] == goal_id and s["target_min"] == 180.0 for s in updated_states)


def test_chat_transcribe_endpoint(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.routes.chat.transcribe_audio",
        lambda raw, mime_type="", filename=None: f"Transkribiert ({len(raw)} Bytes)",
    )
    res = client.post(
        "/api/chat/transcribe",
        headers={"Origin": "http://localhost:3000"},
        files={"audio": ("voice.webm", b"WEBM_FAKE_BYTES", "audio/webm")},
    )
    assert res.status_code == 200
    assert res.json() == {"text": "Transkribiert (15 Bytes)"}
