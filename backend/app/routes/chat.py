"""REST endpoints for global & per-entity chat and voice transcription."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.services.chat_agent import (
    AudioTranscriptionUnavailableError,
    ChatScopeType,
    clear_messages,
    handle_chat_message,
    list_messages,
    transcribe_audio,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatSendRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    scope_type: ChatScopeType = "global"
    scope_id: str = Field(default="global", min_length=1, max_length=128)


@router.get("/messages")
def get_chat_messages(
    scope_type: Literal["global", "entity"] = Query(default="global"),
    scope_id: str = Query(default="global", min_length=1, max_length=128),
) -> dict[str, Any]:
    resolved_id = "global" if scope_type == "global" else scope_id.strip()
    return {
        "scope_type": scope_type,
        "scope_id": resolved_id,
        "messages": list_messages(scope_type, resolved_id),
    }


@router.post("/messages")
def post_chat_message(payload: ChatSendRequest) -> dict[str, Any]:
    if not payload.message.strip():
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "EMPTY_MESSAGE", "message": "Bitte gib eine Nachricht ein."}},
        )
    return handle_chat_message(
        message=payload.message,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
    )


@router.delete("/messages")
def delete_chat_messages(
    scope_type: Literal["global", "entity"] = Query(default="global"),
    scope_id: str = Query(default="global", min_length=1, max_length=128),
) -> dict[str, int]:
    resolved_id = "global" if scope_type == "global" else scope_id.strip()
    return {"deleted": clear_messages(scope_type, resolved_id)}


@router.post("/transcribe")
async def post_transcribe_audio(audio: UploadFile = File(...)) -> dict[str, str]:
    raw = await audio.read()
    try:
        text = transcribe_audio(
            raw,
            mime_type=audio.content_type or "audio/webm",
            filename=audio.filename,
        )
        return {"text": text}
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_AUDIO", "message": str(exc)}},
        ) from exc
    except AudioTranscriptionUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "TRANSCRIPTION_UNAVAILABLE", "message": str(exc)}},
        ) from exc
