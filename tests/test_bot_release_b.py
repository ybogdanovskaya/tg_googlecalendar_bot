from __future__ import annotations

import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.bot import main_menu
from app.db import Database
from app.release_b_handlers import create_release_b_router
from aiogram.types import CallbackQuery, User


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

    def test_confirm_buttons_do_not_match_broader_first_step_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "router.sqlite3")
            database.initialize()
            router = create_release_b_router(self.settings(), database, FakeCalendar())
            handlers = {item.callback.__name__: item for item in router.callback_query.handlers}

            async def matches(handler_name: str, data: str) -> bool:
                event = CallbackQuery(
                    id="callback",
                    from_user=User(id=100, is_bot=False, first_name="Test"),
                    chat_instance="chat",
                    data=data,
                )
                matched, _ = await handlers[handler_name].check(event)
                return bool(matched)

            self.assertTrue(asyncio.run(matches("ask_cancel", "b:cancel:42")))
            self.assertFalse(asyncio.run(matches("ask_cancel", "b:cancel:confirm:42")))
            self.assertTrue(asyncio.run(matches("confirm_cancel", "b:cancel:confirm:42")))
            self.assertTrue(asyncio.run(matches("move_start", "b:move:42")))
            self.assertFalse(asyncio.run(matches("move_start", "b:move:dur:42:30")))
            self.assertTrue(asyncio.run(matches("move_duration", "b:move:dur:42:30")))


if __name__ == "__main__":
    unittest.main()
