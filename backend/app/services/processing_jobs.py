from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx

from app.config import settings
from app.db import db, now_ms
from app.services.gaussian_fusion import TIMELINE_ORDER, recompute_observation_kalman
from app.services.gemini_client import analyze

class JobRunner:
    def __init__(self, workers: int=2):
        self.workers=workers; self.tasks=[]; self.stopping=False; self.wake=asyncio.Event()
    async def start(self):
        self.stopping=False
        self.wake=asyncio.Event()
        with db() as conn:
            conn.execute("UPDATE processing_jobs SET status='queued',next_attempt_at_ms=? WHERE status='running'",(now_ms(),))
        self.tasks=[asyncio.create_task(self._worker()) for _ in range(self.workers)]
    async def stop(self):
        self.stopping=True; self.wake.set()
        for task in self.tasks: task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError): await task
        self.tasks=[]
    def notify(self): self.wake.set()
    async def _worker(self):
        while not self.stopping:
            job=self._claim()
            if job:
                await asyncio.to_thread(self._run,job)
            else:
                self.wake.clear()
                try: await asyncio.wait_for(self.wake.wait(),timeout=1)
                except asyncio.TimeoutError: pass
    def _claim(self):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute("SELECT * FROM processing_jobs WHERE status='queued' AND next_attempt_at_ms<=? ORDER BY created_at_ms, id LIMIT 1",(now_ms(),)).fetchone()
            if not row:return None
            stamp = now_ms()
            conn.execute("UPDATE processing_jobs SET status='running',attempts=attempts+1,updated_at_ms=? WHERE id=? AND status='queued'",(stamp,row['id']))
            if row['note_id']:
                conn.execute("UPDATE gemini_notes SET status='running',error_code=NULL WHERE id=?",(row['note_id'],))
            return dict(row)
    def _run(self,job):
        try:
            if job['kind']=='gemini': self._analysis(job)
            with db() as conn:
                conn.execute("UPDATE processing_jobs SET status='completed',updated_at_ms=? WHERE id=? AND obs_id=? AND status='running'", (now_ms(), job['id'], job['obs_id']))
        except Exception as exc:
            code = str(exc)[:80] or 'PROCESSING_FAILED'
            permanent = code in {'ANALYSIS_NOT_CONFIGURED', 'ANALYSIS_SCHEMA_INVALID', 'ANALYSIS_INPUT_MISSING'}
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                code = 'ANALYSIS_AUTH_FAILED' if status_code in (401, 403) else 'ANALYSIS_HTTP_FAILED'
                permanent = status_code in (400, 401, 403, 404, 413, 422)
            elif isinstance(exc, (ValueError, KeyError, TypeError)):
                permanent = permanent or (job['kind'] == 'gemini')
                if job['kind'] == 'gemini' and code not in {'ANALYSIS_SCHEMA_INVALID','ANALYSIS_INPUT_MISSING'}:
                    code = 'ANALYSIS_SCHEMA_INVALID'
            stamp = now_ms()
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute(
                    "SELECT attempts FROM processing_jobs WHERE id=? AND obs_id=? AND status='running'",
                    (job['id'], job['obs_id']),
                ).fetchone()
                if not row:
                    return
                if row['attempts'] < 3 and not permanent:
                    retry_ms = min(300000, 5000 * 2 ** (row['attempts'] - 1))
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                        retry_after = exc.response.headers.get('Retry-After')
                        if retry_after and retry_after.isdigit():
                            retry_ms = min(300000, max(retry_ms, int(retry_after) * 1000))
                    conn.execute("UPDATE processing_jobs SET status='queued',next_attempt_at_ms=?,error_code=?,updated_at_ms=? WHERE id=?",(stamp+retry_ms,code,stamp,job['id']))
                else:
                    conn.execute("UPDATE processing_jobs SET status='failed',error_code=?,updated_at_ms=? WHERE id=?",(code,stamp,job['id']))
                    if job.get('note_id'): conn.execute("UPDATE gemini_notes SET status='error',error_code=? WHERE id=?",(code,job['note_id']))
    def _analysis(self,job):
        with db() as conn:
            note=conn.execute('SELECT * FROM gemini_notes WHERE id=? AND obs_id=?',(job['note_id'],job['obs_id'])).fetchone()
            media_rows=conn.execute(f'SELECT * FROM media WHERE obs_id=? ORDER BY {TIMELINE_ORDER} LIMIT 6',(job['obs_id'],)).fetchall()
            media=media_rows[0] if media_rows else None
            obs=conn.execute('SELECT title,category,description,environment FROM observations WHERE id=?',(job['obs_id'],)).fetchone()
            if not note or not media or not obs: raise RuntimeError('ANALYSIS_INPUT_MISSING')
            images_payload: list[tuple[bytes, str]] = []
            for mrow in media_rows:
                mpath=settings.photo_storage_dir/mrow['local_path']
                if settings.photo_storage_dir.resolve() not in mpath.resolve().parents: raise RuntimeError('ANALYSIS_INPUT_MISSING')
                images_payload.append((mpath.read_bytes(), mrow['mime_type']))
            image = images_payload[0][0]
            exif=json.loads(media['exif_json'] or '{}')
            st_hint = (
                f"; Umgebung={obs['environment']}; Messdaten des ersten Fotos: "
                f"OrtQuelle={media['location_source']} (sigma_meters={media['sigma_meters']}); "
                f"ZeitQuelle={media['time_source']} (sigma_seconds={media['sigma_seconds']})"
            )
            if media['lat'] is not None and media['lon'] is not None:
                st_hint += f"; Koordinaten={media['lat']:.4f}, {media['lon']:.4f}"
            if media['observed_at_ms'] is not None:
                from datetime import datetime, timezone
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                try:
                    tz = ZoneInfo(settings.import_timezone)
                except ZoneInfoNotFoundError:
                    tz = timezone.utc
                local_dt = datetime.fromtimestamp(media['observed_at_ms'] / 1000.0, tz=tz)
                st_hint += f"; Aufnahmezeit={local_dt.strftime('%d.%m.%Y %H:%M (%B)')}"
            if exif.get('camera_model'):
                st_hint += f"; Kamera={exif.get('camera_make', '')} {exif['camera_model']}"
            if exif.get('time_assumption'):
                st_hint += f"; Zeithinweis={exif['time_assumption']}"
            context=f"Titel={obs['title']}; Kategorie={obs['category']}; Beschreibung={obs['description']}{st_hint}"
            history=conn.execute('SELECT question_text,raw_text,structured_json FROM gemini_notes WHERE obs_id=? ORDER BY created_at_ms,id',(job['obs_id'],)).fetchall()
            parent_chain=[]
            parent_id=note['parent_note_id']
            while parent_id:
                parent=conn.execute('SELECT id,parent_note_id FROM gemini_notes WHERE id=? AND obs_id=?',(parent_id,job['obs_id'])).fetchone()
                if not parent: break
                parent_chain.append(parent['id'])
                parent_id=parent['parent_note_id']
            if history:
                context += '\nBisheriger Analyseverlauf: '
                context += json.dumps([{'question':n['question_text'],'text':n['raw_text'],'result':json.loads(n['structured_json'])} for n in history],ensure_ascii=False)[-12000:]
            if parent_chain:
                context += '\nElternkette: ' + ' -> '.join(reversed(parent_chain))
            prompt=note['question_text']
        result=analyze(image,media['mime_type'],context,prompt,images=images_payload)
        structured={
            'title':result.title,
            'category':result.category,
            'description':result.description,
            'scientific_name':result.scientific_name,
            'confidence':result.confidence,
            'tags':result.tags,
            'alternatives':result.alternatives,
            'questions':result.questions,
            'warnings':result.warnings,
            'estimated_sigma_meters':result.estimated_sigma_meters,
            'estimated_sigma_seconds':result.estimated_sigma_seconds,
            'uncertainty_rationale':result.uncertainty_rationale,
            'environment':result.environment,
            'apply_correction':result.apply_correction,
        }
        valid_categories = {'Pilz', 'Pflanze', 'Person', 'Ding', 'Ereignis', 'Unbekannt'}
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            obs = conn.execute('SELECT title,category,environment FROM observations WHERE id=?', (job['obs_id'],)).fetchone()
            current_media = conn.execute('SELECT * FROM media WHERE id=? AND obs_id=?', (media['id'], job['obs_id'])).fetchone()
            if not obs or not current_media:
                raise RuntimeError('ANALYSIS_INPUT_MISSING')
            saved = conn.execute(
                "UPDATE gemini_notes SET status='ready',raw_text=?,structured_json=?,provider=?,model=?,error_code=NULL WHERE id=? AND obs_id=?",
                (result.description or result.title, json.dumps(structured, ensure_ascii=False), settings.gemini_provider, settings.gemini_model, job['note_id'], job['obs_id']),
            )
            if not saved.rowcount:
                raise RuntimeError('ANALYSIS_INPUT_MISSING')
            next_title = obs['title']
            next_cat = obs['category']
            next_env = obs['environment']
            is_initial = note['parent_note_id'] is None
            current_title = (obs['title'] or '').strip()
            looks_like_filename = current_title.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic'))
            if is_initial and result.title:
                if not current_title or current_title == 'Unbekannt' or looks_like_filename:
                    next_title = result.title[:200]
                    if obs['category'] in ('', 'Unbekannt', None) and result.category in valid_categories:
                        next_cat = result.category
                if obs['environment'] == 'unknown' and result.environment in ('indoor', 'outdoor'):
                    next_env = result.environment
            elif not is_initial and result.apply_correction:
                candidate_title = (result.title or '').strip()
                if candidate_title and candidate_title != 'Unbekannt':
                    next_title = candidate_title[:200]
                if result.category in valid_categories and (result.category != 'Unbekannt' or obs['category'] == 'Unbekannt'):
                    next_cat = result.category
                if result.environment in ('indoor', 'outdoor'):
                    next_env = result.environment
            if (next_title, next_cat, next_env) != (obs['title'], obs['category'], obs['environment']):
                conn.execute(
                    "UPDATE observations SET title=?,category=?,environment=?,updated_at_ms=? WHERE id=?",
                    (next_title, next_cat, next_env, now_ms(), job['obs_id']),
                )
            original_exif = json.loads(current_media['exif_json'] or '{}')
            cur_exif = original_exif.copy()
            sigma_meters = current_media['sigma_meters']
            sigma_seconds = current_media['sigma_seconds']
            measured_gps_error = 'gps_h_positioning_error_m' in cur_exif or 'gps_dop' in cur_exif
            exif_offset = any(k in cur_exif for k in ('offset_time', 'offset_time_original', 'offset_time_digitized'))
            if (
                result.estimated_sigma_meters is not None
                and current_media['lat'] is not None
                and current_media['location_source'] not in ('manual', 'browser')
                and not measured_gps_error
                and cur_exif.get('sigma_meters_source') != 'manual'
            ):
                sigma_meters = result.estimated_sigma_meters
                cur_exif['sigma_meters_source'] = 'gemini'
            if (
                result.estimated_sigma_seconds is not None
                and current_media['observed_at_ms'] is not None
                and current_media['time_source'] != 'manual'
                and not exif_offset
                and cur_exif.get('sigma_seconds_source') != 'manual'
            ):
                sigma_seconds = result.estimated_sigma_seconds
                cur_exif['sigma_seconds_source'] = 'gemini'
            if result.uncertainty_rationale:
                cur_exif['uncertainty_rationale'] = result.uncertainty_rationale
            sigma_changed = (sigma_meters, sigma_seconds) != (current_media['sigma_meters'], current_media['sigma_seconds'])
            if sigma_changed or cur_exif != original_exif:
                conn.execute(
                    'UPDATE media SET sigma_meters=?,sigma_seconds=?,exif_json=? WHERE id=?',
                    (sigma_meters, sigma_seconds, json.dumps(cur_exif, ensure_ascii=False), media['id']),
                )
            if sigma_changed:
                recompute_observation_kalman(conn, job['obs_id'], config=settings)
                conn.execute('UPDATE observations SET updated_at_ms=? WHERE id=?', (now_ms(), job['obs_id']))


runner=JobRunner()
