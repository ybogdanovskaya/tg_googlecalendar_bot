from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import CallbackQuery, User

from app.automation_store import AutomationStore
from app.bot import main_menu
from app.db import Database
from app.release_c_handlers import create_release_c_router


class FakeCalendar:
    async def busy(self, start_at, end_at):
        return []

    async def create_series(self, series):
        return "series-id"

    async def update_series(self, series):
        return series.google_series_id

    async def delete_series(self, series_id):
        return True

    async def update_occurrence(self, series_id, occurrence, start_at, end_at):
        return "occurrence-id"

    async def delete_occurrence(self, series_id, occurrence):
        return True


class ReleaseCBotTests(unittest.TestCase):
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

    def make_router(self):
        temporary = tempfile.TemporaryDirectory()
        database = Database(Path(temporary.name) / "router.sqlite3")
        database.initialize()
        router = create_release_c_router(
            self.settings(),
            database,
            FakeCalendar(),
            AutomationStore(database),
        )
        return temporary, router

    def test_admin_menu_exposes_release_c_workflows(self) -> None:
        callbacks = {
            button.callback_data
            for row in main_menu(True).inline_keyboard
            for button in row
        }
        self.assertIn("c:series", callbacks)

    def test_release_c_router_contains_full_workflow(self) -> None:
        temporary, router = self.make_router()
        try:
            self.assertGreaterEqual(len(router.callback_query.handlers), 20)
            self.assertGreaterEqual(len(router.message.handlers), 8)
        finally:
            temporary.cleanup()

    def test_confirmation_callbacks_do_not_match_first_step_handlers(self) -> None:
        temporary, router = self.make_router()
        try:
            handlers = {item.callback.__name__: item for item in router.callback_query.handlers}

            async def matches(handler_name: str, data: str) -> bool:
                event = CallbackQuery(
                    id="callback",
                    from_user=User(id=1, is_bot=False, first_name="Admin"),
                    chat_instance="chat",
                    data=data,
                )
                matched, _ = await handlers[handler_name].check(event)
                return bool(matched)

            self.assertTrue(asyncio.run(matches("occurrence_cancel", "c:occ:cancel:5")))
            self.assertFalse(asyncio.run(matches("occurrence_cancel", "c:occ:cancel:confirm:5")))
            self.assertTrue(asyncio.run(matches("occurrence_cancel_confirm", "c:occ:cancel:confirm:5")))
            self.assertTrue(asyncio.run(matches("series_cancel", "c:series:cancel:8")))
            self.assertFalse(asyncio.run(matches("series_cancel", "c:series:cancel:confirm:8")))
            self.assertTrue(asyncio.run(matches("series_cancel_confirm", "c:series:cancel:confirm:8")))
            self.assertTrue(asyncio.run(matches("occurrence_move", "c:occ:move:5")))
            self.assertFalse(asyncio.run(matches("occurrence_move", "c:occ:move:apply:1")))
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
