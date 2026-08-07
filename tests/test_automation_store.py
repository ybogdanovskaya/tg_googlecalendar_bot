from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.automation_store import AutomationStore
from app.db import Database, RequestNotEditableError, SlotConflictError
from app.models import (
    JOB_DONE,
    JOB_PENDING,
    OCCURRENCE_CANCELLED,
    OCCURRENCE_MISSING,
    OCCURRENCE_MOVED,
    SERIES_DAILY,
    SERIES_ACTIVE,
    SYNC_MISSING,
    CalendarEventState,
)
from app.recurrence import generate_occurrences


class AutomationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "automation.sqlite3")
        self.db.initialize()
        self.db.upsert_user(100, "User", "user")
        self.db.set_consent(100, "v1")
        self.store = AutomationStore(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approved_request(self, start: datetime):
        request = self.db.create_request(
            telegram_id=100,
            telegram_name="User",
            telegram_username="user",
            email="user@example.com",
            subject="Meeting",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        self.db.claim_for_approval(request.id, 1)
        return self.db.complete_approval(request.id, 1, "event-id")

    def series_values(self, start: datetime, allow_overlap: bool = False):
        end = start + timedelta(minutes=30)
        occurrences = generate_occurrences(start, end, SERIES_DAILY, start.date() + timedelta(days=2))
        return dict(
            admin_id=1,
            admin_name="Admin",
            admin_username="admin",
            email=None,
            subject="Daily",
            description=None,
            location=None,
            start_at=start,
            end_at=end,
            frequency=SERIES_DAILY,
            until_date=(start.date() + timedelta(days=2)).isoformat(),
            blocks_calendar=True,
            allow_overlap=allow_overlap,
            occurrences=occurrences,
        )

    def test_series_requires_override_for_conflict_and_activates_atomically(self) -> None:
        start = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        self.approved_request(start)
        with self.assertRaises(SlotConflictError):
            self.store.create_series_draft(**self.series_values(start))
        draft = self.store.create_series_draft(**self.series_values(start, allow_overlap=True))
        active = self.store.activate_series(draft.id, "series-id", 1)
        self.assertEqual(active.status, SERIES_ACTIVE)
        self.assertEqual(len(self.store.list_occurrences(active.id)), 3)
        self.assertEqual(len(self.db.active_intervals(start, start + timedelta(minutes=30))), 2)

    def test_occurrence_can_be_moved_and_cancelled_once(self) -> None:
        start = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
        draft = self.store.create_series_draft(**self.series_values(start))
        self.store.activate_series(draft.id, "series-id", 1)
        occurrence = self.store.list_occurrences(draft.id)[0]
        moved = self.store.move_occurrence(
            occurrence.id,
            1,
            occurrence.actual_start_at + timedelta(hours=1),
            occurrence.actual_end_at + timedelta(hours=1),
        )
        self.assertEqual(moved.actual_start_at, occurrence.actual_start_at + timedelta(hours=1))
        cancelled = self.store.cancel_occurrence(occurrence.id, 1)
        self.assertEqual(cancelled.status, OCCURRENCE_CANCELLED)
        with self.assertRaises(RequestNotEditableError):
            self.store.cancel_occurrence(occurrence.id, 1)

    def test_google_occurrence_change_and_deletion_update_local_state(self) -> None:
        start = datetime.now(UTC) + timedelta(days=5)
        draft = self.store.create_series_draft(**self.series_values(start))
        series = self.store.activate_series(draft.id, "series-id", 1)
        occurrence = self.store.list_occurrences(series.id)[0]
        moved_start = start + timedelta(hours=2)
        state = CalendarEventState(
            True,
            "instance-id",
            moved_start,
            moved_start + timedelta(minutes=30),
            "Daily",
            None,
            None,
            True,
            datetime.now(UTC),
        )
        moved, _, changed = self.store.apply_occurrence_state(occurrence.id, state)
        self.assertTrue(changed)
        self.assertEqual(moved.status, OCCURRENCE_MOVED)
        self.assertEqual(moved.actual_start_at, moved_start)
        missing, _, changed = self.store.apply_occurrence_state(
            occurrence.id,
            CalendarEventState(False, "instance-id", None, None, None, None, None, None, None),
        )
        self.assertTrue(changed)
        self.assertEqual(missing.status, OCCURRENCE_MISSING)

    def test_reminder_jobs_are_deduplicated_claimed_and_completed(self) -> None:
        now = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        request = self.approved_request(now + timedelta(hours=2))
        self.assertEqual(self.store.rebuild_request_reminders(request.id, 1, (60,), now), 2)
        self.assertEqual(self.store.rebuild_request_reminders(request.id, 1, (60,), now), 2)
        with self.db._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0], 2)
        claimed = self.store.claim_due_jobs(now + timedelta(hours=1), 10)
        self.assertEqual(len(claimed), 2)
        self.store.complete_job(claimed[0].id, now + timedelta(hours=1))
        with self.db._connect() as connection:
            status = connection.execute("SELECT status FROM scheduled_jobs WHERE id = ?", (claimed[0].id,)).fetchone()[0]
        self.assertEqual(status, JOB_DONE)

    def test_failed_job_is_retried_without_duplicate_delivery(self) -> None:
        now = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        request = self.approved_request(now + timedelta(hours=1))
        self.store.rebuild_request_reminders(request.id, 1, (60,), now)
        job = self.store.claim_due_jobs(now, 1)[0]
        self.store.fail_job(job.id, "telegram_error", now)
        with self.db._connect() as connection:
            status = connection.execute("SELECT status FROM scheduled_jobs WHERE id = ?", (job.id,)).fetchone()[0]
            deliveries = connection.execute("SELECT COUNT(*) FROM notification_deliveries WHERE job_id = ?", (job.id,)).fetchone()[0]
        self.assertEqual(status, JOB_PENDING)
        self.assertEqual(deliveries, 1)

    def test_google_change_and_deletion_update_local_state(self) -> None:
        start = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
        request = self.approved_request(start)
        changed_state = CalendarEventState(
            True,
            "event-id",
            start + timedelta(hours=1),
            start + timedelta(hours=1, minutes=30),
            "Changed",
            None,
            "Office",
            True,
            datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        )
        changed, was_changed = self.store.apply_event_state(request.id, changed_state)
        self.assertTrue(was_changed)
        self.assertEqual(changed.subject, "Changed")
        missing_state = CalendarEventState(False, "event-id", None, None, None, None, None, None, None)
        missing, was_changed = self.store.apply_event_state(request.id, missing_state)
        self.assertTrue(was_changed)
        self.assertEqual(missing.status, "CANCELLED_BY_ADMIN")


if __name__ == "__main__":
    unittest.main()
