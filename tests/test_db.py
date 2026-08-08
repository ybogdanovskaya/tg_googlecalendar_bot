from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database, RequestNotEditableError, SlotConflictError
from app.models import CANCELLED_BY_ADMIN, CHANGE_CANCEL, CHANGE_RESCHEDULE


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
        self.assertEqual(version, 6)
        self.assertEqual(release, "release-d")

    def test_miniapp_session_is_hashed_and_can_be_removed(self) -> None:
        token, csrf_token, _ = self.db.create_miniapp_session(100, ttl_seconds=60)
        session = self.db.get_miniapp_session(token)
        self.assertIsNotNone(session)
        self.assertEqual(session.telegram_id, 100)
        self.assertTrue(self.db.verify_miniapp_csrf(session, csrf_token))
        self.db.delete_miniapp_session(token)
        self.assertIsNone(self.db.get_miniapp_session(token))

    def test_latest_requests_are_displayed_with_newest_at_bottom(self) -> None:
        base = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
        for index in range(4):
            self.create(base + timedelta(hours=index), 30)
        recent = self.db.list_user_requests(100, limit=3)
        self.assertEqual([item.id for item in recent], [2, 3, 4])

    def test_user_accepts_one_of_admin_alternatives(self) -> None:
        request = self.create(datetime(2026, 8, 12, 9, 0, tzinfo=UTC))
        alternative = self.db.create_alternative(
            request.id,
            1,
            datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
            datetime(2026, 8, 12, 11, 45, tzinfo=UTC),
            24,
        )
        updated = self.db.accept_alternative(alternative.id, 100, 24)
        self.assertEqual(updated.start_at, datetime(2026, 8, 12, 11, 0, tzinfo=UTC))
        self.assertEqual(self.db.list_offered_alternatives(request.id), [])

    def test_alternatives_are_limited_expire_and_accept_only_once(self) -> None:
        request = self.create(datetime(2026, 8, 12, 9, 0, tzinfo=UTC))
        alternatives = []
        for hour in (11, 12, 13):
            alternatives.append(
                self.db.create_alternative(
                    request.id,
                    1,
                    datetime(2026, 8, 12, hour, 0, tzinfo=UTC),
                    datetime(2026, 8, 12, hour, 30, tzinfo=UTC),
                    24,
                )
            )
        with self.assertRaises(ValueError):
            self.db.create_alternative(
                request.id,
                1,
                datetime(2026, 8, 12, 14, 0, tzinfo=UTC),
                datetime(2026, 8, 12, 14, 30, tzinfo=UTC),
                24,
            )
        self.db.accept_alternative(alternatives[0].id, 100, 24)
        with self.assertRaises(RequestNotEditableError):
            self.db.accept_alternative(alternatives[1].id, 100, 24)

        expired_request = self.create(datetime(2026, 8, 13, 9, 0, tzinfo=UTC))
        expired = self.db.create_alternative(
            expired_request.id,
            1,
            datetime(2026, 8, 13, 11, 0, tzinfo=UTC),
            datetime(2026, 8, 13, 11, 30, tzinfo=UTC),
            -1,
        )
        with self.assertRaises(RequestNotEditableError):
            self.db.accept_alternative(expired.id, 100, 24)

    def test_admin_text_edit_does_not_change_time(self) -> None:
        request = self.create(datetime(2026, 8, 12, 9, 0, tzinfo=UTC))
        updated = self.db.admin_update_pending_details(
            request.id,
            1,
            {"subject": "New subject", "description": "New details"},
        )
        self.assertEqual(updated.subject, "New subject")
        self.assertEqual(updated.start_at, request.start_at)
        self.assertEqual(updated.end_at, request.end_at)

    def test_approved_meeting_change_requires_admin_completion(self) -> None:
        request = self.create(datetime(2026, 8, 13, 9, 0, tzinfo=UTC))
        self.db.claim_for_approval(request.id, 1)
        approved = self.db.complete_approval(request.id, 1, "event-id")
        change = self.db.create_change_request(approved.id, 100, CHANGE_CANCEL)
        self.assertEqual(self.db.get_request(approved.id).status, "APPROVED")
        self.db.claim_change(change.id, 1)
        _, cancelled = self.db.complete_change(change.id, 1)
        self.assertEqual(cancelled.status, CANCELLED_BY_ADMIN)
        with self.assertRaises(RequestNotEditableError):
            self.db.complete_change(change.id, 1)

    def test_only_one_open_change_request_is_allowed_and_claim_can_be_reset(self) -> None:
        request = self.create(datetime(2026, 8, 13, 10, 0, tzinfo=UTC))
        self.db.claim_for_approval(request.id, 1)
        approved = self.db.complete_approval(request.id, 1, "event-id")
        change = self.db.create_change_request(approved.id, 100, CHANGE_CANCEL)
        with self.assertRaises(RequestNotEditableError):
            self.db.create_change_request(approved.id, 100, CHANGE_CANCEL)
        self.assertIsNotNone(self.db.claim_change(change.id, 1))
        self.assertIsNone(self.db.claim_change(change.id, 1))
        self.db.reset_change(change.id, "test_failure")
        self.assertIsNotNone(self.db.claim_change(change.id, 1))

    def test_reschedule_change_updates_approved_meeting(self) -> None:
        request = self.create(datetime(2026, 8, 14, 9, 0, tzinfo=UTC))
        self.db.claim_for_approval(request.id, 1)
        approved = self.db.complete_approval(request.id, 1, "event-id")
        change = self.db.create_change_request(
            approved.id,
            100,
            CHANGE_RESCHEDULE,
            datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 14, 12, 30, tzinfo=UTC),
        )
        self.db.claim_change(change.id, 1)
        _, moved = self.db.complete_change(change.id, 1)
        self.assertEqual(moved.start_at, datetime(2026, 8, 14, 12, 0, tzinfo=UTC))

    def test_only_admin_override_allows_manual_overlap(self) -> None:
        self.create(datetime(2026, 8, 15, 9, 0, tzinfo=UTC), 60)
        values = dict(
            admin_id=100,
            admin_name="Admin",
            admin_username=None,
            email=None,
            subject="Manual",
            description=None,
            location=None,
            start_at=datetime(2026, 8, 15, 9, 30, tzinfo=UTC),
            end_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
            blocks_calendar=True,
        )
        with self.assertRaises(SlotConflictError):
            self.db.create_admin_draft(**values, allow_overlap=False)
        draft = self.db.create_admin_draft(**values, allow_overlap=True)
        self.assertTrue(draft.admin_override)
        self.assertEqual(draft.source, "ADMIN")
        self.db.complete_approval(draft.id, 100, "manual-event")
        with self.assertRaises(RuntimeError):
            self.db.complete_approval(draft.id, 100, "manual-event")

    def test_nonblocking_admin_event_does_not_hide_slot(self) -> None:
        start = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
        draft = self.db.create_admin_draft(
            admin_id=100,
            admin_name="Admin",
            admin_username=None,
            email=None,
            subject="Reminder",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(hours=1),
            blocks_calendar=False,
            allow_overlap=False,
        )
        self.db.complete_approval(draft.id, 100, "free-event")
        self.assertEqual(self.db.active_intervals(start, start + timedelta(hours=1)), [])
        overlapping = self.create(start + timedelta(minutes=15), 30)
        self.assertEqual(overlapping.id, draft.id + 1)


if __name__ == "__main__":
    unittest.main()
