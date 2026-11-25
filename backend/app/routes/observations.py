from __future__ import annotations

import json
import math
import uuid
from fastapi import APIRouter, HTTPException

from app.db import db, now_ms
from app.schemas import ObservationCreate, ObservationMerge, ObservationPatch, from_ms
from app.services.gaussian_fusion import (
    MEASUREMENT_FIELDS,
    TIMELINE_ORDER,
    _xy,
    diagnose_media_locations,
    recompute_observation_kalman,
)
from app.services.observation_merge import move_observation_contents

router = APIRouter(prefix='/api/observations', tags=['observations'])


def _not_found():
    return HTTPException(404, detail={'error': {'code': 'NOT_FOUND', 'message': 'Beobachtung nicht gefunden'}})


def _summary(row):
    return {
        **{key: row[key] for key in ('lat', 'lon', 'sigma_meters', 'sigma_seconds', 'location_measurement_count')},
        **{key: from_ms(row[f'{key}_ms']) for key in ('observed_at', 'first_observed_at', 'last_observed_at')},
    }


def _note_status(note):
    return 'unavailable' if note['error_code'] == 'ANALYSIS_NOT_CONFIGURED' else note['status']


def _detail(conn, oid):
    row = conn.execute('''SELECT o.*, s.* FROM observations o
        JOIN space_time_gauss s ON s.obs_id=o.id WHERE o.id=?''', (oid,)).fetchone()
    if row is None:
        return None
    media = conn.execute(f'SELECT * FROM media WHERE obs_id=? ORDER BY {TIMELINE_ORDER}', (oid,)).fetchall()
    notes = conn.execute('SELECT * FROM gemini_notes WHERE obs_id=? ORDER BY created_at_ms, id', (oid,)).fetchall()
    note = notes[-1] if notes else None
    structured = json.loads(note['structured_json']) if note else {}
    summary = _summary(row)
    patterns = {key: json.loads(row[key]) for key in ('time_of_day', 'season')}
    _, diagnostics = diagnose_media_locations(media, environment=row['environment'])
    outlier_count = sum(1 for item in diagnostics.values() if item['kalman_outlier'])
    return {
        **{key: row[key] for key in ('id', 'title', 'category', 'environment', 'description')},
        'created_at': from_ms(row['created_at_ms']), 'updated_at': from_ms(row['updated_at_ms']),
        **summary, **patterns, 'outlier_count': outlier_count, 'media_count': len(media),
        'source': media[0]['source'] if media else None,
        'thumbnail_url': f"/api/media/{media[0]['id']}/thumbnail" if media else None,
        'space_time': {**summary, **patterns, 'outlier_count': outlier_count,
                       'location_source': row['location_source'], 'time_source': row['time_source']},
        'media': [{
            **{key: m[key] for key in ('id', 'source', 'width', 'height', *MEASUREMENT_FIELDS)},
            **diagnostics.get(idx, {
                'effective_sigma_meters': None, 'kalman_distance_meters': None,
                'kalman_z_score': None, 'kalman_weight': None, 'kalman_outlier': False,
            }),
            'filename': m['original_name'], 'image_url': f"/api/media/{m['id']}/image",
            'thumbnail_url': f"/api/media/{m['id']}/thumbnail", 'exif': json.loads(m['exif_json']),
        } for idx, m in enumerate(media)],
        'gemini': {
            'status': _note_status(note) if note else 'not_requested',
            'source': note['source'] if note else 'placeholder', 'text': note['raw_text'] if note else '',
            'confidence': structured.get('confidence'), 'tags': structured.get('tags', []), 'result': structured or None,
            'note_id': note['id'] if note else None, 'error_code': note['error_code'] if note else None,
            'history': [{
                'note_id': n['id'], 'status': _note_status(n), 'text': n['raw_text'],
                'result': json.loads(n['structured_json']), 'created_at': from_ms(n['created_at_ms']),
                **{key: n[key] for key in ('question_text', 'parent_note_id', 'provider', 'model', 'error_code')},
            } for n in notes],
        },
    }


