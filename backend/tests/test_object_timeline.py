from __future__ import annotations

import io
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import db, migrate
from app.main import app
from app.services.gaussian_fusion import _xy, diagnose_media_locations, effective_location_sigma, fuse_media_measurements

ORIGIN = {'Origin': 'http://localhost:3000'}


@pytest.fixture
def timeline_client(config, monkeypatch):
    import app.db as db_module
    import app.main as main_module
    import app.routes.media as media_route
    import app.routes.uploads as uploads_route
    from app.services.processing_jobs import runner

    monkeypatch.setattr(db_module, 'settings', config)
    monkeypatch.setattr(main_module, 'settings', config)
    monkeypatch.setattr(media_route, 'settings', config)
    monkeypatch.setattr(uploads_route, 'settings', config)
    monkeypatch.setattr(runner, 'notify', lambda: None)
    migrate(config)
    return TestClient(app)


def jpeg(color, *, date=None, lat_minutes=None, lon_minutes=None, sigma=None):
    exif = Image.Exif()
    if date:
        exif[0x8769] = {36867: date, 36881: '+01:00'}
    if lat_minutes is not None:
        exif[0x8825] = {1: 'N', 2: (53.0, lat_minutes, 0.0), 3: 'E', 4: (9.0, lon_minutes, 0.0), 31: sigma}
    output = io.BytesIO()
    Image.new('RGB', (40, 30), color).save(output, 'JPEG', exif=exif.tobytes())
    return output.getvalue()


def upload(client, name, image, *, target=None):
    response = client.post('/api/uploads', files={'files': (name, image, 'image/jpeg')},
                           data={'target_observation_id': target} if target else {}, headers=ORIGIN)
    assert response.status_code == 202
    item = response.json()['items'][0]
    assert item['result'] == 'imported', item
    return item['observation_id'], item['media_id']


def test_photo_timeline_recomputes_on_upload_edit_and_delete(timeline_client, config):
    client = timeline_client
    obs_id, late_id = upload(client, 'late.jpg', jpeg('red', date='2026:11:22 08:05:06',
                                                   lat_minutes=33.0, lon_minutes=48.0, sigma=12.0))
    assert client.get(f'/api/observations/{obs_id}').json()['sigma_meters'] == 12.0
    _, unknown_id = upload(client, 'undated.jpg', jpeg('blue'), target=obs_id)
    _, early_id = upload(client, 'early.jpg', jpeg('green', date='2026:11:15 08:05:06',
                                                lat_minutes=33.006, lon_minutes=48.006, sigma=4.0), target=obs_id)

    detail = client.get(f'/api/observations/{obs_id}').json()
    assert [m['id'] for m in detail['media']] == [early_id, late_id, unknown_id]
    early, late, undated = detail['media']
    assert (late['lat'], late['lon'], late['sigma_meters']) == pytest.approx((53.55, 9.8, 12.0))
    assert (early['lat'], early['lon'], early['sigma_meters']) == pytest.approx((53.5501, 9.8001, 4.0))
    assert early['observed_at_ms'] == int(datetime(2026, 11, 15, 7, 5, 6, tzinfo=timezone.utc).timestamp() * 1000)
    assert (early['location_source'], early['time_source'], early['sigma_seconds']) == ('exif', 'exif', 5.0)
    assert undated['observed_at_ms'] is None and undated['lat'] is None
    assert detail['location_measurement_count'] == 2
    assert (detail['lat'], detail['lon']) == pytest.approx((53.55009, 9.80009), abs=1e-9)
    assert detail['sigma_meters'] == pytest.approx((1 / 4**2 + 1 / 12**2) ** -0.5)
    assert detail['time_of_day']['mean_label'] == '08:05'
    assert detail['season']['mean_label'] in {'18. Nov', '19. Nov'}

    listing = client.get('/api/observations').json()
    item, = listing['items']
    assert item['media_count'] == 3 and item['location_measurement_count'] == 2
    assert item['first_observed_at'] == '2026-11-15T07:05:06.000Z'
    assert item['last_observed_at'] == '2026-11-22T07:05:06.000Z'
    assert listing['time_extent'] == {'min': item['first_observed_at'], 'max': item['last_observed_at']}
    assert late['observed_at_ms'] == int(datetime(2026, 11, 22, 7, 5, 6, tzinfo=timezone.utc).timestamp() * 1000)

    edit_url = f'/api/media/{early_id}'
    for invalid in ({'lat': 53}, {'lat': 53, 'lon': None}, {'sigma_meters': 0},
                    {'observed_at_ms': 'not a timestamp'}, {'observed_at_ms': 10**20}, {}):
        assert client.patch(edit_url, json=invalid, headers=ORIGIN).status_code == 422
    edited = client.patch(edit_url, headers=ORIGIN, json={
        'lat': 53.5504, 'lon': 9.8004, 'sigma_meters': 12.0,
        'observed_at_ms': late['observed_at_ms'] + 86_400_000, 'sigma_seconds': 20.0,
    })
    assert edited.status_code == 200
    updated = edited.json()
    assert [m['id'] for m in updated['media']] == [late_id, early_id, unknown_id]
    assert (updated['lat'], updated['lon']) == pytest.approx((53.5502, 9.8002))
    assert updated['sigma_meters'] > 12 / 2**0.5
    assert updated['media'][1]['location_source'] == updated['media'][1]['time_source'] == 'manual'
    assert updated['last_observed_at'] == '2026-11-23T07:05:06.000Z'

    response = client.patch(f'/api/media/{late_id}', headers=ORIGIN, json={'lat': None, 'lon': None, 'observed_at_ms': None})
    assert response.status_code == 200
    cleared = next(m for m in response.json()['media'] if m['id'] == late_id)
    assert cleared['sigma_meters'] is None and cleared['sigma_seconds'] is None
    assert response.json()['location_measurement_count'] == 1

    removed = client.delete(edit_url, headers=ORIGIN)
    assert removed.status_code == 200
    assert removed.json()['media_count'] == 2
    assert removed.json()['location_measurement_count'] == 0
    assert removed.json()['sigma_meters'] is None and removed.json()['last_observed_at'] is None
    assert client.get(f'{edit_url}/image').status_code == 404
    assert not (config.photo_storage_dir / early_id / 'image.jpg').exists()
    assert client.delete(f'/api/media/{unknown_id}', headers=ORIGIN).status_code == 200
    assert client.delete(f'/api/media/{late_id}', headers=ORIGIN).status_code == 409
    assert client.get(f'/api/media/{late_id}/image').status_code == 200


