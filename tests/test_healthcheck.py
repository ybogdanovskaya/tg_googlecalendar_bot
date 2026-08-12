from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.calendar_client import CalendarUnavailable
from scripts.healthcheck import alert_is_new, clear_alert_state, newest_backup, verify_apps_script, verify_sqlite


class HealthCheckTests(unittest.TestCase):
    def _create_sample_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE sample (id INTEGER)")
        finally:
            connection.close()

    def test_database_and_recent_backup_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "calendar_bot.sqlite3"
            self._create_sample_database(database)
            verify_sqlite(database)
            backup = root / "calendar_bot_20260807T000000Z.sqlite3"
            source = sqlite3.connect(database)
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            self.assertEqual(newest_backup(root, 48), backup)

    def test_stale_backup_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "calendar_bot_20260807T000000Z.sqlite3"
            self._create_sample_database(backup)
            stale = (datetime.now(UTC) - timedelta(hours=72)).timestamp()
            os.utime(backup, (stale, stale))
            with self.assertRaisesRegex(RuntimeError, "backup_stale"):
                newest_backup(root, 48)

    def test_apps_script_check_retries_a_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url_file = root / "url"
            secret_file = root / "secret"
            url_file.write_text("https://script.google.com/macros/s/example/exec", encoding="utf-8")
            secret_file.write_text("test-secret", encoding="utf-8")
            with patch("scripts.healthcheck.AppsScriptCalendar") as client_class:
                client_class.return_value.busy = AsyncMock(side_effect=[CalendarUnavailable("temporary"), []])
                import asyncio
                asyncio.run(verify_apps_script(url_file, secret_file, attempts=2, retry_delay_seconds=0))
            self.assertEqual(client_class.return_value.busy.await_count, 2)

    def test_alert_state_suppresses_duplicates_and_resets_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "healthcheck-alert.state"
            failures = ["apps_script:CalendarUnavailable"]
            self.assertTrue(alert_is_new(state_file, failures))
            self.assertFalse(alert_is_new(state_file, failures))
            clear_alert_state(state_file)
            self.assertTrue(alert_is_new(state_file, failures))


if __name__ == "__main__":
    unittest.main()
