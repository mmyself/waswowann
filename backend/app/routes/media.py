import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.db import db, now_ms
from app.routes.observations import _detail
from app.schemas import MediaPatch
from app.services.gaussian_fusion import MEASUREMENT_FIELDS, recompute_observation_kalman

router = APIRouter(prefix='/api/media', tags=['media'])
logger = logging.getLogger(__name__)


def _not_found():
    return HTTPException(404, detail={'error': {'code': 'NOT_FOUND', 'message': 'Medium nicht gefunden'}})


def _media(media_id, column):
    with db() as conn:
        row = conn.execute(f'SELECT {column} AS path FROM media WHERE id=?', (media_id,)).fetchone()
    if not row:
        raise _not_found()
    root = settings.photo_storage_dir.resolve()
    path = (root / row['path']).resolve()
    if root not in path.parents or not path.is_file():
        raise _not_found()
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


@router.get('/{media_id}/image')
def image(media_id: str):
    return _media(media_id, 'local_path')


@router.get('/{media_id}/thumbnail')
def thumbnail(media_id: str):
    return _media(media_id, 'thumbnail_path')


def _updated_observation(conn, obs_id):
    recompute_observation_kalman(conn, obs_id)
    conn.execute('UPDATE observations SET updated_at_ms=? WHERE id=?', (now_ms(), obs_id))
    return _detail(conn, obs_id)


@router.patch('/{media_id}')
def patch_media(media_id: str, body: MediaPatch):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, detail='Mindestens ein Feld ist erforderlich')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM media WHERE id=?', (media_id,)).fetchone()
        if row is None:
            raise _not_found()
        values = {key: row[key] for key in MEASUREMENT_FIELDS} | changes
        exif = json.loads(row['exif_json'])
        for value_key, sigma_key, source_key in (
            ('lat', 'sigma_meters', 'location_source'), ('observed_at_ms', 'sigma_seconds', 'time_source'),
        ):
            if values[value_key] is None:
                if changes.get(sigma_key) is not None:
                    raise HTTPException(422, detail=f'{sigma_key} erfordert einen Messwert')
                values[sigma_key] = None
                values[source_key] = 'unknown'
                exif.pop(f'{sigma_key}_source', None)
            else:
                if value_key in changes:
                    values[source_key] = changes.get(source_key) or 'manual'
                elif values[source_key] is None:
                    values[source_key] = 'unknown'
                if sigma_key in changes:
                    exif[f'{sigma_key}_source'] = 'manual'
        values.update(exif_json=json.dumps(exif, ensure_ascii=False), id=media_id)
        conn.execute(f"UPDATE media SET {', '.join(f'{key}=:{key}' for key in (*MEASUREMENT_FIELDS, 'exif_json'))} WHERE id=:id", values)
        return _updated_observation(conn, row['obs_id'])


@router.delete('/{media_id}')
def delete_media(media_id: str):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT obs_id, local_path, thumbnail_path FROM media WHERE id=?', (media_id,)).fetchone()
        if row is None:
            raise _not_found()
        if not conn.execute('SELECT 1 FROM media WHERE obs_id=? AND id!=? LIMIT 1', (row['obs_id'], media_id)).fetchone():
            raise HTTPException(409, detail={'error': {'code': 'LAST_PHOTO', 'message': 'Das letzte Foto kann nicht gelöscht werden'}})
        conn.execute('DELETE FROM media WHERE id=?', (media_id,))
        result = _updated_observation(conn, row['obs_id'])
        paths = [path for path in {row['local_path'], row['thumbnail_path']} if not conn.execute(
            'SELECT 1 FROM media WHERE local_path=? OR thumbnail_path=? LIMIT 1', (path, path),
        ).fetchone()]
    root = settings.photo_storage_dir.resolve()
    for path in paths:
        stored = root / path
        if root not in stored.resolve().parents:
            continue
        try:
            stored.unlink(missing_ok=True)
        except OSError:
            logger.warning('Could not remove files for deleted media %s', media_id)
    return result