def test_multi_source_merge_is_atomic_and_preserves_analysis_tree(timeline_client, config):
    client = timeline_client
    first, first_photo = upload(client, 'first.jpg', jpeg('green', date='2026:11:13 08:00:00',
                                                       lat_minutes=33.0, lon_minutes=48.0, sigma=10.0))
    target, target_photo = upload(client, 'target.jpg', jpeg('blue', date='2026:11:15 08:00:00',
                                                           lat_minutes=33.012, lon_minutes=48.012, sigma=10.0))
    last, last_photo = upload(client, 'last.jpg', jpeg('red', date='2026:11:22 08:00:00',
                                                     lat_minutes=33.006, lon_minutes=48.006, sigma=10.0))
    with db(config) as conn:
        conn.executemany('UPDATE observations SET description=? WHERE id=?', [('Start', first), ('Mitte', target), ('Ende', last)])
        notes = dict(conn.execute('SELECT obs_id,id FROM gemini_notes').fetchall())
        conn.execute("""INSERT INTO gemini_notes(id,obs_id,source,status,raw_text,created_at_ms,parent_note_id)
            VALUES('followup',?,'gemini_api','ready','Analyse erhalten',?,?)""", (first, 1_800_000_000_000, notes[first]))
    url = f'/api/observations/{target}'
    candidates = client.get(f'{url}/merge-candidates').json()['items']
    assert {item['observation_id'] for item in candidates} == {first, last}
    assert client.post(f'{url}/merge', headers=ORIGIN, json={'source_observation_ids': [first, 'missing']}).status_code == 404
    assert client.get(f'/api/observations/{first}').status_code == 200
    assert client.post(f'{url}/merge', headers=ORIGIN, json={'source_observation_ids': [target]}).status_code == 422

    response = client.post(f'{url}/merge', headers=ORIGIN, json={'source_observation_ids': [last, first]})
    assert response.status_code == 200
    merged = response.json()
    assert merged['id'] == target
    assert set(merged['description'].split('\n\n')) == {'Start', 'Mitte', 'Ende'}
    assert [m['id'] for m in merged['media']] == [first_photo, target_photo, last_photo]
    assert merged['location_measurement_count'] == 3
    assert (merged['lat'], merged['lon'], merged['sigma_meters']) == pytest.approx((53.5501, 9.8001, 10 / 3**0.5))
    assert len(client.get('/api/observations').json()['items']) == 1
    assert client.get(f'/api/observations/{first}').status_code == 404
    assert client.get(f'/api/observations/{last}').status_code == 404
    history = {note['note_id']: note for note in merged['gemini']['history']}
    assert set(history) == {*notes.values(), 'followup'}
    assert history['followup']['text'] == 'Analyse erhalten'
    assert history['followup']['parent_note_id'] == notes[first]
    with db(config) as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert {row['obs_id'] for row in conn.execute('SELECT obs_id FROM processing_jobs')} == {target}


