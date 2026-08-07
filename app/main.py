from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, MenuButtonCommands

from app.apps_script_calendar import AppsScriptCalendar
from app.bot import create_router
from app.config import Settings
from app.db import Database
from app.logging_setup import configure_logging
from app.release_b_handlers import create_release_b_router


LOGGER = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="my", description="Мои заявки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="privacy", description="Конфиденциальность"),
]


async def configure_bot_menu(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        LOGGER.info("bot_menu_configured")
    except Exception:
        LOGGER.exception("bot_menu_configuration_failed")


async def run() -> None:
    settings = Settings.from_env()
    settings.ensure_directories()
    configure_logging(settings.log_path)
    database = Database(settings.database_path)
    database.initialize()
    schema_version, release_id = database.schema_info()
    LOGGER.info(
        "database_ready",
        extra={
            "schema_version": schema_version,
            "release_id": release_id,
            "migration_backup_created": bool(
                database.last_migration_result and database.last_migration_result.backup_path
            ),
        },
    )
    if settings.apps_script_url:
        calendar = AppsScriptCalendar(settings.apps_script_url, settings.apps_script_secret_file)
    else:
        from app.google_calendar import GoogleCalendar

        calendar = GoogleCalendar(settings.google_token_file, settings.google_calendar_id)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_release_b_router(settings, database, calendar))
    dispatcher.include_router(create_router(settings, database, calendar))
    await configure_bot_menu(bot)
    LOGGER.info("bot_starting")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        LOGGER.info("bot_stopped")


if __name__ == "__main__":
    asyncio.run(run())
