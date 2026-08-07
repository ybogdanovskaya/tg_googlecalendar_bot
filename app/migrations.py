from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


LOGGER = logging.getLogger(__name__)
RELEASE_ID = "release-a"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationResult:
    previous_version: int
    current_version: int
    backup_path: Path | None
    applied_versions: tuple[int, ...]


MIGRATIONS = (
    Migration(
        1,
        "baseline",
        (
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                telegram_name TEXT NOT NULL,
                telegram_username TEXT,
                consent_version TEXT,
                consent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meeting_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                telegram_name TEXT NOT NULL,
                telegram_username TEXT,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                description TEXT,
                location TEXT,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                status TEXT NOT NULL,
                hold_until TEXT NOT NULL,
                google_event_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_requests_user
            ON meeting_requests(telegram_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_requests_slot
            ON meeting_requests(start_at, end_at, status, hold_until)
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                actor_telegram_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                details_json TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        2,
        "release_a_settings",
        (
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                admin_telegram_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                old_value_json TEXT,
                new_value_json TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_settings_history_time
            ON settings_history(occurred_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS availability_exceptions (
                local_date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
    return int(row["version"])


def create_verified_backup(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{database_path.stem}.pre-migration-{stamp}.sqlite3"
    source = _connect(database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError("backup integrity check failed")
    except Exception:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    LOGGER.info("migration_backup_created", extra={"backup_path": str(backup_path)})
    return backup_path


def migrate_database(database_path: Path, backup_dir: Path | None = None) -> MigrationResult:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    existed = database_path.exists() and database_path.stat().st_size > 0
    connection = _connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        previous_version = _schema_version(connection)
    finally:
        connection.close()

    pending = tuple(item for item in MIGRATIONS if item.version > previous_version)
    backup_path = None
    if existed and pending:
        backup_path = create_verified_backup(
            database_path,
            backup_dir or database_path.parent / "backups",
        )

    applied: list[int] = []
    connection = _connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        for migration in pending:
            LOGGER.info(
                "migration_started",
                extra={"schema_version": migration.version, "migration_name": migration.name},
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                for statement in migration.statements:
                    connection.execute(statement)
                now = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, now),
                )
                connection.execute(
                    """
                    INSERT INTO schema_metadata (key, value, updated_at)
                    VALUES ('release_id', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (RELEASE_ID, now),
                ) if migration.version >= 2 else None
                connection.commit()
                applied.append(migration.version)
                LOGGER.info("migration_completed", extra={"schema_version": migration.version})
            except Exception:
                connection.rollback()
                LOGGER.exception("migration_failed", extra={"schema_version": migration.version})
                raise
    finally:
        connection.close()

    return MigrationResult(
        previous_version=previous_version,
        current_version=max((item.version for item in MIGRATIONS), default=0),
        backup_path=backup_path,
        applied_versions=tuple(applied),
    )
