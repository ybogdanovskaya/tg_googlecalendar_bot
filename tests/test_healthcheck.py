from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.healthcheck import newest_backup, verify_sqlite


class HealthCheckTests(unittest.TestCase):
    def test_database_and_recent_backup_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "calendar_bot.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sample (id INTEGER)")
            verify_sqlite(database)
            backup = root / "calendar_bot_20260807T000000Z.sqlite3"
            with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
                source.backup(destination)
            self.assertEqual(newest_backup(root, 48), backup)

    def test_stale_backup_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "calendar_bot_20260807T000000Z.sqlite3"
            with sqlite3.connect(backup) as connection:
                connection.execute("CREATE TABLE sample (id INTEGER)")
            stale = (datetime.now(UTC) - timedelta(hours=72)).timestamp()
            os.utime(backup, (stale, stale))
            with self.assertRaisesRegex(RuntimeError, "backup_stale"):
                newest_backup(root, 48)


if __name__ == "__main__":
    unittest.main()
