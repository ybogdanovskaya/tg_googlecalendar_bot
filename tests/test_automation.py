from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.automation import process_due_jobs, reconcile_once
from app.automation_store import AutomationStore
from app.db import Database
from app.models import CalendarEventState


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, recipient: int, text: str):
        self.sent.append((recipient, text))


class FakeCalendar:
    def __init__(self, state: CalendarEventState) -> None:
        self.state = state

    async def event_state(self, event_id: str) -> CalendarEventState:
        return self.state


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "automation.sqlite3")
        self.db.initialize()
        self.db.upsert_user(100, "User", "user")
        self.store = AutomationStore(self.db)
        self.settings = SimpleNamespace(timezone="Europe/Moscow", admin_telegram_id=1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approved(self, start: datetime):
        request = self.db.create_request(
            telegram_id=100,
            telegram_name="User",
            telegram_username="user",
            email="user@example.com",
            subject="Meeting",
            description=None,
            location="Office",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        self.db.claim_for_approval(request.id, 1)
        return self.db.complete_approval(request.id, 1, "event-id")

    async def test_due_meeting_reminder_is_delivered_once(self) -> None:
        now = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        request = self.approved(now + timedelta(hours=1))
        self.store.rebuild_request_reminders(request.id, 1, (60,), now)
        bot = FakeBot()
        self.assertEqual(await process_due_jobs(bot, self.store, self.settings, now), 2)
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(await process_due_jobs(bot, self.store, self.settings, now), 0)

    async def test_pending_reminder_schedules_next_daily_job(self) -> None:
        now = datetime.now(UTC)
        request = self.db.create_request(
            telegram_id=100,
            telegram_name="User",
            telegram_username=None,
            email="user@example.com",
            subject="Pending",
            description=None,
            location=None,
            start_at=now + timedelta(days=2),
            end_at=now + timedelta(days=2, minutes=30),
            hold_hours=24,
        )
        self.store.ensure_pending_reminder(request.id, 1, 1, now)
        bot = FakeBot()
        self.assertEqual(await process_due_jobs(bot, self.store, self.settings, now + timedelta(hours=1, seconds=1)), 1)
        with self.db._connect() as connection:
            pending = connection.execute("SELECT COUNT(*) FROM scheduled_jobs WHERE status = 'PENDING'").fetchone()[0]
        self.assertEqual(pending, 1)

    async def test_reconciliation_updates_time_and_notifies_both_parties(self) -> None:
        start = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        self.approved(start)
        state = CalendarEventState(
            True,
            "event-id",
            start + timedelta(hours=1),
            start + timedelta(hours=1, minutes=30),
            "Changed",
            None,
            "Online",
            True,
            datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        )
        bot = FakeBot()
        result = await reconcile_once(bot, self.store, FakeCalendar(state), self.settings)
        self.assertEqual(result, {"checked": 1, "changed": 1, "missing": 0, "errors": 0})
        self.assertEqual({item[0] for item in bot.sent}, {1, 100})
        self.assertEqual(self.db.get_request(1).subject, "Changed")

    async def test_reconciliation_marks_deleted_event_without_retry_storm(self) -> None:
        start = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        self.approved(start)
        missing = CalendarEventState(False, "event-id", None, None, None, None, None, None, None)
        bot = FakeBot()
        first = await reconcile_once(bot, self.store, FakeCalendar(missing), self.settings)
        second = await reconcile_once(bot, self.store, FakeCalendar(missing), self.settings)
        self.assertEqual(first["missing"], 1)
        self.assertEqual(second["checked"], 0)


if __name__ == "__main__":
    unittest.main()
