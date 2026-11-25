from __future__ import annotations

import io
import sqlite3
from PIL import Image
from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.config import Settings
from app.services.photo_ingest import IngestError, ingest_image
from app.services.demo_data import seed_demo
from app.main import app


def image_bytes(size=(64,48),fmt='JPEG'):
    stream=io.BytesIO(); Image.new('RGB',size,(20,120,60)).save(stream,fmt); return stream.getvalue()


def test_migrations_foreign_keys_and_checks(tmp_path):
    config=Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'db.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','')
    migrate(config); migrate(config)
    with connect(config.database_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==6
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        with conn:
            conn.execute("INSERT INTO observations(id,category,title,description,created_at_ms,updated_at_ms) VALUES('o','Pilz','title','',1,1)")
            try:
                conn.execute("INSERT INTO space_time_gauss(obs_id,lat,lon,sigma_meters) VALUES('o',NULL,NULL,1)")
                assert False
            except sqlite3.IntegrityError: pass


def test_migration_002_preserves_existing_data_and_creates_backup(tmp_path):
    config=Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'db.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','')
    database = connect(config.database_path)
    database.executescript((__import__('pathlib').Path(__file__).parents[1] / 'app/migrations/001_initial.sql').read_text())
    database.execute('PRAGMA user_version=1')
    database.execute("INSERT INTO observations VALUES('legacy','Pilz','Legacy title','legacy description',1,2)")
    database.execute("INSERT INTO space_time_gauss(obs_id,lat,lon,sigma_meters,observed_at_ms,sigma_seconds,location_source,time_source) VALUES('legacy',52.1,13.2,NULL,1234,NULL,'exif','exif')")
    database.execute("INSERT INTO media(id,obs_id,source,external_id,original_name,local_path,thumbnail_path,mime_type,width,height,sha256,exif_json,created_at_ms) VALUES('media','legacy','local','legacy-file','legacy.jpg','image.jpg','thumb.jpg','image/jpeg',10,20,'hash','{}',1)")
    database.execute("INSERT INTO gemini_notes(id,obs_id,source,status,raw_text,structured_json,created_at_ms) VALUES('note','legacy','placeholder','ready','legacy note','{}',1)")
    database.commit()
    database.close()

    migrate(config)
    backups = list(tmp_path.glob('db.sqlite3.pre-002.*.bak'))
    assert len(backups) == 1
    with connect(config.database_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
        assert conn.execute("SELECT title FROM observations WHERE id='legacy'").fetchone()[0] == 'Legacy title'
        assert conn.execute("SELECT sha256 FROM media WHERE id='media'").fetchone()[0] == 'hash'
        note = conn.execute("SELECT status,raw_text FROM gemini_notes WHERE id='note'").fetchone()
        assert tuple(note) == ('ready','legacy note')
        assert tuple(conn.execute("SELECT lat,lon,sigma_meters,observed_at_ms,sigma_seconds FROM space_time_gauss WHERE obs_id='legacy'").fetchone()) == (52.1,13.2,config.fusion_default_sigma_meters,1234,config.fusion_default_sigma_seconds)
    with connect(backups[0]) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        assert conn.execute("SELECT id FROM observations WHERE id='legacy'").fetchone()[0] == 'legacy'


def test_ingest_limits_dedup_and_media(tmp_path):
    c=Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'db.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','')
    migrate(c)
    media_id,status=ingest_image(source='local',external_id='one',filename='a.jpg',data=image_bytes(),config=c)
    assert status=='imported'
    assert ingest_image(source='local',external_id='one',filename='a.jpg',data=image_bytes(),config=c)==(media_id,'skipped')
    assert (c.photo_storage_dir/media_id/'image.jpg').exists()
    with pytest.raises(IngestError):ingest_image(source='local',external_id='bad',filename='x.jpg',data=b'not image',config=c)


def test_api_observation_validation_and_patch(tmp_path,monkeypatch):
    c=Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'db.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','')
    import app.config as cm, app.db as dm, app.main as mm
    monkeypatch.setattr(cm,'settings',c);monkeypatch.setattr(dm,'settings',c);monkeypatch.setattr(mm,'settings',c)
    with TestClient(app) as client:
        h=client.get('/api/health'); assert h.status_code==200
        headers={'Origin':'http://localhost:3000','Content-Type':'application/json'}
        invalid=client.post('/api/observations',headers=headers,json={'title':'x','space_time':{'observed_at':'2026-01-01T10:00:00'}})
        assert invalid.status_code==422
        created=client.post('/api/observations',headers=headers,json={'title':'Test'})
        assert created.status_code==201
        oid=created.json()['id']
        assert created.json()['space_time']['observed_at'] is None
        patched=client.patch(f'/api/observations/{oid}',headers=headers,json={'title':'Umbenannt'})
        assert patched.status_code==200 and patched.json()['title']=='Umbenannt'
        assert client.patch(f'/api/observations/{oid}',headers=headers,json={'lat':None,'lon':None}).status_code==422
        assert client.get('/api/observations/missing').status_code==404
        assert client.post('/api/observations',json={'title':'bad'}).status_code==403


def test_demo_idempotent_and_null_values(tmp_path):
    c=Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'db.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','')
    migrate(c)
    assert seed_demo(c)==16
    assert seed_demo(c)==0
    with connect(c.database_path) as conn:
        assert conn.execute('SELECT count(*) FROM observations').fetchone()[0]==16
        assert conn.execute('SELECT count(*) FROM space_time_gauss WHERE lat IS NULL').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM space_time_gauss WHERE observed_at_ms IS NULL').fetchone()[0]==1

import pytest