def test_migration_recovers_per_photo_exif_from_legacy_images(config):
    migrations = Path(__file__).resolve().parents[1] / 'app' / 'migrations'
    with db(config) as conn:
        for migration in sorted(migrations.glob('00[1-4]_*.sql')):
            conn.executescript(migration.read_text())
        conn.execute('PRAGMA user_version=4')
        conn.execute("INSERT INTO observations(id, title, created_at_ms, updated_at_ms) VALUES('legacy', 'Legacy', 1, 1)")
        conn.execute("""INSERT INTO space_time_gauss(obs_id, lat, lon, sigma_meters, observed_at_ms, sigma_seconds, location_source, time_source)
            VALUES('legacy', 53.55, 9.8, 10.0, 1794800000000, 20.0, 'exif', 'exif')""")
        conn.executemany("""INSERT INTO media(id, obs_id, source, external_id, original_name, local_path, thumbnail_path, mime_type, width, height, sha256, created_at_ms)
            VALUES(?, 'legacy', 'local', ?, ?, ?, 'thumb.jpg', 'image/jpeg', 40, 30, 'hash', ?)""",
                         [('late', 'late', 'late.jpg', 'late.jpg', 1), ('early', 'early', 'early.jpg', 'early.jpg', 2)])
    config.photo_storage_dir.mkdir()
    (config.photo_storage_dir / 'late.jpg').write_bytes(jpeg('red', date='2026:11:22 08:05:06',
                                                           lat_minutes=33.0, lon_minutes=48.0, sigma=12.0))
    (config.photo_storage_dir / 'early.jpg').write_bytes(jpeg('green', date='2026:11:15 08:05:06',
                                                             lat_minutes=33.006, lon_minutes=48.006, sigma=4.0))

    migrate(config)
    with db(config) as conn:
        rows = conn.execute('SELECT * FROM media ORDER BY observed_at_ms').fetchall()
        assert [row['id'] for row in rows] == ['early', 'late']
        assert (rows[0]['lat'], rows[0]['lon'], rows[0]['sigma_meters']) == pytest.approx((53.5501, 9.8001, 4.0))
        assert (rows[1]['lat'], rows[1]['lon'], rows[1]['sigma_meters']) == pytest.approx((53.55, 9.8, 12.0))
        assert [r['observed_at_ms'] for r in rows] == [1_794_726_306_000, 1_795_331_106_000]
        posterior = conn.execute("SELECT * FROM space_time_gauss WHERE obs_id='legacy'").fetchone()
        assert posterior['sigma_meters'] == pytest.approx((1 / 4**2 + 1 / 12**2) ** -0.5)


def test_migration_on_pre_timeline_backup(config):
    backup = Path(__file__).resolve().parents[2] / 'data' / 'waswowann.sqlite3.bak-pre-object-timeline'
    if not backup.is_file():
        pytest.skip('local pre-timeline database backup is not available')
    shutil.copyfile(backup, config.database_path)
    group_id = '2ef0e3a2-8626-4670-a4fb-2228c5551f36'
    media_fields = 'id,source,external_id,original_name,local_path,thumbnail_path,mime_type,width,height,sha256,exif_json,created_at_ms'
    note_fields = 'id,source,status,raw_text,structured_json,created_at_ms,parent_note_id,question_text,provider,model,error_code'
    with db(config) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
        member_ids = {r['obs_id'] for r in conn.execute('SELECT obs_id FROM space_time_gauss WHERE fused_group_id=?', (group_id,))}
        assert len(member_ids) == 6
        photos = {tuple(r) for r in conn.execute(f'SELECT {media_fields} FROM media')}
        notes = {tuple(r) for r in conn.execute(f'SELECT {note_fields} FROM gemini_notes')}
        mushroom_media_ids = {r['id'] for r in conn.execute('SELECT id,obs_id FROM media') if r['obs_id'] in member_ids}
        mushroom_note_ids = {r['id'] for r in conn.execute('SELECT id,obs_id FROM gemini_notes') if r['obs_id'] in member_ids}
        total = conn.execute('SELECT count(*) FROM observations').fetchone()[0]
        removed = sum(n - 1 for (n,) in conn.execute('''SELECT count(*) FROM space_time_gauss s
            JOIN fusion_groups g ON g.id=s.fused_group_id GROUP BY g.id'''))

    migrate(config)
    migrate(config)
    with db(config) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='fusion_groups'").fetchall()
        assert 'fused_group_id' not in {column['name'] for column in conn.execute('PRAGMA table_info(space_time_gauss)')}
        assert conn.execute('SELECT count(*) FROM observations').fetchone()[0] == total - removed
        assert {tuple(r) for r in conn.execute(f'SELECT {media_fields} FROM media')} == photos
        assert {tuple(r) for r in conn.execute(f'SELECT {note_fields} FROM gemini_notes')} == notes
        mushroom_rows = [r for r in conn.execute('SELECT * FROM media ORDER BY observed_at_ms') if r['id'] in mushroom_media_ids]
        assert len(mushroom_rows) == 8
        assert len({r['observed_at_ms'] for r in mushroom_rows}) == 8
        target, = {r['obs_id'] for r in mushroom_rows}
        assert target in member_ids
        assert {r['id'] for r in conn.execute('SELECT id FROM gemini_notes WHERE obs_id=?', (target,))} == mushroom_note_ids
        posterior = conn.execute('SELECT * FROM space_time_gauss WHERE obs_id=?', (target,)).fetchone()
        assert posterior['location_measurement_count'] == 8
        assert posterior['first_observed_at_ms'] == mushroom_rows[0]['observed_at_ms'] == 1_794_767_346_000
        assert posterior['last_observed_at_ms'] == mushroom_rows[-1]['observed_at_ms'] == 1_795_327_189_000
        expected_sigma = sum(
            1 / effective_location_sigma(r['sigma_meters'], r['location_source'], 'indoor', config=config) ** 2
            for r in mushroom_rows
        ) ** -0.5
        assert posterior['sigma_meters'] == pytest.approx(expected_sigma)


