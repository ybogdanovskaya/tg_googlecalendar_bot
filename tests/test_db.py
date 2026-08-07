from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database, SlotConflictError


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


if __name__ == "__main__":
    unittest.main()
