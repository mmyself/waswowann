"""Shared pytest fixtures for MetaCore tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from metacore.config import Settings
from metacore.db import migrate
from metacore.repository import MetaCoreRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Create isolated database settings per test."""
    return Settings(database_path=tmp_path / "test_metacore.sqlite3")


@pytest.fixture
def repo(settings: Settings) -> MetaCoreRepository:
    """Migrate a fresh SQLite database and return a repository instance."""
    migrate(settings)
    return MetaCoreRepository(settings)
