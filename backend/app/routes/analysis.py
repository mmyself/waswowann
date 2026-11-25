from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db import db, now_ms
from app.services.processing_jobs import runner

router = APIRouter(prefix='/api/observations', tags=['analysis'])


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str | None = Field(default=None, max_length=2000)
    parent_note_id: str | None = None


@router.post('/{observation_id}/analysis', status_code=202)
def request_analysis(observation_id: str, body: Request):
    question = body.question.strip() if body.question else None
    with db() as conn:
        if not conn.execute('SELECT 1 FROM observations WHERE id=?', (observation_id,)).fetchone():
            raise HTTPException(404, detail={'error': {'code': 'NOT_FOUND', 'message': 'Beobachtung nicht gefunden'}})
        if body.parent_note_id and not conn.execute(
            'SELECT 1 FROM gemini_notes WHERE id=? AND obs_id=?', (body.parent_note_id, observation_id)
        ).fetchone():
            raise HTTPException(422, detail={'error': {'code': 'INVALID_PARENT_NOTE', 'message': 'Rückfrage gehört nicht zu dieser Beobachtung'}})
        input_key = str(uuid.uuid4())
        note_id, stamp = str(uuid.uuid4()), now_ms()
        conn.execute("INSERT INTO gemini_notes(id,obs_id,source,status,created_at_ms,parent_note_id,question_text,provider,model) VALUES(?,?,?,?,?,?,?,?,?)",
                     (note_id, observation_id, 'gemini_api', 'queued', stamp, body.parent_note_id, question, '', ''))
        job_id = str(uuid.uuid4())
        conn.execute('''INSERT INTO processing_jobs(id,obs_id,kind,note_id,input_key,status,created_at_ms,updated_at_ms)
            VALUES(?,?,'gemini',?,?,'queued',?,?)''', (job_id, observation_id, note_id, input_key, stamp, stamp))
    runner.notify()
    return {'note_id': note_id, 'status': 'queued'}
