from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import CallbackQuery, User

from app.db import Database
from app.release_d_handlers import create_release_d_router


class FakeCalendar:
    async def delete_event(self, event_id):
        return True


class ReleaseDBotTests(unittest.TestCase):
    @staticmethod
    def settings():
        return SimpleNamespace(timezone="Europe/Moscow", admin_telegram_id=1)

    def test_router_exposes_statistics_and_data_management(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "router.sqlite3")
            database.initialize()
            router = create_release_d_router(self.settings(), database, FakeCalendar())
            handlers = {item.callback.__name__: item for item in router.callback_query.handlers}
            self.assertIn("statistics_menu", handlers)
            self.assertIn("data_menu", handlers)
            self.assertIn("deletion_execute", handlers)

            async def matches(handler_name: str, data: str) -> bool:
                event = CallbackQuery(
                    id="callback",
                    from_user=User(id=1, is_bot=False, first_name="Admin"),
                    chat_instance="chat",
                    data=data,
                )
                matched, _ = await handlers[handler_name].check(event)
                return bool(matched)

            self.assertTrue(asyncio.run(matches("statistics_menu", "d:stats")))
            self.assertFalse(asyncio.run(matches("statistics_menu", "d:stats:30")))
            self.assertTrue(asyncio.run(matches("statistics_period", "d:stats:30")))
            self.assertTrue(asyncio.run(matches("deletion_execute", "d:data:execute:5")))


if __name__ == "__main__":
    unittest.main()
