from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.apps_script_calendar import AppsScriptCalendar
from app.bot import create_router
from app.config import Settings
from app.db import Database
from app.logging_setup import configure_logging


LOGGER = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings.from_env()
    settings.ensure_directories()
    configure_logging(settings.log_path)
    database = Database(settings.database_path)
    database.initialize()
    if settings.apps_script_url:
        calendar = AppsScriptCalendar(settings.apps_script_url, settings.apps_script_secret_file)
    else:
        from app.google_calendar import GoogleCalendar

        calendar = GoogleCalendar(settings.google_token_file, settings.google_calendar_id)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(settings, database, calendar))
    LOGGER.info("bot_starting")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        LOGGER.info("bot_stopped")


if __name__ == "__main__":
    asyncio.run(run())
