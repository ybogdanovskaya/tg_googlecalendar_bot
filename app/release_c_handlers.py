from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.automation_store import AutomationStore
from app.booking_rules import load_rules
from app.bot import EMAIL_RE, _keyboard, cancel_keyboard, home_keyboard
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.date_picker import calendar_keyboard, month_from_callback
from app.db import Database, RequestNotEditableError, SlotConflictError
from app.input_parsing import parse_time_input
from app.models import (
    OCCURRENCE_CANCELLED,
    OCCURRENCE_MISSING,
    SERIES_DAILY,
    SERIES_ACTIVE,
    SERIES_CREATING,
    SERIES_FAILED,
    SERIES_MONTHLY,
    SERIES_WEEKLY,
    EventOccurrence,
    EventSeries,
)
from app.notification_rules import (
    AUTOMATION_ENABLED,
    PENDING_REMINDER_HOURS,
    REMINDER_MINUTES,
    load_notification_rules,
    validate_value,
)
from app.recurrence import generate_occurrences


LOGGER = logging.getLogger(__name__)

FREQUENCY_LABELS = {
    SERIES_DAILY: "ежедневно",
    SERIES_WEEKLY: "еженедельно",
    SERIES_MONTHLY: "ежемесячно",
}
OCCURRENCE_STATUS_LABELS = {
    "SCHEDULED": "запланирована",
    "MOVED": "перенесена",
    OCCURRENCE_CANCELLED: "отменена",
    OCCURRENCE_MISSING: "удалена в Google",
}


class ReleaseCFlow(StatesGroup):
    series_subject = State()
    series_date = State()
    series_time = State()
    series_until = State()
    series_email = State()
    series_description = State()
    series_location = State()
    occurrence_date = State()
    occurrence_time = State()
    series_new_time = State()
    series_edit_date = State()
    series_edit_until = State()
    series_edit_value = State()
    notification_value = State()


def _date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def _clock(value: str) -> time:
    return parse_time_input(value)


def _overlaps(first_start: datetime, first_end: datetime, second_start: datetime, second_end: datetime) -> bool:
    return first_start < second_end and first_end > second_start


