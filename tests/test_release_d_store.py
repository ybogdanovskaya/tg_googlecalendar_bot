from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database
from app.models import CANCELLED
from app.release_d_store import (
    DELETE_CANCEL_FUTURE,
    DELETE_COMPLETED,
    DELETE_KEEP_FUTURE,
    DELETE_WAITING,
    ReleaseDStore,
)


class ReleaseDStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "release-d.sqlite3")
        self.database.initialize()
        self.database.upsert_user(100, "Test User", "tester")
        self.database.set_consent(100, "v1")
        self.store = ReleaseDStore(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approved(self, start: datetime):
        request = self.database.create_request(
            telegram_id=100,
            telegram_name="Test User",
            telegram_username="tester",
            email="test@example.com",
            subject="Private subject",
            description="Private description",
            location="Private location",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        self.database.claim_for_approval(request.id, 1)
        return self.database.complete_approval(request.id, 1, f"event-{request.id}")

    def test_statistics_counts_requests_and_calendar_meetings(self) -> None:
        now = datetime.now(UTC)
        self.approved(now + timedelta(days=2))
        result = self.store.statistics(now - timedelta(days=1), now + timedelta(days=3))
        self.assertEqual(result.user_requests, 1)
        self.assertEqual(result.calendar_meetings, 1)
        self.assertEqual(result.unique_users, 1)
        self.assertEqual(result.statuses["APPROVED"], 1)

    def test_cancel_future_anonymizes_user_and_request(self) -> None:
        meeting = self.approved(datetime.now(UTC) + timedelta(days=2))
        deletion = self.store.create_deletion_request(100, DELETE_CANCEL_FUTURE)
        self.assertEqual(self.store.future_google_events(deletion), [(meeting.id, f"event-{meeting.id}")])
        self.store.complete_cancel_future(deletion.id, 100)
        changed = self.database.get_request(meeting.id)
        self.assertIsNotNone(changed)
        self.assertLess(changed.telegram_id, 0)
        self.assertEqual(changed.status, CANCELLED)
        self.assertEqual(changed.email, "deleted@example.invalid")
        with self.database._connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM users WHERE telegram_id = 100").fetchone())
        self.assertEqual(self.store.get_deletion_request(deletion.id).status, DELETE_COMPLETED)

    def test_keep_future_minimizes_then_finishes_after_meeting(self) -> None:
        meeting = self.approved(datetime.now(UTC) + timedelta(days=2))
        deletion = self.store.create_deletion_request(100, DELETE_KEEP_FUTURE)
        waiting = self.store.complete_keep_future(deletion.id, 100)
        self.assertEqual(waiting.status, DELETE_WAITING)
        minimized = self.database.get_request(meeting.id)
        self.assertEqual(minimized.telegram_id, 100)
        self.assertEqual(minimized.email, "hidden@example.invalid")
        self.assertEqual(minimized.subject, "Встреча")
        result = self.store.run_retention(now=meeting.end_at + timedelta(minutes=1))
        self.assertEqual(result["completed_deletions"], 1)
        anonymized = self.database.get_request(meeting.id)
        self.assertLess(anonymized.telegram_id, 0)
        self.assertEqual(anonymized.email, "deleted@example.invalid")
        self.assertEqual(self.store.get_deletion_request(deletion.id).status, DELETE_COMPLETED)


if __name__ == "__main__":
    unittest.main()
