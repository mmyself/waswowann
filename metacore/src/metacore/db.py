"""SQLite connection and atomic schema migration utilities for MetaCore."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from metacore.config import Settings, default_settings


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys and busy timeout enabled."""
    target = path or default_settings.database_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(config: Settings | None = None) -> int:
    """Apply all pending numbered SQL migrations atomically and return user_version."""
    active = config or default_settings
    active.database_path.parent.mkdir(parents=True, exist_ok=True)
    migration_dir = Path(__file__).parent / "migrations"
    migrations: list[tuple[int, Path]] = []
    for path in migration_dir.glob("*.sql"):
        match = re.match(r"^(\d+)_.*\.sql$", path.name)
        if match:
            migrations.append((int(match.group(1)), path))

    with connect(active.database_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for version, path in sorted(migrations):
            if version <= current:
                continue
            if version != current + 1:
                raise RuntimeError(f"Missing database migration {current + 1:03d}")
            sql = path.read_text(encoding="utf-8")
            try:
                conn.execute("BEGIN IMMEDIATE")
                if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= version:
                    conn.rollback()
                    current = version
                    continue
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        if statement.strip():
                            conn.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Incomplete SQL statement in migration {version:03d}")
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"Foreign key violations in migration {version:03d}: {violations!r}"
                    )
                conn.execute(f"PRAGMA user_version={version}")
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            current = version
        return current


@contextmanager
def db(config: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a transactional SQLite connection and commit or rollback on exit."""
    conn = connect((config or default_settings).database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_ms() -> int:
    """Return current UTC epoch timestamp in milliseconds."""
    return time.time_ns() // 1_000_000
