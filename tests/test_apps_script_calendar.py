from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.apps_script_calendar import AppsScriptCalendar


class AppsScriptCalendarTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_response_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar(
                "https://script.google.com/macros/s/example/exec",
                secret_file,
            )
            client._post = lambda payload: {
                "ok": True,
                "busy": [{"start": "2026-08-08T06:00:00Z", "end": "2026-08-08T07:00:00Z"}],
            }
            result = await client.busy(
                datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
                datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            )
            self.assertEqual(result[0][0], datetime(2026, 8, 8, 6, 0, tzinfo=UTC))
            self.assertEqual(result[0][1], datetime(2026, 8, 8, 7, 0, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
