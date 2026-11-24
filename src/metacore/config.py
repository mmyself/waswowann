"""Configuration settings for WoWannWas MetaCore."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for MetaCore database access."""

    database_path: Path = Path("data/metacore.sqlite3")

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables."""
        raw_path = os.environ.get("METACORE_DB_PATH", "data/metacore.sqlite3")
        return cls(database_path=Path(raw_path).expanduser())


default_settings = Settings.from_env()