def _series_text(series: EventSeries, zone: ZoneInfo) -> str:
    start = series.start_at.astimezone(zone)
    duration = int((series.end_at - series.start_at).total_seconds() // 60)
    guest = html.escape(series.email) if series.email else "только я"
    calendar_state = "занимает время" if series.blocks_calendar else "не блокирует календарь"
    return (
        f"<b>Серия №{series.id}</b>\n"
        f"{html.escape(series.subject)}\n"
        f"Первая встреча: {start:%d.%m.%Y %H:%M} (МСК)\n"
        f"Повтор: {FREQUENCY_LABELS.get(series.frequency, series.frequency)} до {series.until_date}\n"
        f"Длительность: {duration} мин.\n"
        f"Участник: {guest}\n"
        f"Событие {calendar_state}."
    )


def _occurrence_text(occurrence: EventOccurrence, series: EventSeries, zone: ZoneInfo) -> str:
    start = occurrence.actual_start_at.astimezone(zone)
    end = occurrence.actual_end_at.astimezone(zone)
    return (
        f"<b>{html.escape(series.subject)}</b>\n"
        f"{start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)\n"
        f"Статус: {OCCURRENCE_STATUS_LABELS.get(occurrence.status, occurrence.status)}"
    )


def create_release_c_router(
    settings: Settings,
    db: Database,
    calendar: CalendarClient,
    automation: AutomationStore,
) -> Router:
    router = Router(name="release-c")
    zone = ZoneInfo(settings.timezone)

    def is_admin(user_id: int) -> bool:
        return user_id == settings.admin_telegram_id

    async def require_admin(callback: CallbackQuery) -> bool:
        if is_admin(callback.from_user.id):
            return True
        await callback.answer("Нет доступа", show_alert=True)
        return False

    async def external_conflict(occurrences: list[tuple[datetime, datetime]]) -> bool:
        if not occurrences:
            return False
        first = min(item[0] for item in occurrences)
        last = max(item[1] for item in occurrences)
        cursor = first
        while cursor < last:
            boundary = min(cursor + timedelta(days=30), last)
            for attempt in range(2):
                try:
                    busy = await calendar.busy(cursor, boundary)
                    break
                except CalendarUnavailable:
                    if attempt:
                        raise
                    LOGGER.warning("calendar_busy_retry")
                    await asyncio.sleep(1)
            for item_start, item_end in occurrences:
                if item_start >= boundary or item_end <= cursor:
                    continue
                if any(_overlaps(item_start, item_end, busy_start, busy_end) for busy_start, busy_end in busy):
                    return True
            cursor = boundary
        return False

    def local_conflict(
        occurrences: list[tuple[datetime, datetime]],
        *,
        exclude_occurrence_id: int | None = None,
        exclude_series_id: int | None = None,
    ) -> bool:
        return any(
            db.active_intervals(
                item_start,
                item_end,
                exclude_occurrence_id=exclude_occurrence_id,
                exclude_series_id=exclude_series_id,
            )
            for item_start, item_end in occurrences
        )

    async def has_conflict(
        occurrences: list[tuple[datetime, datetime]],
        *,
        exclude_occurrence_id: int | None = None,
        exclude_series_id: int | None = None,
    ) -> bool:
        if local_conflict(
            occurrences,
            exclude_occurrence_id=exclude_occurrence_id,
            exclude_series_id=exclude_series_id,
        ):
            return True
        return await external_conflict(occurrences)

    async def schedule_series_reminders(series_id: int) -> None:
        rules = load_notification_rules(db, settings)
        if not rules.automation_enabled:
            return
        for occurrence in automation.list_occurrences(series_id, future_only=True, limit=400):
            automation.rebuild_occurrence_reminders(
                occurrence.id,
                settings.admin_telegram_id,
                rules.reminder_minutes,
            )

    async def render_series_menu(message: Message) -> None:
        await message.answer(
            "<b>Повторяющиеся встречи</b>\n\n"
            "Можно создать ежедневную, еженедельную или ежемесячную серию, "
            "перенести/отменить одну встречу либо изменить/отменить всю серию.",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="➕ Создать серию", callback_data="c:series:new")],
                    [InlineKeyboardButton(text="📋 Мои серии", callback_data="c:series:list")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
                ]
            ),
        )

    async def render_notification_settings(message: Message) -> None:
        rules = load_notification_rules(db, settings)
        intervals = ", ".join(str(item) for item in rules.reminder_minutes)
        state = "включены" if rules.automation_enabled else "выключены"
        await message.answer(
            "<b>Напоминания</b>\n\n"
            f"Автоматические уведомления: <b>{state}</b>\n"
            f"Перед встречей: <b>{intervals} мин.</b>\n"
            f"О заявке без решения: через <b>{rules.pending_reminder_hours} ч.</b>, затем с тем же интервалом.\n\n"
            "Изменения применяются к будущим уведомлениям.",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="⏱ Интервалы перед встречей", callback_data="c:notify:minutes")],
                    [InlineKeyboardButton(text="⌛ Заявка без решения", callback_data="c:notify:pending")],
                    [
                        InlineKeyboardButton(
                            text="⏸ Выключить" if rules.automation_enabled else "▶️ Включить",
                            callback_data="c:notify:toggle",
                        )
                    ],
                    [InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")],
                ]
            ),
        )

    async def ask_series_participant(message: Message) -> None:
        await message.answer(
            "Кого добавить участником?",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="👤 Только меня", callback_data="c:series:email:self")],
                    [InlineKeyboardButton(text="✉️ Добавить гостя по email", callback_data="c:series:email:guest")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    async def render_series_review(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            start = datetime.combine(
                date.fromisoformat(str(data["start_date"])),
                _clock(str(data["start_time"])),
                zone,
            )
            end = start + timedelta(minutes=int(data["duration"]))
            occurrences = generate_occurrences(
                start,
                end,
                str(data["frequency"]),
                date.fromisoformat(str(data["until_date"])),
            )
        except (KeyError, TypeError, ValueError):
            await state.clear()
            await message.answer("Черновик повреждён. Начните создание заново.", reply_markup=home_keyboard())
            return
        guest = html.escape(str(data["email"])) if data.get("email") else "только я"
        lines = [
            "<b>Проверьте серию</b>",
            "",
            f"Тема: {html.escape(str(data['subject']))}",
            f"Первая встреча: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)",
            f"Повтор: {FREQUENCY_LABELS[str(data['frequency'])]} до {data['until_date']}",
            f"Количество встреч: {len(occurrences)}",
            f"Участник: {guest}",
            f"Календарь: {'занято' if data.get('blocks_calendar') else 'свободно'}",
        ]
        if data.get("description"):
            lines.append(f"Описание: {html.escape(str(data['description']))}")
        if data.get("location"):
            lines.append(f"Место/ссылка: {html.escape(str(data['location']))}")
        await state.set_state(None)
        await message.answer(
            "\n".join(lines),
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="✅ Создать серию", callback_data="c:series:create")],
                    [InlineKeyboardButton(text="✏️ Изменить", callback_data="c:series:edit")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    @router.callback_query(F.data == "c:series")
    async def series_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await render_series_menu(callback.message)

    @router.callback_query(F.data == "c:series:new")
    async def series_new(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.set_state(ReleaseCFlow.series_subject)
        await state.set_data({})
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите тему повторяющейся встречи:", reply_markup=cancel_keyboard())

    @router.callback_query(F.data == "datepick:noop")
    async def calendar_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data.startswith("cdate:"))
    async def calendar_choice(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        try:
            _, kind, action, raw_value = callback.data.split(":", 3)
            data = await state.get_data()
            expected_states = {
                "start": ReleaseCFlow.series_date.state,
                "until": ReleaseCFlow.series_until.state,
                "occurrence": ReleaseCFlow.occurrence_date.state,
                "editstart": ReleaseCFlow.series_edit_date.state,
                "edituntil": ReleaseCFlow.series_edit_until.state,
            }
            if kind not in expected_states or await state.get_state() != expected_states[kind]:
                raise ValueError
            today = datetime.now(zone).date()
            if kind in {"start", "editstart"}:
                minimum, maximum = today, today + timedelta(days=366)
            elif kind in {"until", "edituntil"}:
                minimum = date.fromisoformat(str(data["start_date"]))
                maximum = minimum + timedelta(days=366)
            elif kind == "occurrence":
                minimum, maximum = today, today + timedelta(days=366)
            else:
                raise ValueError
            if action == "nav":
                shown = month_from_callback(raw_value)
                if not date(minimum.year, minimum.month, 1) <= shown <= date(maximum.year, maximum.month, 1):
                    raise ValueError
                await callback.answer()
                if callback.message:
                    await callback.message.edit_reply_markup(
                        reply_markup=calendar_keyboard(f"cdate:{kind}", shown, minimum, maximum)
                    )
                return
            if action != "day":
                raise ValueError
            selected = date.fromisoformat(raw_value)
            if not minimum <= selected <= maximum:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            await callback.answer("Дата недоступна. Откройте календарь заново.", show_alert=True)
            return
        await callback.answer(f"Выбрано: {selected:%d.%m.%Y}")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        if kind == "start":
            await state.update_data(start_date=selected.isoformat())
            await state.set_state(ReleaseCFlow.series_time)
            if callback.message:
                await callback.message.answer(
                    "Введите время первой встречи, например 930 или 1430 (МСК):",
                    reply_markup=cancel_keyboard(),
                )
        elif kind == "until":
            await state.update_data(until_date=selected.isoformat())
            await state.set_state(None)
            if callback.message:
                await ask_series_participant(callback.message)
        elif kind == "occurrence":
            await state.update_data(move_date=selected.isoformat())
            await state.set_state(ReleaseCFlow.occurrence_time)
            if callback.message:
                await callback.message.answer(
                    "Введите новое время, например 930 или 1430 (МСК):",
                    reply_markup=cancel_keyboard(),
                )
        elif kind == "editstart":
            updates: dict[str, object] = {"start_date": selected.isoformat()}
            if date.fromisoformat(str(data["until_date"])) < selected:
                updates["until_date"] = selected.isoformat()
            await state.update_data(**updates)
            await state.set_state(None)
            if callback.message:
                await render_series_review(callback.message, state)
        else:
            await state.update_data(until_date=selected.isoformat())
            await state.set_state(None)
            if callback.message:
                await render_series_review(callback.message, state)

    @router.message(ReleaseCFlow.series_subject)
    async def series_subject(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        value = (message.text or "").strip()
        if not 1 <= len(value) <= 200:
            await message.answer("Тема должна содержать от 1 до 200 символов.", reply_markup=cancel_keyboard())
            return
        await state.update_data(subject=value)
        await state.set_state(ReleaseCFlow.series_date)
        today = datetime.now(zone).date()
        await message.answer(
            "Выберите дату первой встречи:",
            reply_markup=calendar_keyboard("cdate:start", today, today, today + timedelta(days=366)),
        )

    @router.message(ReleaseCFlow.series_date)
    async def series_date(message: Message, state: FSMContext) -> None:
        try:
            value = _date(message.text or "")
            if value < datetime.now(zone).date() or value > datetime.now(zone).date() + timedelta(days=366):
                raise ValueError
        except ValueError:
            await message.answer("Нужна дата от сегодняшнего дня до одного года вперёд, например 15.08.2026.")
            return
        await state.update_data(start_date=value.isoformat())
        await state.set_state(ReleaseCFlow.series_time)
        await message.answer("Введите время первой встречи, например 930 или 1430 (МСК):", reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.series_time)
    async def series_time(message: Message, state: FSMContext) -> None:
        try:
            value = _clock(message.text or "")
        except ValueError:
            await message.answer("Введите время четырьмя цифрами, например 0930 или 1430.")
            return
        await state.update_data(start_time=value.isoformat(timespec="minutes"))
        durations = load_rules(db, settings).durations
        await message.answer(
            "Выберите длительность:",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text=f"{item} мин.", callback_data=f"c:series:duration:{item}")]
                    for item in durations
                ]
                + [[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]
            ),
        )

    @router.callback_query(F.data.startswith("c:series:duration:"))
    async def series_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        duration = int(callback.data.rsplit(":", 1)[1])
        if duration not in load_rules(db, settings).durations:
            await callback.answer("Длительность больше недоступна", show_alert=True)
            return
        await state.update_data(duration=duration)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Как часто повторять встречу?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="Ежедневно", callback_data=f"c:series:frequency:{SERIES_DAILY}")],
                        [InlineKeyboardButton(text="Еженедельно", callback_data=f"c:series:frequency:{SERIES_WEEKLY}")],
                        [InlineKeyboardButton(text="Ежемесячно", callback_data=f"c:series:frequency:{SERIES_MONTHLY}")],
                        [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:series:frequency:"))
    async def series_frequency(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        frequency = callback.data.rsplit(":", 1)[1]
        if frequency not in FREQUENCY_LABELS:
            await callback.answer("Недоступная периодичность", show_alert=True)
            return
        await state.update_data(frequency=frequency)
        await state.set_state(ReleaseCFlow.series_until)
        await callback.answer()
        if callback.message:
            suffix = " Если такого числа в месяце нет, этот месяц будет пропущен." if frequency == SERIES_MONTHLY else ""
            await callback.message.answer(
                "Выберите последнюю дату серии включительно. Максимум — один год."
                + suffix,
                reply_markup=calendar_keyboard(
                    "cdate:until",
                    date.fromisoformat(str((await state.get_data())["start_date"])),
                    date.fromisoformat(str((await state.get_data())["start_date"])),
                    date.fromisoformat(str((await state.get_data())["start_date"])) + timedelta(days=366),
                ),
            )

    @router.message(ReleaseCFlow.series_until)
    async def series_until(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        start_date = date.fromisoformat(str(data["start_date"]))
        try:
            value = _date(message.text or "")
            if value < start_date or value > start_date + timedelta(days=366):
                raise ValueError
        except ValueError:
            await message.answer("Конечная дата должна быть не раньше первой встречи и не дальше одного года.")
            return
        await state.update_data(until_date=value.isoformat())
        await state.set_state(None)
        await ask_series_participant(message)

    async def ask_description(message: Message, state: FSMContext) -> None:
        await state.set_state(ReleaseCFlow.series_description)
        await message.answer("Введите описание или отправьте дефис «-», чтобы пропустить:", reply_markup=cancel_keyboard())

    @router.callback_query(F.data == "c:series:email:self")
    async def series_email_self(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(email=None)
        await callback.answer()
        if callback.message:
            await ask_description(callback.message, state)

    @router.callback_query(F.data == "c:series:email:guest")
    async def series_email_guest(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.set_state(ReleaseCFlow.series_email)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите email гостя:", reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.series_email)
    async def series_email(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip().lower()
        if len(value) > 254 or not EMAIL_RE.fullmatch(value):
            await message.answer("Проверьте формат email и попробуйте ещё раз.")
            return
        await state.update_data(email=value)
        await ask_description(message, state)

    @router.message(ReleaseCFlow.series_description)
    async def series_description(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 2000:
            await message.answer("Описание слишком длинное: максимум 2000 символов.")
            return
        await state.update_data(description=None if value == "-" else value)
        await state.set_state(ReleaseCFlow.series_location)
        await message.answer("Введите место/ссылку или отправьте дефис «-», чтобы пропустить:", reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.series_location)
    async def series_location(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 500:
            await message.answer("Место/ссылка слишком длинные: максимум 500 символов.")
            return
        await state.update_data(location=None if value == "-" else value)
        await state.set_state(None)
        await message.answer(
            "Должны встречи этой серии занимать время в календаре?",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="🔒 Да, занимать время", callback_data="c:series:block:1")],
                    [InlineKeyboardButton(text="🟢 Нет, показывать свободной", callback_data="c:series:block:0")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("c:series:block:"))
    async def series_block(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        blocks = callback.data.rsplit(":", 1)[1] == "1"
        await state.update_data(blocks_calendar=blocks)
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    @router.callback_query(F.data == "c:series:edit")
    async def series_edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        data = await state.get_data()
        required = {"subject", "start_date", "start_time", "duration", "frequency", "until_date", "blocks_calendar"}
        if not required.issubset(data) or data.get("series_draft_id"):
            await callback.answer(
                "Черновик уже отправлялся или устарел. Начните создание серии заново.",
                show_alert=True,
            )
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Что изменить в черновике серии?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="📝 Тема", callback_data="c:series:edit:value:subject")],
                        [
                            InlineKeyboardButton(text="📅 Дата начала", callback_data="c:series:edit:date"),
                            InlineKeyboardButton(text="🕐 Время", callback_data="c:series:edit:value:time"),
                        ],
                        [
                            InlineKeyboardButton(text="⏱ Длительность", callback_data="c:series:edit:duration"),
                            InlineKeyboardButton(text="🔁 Повтор", callback_data="c:series:edit:frequency"),
                        ],
                        [InlineKeyboardButton(text="🏁 Последняя дата", callback_data="c:series:edit:until")],
                        [InlineKeyboardButton(text="✉️ Участник", callback_data="c:series:edit:email")],
                        [
                            InlineKeyboardButton(text="📄 Описание", callback_data="c:series:edit:value:description"),
                            InlineKeyboardButton(text="📍 Место", callback_data="c:series:edit:value:location"),
                        ],
                        [InlineKeyboardButton(text="🔒 Занято/свободно", callback_data="c:series:edit:block")],
                        [InlineKeyboardButton(text="← К проверке", callback_data="c:series:edit:review")],
                    ]
                ),
            )

    @router.callback_query(F.data == "c:series:edit:review")
    async def series_edit_review(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    @router.callback_query(F.data == "c:series:edit:date")
    async def series_edit_date(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        today = datetime.now(zone).date()
        await state.set_state(ReleaseCFlow.series_edit_date)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новую дату первой встречи:",
                reply_markup=calendar_keyboard("cdate:editstart", today, today, today + timedelta(days=366)),
            )

    @router.callback_query(F.data == "c:series:edit:until")
    async def series_edit_until(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        data = await state.get_data()
        start_day = date.fromisoformat(str(data["start_date"]))
        await state.set_state(ReleaseCFlow.series_edit_until)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новую последнюю дату серии:",
                reply_markup=calendar_keyboard(
                    "cdate:edituntil",
                    start_day,
                    start_day,
                    start_day + timedelta(days=366),
                ),
            )

    @router.callback_query(F.data.startswith("c:series:edit:value:"))
    async def series_edit_value_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        field = callback.data.rsplit(":", 1)[1]
        prompts = {
            "subject": "Введите новую тему:",
            "time": "Введите новое время, например 930 или 1430 (МСК):",
            "email": "Введите новый email участника:",
            "description": "Введите новое описание или дефис «-», чтобы очистить:",
            "location": "Введите новое место/ссылку или дефис «-», чтобы очистить:",
        }
        if field not in prompts:
            await callback.answer("Поле недоступно", show_alert=True)
            return
        await state.update_data(series_edit_field=field)
        await state.set_state(ReleaseCFlow.series_edit_value)
        await callback.answer()
        if callback.message:
            await callback.message.answer(prompts[field], reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.series_edit_value)
    async def series_edit_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        field = str(data.get("series_edit_field") or "")
        raw = (message.text or "").strip()
        try:
            if field == "subject":
                if not 1 <= len(raw) <= 200:
                    raise ValueError
                value: object = raw
            elif field == "time":
                value = _clock(raw).isoformat(timespec="minutes")
            elif field == "email":
                value = raw.lower()
                if len(str(value)) > 254 or not EMAIL_RE.fullmatch(str(value)):
                    raise ValueError
            elif field == "description":
                if len(raw) > 2000:
                    raise ValueError
                value = None if raw == "-" else raw
            elif field == "location":
                if len(raw) > 500:
                    raise ValueError
                value = None if raw == "-" else raw
            else:
                raise ValueError
        except ValueError:
            await message.answer("Значение не подходит. Проверьте формат и длину.", reply_markup=cancel_keyboard())
            return
        target = "start_time" if field == "time" else field
        await state.update_data(**{target: value})
        await state.set_state(None)
        await render_series_review(message, state)

    @router.callback_query(F.data == "c:series:edit:duration")
    async def series_edit_duration(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новую длительность:",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text=f"{item} мин.", callback_data=f"c:series:edit:setduration:{item}")]
                        for item in load_rules(db, settings).durations
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:series:edit:setduration:"))
    async def series_edit_set_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        duration = int(callback.data.rsplit(":", 1)[1])
        if duration not in load_rules(db, settings).durations:
            await callback.answer("Длительность недоступна", show_alert=True)
            return
        await state.update_data(duration=duration)
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    @router.callback_query(F.data == "c:series:edit:frequency")
    async def series_edit_frequency(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новый повтор:",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="Ежедневно", callback_data=f"c:series:edit:setfrequency:{SERIES_DAILY}")],
                        [InlineKeyboardButton(text="Еженедельно", callback_data=f"c:series:edit:setfrequency:{SERIES_WEEKLY}")],
                        [InlineKeyboardButton(text="Ежемесячно", callback_data=f"c:series:edit:setfrequency:{SERIES_MONTHLY}")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:series:edit:setfrequency:"))
    async def series_edit_set_frequency(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        frequency = callback.data.rsplit(":", 1)[1]
        if frequency not in FREQUENCY_LABELS:
            await callback.answer("Повтор недоступен", show_alert=True)
            return
        await state.update_data(frequency=frequency)
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    @router.callback_query(F.data == "c:series:edit:email")
    async def series_edit_email(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Изменить участника:",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="👤 Только я", callback_data="c:series:edit:setemail:self")],
                        [InlineKeyboardButton(text="✉️ Ввести email", callback_data="c:series:edit:value:email")],
                    ]
                ),
            )

    @router.callback_query(F.data == "c:series:edit:setemail:self")
    async def series_edit_email_self(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(email=None)
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    @router.callback_query(F.data == "c:series:edit:block")
    async def series_edit_block(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Должна серия занимать время?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🔒 Да", callback_data="c:series:edit:setblock:1")],
                        [InlineKeyboardButton(text="🟢 Нет", callback_data="c:series:edit:setblock:0")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:series:edit:setblock:"))
    async def series_edit_set_block(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(blocks_calendar=callback.data.endswith(":1"))
        await callback.answer()
        if callback.message:
            await render_series_review(callback.message, state)

    async def create_series_from_state(callback: CallbackQuery, state: FSMContext, allow_overlap: bool) -> None:
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await callback.message.answer("⏳ Проверяю календарь и создаю серию. Это может занять до минуты…")
        data = await state.get_data()
        try:
            start = datetime.combine(date.fromisoformat(str(data["start_date"])), _clock(str(data["start_time"])), zone)
            end = start + timedelta(minutes=int(data["duration"]))
            until = date.fromisoformat(str(data["until_date"]))
            occurrences = generate_occurrences(start, end, str(data["frequency"]), until)
        except (KeyError, TypeError, ValueError):
            if callback.message:
                await callback.message.answer("Черновик устарел. Начните создание заново.", reply_markup=home_keyboard())
            await state.clear()
            return
        if start <= datetime.now(zone):
            if callback.message:
                await callback.message.answer("Первая встреча уже в прошлом. Начните создание заново.", reply_markup=home_keyboard())
            return
        try:
            conflict = bool(data.get("blocks_calendar")) and await has_conflict(occurrences)
        except CalendarUnavailable:
            LOGGER.exception("series_conflict_check_failed")
            if callback.message:
                await callback.message.answer(
                    "Google Календарь временно не ответил. Серия не создана — можно повторить этой же кнопкой.",
                    reply_markup=home_keyboard(),
                )
            return
        if conflict and not allow_overlap:
            if callback.message:
                await callback.message.answer(
                    "В серии есть время, уже занятое в календаре. Создать её с пересечениями?",
                    reply_markup=_keyboard(
                        [
                            [InlineKeyboardButton(text="⚠️ Да, создать с пересечениями", callback_data="c:series:create:overlap")],
                            [InlineKeyboardButton(text="✖ Не создавать", callback_data="abort")],
                        ]
                    ),
                )
            return
        user = callback.from_user
        draft: EventSeries | None = None
        google_id: str | None = None
        series: EventSeries | None = None
        try:
            draft_id = int(data.get("series_draft_id", 0))
            draft = automation.get_series(draft_id) if draft_id else None
            if draft and draft.created_by == user.id and draft.status == SERIES_ACTIVE:
                series = draft
            elif draft and draft.created_by == user.id and draft.status == SERIES_FAILED:
                draft = automation.retry_series_draft(draft.id, user.id)
            elif draft is None or draft.created_by != user.id or draft.status != SERIES_CREATING:
                draft = automation.create_series_draft(
                    admin_id=user.id,
                    admin_name=user.full_name,
                    admin_username=user.username,
                    email=data.get("email"),
                    subject=str(data["subject"]),
                    description=data.get("description"),
                    location=data.get("location"),
                    start_at=start,
                    end_at=end,
                    frequency=str(data["frequency"]),
                    until_date=until.isoformat(),
                    blocks_calendar=bool(data.get("blocks_calendar")),
                    allow_overlap=allow_overlap,
                    occurrences=occurrences,
                )
                await state.update_data(series_draft_id=draft.id)
            if series is None:
                for attempt in range(2):
                    try:
                        google_id = await calendar.create_series(draft)
                        break
                    except CalendarUnavailable:
                        if attempt:
                            raise
                        LOGGER.warning("series_create_retry", extra={"series_id": draft.id})
                        await asyncio.sleep(2)
                if not google_id:
                    raise CalendarUnavailable("series create returned no ID")
                series = automation.activate_series(draft.id, google_id, user.id)
        except SlotConflictError:
            if callback.message:
                await callback.message.answer("Пока вы заполняли форму, время заняли. Проверьте серию ещё раз.")
            return
        except Exception as exc:
            LOGGER.exception("series_create_failed")
            if draft:
                automation.fail_series(draft.id, type(exc).__name__)
            if google_id and series is None:
                try:
                    await calendar.delete_series(google_id)
                except Exception:
                    LOGGER.exception("series_create_rollback_failed")
            if callback.message:
                await callback.message.answer(
                    "Google Календарь не подтвердил операцию. Нажмите «Создать серию» ещё раз: повтор не создаст дубликат.",
                    reply_markup=home_keyboard(),
                )
            return
        try:
            await schedule_series_reminders(series.id)
        except Exception:
            LOGGER.exception("series_reminders_schedule_failed", extra={"series_id": series.id})
        await state.clear()
        if callback.message:
            await callback.message.answer(
                "✅ <b>Серия успешно создана</b>\n\n"
                + _series_text(series, zone)
                + f"\nСоздано встреч: {len(occurrences)}.",
                reply_markup=home_keyboard(),
            )

    @router.callback_query(F.data == "c:series:create")
    async def series_create(callback: CallbackQuery, state: FSMContext) -> None:
        if await require_admin(callback):
            await create_series_from_state(callback, state, False)

    @router.callback_query(F.data == "c:series:create:overlap")
    async def series_create_overlap(callback: CallbackQuery, state: FSMContext) -> None:
        if await require_admin(callback):
            await create_series_from_state(callback, state, True)

    @router.callback_query(F.data == "c:series:list")
    async def series_list(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        items = automation.list_series(callback.from_user.id, limit=20)
        await callback.answer()
        if not callback.message:
            return
        if not items:
            await callback.message.answer("Активных повторяющихся встреч пока нет.", reply_markup=home_keyboard())
            return
        await callback.message.answer("Активные серии (последняя — внизу):")
        for series in items:
            await callback.message.answer(
                _series_text(series, zone),
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="Открыть", callback_data=f"c:series:view:{series.id}")]]
                ),
            )
        await callback.message.answer("Выберите нужную серию выше.", reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("c:series:view:"))
    async def series_view(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        series_id = int(callback.data.rsplit(":", 1)[1])
        series = automation.get_series(series_id)
        if series is None or series.created_by != callback.from_user.id:
            await callback.answer("Серия не найдена", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                _series_text(series, zone),
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="📅 Встречи серии", callback_data=f"c:occ:list:{series.id}")],
                        [InlineKeyboardButton(text="🕐 Изменить время всей серии", callback_data=f"c:series:time:{series.id}")],
                        [InlineKeyboardButton(text="🗑 Отменить всю серию", callback_data=f"c:series:cancel:{series.id}")],
                        [InlineKeyboardButton(text="← К списку", callback_data="c:series:list")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:occ:list:"))
    async def occurrence_list(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        series_id = int(callback.data.rsplit(":", 1)[1])
        series = automation.get_series(series_id)
        if series is None or series.created_by != callback.from_user.id:
            await callback.answer("Серия не найдена", show_alert=True)
            return
        items = automation.list_occurrences(series_id, future_only=True, limit=30)
        await callback.answer()
        if not callback.message:
            return
        if not items:
            await callback.message.answer("Будущих встреч в этой серии нет.", reply_markup=home_keyboard())
            return
        await callback.message.answer("Ближайшие встречи серии:")
        for occurrence in items:
            start = occurrence.actual_start_at.astimezone(zone)
            await callback.message.answer(
                f"{start:%d.%m.%Y %H:%M} — {OCCURRENCE_STATUS_LABELS.get(occurrence.status, occurrence.status)}",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="Открыть", callback_data=f"c:occ:view:{occurrence.id}")]]
                ),
            )
        await callback.message.answer(
            "Показаны ближайшие встречи.",
            reply_markup=_keyboard([[InlineKeyboardButton(text="← К серии", callback_data=f"c:series:view:{series.id}")]]),
        )

    @router.callback_query(F.data.startswith("c:occ:view:"))
    async def occurrence_view(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        occurrence_id = int(callback.data.rsplit(":", 1)[1])
        occurrence = automation.get_occurrence(occurrence_id)
        series = automation.get_series(occurrence.series_id) if occurrence else None
        if occurrence is None or series is None or series.created_by != callback.from_user.id:
            await callback.answer("Встреча не найдена", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                _occurrence_text(occurrence, series, zone),
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🕐 Перенести эту встречу", callback_data=f"c:occ:move:{occurrence.id}")],
                        [InlineKeyboardButton(text="🗑 Отменить эту встречу", callback_data=f"c:occ:cancel:{occurrence.id}")],
                        [InlineKeyboardButton(text="← К серии", callback_data=f"c:series:view:{series.id}")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:occ:move:") & ~F.data.contains(":apply:"))
    async def occurrence_move(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        occurrence_id = int(callback.data.rsplit(":", 1)[1])
        occurrence = automation.get_occurrence(occurrence_id)
        if occurrence is None:
            await callback.answer("Встреча не найдена", show_alert=True)
            return
        await state.set_state(ReleaseCFlow.occurrence_date)
        await state.set_data({"occurrence_id": occurrence_id})
        await callback.answer()
        if callback.message:
            today = datetime.now(zone).date()
            await callback.message.answer(
                "Выберите новую дату:",
                reply_markup=calendar_keyboard("cdate:occurrence", today, today, today + timedelta(days=366)),
            )

    @router.message(ReleaseCFlow.occurrence_date)
    async def occurrence_date(message: Message, state: FSMContext) -> None:
        try:
            value = _date(message.text or "")
            if value < datetime.now(zone).date() or value > datetime.now(zone).date() + timedelta(days=366):
                raise ValueError
        except ValueError:
            await message.answer("Введите будущую дату не дальше одного года.")
            return
        await state.update_data(move_date=value.isoformat())
        await state.set_state(ReleaseCFlow.occurrence_time)
        await message.answer("Введите новое время, например 930 или 1430 (МСК):", reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.occurrence_time)
    async def occurrence_time(message: Message, state: FSMContext) -> None:
        try:
            value = _clock(message.text or "")
        except ValueError:
            await message.answer("Введите время четырьмя цифрами, например 0930 или 1430.")
            return
        data = await state.get_data()
        occurrence = automation.get_occurrence(int(data["occurrence_id"]))
        if occurrence is None:
            await state.clear()
            await message.answer("Встреча больше не найдена.", reply_markup=home_keyboard())
            return
        start = datetime.combine(date.fromisoformat(str(data["move_date"])), value, zone)
        end = start + (occurrence.actual_end_at - occurrence.actual_start_at)
        if start <= datetime.now(zone):
            await message.answer("Новое время должно быть в будущем.")
            return
        await state.update_data(move_start=start.isoformat(), move_end=end.isoformat())
        try:
            conflict = await has_conflict([(start, end)], exclude_occurrence_id=occurrence.id)
        except CalendarUnavailable:
            LOGGER.exception("occurrence_conflict_check_failed")
            await message.answer("Google Календарь временно недоступен. Перенос не выполнен.", reply_markup=home_keyboard())
            return
        rows = []
        if conflict:
            rows.append([InlineKeyboardButton(text="⚠️ Перенести с пересечением", callback_data="c:occ:move:apply:1")])
        else:
            rows.append([InlineKeyboardButton(text="✅ Подтвердить перенос", callback_data="c:occ:move:apply:0")])
        rows.append([InlineKeyboardButton(text="✖ Не переносить", callback_data="abort")])
        await state.set_state(None)
        await message.answer(
            f"Новое время: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)."
            + (" Найдено пересечение." if conflict else ""),
            reply_markup=_keyboard(rows),
        )

    @router.callback_query(F.data.startswith("c:occ:move:apply:"))
    async def occurrence_move_apply(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        data = await state.get_data()
        occurrence = automation.get_occurrence(int(data.get("occurrence_id", 0)))
        series = automation.get_series(occurrence.series_id) if occurrence else None
        if occurrence is None or series is None or not series.google_series_id:
            await callback.answer("Встреча больше недоступна", show_alert=True)
            await state.clear()
            return
        await callback.answer("Переношу встречу…")
        start = datetime.fromisoformat(str(data["move_start"]))
        end = datetime.fromisoformat(str(data["move_end"]))
        try:
            try:
                google_id = await calendar.update_occurrence(series.google_series_id, occurrence, start, end)
            except CalendarUnavailable:
                LOGGER.warning("occurrence_move_verifying_after_timeout", extra={"occurrence_id": occurrence.id})
                remote_state = await calendar.occurrence_state(series.google_series_id, occurrence)
                if not remote_state.exists or remote_state.start_at != start.astimezone(UTC) or remote_state.end_at != end.astimezone(UTC):
                    raise
                google_id = remote_state.event_id
            moved = automation.move_occurrence(occurrence.id, callback.from_user.id, start, end)
            rules = load_notification_rules(db, settings)
            if rules.automation_enabled:
                automation.rebuild_occurrence_reminders(moved.id, settings.admin_telegram_id, rules.reminder_minutes)
            LOGGER.info("occurrence_moved", extra={"occurrence_id": moved.id, "google_event_id": google_id})
        except Exception:
            LOGGER.exception("occurrence_move_failed")
            if callback.message:
                await callback.message.answer("Не удалось перенести встречу. Попробуйте ещё раз позже.", reply_markup=home_keyboard())
            return
        await state.clear()
        if callback.message:
            await callback.message.answer("✅ Встреча перенесена\n\n" + _occurrence_text(moved, series, zone), reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("c:occ:cancel:") & ~F.data.contains(":confirm:"))
    async def occurrence_cancel(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        occurrence_id = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отменить только эту встречу? Остальные встречи серии останутся.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🗑 Да, отменить встречу", callback_data=f"c:occ:cancel:confirm:{occurrence_id}")],
                        [InlineKeyboardButton(text="← Не отменять", callback_data=f"c:occ:view:{occurrence_id}")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:occ:cancel:confirm:"))
    async def occurrence_cancel_confirm(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        occurrence_id = int(callback.data.rsplit(":", 1)[1])
        occurrence = automation.get_occurrence(occurrence_id)
        series = automation.get_series(occurrence.series_id) if occurrence else None
        if occurrence is None or series is None or not series.google_series_id:
            await callback.answer("Встреча больше недоступна", show_alert=True)
            return
        await callback.answer("Отменяю встречу…")
        try:
            try:
                await calendar.delete_occurrence(series.google_series_id, occurrence)
            except CalendarUnavailable:
                LOGGER.warning("occurrence_cancel_verifying_after_timeout", extra={"occurrence_id": occurrence.id})
                remote_state = await calendar.occurrence_state(series.google_series_id, occurrence)
                if remote_state.exists:
                    raise
            automation.cancel_occurrence(occurrence.id, callback.from_user.id)
        except Exception:
            LOGGER.exception("occurrence_cancel_failed")
            if callback.message:
                await callback.message.answer("Не удалось отменить встречу. Попробуйте ещё раз позже.", reply_markup=home_keyboard())
            return
        if callback.message:
            await callback.message.answer("✅ Эта встреча отменена. Остальная серия сохранена.", reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("c:series:cancel:") & ~F.data.contains(":confirm:"))
    async def series_cancel(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        series_id = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отменить всю серию и все её будущие встречи?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🗑 Да, отменить всю серию", callback_data=f"c:series:cancel:confirm:{series_id}")],
                        [InlineKeyboardButton(text="← Не отменять", callback_data=f"c:series:view:{series_id}")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("c:series:cancel:confirm:"))
    async def series_cancel_confirm(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        series_id = int(callback.data.rsplit(":", 1)[1])
        series = automation.get_series(series_id)
        if series is None or not series.google_series_id:
            await callback.answer("Серия больше недоступна", show_alert=True)
            return
        await callback.answer("Отменяю серию…")
        try:
            try:
                await calendar.delete_series(series.google_series_id)
            except CalendarUnavailable:
                LOGGER.warning("series_cancel_retry", extra={"series_id": series.id})
                await asyncio.sleep(1)
                await calendar.delete_series(series.google_series_id)
            automation.cancel_series(series.id, callback.from_user.id)
        except Exception:
            LOGGER.exception("series_cancel_failed")
            if callback.message:
                await callback.message.answer("Не удалось отменить серию. Попробуйте ещё раз позже.", reply_markup=home_keyboard())
            return
        if callback.message:
            await callback.message.answer("✅ Серия и её будущие встречи отменены.", reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("c:series:time:") & (F.data != "c:series:time:apply"))
    async def series_time_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        series_id = int(callback.data.rsplit(":", 1)[1])
        series = automation.get_series(series_id)
        if series is None:
            await callback.answer("Серия не найдена", show_alert=True)
            return
        occurrences = automation.list_occurrences(series_id, limit=400)
        if any(item.status != "SCHEDULED" for item in occurrences):
            await callback.answer(
                "В серии уже есть перенесённые или отменённые встречи. Меняйте такие встречи отдельно.",
                show_alert=True,
            )
            return
        await state.set_state(ReleaseCFlow.series_new_time)
        await state.set_data({"series_id": series_id})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Введите новое время начала всей серии, например 930 или 1430 (МСК). Даты и длительность сохранятся:",
                reply_markup=cancel_keyboard(),
            )

    @router.message(ReleaseCFlow.series_new_time)
    async def series_time_value(message: Message, state: FSMContext) -> None:
        try:
            value = _clock(message.text or "")
        except ValueError:
            await message.answer("Введите время четырьмя цифрами, например 0930 или 1430.")
            return
        data = await state.get_data()
        series = automation.get_series(int(data["series_id"]))
        if series is None:
            await state.clear()
            await message.answer("Серия не найдена.", reply_markup=home_keyboard())
            return
        local_start = series.start_at.astimezone(zone)
        start = datetime.combine(local_start.date(), value, zone)
        end = start + (series.end_at - series.start_at)
        occurrences = generate_occurrences(start, end, series.frequency, date.fromisoformat(series.until_date))
        await state.update_data(series_start=start.isoformat(), series_end=end.isoformat())
        try:
            conflict = series.blocks_calendar and await has_conflict(occurrences, exclude_series_id=series.id)
        except CalendarUnavailable:
            LOGGER.exception("series_retime_conflict_check_failed")
            await message.answer("Google Календарь временно недоступен. Изменение не выполнено.", reply_markup=home_keyboard())
            return
        await state.set_state(None)
        await message.answer(
            f"Новое время всех встреч: {start:%H:%M}–{end:%H:%M} (МСК)."
            + (" Найдены пересечения." if conflict else ""),
            reply_markup=_keyboard(
                [
                    [
                        InlineKeyboardButton(
                            text="⚠️ Изменить с пересечениями" if conflict else "✅ Подтвердить изменение",
                            callback_data="c:series:time:apply",
                        )
                    ],
                    [InlineKeyboardButton(text="✖ Не изменять", callback_data="abort")],
                ]
            ),
        )

    @router.callback_query(F.data == "c:series:time:apply")
    async def series_time_apply(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        data = await state.get_data()
        series = automation.get_series(int(data.get("series_id", 0)))
        if series is None or not series.google_series_id:
            await callback.answer("Серия больше недоступна", show_alert=True)
            await state.clear()
            return
        await callback.answer("Изменяю серию…")
        start = datetime.fromisoformat(str(data["series_start"]))
        end = datetime.fromisoformat(str(data["series_end"]))
        occurrences = generate_occurrences(start, end, series.frequency, date.fromisoformat(series.until_date))
        updated_for_google = replace(series, start_at=start, end_at=end)
        try:
            for attempt in range(2):
                try:
                    await calendar.update_series(updated_for_google)
                    break
                except CalendarUnavailable:
                    if attempt:
                        raise
                    LOGGER.warning("series_retime_retry", extra={"series_id": series.id})
                    await asyncio.sleep(1)
            updated = automation.retime_series(series.id, callback.from_user.id, start, end, occurrences)
            await schedule_series_reminders(updated.id)
        except Exception:
            LOGGER.exception("series_retime_failed")
            if callback.message:
                await callback.message.answer(
                    "Google Календарь не подтвердил изменение. Повторите операцию позже; повтор не создаёт новую серию.",
                    reply_markup=home_keyboard(),
                )
            return
        await state.clear()
        if callback.message:
            await callback.message.answer("✅ Время всей серии изменено\n\n" + _series_text(updated, zone), reply_markup=home_keyboard())

    @router.callback_query(F.data == "c:notify")
    async def notification_settings(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await render_notification_settings(callback.message)

    @router.callback_query(F.data.in_({"c:notify:minutes", "c:notify:pending"}))
    async def notification_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        kind = "minutes" if callback.data.endswith("minutes") else "pending"
        await state.set_state(ReleaseCFlow.notification_value)
        await state.set_data({"notification_kind": kind})
        await callback.answer()
        if callback.message:
            text = (
                "Введите от 1 до 5 интервалов в минутах через запятую. Например: 1440, 60, 5"
                if kind == "minutes"
                else "Через сколько часов напоминать о заявке без решения? Введите число от 1 до 168."
            )
            await callback.message.answer(text, reply_markup=cancel_keyboard())

    @router.message(ReleaseCFlow.notification_value)
    async def notification_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        try:
            if data.get("notification_kind") == "minutes":
                raw = (message.text or "").replace(";", ",")
                value = validate_value(REMINDER_MINUTES, [int(item.strip()) for item in raw.split(",") if item.strip()])
                key = REMINDER_MINUTES
            else:
                value = validate_value(PENDING_REMINDER_HOURS, int((message.text or "").strip()))
                key = PENDING_REMINDER_HOURS
        except (TypeError, ValueError):
            await message.answer("Значение не подходит. Проверьте формат и допустимый диапазон.")
            return
        db.set_setting(key, value, message.from_user.id)
        await state.clear()
        rules = load_notification_rules(db, settings)
        if rules.automation_enabled:
            automation.bootstrap_jobs(
                settings.admin_telegram_id,
                rules.reminder_minutes,
                rules.pending_reminder_hours,
            )
        await message.answer("Настройка сохранена.")
        await render_notification_settings(message)

    @router.callback_query(F.data == "c:notify:toggle")
    async def notification_toggle(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        current = load_notification_rules(db, settings)
        value = validate_value(AUTOMATION_ENABLED, not current.automation_enabled)
        db.set_setting(AUTOMATION_ENABLED, value, callback.from_user.id)
        if value:
            rules = load_notification_rules(db, settings)
            automation.bootstrap_jobs(
                settings.admin_telegram_id,
                rules.reminder_minutes,
                rules.pending_reminder_hours,
            )
        await callback.answer("Настройка сохранена")
        if callback.message:
            await render_notification_settings(callback.message)

    return router
