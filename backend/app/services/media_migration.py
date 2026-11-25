import json
from collections import defaultdict

from PIL import Image

from app.config import Settings
from app.services.gaussian_fusion import MEASUREMENT_FIELDS, recompute_observation_kalman
from app.services.observation_merge import move_observation_contents
from app.services.photo_ingest import _exif_timestamp, _exif_uncertainties, _metadata


def migrate_media_measurements(conn, config: Settings, stamp: int) -> None:
    root = config.photo_storage_dir.resolve()
    photos = conn.execute('''SELECT m.*, row_number() OVER (PARTITION BY obs_id ORDER BY created_at_ms, id) AS photo_number,
        count(*) OVER (PARTITION BY obs_id) AS photo_count FROM media AS m ORDER BY obs_id, photo_number''').fetchall()
    for row in photos:
        values = dict(row)
        raw = json.loads(row['exif_json'])
        prefix = 'No EXIF offset; interpreted in '
        assumption = raw.get('time_assumption') or ''
        timezone_name = assumption[len(prefix):] if assumption.startswith(prefix) else config.import_timezone
        exif_time, _ = _exif_timestamp(raw, timezone_name)
        path = (root / row['local_path']).resolve()
        lat = lon = photo_sigma_m = photo_sigma_s = None
        if root in path.parents and path.is_file():
            try:
                with Image.open(path) as image:
                    tags, lat, lon, original_time, _ = _metadata(image, timezone_name)
                    photo_sigma_m, photo_sigma_s = _exif_uncertainties(image, tags, lat, original_time, config)
                    exif_time = original_time if original_time is not None else exif_time
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        keep_location = row['photo_number'] == 1 and row['location_source'] in ('manual', 'browser')
        keep_time = row['photo_number'] == 1 and row['time_source'] in ('manual', 'google')
        if lat is not None and not keep_location:
            values.update(lat=lat, lon=lon, sigma_meters=photo_sigma_m, location_source='exif')
        elif row['lat'] is not None and row['photo_count'] > 1 and not keep_location:
            values['location_source'] = 'legacy'
        if exif_time is not None and not keep_time:
            values.update(observed_at_ms=exif_time, sigma_seconds=photo_sigma_s or row['sigma_seconds'], time_source='exif')
        elif row['observed_at_ms'] is not None and row['photo_count'] > 1 and not keep_time:
            values['time_source'] = 'legacy'
        conn.execute(f"UPDATE media SET {', '.join(f'{k}=:{k}' for k in MEASUREMENT_FIELDS)} WHERE id=:id", values)

    groups = defaultdict(list)
    for row in conn.execute('''SELECT g.group_id, o.id FROM legacy_group_members AS g
        JOIN observations AS o ON o.id=g.id
        ORDER BY g.group_id, (SELECT count(*) FROM media WHERE obs_id=o.id) DESC, o.created_at_ms, o.id'''):
        groups[row['group_id']].append(row['id'])
    for target_id, *sources in groups.values():
        if sources:
            move_observation_contents(conn, target_id, sources, stamp)
    for row in conn.execute('SELECT id FROM observations ORDER BY id'):
        recompute_observation_kalman(conn, row['id'], config=config)
    conn.execute('DROP TABLE legacy_group_members')
