from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.bot import create_router, format_request, main_menu, privacy_text
from app.db import Database
from app.models import MeetingRequest, PENDING


class FakeCalendar:
    async def busy(self, start_at, end_at):
        return []

    async def is_free(self, start_at, end_at):
        return True

    async def create_event(self, request):
        return "test-event"


class ReleaseABotTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            timezone="Europe/Moscow",
            admin_telegram_id=1,
            privacy_policy_version="2026-08-07",
            min_lead_minutes=120,
            booking_horizon_days=30,
            hold_hours=24,
        )

    def request(self, hold_until: datetime) -> MeetingRequest:
        now = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
        return MeetingRequest(
            id=1,
            telegram_id=100,
            telegram_name="Test User",
            telegram_username="tester",
            email="test@example.com",
            subject="Test",
            description=None,
            location=None,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, minutes=30),
            status=PENDING,
            hold_until=hold_until,
            google_event_id=None,
            created_at=now,
            updated_at=now,
        )

    def test_expired_hold_is_shown_without_changing_request_status(self) -> None:
        now = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
        text = format_request(
            self.request(now - timedelta(minutes=1)),
            "Europe/Moscow",
            include_private=False,
            now=now,
        )
        self.assertIn("резерв слота завершён", text)
        self.assertIn("ожидает решения", text)

    def test_admin_menu_contains_settings_but_user_menu_does_not(self) -> None:
        admin_callbacks = {
            button.callback_data
            for row in main_menu(True).inline_keyboard
            for button in row
        }
        user_callbacks = {
            button.callback_data
            for row in main_menu(False).inline_keyboard
            for button in row
        }
        self.assertIn("admin:settings", admin_callbacks)
        self.assertNotIn("admin:settings", user_callbacks)
        self.assertIn("privacy", user_callbacks)

    def test_router_is_constructed_with_release_a_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "router.sqlite3")
            database.initialize()
            router = create_router(self.settings(), database, FakeCalendar())
        self.assertGreater(len(router.callback_query.handlers), 20)
        self.assertGreater(len(router.message.handlers), 5)

    def test_privacy_text_does_not_claim_unimplemented_automatic_deletion(self) -> None:
        text = privacy_text(self.settings())
        self.assertIn("До запуска автоматического срока хранения", text)
        self.assertIn("сервере в России", text)


if __name__ == "__main__":
    unittest.main()
