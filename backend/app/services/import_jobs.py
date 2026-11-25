from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings, settings
from app.db import db, now_ms
from app.services.demo_data import seed_demo
from app.services.local_import import import_local

_active = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='import-job')


def recover_jobs(config: Settings = settings) -> None:
    with db(config) as conn:
        conn.execute("UPDATE import_jobs SET status='failed',finished_at_ms=?,errors_json='[\"PROCESS_RESTARTED\"]' WHERE status IN ('queued','running')", (now_ms(),))
        conn.execute("UPDATE picker_sessions SET status='failed',last_error_code='PROCESS_RESTARTED',updated_at_ms=? WHERE status IN ('waiting','ready','importing')", (now_ms(),))


def start_job(source: str, config: Settings = settings, picker_session_id: str | None = None) -> str:
    if not _active.acquire(blocking=False):
        raise RuntimeError('IMPORT_BUSY')
    job_id = str(uuid.uuid4())
    try:
        with db(config) as conn:
            conn.execute("INSERT INTO import_jobs(id,source,picker_session_id,status,created_at_ms) VALUES(?,?,?,'queued',?)", (job_id, source, picker_session_id, now_ms()))
        loop = asyncio.get_running_loop()
        loop.run_in_executor(_executor, _run, job_id, source, config, picker_session_id)
        return job_id
    except Exception:
        _active.release()
        raise


def _run(job_id: str, source: str, config: Settings, picker_session_id: str | None):
    with db(config) as conn:
        conn.execute("UPDATE import_jobs SET status='running' WHERE id=?", (job_id,))
    counts = {'imported': 0, 'skipped': 0, 'failed': 0}
    errors = []
    def report(result, code):
        counts[result] += 1
        if code and len(errors) < 100: errors.append(code)
        with db(config) as conn:
            conn.execute('UPDATE import_jobs SET processed=?,imported=?,skipped=?,failed=?,errors_json=? WHERE id=?',
                         (sum(counts.values()), counts['imported'], counts['skipped'], counts['failed'], json.dumps(errors), job_id))
    try:
        if source == 'demo':
            count = seed_demo(config)
            # Demo seed reports the actual number of newly created fixtures.
            for _ in range(count):
                report('imported', None)
        elif source == 'local':
            import_local(config, report)
        else:
            raise RuntimeError('GOOGLE_JOB_REQUIRES_PICKER')
        status = 'partial' if counts['failed'] else 'completed'
    except Exception:
        errors.append('IMPORT_FAILED')
        status = 'failed'
    finally:
        with db(config) as conn:
            conn.execute('UPDATE import_jobs SET status=?,finished_at_ms=?,errors_json=? WHERE id=?', (status, now_ms(), json.dumps(errors[:100]), job_id))
            if picker_session_id:
                conn.execute('UPDATE picker_sessions SET status=?,updated_at_ms=? WHERE id=?', ('completed' if status in ('completed','partial') else 'failed', now_ms(), picker_session_id))
        _active.release()


def get_job(job_id: str, config: Settings = settings):
    with db(config) as conn:
        row = conn.execute('SELECT * FROM import_jobs WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None
