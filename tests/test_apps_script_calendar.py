from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.apps_script_calendar import AppsScriptCalendar
from app.models import EventOccurrence, EventSeries, MeetingRequest


class AppsScriptCalendarTests(unittest.IsolatedAsyncioTestCase):
    def test_default_timeout_allows_apps_script_cold_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar("https://script.google.com/macros/s/example/exec", secret_file)
            self.assertEqual(client.timeout_seconds, 45)

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

    async def test_all_day_create_payload_has_inclusive_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar("https://script.google.com/macros/s/example/exec", secret_file)
            captured = []

            def post(payload):
                captured.append(payload)
                return {"ok": True, "eventId": "all-day-event"}

            client._post = post
            start = datetime(2026, 8, 20, tzinfo=UTC)
            meeting = MeetingRequest(
                1, 100, "Admin", None, "", "Отпуск", None, None,
                start, start + timedelta(days=4), "APPROVING", start, None, start, start,
                source="ADMIN", blocks_calendar=True, admin_override=True, all_day=True,
            )
            self.assertEqual(await client.create_event(meeting), "all-day-event")
            self.assertTrue(captured[0]["allDay"])
            self.assertEqual(captured[0]["allDayStart"], "2026-08-20")
            self.assertEqual(captured[0]["allDayEnd"], "2026-08-24")

    async def test_manual_self_only_create_has_no_guest_and_can_be_transparent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar("https://script.google.com/macros/s/example/exec", secret_file)
            captured = []

            def post(payload):
                captured.append(payload)
                return {"ok": True, "eventId": "manual-event"}

            client._post = post
            now = datetime(2026, 8, 8, 6, 0, tzinfo=UTC)
            meeting = MeetingRequest(
                1, 100, "Admin", None, "", "Manual", None, None,
                now, now + timedelta(minutes=30), "APPROVING", now, None, now, now,
                source="ADMIN", blocks_calendar=False, admin_override=True,
            )
            self.assertEqual(await client.create_event(meeting), "manual-event")
            self.assertEqual(captured[0]["email"], "")
            self.assertTrue(captured[0]["allowOverlap"])
            self.assertTrue(captured[0]["transparent"])

    async def test_event_status_and_series_operations_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "secret"
            secret_file.write_text("test-secret", encoding="utf-8")
            client = AppsScriptCalendar("https://script.google.com/macros/s/example/exec", secret_file)
            captured = []

            def post(payload):
                captured.append(payload)
                if payload["action"] in {"status", "occurrenceStatus"}:
                    return {
                        "ok": True,
                        "exists": True,
                        "start": "2026-08-20T06:00:00Z",
                        "end": "2026-08-20T06:30:00Z",
                        "subject": "Changed",
                        "description": "Details",
                        "location": "Office",
                        "transparent": False,
                        "updated": "2026-08-07T10:00:00Z",
                    }
                if payload["action"] in {"seriesCreate", "seriesUpdate", "occurrenceUpdate"}:
                    return {"ok": True, "eventId": "series-id"}
                return {"ok": True, "deleted": True}

            client._post = post
            state = await client.event_state("event-id")
            self.assertTrue(state.exists)
            self.assertEqual(state.subject, "Changed")
            now = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
            series = EventSeries(
                1, 1, "Admin", "admin", None, "Daily", None, None,
                now, now + timedelta(minutes=30), "DAILY", "2026-08-22", "ACTIVE",
                "series-id", True, False, "SYNCED", None, now, now,
            )
            occurrence = EventOccurrence(
                1, 1, now, now + timedelta(minutes=30), now, now + timedelta(minutes=30),
                "SCHEDULED", "series-id", now, now,
            )
            self.assertEqual(await client.create_series(series), "series-id")
            self.assertEqual(await client.update_series(series), "series-id")
            self.assertEqual(
                await client.update_occurrence("series-id", occurrence, now + timedelta(hours=1), now + timedelta(hours=1, minutes=30)),
                "series-id",
            )
            self.assertEqual(captured[-1]["expectedStart"], now.isoformat())
            occurrence_state = await client.occurrence_state("series-id", occurrence)
            self.assertTrue(occurrence_state.exists)
            self.assertTrue(await client.delete_occurrence("series-id", occurrence))
            self.assertTrue(await client.delete_series("series-id"))
            actions = [item["action"] for item in captured]
            self.assertEqual(
                actions,
                [
                    "status",
                    "seriesCreate",
                    "seriesUpdate",
                    "occurrenceUpdate",
                    "occurrenceStatus",
                    "occurrenceDelete",
                    "seriesDelete",
                ],
            )


if __name__ == "__main__":
    unittest.main()