def _list_rows(conn):
    return conn.execute(f'''SELECT o.id, o.title, o.category, o.environment, s.*, m.id AS media_id, m.source,
        (SELECT count(*) FROM media WHERE obs_id=o.id) AS media_count
        FROM observations o JOIN space_time_gauss s ON s.obs_id=o.id
        LEFT JOIN media m ON m.id=(SELECT id FROM media WHERE obs_id=o.id ORDER BY {TIMELINE_ORDER} LIMIT 1)
        ORDER BY o.created_at_ms DESC, o.id DESC''').fetchall()


def _list_item(row):
    return {
        **{key: row[key] for key in ('id', 'title', 'category', 'environment', 'media_count', 'source')},
        **_summary(row),
        'thumbnail_url': f"/api/media/{row['media_id']}/thumbnail" if row['media_id'] else None,
    }


@router.get('')
def list_observations():
    with db() as conn:
        rows = _list_rows(conn)
    dated = [r for r in rows if r['first_observed_at_ms'] is not None]
    return {
        'items': [_list_item(row) for row in rows], 'total': len(rows),
        'located_count': sum(r['lat'] is not None for r in rows), 'undated_count': len(rows) - len(dated),
        'time_extent': {'min': from_ms(min(r['first_observed_at_ms'] for r in dated)),
                        'max': from_ms(max(r['last_observed_at_ms'] for r in dated))} if dated else None,
    }


@router.get('/{observation_id}')
def get_observation(observation_id: str):
    with db() as conn:
        result = _detail(conn, observation_id)
    if result is None:
        raise _not_found()
    return result


@router.get('/{observation_id}/merge-candidates')
def merge_candidates(observation_id: str):
    with db() as conn:
        rows = _list_rows(conn)
    target = next((row for row in rows if row['id'] == observation_id), None)
    if target is None:
        raise _not_found()
    if target['lat'] is None:
        return {'items': []}
    items = []
    for row in rows:
        if row['id'] == observation_id or row['lat'] is None:
            continue
        distance = math.hypot(*_xy(row['lat'], row['lon'], target['lat'], target['lon']))
        if distance <= 200:
            items.append({**_list_item(row), 'observation_id': row['id'], 'distance_meters': round(distance, 2)})
    return {'items': sorted(items, key=lambda item: (item['distance_meters'], item['observation_id']))}


@router.post('', status_code=201)
def create_observation(body: ObservationCreate):
    oid, current = str(uuid.uuid4()), now_ms()
    with db() as conn:
        conn.execute('INSERT INTO observations(id,category,title,description,environment,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?)',
                     (oid, body.category, body.title, body.description, body.environment, current, current))
        recompute_observation_kalman(conn, oid)
        conn.execute('INSERT INTO gemini_notes(id,obs_id,source,status,created_at_ms) VALUES(?,?,?,?,?)',
                     (str(uuid.uuid4()), oid, 'placeholder', 'not_requested', current))
        return _detail(conn, oid)


@router.patch('/{observation_id}')
def patch_observation(observation_id: str, body: ObservationPatch):
    values = body.model_dump(exclude_unset=True)
    if not values or None in values.values():
        raise HTTPException(422, detail='Mindestens ein Feld mit einem Wert ist erforderlich')
    with db() as conn:
        cursor = conn.execute(f"UPDATE observations SET {', '.join(f'{key}=?' for key in values)}, updated_at_ms=? WHERE id=?",
                              (*values.values(), now_ms(), observation_id))
        if not cursor.rowcount:
            raise _not_found()
        if 'environment' in values:
            recompute_observation_kalman(conn, observation_id)
        return _detail(conn, observation_id)


@router.post('/{observation_id}/merge')
def merge_observation(observation_id: str, body: ObservationMerge):
    source_ids = body.source_observation_ids
    if observation_id in source_ids:
        raise HTTPException(422, detail={'error': {'code': 'SAME_OBSERVATION', 'message': 'Beobachtung kann nicht mit sich selbst zusammengeführt werden'}})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        ids = [observation_id, *source_ids]
        count = conn.execute(f"SELECT count(*) FROM observations WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchone()[0]
        if count != len(ids):
            raise _not_found()
        move_observation_contents(conn, observation_id, source_ids, now_ms())
        recompute_observation_kalman(conn, observation_id)
        return _detail(conn, observation_id)
