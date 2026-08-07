from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.bot import main_menu
from app.db import Database
from app.release_b_handlers import create_release_b_router


class FakeCalendar:
    async def busy(self, start_at, end_at):
        return []

    async def is_free(self, start_at, end_at):
        return True

    async def create_event(self, request):
        return "event-id"

    async def update_event(self, request):
        return request.google_event_id

    async def delete_event(self, event_id):
        return True


class ReleaseBBotTests(unittest.TestCase):
    @staticmethod
    def settings():
        return SimpleNamespace(
            timezone="Europe/Moscow",
            admin_telegram_id=1,
            privacy_policy_version="2026-08-07",
            min_lead_minutes=120,
            booking_horizon_days=30,
            hold_hours=24,
        )

    def test_admin_menu_exposes_release_b_workflows(self) -> None:
        callbacks = {
            button.callback_data
            for row in main_menu(True).inline_keyboard
            for button in row
        }
        self.assertIn("b:changes", callbacks)
        self.assertIn("b:manual", callbacks)

    def test_release_b_router_contains_message_and_callback_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "router.sqlite3")
            database.initialize()
            router = create_release_b_router(self.settings(), database, FakeCalendar())
        self.assertGreaterEqual(len(router.callback_query.handlers), 20)
        self.assertGreaterEqual(len(router.message.handlers), 10)


if __name__ == "__main__":
    unittest.main()
