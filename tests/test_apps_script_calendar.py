from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.apps_script_calendar import AppsScriptCalendar
from app.models import MeetingRequest


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

    async def test_update_and_delete_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar("https://script.google.com/macros/s/example/exec", secret_file)
            captured = []

            def post(payload):
                captured.append(payload)
                if payload["action"] == "update":
                    return {"ok": True, "eventId": "event-id"}
                return {"ok": True, "deleted": True}

            client._post = post
            now = datetime(2026, 8, 8, 6, 0, tzinfo=UTC)
            meeting = MeetingRequest(
                1, 100, "Test", None, "", "Manual", None, None,
                now, now.replace(hour=7), "APPROVED", now, "event-id", now, now,
                source="ADMIN", blocks_calendar=False, admin_override=True,
            )
            self.assertEqual(await client.update_event(meeting), "event-id")
            self.assertTrue(await client.delete_event("event-id"))
            self.assertTrue(captured[0]["allowOverlap"])
            self.assertTrue(captured[0]["transparent"])


if __name__ == "__main__":
    unittest.main()
