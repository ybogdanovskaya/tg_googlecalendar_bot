from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database, RequestNotEditableError, SlotConflictError


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "test.sqlite3")
        self.db.initialize()
        self.db.upsert_user(100, "Test User", "tester")
        self.db.set_consent(100, "v1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self, start: datetime, duration: int = 30):
        return self.db.create_request(
            telegram_id=100,
            telegram_name="Test User",
            telegram_username="tester",
            email="test@example.com",
            subject="Test",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=duration),
            hold_hours=24,
        )

    def test_overlapping_reservation_is_rejected(self) -> None:
        start = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
        self.create(start, 60)
        with self.assertRaises(SlotConflictError):
            self.create(start + timedelta(minutes=30), 30)

    def test_adjacent_reservation_is_allowed(self) -> None:
        start = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
        self.create(start, 30)
        second = self.create(start + timedelta(minutes=30), 30)
        self.assertEqual(second.id, 2)

    def test_only_owner_can_cancel_pending_request(self) -> None:
        request = self.create(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
        self.assertFalse(self.db.cancel_pending(request.id, 999))
        self.assertTrue(self.db.cancel_pending(request.id, 100))
        self.assertFalse(self.db.cancel_pending(request.id, 100))

    def test_approval_can_be_claimed_once(self) -> None:
        request = self.create(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
        first = self.db.claim_for_approval(request.id, 1)
        second = self.db.claim_for_approval(request.id, 1)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_pending_details_can_be_edited_only_by_owner(self) -> None:
        request = self.create(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
        updated = self.db.update_pending_details(
            request.id,
            100,
            {"subject": "Updated", "description": "Details"},
        )
        self.assertEqual(updated.subject, "Updated")
        self.assertEqual(updated.description, "Details")
        with self.assertRaises(RequestNotEditableError):
            self.db.update_pending_details(request.id, 999, {"subject": "Wrong owner"})

    def test_reschedule_is_atomic_and_renews_hold(self) -> None:
        first = self.create(datetime(2026, 8, 10, 9, 0, tzinfo=UTC), 30)
        self.create(datetime(2026, 8, 10, 10, 0, tzinfo=UTC), 30)
        old_hold = first.hold_until
        updated = self.db.reschedule_pending(
            first.id,
            100,
            datetime(2026, 8, 10, 11, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 11, 45, tzinfo=UTC),
            48,
        )
        self.assertEqual(updated.start_at, datetime(2026, 8, 10, 11, 0, tzinfo=UTC))
        self.assertGreater(updated.hold_until, old_hold)
        with self.assertRaises(SlotConflictError):
            self.db.reschedule_pending(
                first.id,
                100,
                datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
                datetime(2026, 8, 10, 10, 30, tzinfo=UTC),
                24,
            )
        self.assertEqual(
            self.db.get_request(first.id).start_at,
            datetime(2026, 8, 10, 11, 0, tzinfo=UTC),
        )

    def test_settings_history_and_closed_dates(self) -> None:
        self.db.set_setting("min_lead_minutes", 180, 1)
        self.assertEqual(self.db.get_settings()["min_lead_minutes"], 180)
        history = self.db.list_setting_history()
        self.assertEqual(history[0]["key"], "min_lead_minutes")
        self.assertEqual(history[0]["new_value"], 180)
        self.assertTrue(self.db.add_closed_date("2026-08-15", 1))
        self.assertFalse(self.db.add_closed_date("2026-08-15", 1))
        self.assertEqual(self.db.list_closed_dates("2026-08-01"), ["2026-08-15"])
        self.assertTrue(self.db.remove_closed_date("2026-08-15", 1))

    def test_schema_is_versioned(self) -> None:
        version, release = self.db.schema_info()
        self.assertEqual(version, 2)
        self.assertEqual(release, "release-a")


if __name__ == "__main__":
    unittest.main()
