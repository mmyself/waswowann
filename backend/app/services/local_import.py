from __future__ import annotations

import os
from pathlib import Path

from app.config import Settings, settings
from app.db import db
from app.services.photo_ingest import IngestError, SUPPORTED, ingest_image
from app.services.upload_ingest import queue_observation_jobs


def import_local(config: Settings = settings, report=None) -> tuple[int, int, int]:
    root = config.local_import_dir
    if not root.is_dir():
        raise FileNotFoundError('LOCAL_IMPORT_NOT_CONFIGURED')
    root_real = root.resolve()
    imported = skipped = failed = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        dirs[:] = [d for d in dirs if not (base_path / d).is_symlink()]
        for name in files:
            path = base_path / name
            if path.is_symlink() or path.suffix.lower() not in SUPPORTED:
                if path.suffix.lower() in SUPPORTED:
                    failed += 1
                    if report: report('failed', 'UNSAFE_PATH')
                continue
            try:
                real = path.resolve(strict=True)
                if root_real not in real.parents:
                    raise IngestError('Unsafe path')
                data = real.read_bytes()
                media_id, result = ingest_image(source='local', external_id=str(real), filename=path.name, data=data, config=config)
                if result == 'imported':
                    imported += 1
                    if media_id:
                        with db(config) as conn:
                            row = conn.execute('SELECT obs_id FROM media WHERE id=?', (media_id,)).fetchone()
                        if row:
                            queue_observation_jobs(row['obs_id'], config)
                else: skipped += 1
                if report: report(result, None)
            except (OSError, IngestError):
                failed += 1
                if report: report('failed', 'IMAGE_IMPORT_FAILED')
    return imported, skipped, failed
