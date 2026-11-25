from __future__ import annotations

import sqlite3
import time
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import Settings, settings


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or settings.database_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _backup_before_002(database_path: Path, conn: sqlite3.Connection) -> None:
    """Create a one-time SQLite-consistent backup before applying migration 002."""
    if next(database_path.parent.glob(f"{database_path.name}.pre-002.*.bak"), None):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(f"{database_path.name}.pre-002.{stamp}.bak")
    # Exclusive creation prevents overwriting even in the unlikely event of a
    # timestamp collision; sqlite's backup API safely handles WAL databases.
    descriptor = None
    try:
        descriptor = backup_path.open("xb")
        descriptor.close()
        descriptor = None
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    except Exception:
        if descriptor is not None:
            descriptor.close()
        backup_path.unlink(missing_ok=True)
        raise


def migrate(config: Settings | None = None) -> None:
    active = config or settings
    active.database_path.parent.mkdir(parents=True, exist_ok=True)
    database_exists = active.database_path.exists() and active.database_path.stat().st_size > 0
    with connect(active.database_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        migration_dir = Path(__file__).parent / "migrations"
        migrations = []
        for path in migration_dir.glob("*.sql"):
            match = re.match(r"^(\d+)_.*\.sql$", path.name)
            if match:
                migrations.append((int(match.group(1)), path))
        if not migrations:
            return
        for version, path in sorted(migrations):
            if version <= current:
                continue
            if version != current + 1:
                raise RuntimeError(f"Missing database migration {current + 1:03d}")
            if version == 2 and current < 2 and database_exists:
                _backup_before_002(active.database_path, conn)
            sql = path.read_text()
            # executescript commits any open transaction, so wrap each file's
            # statements explicitly and update user_version in the same transaction.
            body = re.sub(r"(?im)^\s*PRAGMA\s+foreign_keys\s*=\s*(?:ON|OFF)\s*;?\s*$", "", sql)
            body = re.sub(r"(?im)^\s*PRAGMA\s+foreign_key_check\s*;?\s*$", "", body)
            body = re.sub(r"(?im)^\s*BEGIN(?:\s+IMMEDIATE)?\s*;?\s*$", "", body)
            body = re.sub(r"(?im)^\s*COMMIT\s*;?\s*$", "", body)
            uses_rebuild = "PRAGMA foreign_keys=OFF" in sql
            if uses_rebuild:
                conn.execute("PRAGMA foreign_keys=OFF")
            try:
                # Check constraints while still inside the migration transaction;
                # the check cannot be part of executescript because its result
                # must be inspected before COMMIT.
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('PRAGMA user_version').fetchone()[0] >= version:
                    conn.rollback()
                    current = version
                    continue
                statement = ''
                for line in body.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ''
                if statement.strip():
                    raise RuntimeError(f'Incomplete SQL statement in migration {version:03d}')
                if version == 5:
                    from app.services.media_migration import migrate_media_measurements
                    migrate_media_measurements(conn, active, now_ms())
                if uses_rebuild or version == 5:
                    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise sqlite3.IntegrityError(f"Foreign key violations in migration {version}: {violations!r}")
                conn.execute(f"PRAGMA user_version={version}")
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                if uses_rebuild:
                    conn.execute("PRAGMA foreign_keys=ON")
            current = version


@contextmanager
def db(config: Settings | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect((config or settings).database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_ms() -> int:
    return time.time_ns() // 1_000_000
