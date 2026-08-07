from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from app.bot import _keyboard, home_keyboard
from app.calendar_client import CalendarClient
from app.config import Settings
from app.db import Database
from app.release_d_store import (
    DELETE_CANCEL_FUTURE,
    DELETE_COMPLETED,
    DELETE_KEEP_FUTURE,
    DELETE_REQUESTED,
    DELETE_WAITING,
    DeletionRequest,
    PeriodStatistics,
    ReleaseDStore,
)


LOGGER = logging.getLogger(__name__)


def create_release_d_router(settings: Settings, db: Database, calendar: CalendarClient) -> Router:
    router = Router(name="release-d")
    store = ReleaseDStore(db)
    zone = ZoneInfo(settings.timezone)

    def is_admin(telegram_id: int) -> bool:
        return telegram_id == settings.admin_telegram_id

    async def require_admin(callback: CallbackQuery) -> bool:
        if is_admin(callback.from_user.id):
            return True
        await callback.answer("Нет доступа", show_alert=True)
        return False

    def stats_text(stats: PeriodStatistics, label: str) -> str:
        status_labels = {
            "PENDING": "ожидают решения",
            "APPROVING": "создаются",
            "APPROVED": "согласованы",
            "REJECTED": "отклонены",
            "CANCELLED": "отменены пользователем",
            "CANCELLED_BY_ADMIN": "отменены администратором",
        }
        breakdown = [
            f"• {status_labels.get(key, html.escape(key))}: {value}"
            for key, value in stats.statuses.items()
        ]
        return (
            f"<b>Статистика — {html.escape(label)}</b>\n\n"
            f"Заявки пользователей: <b>{stats.user_requests}</b>\n"
            f"Созданные вручную встречи: <b>{stats.manual_meetings}</b>\n"
            f"Всего встреч в календаре за период: <b>{stats.calendar_meetings}</b>\n"
            f"Уникальные пользователи: <b>{stats.unique_users}</b>\n\n"
            + ("Статусы заявок:\n" + "\n".join(breakdown) if breakdown else "Заявок за период нет.")
        )

    def stats_keyboard():
        return _keyboard(
            [
                [
                    InlineKeyboardButton(text="Сегодня", callback_data="d:stats:1"),
                    InlineKeyboardButton(text="7 дней", callback_data="d:stats:7"),
                ],
                [
                    InlineKeyboardButton(text="30 дней", callback_data="d:stats:30"),
                    InlineKeyboardButton(text="12 месяцев", callback_data="d:stats:365"),
                ],
                [InlineKeyboardButton(text="За всё время", callback_data="d:stats:all")],
                [InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")],
            ]
        )

    @router.callback_query(F.data == "d:stats")
    async def statistics_menu(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите период статистики:", reply_markup=stats_keyboard())

    @router.callback_query(F.data.startswith("d:stats:"))
    async def statistics_period(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        raw = callback.data.rsplit(":", 1)[1]
        current = datetime.now(UTC)
        if raw == "all":
            start = datetime(2020, 1, 1, tzinfo=UTC)
            label = "за всё время"
        else:
            try:
                days = int(raw)
            except ValueError:
                await callback.answer("Период недоступен", show_alert=True)
                return
            if days not in {1, 7, 30, 365}:
                await callback.answer("Период недоступен", show_alert=True)
                return
            start = current - timedelta(days=days)
            label = "сегодня" if days == 1 else f"последние {days} дней"
        stats = await asyncio.to_thread(store.statistics, start, current)
        await callback.answer()
        if callback.message:
            await callback.message.answer(stats_text(stats, label), reply_markup=stats_keyboard())

    @router.callback_query(F.data == "d:data")
    async def data_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "<b>Управление моими данными</b>\n\n"
                "Выберите, что делать с будущими согласованными встречами. "
                "Перед выполнением бот ещё дважды покажет последствия и попросит подтверждение.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="Удалить данные и отменить будущие встречи", callback_data="d:data:prepare:cancel")],
                        [InlineKeyboardButton(text="Удалить историю, будущие встречи сохранить", callback_data="d:data:prepare:keep")],
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("d:data:prepare:"))
    async def deletion_prepare(callback: CallbackQuery) -> None:
        raw = callback.data.rsplit(":", 1)[1]
        mode = DELETE_CANCEL_FUTURE if raw == "cancel" else DELETE_KEEP_FUTURE if raw == "keep" else ""
        if not mode:
            await callback.answer("Вариант недоступен", show_alert=True)
            return
        request = await asyncio.to_thread(store.create_deletion_request, callback.from_user.id, mode)
        if request.telegram_id != callback.from_user.id or request.status not in {DELETE_REQUESTED, DELETE_WAITING}:
            await callback.answer("Запрос уже обработан", show_alert=True)
            return
        if request.status == DELETE_WAITING:
            await callback.answer("История уже удалена; будущие встречи ожидают завершения", show_alert=True)
            return
        action = (
            "будут отменены в Google Calendar"
            if mode == DELETE_CANCEL_FUTURE
            else "останутся в Google Calendar; до их окончания сохранится только минимум данных"
        )
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "<b>Первое подтверждение</b>\n\n"
                f"Будущих согласованных встреч: <b>{request.future_meeting_count}</b>. Они {action}.\n"
                "Закрытая история будет обезличена. Операцию нельзя отменить после окончательного подтверждения.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="Продолжить", callback_data=f"d:data:confirm:{request.id}")],
                        [InlineKeyboardButton(text="Не удалять", callback_data="home")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("d:data:confirm:"))
    async def deletion_confirm(callback: CallbackQuery) -> None:
        request = await asyncio.to_thread(store.get_deletion_request, int(callback.data.rsplit(":", 1)[1]))
        if request is None or request.telegram_id != callback.from_user.id or request.status != DELETE_REQUESTED:
            await callback.answer("Запрос устарел", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            consequence = (
                "Отменить будущие встречи и удалить мои данные"
                if request.mode == DELETE_CANCEL_FUTURE
                else "Удалить историю и сохранить будущие встречи"
            )
            await callback.message.answer(
                "<b>Окончательное подтверждение</b>\n\n"
                "После нажатия начнётся удаление или обезличивание данных.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text=consequence, callback_data=f"d:data:execute:{request.id}")],
                        [InlineKeyboardButton(text="Не удалять", callback_data="home")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("d:data:execute:"))
    async def deletion_execute(callback: CallbackQuery) -> None:
        request_id = int(callback.data.rsplit(":", 1)[1])
        request = await asyncio.to_thread(store.get_deletion_request, request_id)
        if request is None or request.telegram_id != callback.from_user.id or request.status != DELETE_REQUESTED:
            await callback.answer("Запрос уже выполнен или устарел", show_alert=True)
            return
        await callback.answer("Выполняю запрос…")
        if callback.message:
            await callback.message.answer("⏳ Выполняю запрос. Не нажимайте кнопку повторно.")
        try:
            if request.mode == DELETE_CANCEL_FUTURE:
                for meeting_id, event_id in await asyncio.to_thread(store.future_google_events, request):
                    try:
                        await calendar.delete_event(event_id)
                    except Exception as exc:
                        LOGGER.exception("user_deletion_calendar_failed", extra={"request_id": request.id, "meeting_id": meeting_id})
                        await asyncio.to_thread(store.mark_deletion_failed, request.id, type(exc).__name__)
                        raise
                await asyncio.to_thread(store.complete_cancel_future, request.id, callback.from_user.id)
                result_text = "Данные обезличены, будущие встречи отменены. При следующем запуске бот снова запросит согласие."
            else:
                completed = await asyncio.to_thread(store.complete_keep_future, request.id, callback.from_user.id)
                if completed.status == DELETE_WAITING and completed.execute_after:
                    local_until = completed.execute_after.astimezone(zone)
                    result_text = (
                        "Закрытая история обезличена. Будущие встречи сохранены; оставшиеся минимальные данные "
                        f"будут удалены после {local_until:%d.%m.%Y %H:%M} (МСК)."
                    )
                elif completed.status == DELETE_COMPLETED:
                    result_text = "Данные обезличены. Будущих встреч для сохранения не было."
                else:
                    raise RuntimeError("unexpected deletion status")
        except Exception:
            if callback.message:
                await callback.message.answer(
                    "Удаление пока не завершено из-за временной ошибки Google. Данные не помечены удалёнными; повторите запрос позже.",
                    reply_markup=home_keyboard(),
                )
            return
        if callback.message:
            await callback.message.answer("✅ " + result_text, reply_markup=home_keyboard())

    return router
