from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.migrations import MIGRATIONS, Migration, migrate_database
from scripts.restore_backup import restore_database


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_legacy_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                telegram_name TEXT NOT NULL,
                telegram_username TEXT,
                consent_version TEXT,
                consent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE meeting_requests (
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
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                actor_telegram_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                details_json TEXT NOT NULL
            );
            INSERT INTO users (
                telegram_id, telegram_name, created_at, updated_at
            ) VALUES (100, 'Existing User', '2026-08-07T00:00:00+00:00', '2026-08-07T00:00:00+00:00');
            """
        )
        connection.close()

    def test_clean_database_is_created_without_backup(self) -> None:
        path = self.root / "clean.sqlite3"
        result = migrate_database(path)
        self.assertEqual(result.applied_versions, (1, 2, 3))
        self.assertIsNone(result.backup_path)
        self.assertEqual(Database(path).schema_info(), (3, "release-b"))

    def test_legacy_database_is_backed_up_and_preserved(self) -> None:
        path = self.root / "legacy.sqlite3"
        self.create_legacy_database(path)
        database = Database(path)
        database.initialize()
        result = database.last_migration_result
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(result.backup_path.exists())
        connection = sqlite3.connect(path)
        row = connection.execute("SELECT telegram_name FROM users WHERE telegram_id = 100").fetchone()
        connection.close()
        self.assertEqual(row[0], "Existing User")
        backup_count = len(list((self.root / "backups").glob("*.sqlite3")))
        database.initialize()
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), backup_count)
        self.assertEqual(database.last_migration_result.applied_versions, ())

    def test_failed_migration_rolls_back(self) -> None:
        path = self.root / "current.sqlite3"
        Database(path).initialize()
        bad = Migration(
            4,
            "failure_test",
            (
                "CREATE TABLE should_not_remain (id INTEGER)",
                "THIS IS NOT SQL",
            ),
        )
        with patch("app.migrations.MIGRATIONS", MIGRATIONS + (bad,)):
            with self.assertRaises(sqlite3.OperationalError):
                migrate_database(path)
        connection = sqlite3.connect(path)
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_not_remain'"
        ).fetchone()
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        connection.close()
        self.assertIsNone(exists)
        self.assertEqual(version, 3)

    def test_verified_backup_can_be_restored(self) -> None:
        target = self.root / "target.sqlite3"
        database = Database(target)
        database.initialize()
        database.upsert_user(100, "Before", None)
        backup = self.root / "backup.sqlite3"
        source = sqlite3.connect(target)
        destination = sqlite3.connect(backup)
        source.backup(destination)
        destination.close()
        source.close()
        database.upsert_user(100, "After", None)
        safety = restore_database(backup, target)
        self.assertIsNotNone(safety)
        connection = sqlite3.connect(target)
        name = connection.execute("SELECT telegram_name FROM users WHERE telegram_id = 100").fetchone()[0]
        connection.close()
        self.assertEqual(name, "Before")


if __name__ == "__main__":
    unittest.main()
