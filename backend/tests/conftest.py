import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import app
from app.db import migrate

@pytest.fixture
def config(tmp_path):
    return Settings('127.0.0.1',8000,'http://localhost:3000',tmp_path/'test.sqlite3',tmp_path/'photos',tmp_path/'tmp',tmp_path/'incoming','Europe/Berlin','','','http://localhost:8000/api/google/oauth/callback')

@pytest.fixture
def client(monkeypatch,config):
    from app import config as config_module
    from app import db as db_module
    monkeypatch.setattr(config_module,'settings',config)
    monkeypatch.setattr(db_module,'settings',config)
    with TestClient(app) as test:
        yield test