def test_robust_fusion_dampens_indoor_outlier_and_inflates_conflicts(config):
    cluster_rows = [
        dict(lat=53.55827 + i * 0.000003, lon=9.80831 + (i % 2) * 0.00001, sigma_meters=s,
             location_source='exif', observed_at_ms=1_794_767_346_000 + i * 86_400_000, sigma_seconds=15.0, time_source='exif')
        for i, s in enumerate([15.0, 12.0, 12.0, 6.0, 6.0, 15.0, 15.0, 15.0])
    ]
    baseline = fuse_media_measurements(cluster_rows, environment='indoor', config=config)
    with_outlier = cluster_rows + [
        dict(lat=53.558667, lon=9.808397, sigma_meters=4.0, location_source='exif',
             observed_at_ms=1_795_363_866_000, sigma_seconds=2.0, time_source='exif')
    ]
    fused, diagnostics = diagnose_media_locations(with_outlier, environment='indoor', config=config)
    shift_m = sum(v ** 2 for v in _xy(fused['lat'], fused['lon'], baseline['lat'], baseline['lon'])) ** 0.5
    assert shift_m < 2.0
    assert diagnostics[8]['effective_sigma_meters'] == 12.0
    assert diagnostics[8]['kalman_outlier'] is True
    assert diagnostics[8]['kalman_weight'] < 0.05

    conflict = [
        dict(lat=53.55, lon=9.8, sigma_meters=6.0, location_source='exif', observed_at_ms=1_794_800_000_000, sigma_seconds=5.0, time_source='exif'),
        dict(lat=53.55036, lon=9.8, sigma_meters=6.0, location_source='exif', observed_at_ms=1_794_800_060_000, sigma_seconds=5.0, time_source='exif'),
    ]
    conflict_fused = fuse_media_measurements(conflict, environment='outdoor', config=config)
    assert conflict_fused['sigma_meters'] > 6.0 / 2 ** 0.5


def test_manual_pin_ignores_indoor_floor_and_environment_patch_recomputes(timeline_client, config):
    client = timeline_client
    obs_id, exif_id = upload(client, 'exif.jpg', jpeg('red', date='2026:11:15 08:00:00',
                                                   lat_minutes=33.496, lon_minutes=48.498, sigma=4.0))
    assert client.get(f'/api/observations/{obs_id}').json()['sigma_meters'] == 4.0

    patched_env = client.patch(f'/api/observations/{obs_id}', headers=ORIGIN, json={'environment': 'indoor'})
    assert patched_env.status_code == 200
    assert patched_env.json()['sigma_meters'] == 12.0
    assert patched_env.json()['media'][0]['effective_sigma_meters'] == 12.0

    _, manual_id = upload(client, 'pin.jpg', jpeg('blue', date='2026:11:16 08:00:00'))
    with db(config) as conn:
        conn.execute('UPDATE media SET obs_id=? WHERE id=?', (obs_id, manual_id))
    pin_resp = client.patch(f'/api/media/{manual_id}', headers=ORIGIN, json={
        'lat': 53.55828, 'lon': 9.80831, 'sigma_meters': 5.3,
    })
    assert pin_resp.status_code == 200
    manual_item = next(m for m in pin_resp.json()['media'] if m['id'] == manual_id)
    assert manual_item['location_source'] == 'manual'
    assert manual_item['effective_sigma_meters'] == 5.3
