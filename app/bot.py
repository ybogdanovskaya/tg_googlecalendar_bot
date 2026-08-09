from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.booking_rules import (
    ALLOWED_DURATIONS,
    ALLOWED_STEPS,
    BOOKING_ENABLED,
    BOOKING_HORIZON_DAYS,
    DURATIONS,
    HOLD_HOURS,
    MIN_LEAD_MINUTES,
    STEP_MINUTES,
    USER_BOOKING_WINDOW,
    BookingRules,
    format_clock_minutes,
    is_closed_date,
    load_rules,
    parse_booking_window,
    validate_value,
)
from app.automation_store import AutomationStore
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.date_picker import calendar_keyboard, month_from_callback
from app.db import Database, RequestNotEditableError, SlotConflictError
from app.models import APPROVED, APPROVING, CANCELLED, CANCELLED_BY_ADMIN, PENDING, REJECTED, MeetingRequest
from app.notification_rules import load_notification_rules
from app.slots import SlotPeriod, available_slots, booking_periods, slot_in_period


LOGGER = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DATE_PAGE_SIZE = 7
SLOT_PAGE_SIZE = 24

SETTING_LABELS = {
    BOOKING_ENABLED: "Новая запись",
    MIN_LEAD_MINUTES: "Минимальный срок",
    BOOKING_HORIZON_DAYS: "Горизонт записи",
    HOLD_HOURS: "Резерв слота",
    DURATIONS: "Длительности",
    STEP_MINUTES: "Шаг слотов",
    USER_BOOKING_WINDOW: "Время записи пользователей",
}

EDIT_FIELD_LABELS = {
    "telegram_name": "Имя участника",
    "email": "Email",
    "subject": "Тема",
    "description": "Описание",
    "location": "Место/ссылка",
}


class Booking(StatesGroup):
    choosing_date = State()
    choosing_slot = State()
    email = State()
    subject = State()
    description = State()
    location = State()
    confirm = State()


class Editing(StatesGroup):
    value = State()


class AdminSettings(StatesGroup):
    value = State()
    booking_window = State()
    closed_date = State()


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_keyboard() -> InlineKeyboardMarkup:
    return _keyboard([[InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return _keyboard([[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]])


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my")],
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
            InlineKeyboardButton(text="🔐 Конфиденциальность", callback_data="privacy"),
        ],
    ]
    if is_admin:
        rows.extend(
            [
                [InlineKeyboardButton(text="🛠 Заявки на рассмотрении", callback_data="admin:list")],
                [
                    InlineKeyboardButton(text="🔄 Переносы и отмены", callback_data="b:changes"),
                    InlineKeyboardButton(text="➕ Создать встречу", callback_data="b:manual"),
                ],
                [InlineKeyboardButton(text="🔁 Повторяющиеся встречи", callback_data="c:series")],
                [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
            ]
        )
    return _keyboard(rows)


def privacy_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🗑 Управление моими данными", callback_data="d:data")]]
    rows.extend(main_menu(is_admin).inline_keyboard)
    return _keyboard(rows)


def format_request(
    request: MeetingRequest,
    timezone_name: str,
    include_private: bool = True,
    now: datetime | None = None,
) -> str:
    zone = ZoneInfo(timezone_name)
    start = request.start_at.astimezone(zone)
    end = request.end_at.astimezone(zone)
    status_labels = {
        PENDING: "ожидает решения",
        APPROVING: "создаётся в календаре",
        APPROVED: "согласована",
        REJECTED: "отклонена",
        CANCELLED: "отменена",
        CANCELLED_BY_ADMIN: "отменена администратором",
    }
    current = (now or datetime.now(UTC)).astimezone(UTC)
    status = status_labels.get(request.status, request.status)
    if request.status == PENDING and request.hold_until <= current:
        status += "; резерв слота завершён"
    lines = [
        f"<b>Заявка №{request.id}</b>",
        f"Статус: {status}",
        f"Время: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)",
        f"Тема: {html.escape(request.subject)}",
    ]
    if include_private:
        lines.extend(
            [
                f"Участник: {html.escape(request.telegram_name)}",
            ]
        )
        if request.email:
            lines.append(f"Email: {html.escape(request.email)}")
        if request.description:
            lines.append(f"Описание: {html.escape(request.description)}")
        if request.location:
            lines.append(f"Место/ссылка: {html.escape(request.location)}")
    return "\n".join(lines)


def privacy_text(settings: Settings) -> str:
    return (
        "<b>Политика конфиденциальности</b>\n\n"
        "Бот обрабатывает имя и Telegram ID, username при наличии, email, тему, время, "
        "описание и место встречи. Данные нужны только для согласования встречи, уведомлений "
        "и создания события в Google Calendar владельца.\n\n"
        "Пользователю показываются только свободные интервалы — содержимое календаря, названия "
        "и участники других событий не раскрываются. Данные размещены на сервере в России. "
        "Рабочие токены и пароли не хранятся в базе заявок и не выводятся в логах.\n\n"
        "Персональные данные и история хранятся не более 12 месяцев, после чего автоматически "
        "обезличиваются. Запросить удаление раньше можно по кнопке «Управление моими данными».\n\n"
        f"Версия политики: {html.escape(settings.privacy_policy_version)}."
    )


def help_text() -> str:
    return (
        "<b>Как записаться</b>\n\n"
        "1. Нажмите «Записаться».\n"
        "2. Выберите длительность, дату и свободное время.\n"
        "3. Укажите email и тему; описание и место можно пропустить.\n"
        "4. Проверьте данные и отправьте заявку.\n\n"
        "Пока заявка ожидает решения, её можно изменить или отменить в разделе «Мои заявки». "
        "Если резерв слота завершился, выберите время заново. После решения владельца бот пришлёт уведомление."
    )


def _date_from_callback(raw: str) -> date:
    return datetime.strptime(raw, "%Y%m%d").date()


def _request_edit_keyboard(request_id: int, include_home: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{request_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel:{request_id}"),
        ]
    ]
    if include_home:
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return _keyboard(rows)


def create_router(
    settings: Settings,
    db: Database,
    calendar: CalendarClient,
    automation: AutomationStore | None = None,
) -> Router:
    router = Router()
    zone = ZoneInfo(settings.timezone)

    def is_admin(telegram_id: int) -> bool:
        return telegram_id == settings.admin_telegram_id

    def rules() -> BookingRules:
        return load_rules(db, settings)

    async def show_home(message: Message, telegram_id: int) -> None:
        await message.answer("Выберите действие:", reply_markup=main_menu(is_admin(telegram_id)))

    async def ensure_consent(callback: CallbackQuery) -> bool:
        if db.has_consent(callback.from_user.id, settings.privacy_policy_version):
            return True
        await callback.answer("Сначала примите условия", show_alert=True)
        return False

    async def require_admin(callback: CallbackQuery) -> bool:
        if is_admin(callback.from_user.id):
            return True
        await callback.answer("Нет доступа", show_alert=True)
        return False

    async def block_during_active_form(callback: CallbackQuery, state: FSMContext) -> bool:
        if await state.get_state() is None:
            return False
        await callback.answer(
            "Сначала завершите текущую операцию или нажмите «Отменить».",
            show_alert=True,
        )
        return True

    def request_owned_pending(request_id: int, telegram_id: int) -> MeetingRequest | None:
        request = db.get_request(request_id)
        if request is None or request.telegram_id != telegram_id or request.status != PENDING:
            return None
        return request

    async def render_date_page(target: Message, state: FSMContext, page: int) -> None:
        current_rules = rules()
        today = datetime.now(zone).date()
        max_page = (current_rules.booking_horizon_days - 1) // DATE_PAGE_SIZE
        page = max(0, min(page, max_page))
        start_offset = page * DATE_PAGE_SIZE
        closed = set(
            await asyncio.to_thread(
                db.list_closed_dates,
                today.isoformat(),
                current_rules.booking_horizon_days,
            )
        )
        rows: list[list[InlineKeyboardButton]] = []
        for offset in range(
            start_offset,
            min(start_offset + DATE_PAGE_SIZE, current_rules.booking_horizon_days),
        ):
            value = today + timedelta(days=offset)
            if is_closed_date(value, closed, current_rules):
                continue
            weekday = (
                value.strftime("%A")
                .replace("Monday", "пн")
                .replace("Tuesday", "вт")
                .replace("Wednesday", "ср")
                .replace("Thursday", "чт")
                .replace("Friday", "пт")
                .replace("Saturday", "сб")
                .replace("Sunday", "вс")
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{value:%d.%m}, {weekday}",
                        callback_data=f"date:{value:%Y%m%d}",
                    )
                ]
            )
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="← Раньше", callback_data=f"dpage:{page - 1}"))
        if page < max_page:
            navigation.append(InlineKeyboardButton(text="Дальше →", callback_data=f"dpage:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="abort")])
        await state.update_data(date_page=page)
        text = "Выберите дату:" if len(rows) > 1 else "На этой странице все даты закрыты."
        await target.edit_text(text, reply_markup=_keyboard(rows))

    async def load_slots(
        selected_date: date,
        duration: int,
        exclude_request_id: int | None = None,
        unrestricted_time: bool = False,
    ) -> list[datetime]:
        current_rules = rules()
        if is_closed_date(
            selected_date,
            set(await asyncio.to_thread(db.list_closed_dates, selected_date.isoformat(), 1)),
            current_rules,
        ):
            return []
        day_start = datetime.combine(selected_date, time.min, zone)
        day_end = day_start + timedelta(days=1)
        google_busy, local_busy = await asyncio.gather(
            calendar.busy(day_start, day_end),
            asyncio.to_thread(db.active_intervals, day_start, day_end, exclude_request_id),
        )
        return available_slots(
            local_date=selected_date,
            duration_minutes=duration,
            busy_intervals=google_busy + local_busy,
            now=datetime.now(UTC),
            timezone_name=settings.timezone,
            min_lead_minutes=current_rules.min_lead_minutes,
            step_minutes=current_rules.step_minutes,
            window_start_minutes=(0 if unrestricted_time else current_rules.user_booking_start_minutes),
            window_end_minutes=(24 * 60 if unrestricted_time else current_rules.user_booking_end_minutes),
        )

    def period_label(period: SlotPeriod) -> str:
        return (
            f"{period.title} "
            f"{format_clock_minutes(period.start_minutes)}–{format_clock_minutes(period.end_minutes)}"
        )

    async def render_period_selection(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        selected_date = _date_from_callback(data["selected_date"])
        duration = int(data["duration"])
        exclude_request_id = int(data["edit_request_id"]) if data.get("flow") == "edit" else None
        try:
            slots = await load_slots(selected_date, duration, exclude_request_id)
        except CalendarUnavailable:
            await target.edit_text(
                "Google Calendar сейчас недоступен. Попробуйте позже.",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")]]
                ),
            )
            return
        if not slots:
            await target.edit_text(
                "На эту дату нет свободных слотов в доступное время.",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")]]
                ),
            )
            return
        current = rules()
        periods = booking_periods(
            current.user_booking_start_minutes,
            current.user_booking_end_minutes,
        )
        rows: list[list[InlineKeyboardButton]] = []
        lines = [
            f"Выберите часть дня на {selected_date:%d.%m.%Y}:",
            f"Длительность встречи: {duration} мин.",
            "",
        ]
        for period in periods:
            count = sum(slot_in_period(slot, period) for slot in slots)
            suffix = "слот" if count == 1 else "слота" if 2 <= count <= 4 else "слотов"
            count_text = f"{count} {suffix}"
            lines.append(f"{period_label(period)} — {count_text if count else 'нет свободного времени'}")
            if count:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{period.title} — {count_text}",
                            callback_data=f"period:{period.key}",
                        )
                    ]
                )
        rows.append([InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")])
        await state.update_data(slot_period=None)
        await target.edit_text("\n".join(lines), reply_markup=_keyboard(rows))

    async def render_slot_page(target: Message, state: FSMContext, page: int) -> None:
        data = await state.get_data()
        selected_date = _date_from_callback(data["selected_date"])
        duration = int(data["duration"])
        exclude_request_id = int(data["edit_request_id"]) if data.get("flow") == "edit" else None
        unrestricted_time = bool(data.get("unrestricted_time"))
        try:
            slots = await load_slots(selected_date, duration, exclude_request_id, unrestricted_time)
        except CalendarUnavailable:
            await target.edit_text(
                "Google Calendar сейчас недоступен. Попробуйте позже.",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")]]
                ),
            )
            return
        selected_period: SlotPeriod | None = None
        if not unrestricted_time:
            current = rules()
            selected_period = next(
                (
                    period
                    for period in booking_periods(
                        current.user_booking_start_minutes,
                        current.user_booking_end_minutes,
                    )
                    if period.key == data.get("slot_period")
                ),
                None,
            )
            if selected_period is None:
                await render_period_selection(target, state)
                return
            slots = [slot for slot in slots if slot_in_period(slot, selected_period)]
        if not slots:
            back_button = (
                InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")
                if unrestricted_time
                else InlineKeyboardButton(text="← К частям дня", callback_data="periods")
            )
            await target.edit_text(
                "На эту дату нет свободных слотов."
                if unrestricted_time
                else "В выбранной части дня свободных слотов больше нет.",
                reply_markup=_keyboard([[back_button]]),
            )
            return
        max_page = (len(slots) - 1) // SLOT_PAGE_SIZE
        page = max(0, min(page, max_page))
        chunk = slots[page * SLOT_PAGE_SIZE : (page + 1) * SLOT_PAGE_SIZE]
        rows: list[list[InlineKeyboardButton]] = []
        for index in range(0, len(chunk), 4):
            rows.append(
                [
                    InlineKeyboardButton(text=item.strftime("%H:%M"), callback_data=f"slot:{item:%H%M}")
                    for item in chunk[index : index + 4]
                ]
            )
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"spage:{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="noop"))
        if page < max_page:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"spage:{page + 1}"))
        rows.append(navigation)
        if unrestricted_time:
            rows.append([InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")])
        else:
            rows.append([InlineKeyboardButton(text="← К частям дня", callback_data="periods")])
        period_text = f", {period_label(selected_period).lower()}" if selected_period else ""
        await target.edit_text(
            f"Свободно {selected_date:%d.%m.%Y}, длительность {duration} мин{period_text}:\n"
            "Время указано по Москве.",
            reply_markup=_keyboard(rows),
        )

    async def show_my_requests(message: Message, telegram_id: int) -> None:
        requests = await asyncio.to_thread(db.list_user_requests, telegram_id)
        if not requests:
            await message.answer("У вас пока нет заявок.", reply_markup=main_menu(is_admin(telegram_id)))
            return
        await message.answer("Последние заявки:")
        for index, request in enumerate(requests):
            is_last = index == len(requests) - 1
            buttons = None
            if request.status == PENDING:
                buttons = _request_edit_keyboard(request.id, include_home=is_last)
            elif request.status == APPROVED:
                rows = [
                    [
                        InlineKeyboardButton(text="🔄 Запросить перенос", callback_data=f"b:move:{request.id}"),
                        InlineKeyboardButton(text="❌ Запросить отмену", callback_data=f"b:cancel:{request.id}"),
                    ]
                ]
                if is_last:
                    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
                buttons = _keyboard(rows)
            elif is_last:
                buttons = home_keyboard()
            await message.answer(
                format_request(request, settings.timezone, include_private=False),
                reply_markup=buttons,
            )

    async def show_admin_requests(message: Message) -> None:
        requests = await asyncio.to_thread(db.list_pending)
        if not requests:
            await message.answer("Нет заявок на рассмотрении.")
            return
        for request in requests:
            buttons = None
            if request.status == PENDING:
                buttons = _keyboard(
                    [
                        [
                            InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve:{request.id}"),
                            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request.id}"),
                        ],
                        [InlineKeyboardButton(text="✏️ Изменить / предложить время", callback_data=f"b:admin:req:{request.id}")],
                    ]
                )
            await message.answer(format_request(request, settings.timezone), reply_markup=buttons)

    async def notify_admin_changed(bot: Bot, request: MeetingRequest) -> None:
        try:
            await bot.send_message(
                settings.admin_telegram_id,
                "<b>Пользователь изменил заявку</b>\n\n" + format_request(request, settings.timezone),
                reply_markup=_keyboard(
                    [
                        [
                            InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve:{request.id}"),
                            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request.id}"),
                        ],
                        [InlineKeyboardButton(text="✏️ Изменить / предложить время", callback_data=f"b:admin:req:{request.id}")],
                    ]
                ),
            )
        except Exception:
            LOGGER.exception("admin_update_notification_failed", extra={"request_id": request.id})

    async def render_edit_menu(message: Message, request: MeetingRequest) -> None:
        await message.answer(
            format_request(request, settings.timezone, include_private=True) + "\n\nЧто изменить?",
            reply_markup=_keyboard(
                [
                    [
                        InlineKeyboardButton(text="👤 Имя", callback_data=f"editfield:{request.id}:telegram_name"),
                        InlineKeyboardButton(text="✉️ Email", callback_data=f"editfield:{request.id}:email"),
                    ],
                    [InlineKeyboardButton(text="📝 Тема", callback_data=f"editfield:{request.id}:subject")],
                    [
                        InlineKeyboardButton(text="📄 Описание", callback_data=f"editfield:{request.id}:description"),
                        InlineKeyboardButton(text="📍 Место/ссылка", callback_data=f"editfield:{request.id}:location"),
                    ],
                    [InlineKeyboardButton(text="🕐 Время и длительность", callback_data=f"editwhen:{request.id}")],
                    [InlineKeyboardButton(text="← Мои заявки", callback_data="my")],
                ]
            ),
        )

    def rules_text(current: BookingRules) -> str:
        booking_state = "включена" if current.booking_enabled else "выключена"
        durations_text = ", ".join(str(item) for item in current.durations)
        return (
            "<b>Настройки записи</b>\n\n"
            f"Новая запись: <b>{booking_state}</b>\n"
            f"Минимальный срок: <b>{current.min_lead_minutes} мин</b>\n"
            f"Горизонт: <b>{current.booking_horizon_days} дн.</b>\n"
            f"Резерв слота: <b>{current.hold_hours} ч.</b>\n"
            f"Длительности: <b>{durations_text} мин</b>\n"
            f"Шаг слотов: <b>{current.step_minutes} мин</b>\n"
            "Время записи пользователей: "
            f"<b>{format_clock_minutes(current.user_booking_start_minutes)}–"
            f"{format_clock_minutes(current.user_booking_end_minutes)}</b>\n"
            "Часовой пояс: <b>Москва</b>"
        )

    async def render_admin_settings(message: Message) -> None:
        current = rules()
        toggle_text = "⏸ Выключить новую запись" if current.booking_enabled else "▶️ Включить новую запись"
        await message.answer(
            rules_text(current),
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text=toggle_text, callback_data="aset:toggle")],
                    [
                        InlineKeyboardButton(text="⏱ Минимальный срок", callback_data=f"aset:value:{MIN_LEAD_MINUTES}"),
                        InlineKeyboardButton(text="📆 Горизонт", callback_data=f"aset:value:{BOOKING_HORIZON_DAYS}"),
                    ],
                    [InlineKeyboardButton(text="⌛ Резерв слота", callback_data=f"aset:value:{HOLD_HOURS}")],
                    [InlineKeyboardButton(text="🕘 Время для пользователей", callback_data="aset:window")],
                    [
                        InlineKeyboardButton(text="🕒 Длительности", callback_data="aset:durations"),
                        InlineKeyboardButton(text="📏 Шаг слотов", callback_data="aset:steps"),
                    ],
                    [InlineKeyboardButton(text="🚫 Закрытые даты", callback_data="aset:closed")],
                    [InlineKeyboardButton(text="🔔 Напоминания", callback_data="c:notify")],
                    [InlineKeyboardButton(text="📊 Статистика", callback_data="d:stats")],
                    [InlineKeyboardButton(text="🧾 История изменений", callback_data="aset:history")],
                    [InlineKeyboardButton(text="↩️ Вернуть значения по умолчанию", callback_data="aset:reset:ask")],
                    [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
                ]
            ),
        )

    async def render_durations(message: Message) -> None:
        enabled = set(rules().durations)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if value in enabled else '➕'} {value} мин",
                    callback_data=f"aset:duration:{value}",
                )
            ]
            for value in ALLOWED_DURATIONS
        ]
        rows.append([InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")])
        await message.answer(
            "Включите нужные длительности. Должна остаться хотя бы одна:",
            reply_markup=_keyboard(rows),
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = message.from_user
        if user is None:
            return
        await asyncio.to_thread(db.upsert_user, user.id, user.full_name, user.username)
        if db.has_consent(user.id, settings.privacy_policy_version):
            await show_home(message, user.id)
            return
        text = (
            "<b>Запись на встречу</b>\n\n"
            "Бот использует имя из Telegram, email и введённые вами сведения только для записи "
            "на встречу, уведомлений и создания события в Google Calendar. Содержимое календаря "
            "вам не показывается. Данные пилота хранятся на сервере в России.\n\n"
            "Нажимая «Согласен», вы подтверждаете согласие на обработку этих данных. "
            "Политика доступна по кнопке ниже."
        )
        await message.answer(
            text,
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="✅ Согласен", callback_data="consent:yes")],
                    [InlineKeyboardButton(text="🔐 Политика конфиденциальности", callback_data="privacy")],
                ]
            ),
        )

    @router.callback_query(F.data == "consent:yes")
    async def consent(callback: CallbackQuery) -> None:
        await asyncio.to_thread(db.upsert_user, callback.from_user.id, callback.from_user.full_name, callback.from_user.username)
        await asyncio.to_thread(db.set_consent, callback.from_user.id, settings.privacy_policy_version)
        await callback.answer("Согласие сохранено")
        if callback.message:
            await callback.message.edit_text("Согласие принято.")
            await show_home(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "home")
    async def home(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await show_home(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "help")
    async def help_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(help_text(), reply_markup=main_menu(is_admin(callback.from_user.id)))

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(help_text(), reply_markup=main_menu(bool(message.from_user and is_admin(message.from_user.id))))

    @router.callback_query(F.data == "privacy")
    async def privacy_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(privacy_text(settings), reply_markup=privacy_keyboard(is_admin(callback.from_user.id)))

    @router.message(Command("privacy"))
    async def privacy_command(message: Message) -> None:
        await message.answer(privacy_text(settings), reply_markup=privacy_keyboard(bool(message.from_user and is_admin(message.from_user.id))))

    @router.callback_query(F.data == "book")
    async def begin_booking(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        if not await ensure_consent(callback) or callback.message is None:
            return
        current = rules()
        if not current.booking_enabled:
            await callback.answer("Новая запись временно приостановлена", show_alert=True)
            return
        await state.clear()
        rows = [
            [InlineKeyboardButton(text=f"{duration} мин", callback_data=f"dur:{duration}")]
            for duration in current.durations
        ]
        rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="abort")])
        await callback.message.edit_text("Выберите длительность встречи:", reply_markup=_keyboard(rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("dur:"))
    async def select_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        duration = int(callback.data.split(":", 1)[1])
        current = rules()
        if not current.booking_enabled or duration not in current.durations:
            await callback.answer("Эта длительность сейчас недоступна", show_alert=True)
            return
        await state.set_state(Booking.choosing_date)
        await state.update_data(
            duration=duration,
            flow="new",
            unrestricted_time=is_admin(callback.from_user.id),
        )
        await render_date_page(callback.message, state, 0)
        await callback.answer()

    @router.callback_query(F.data.startswith("dpage:"))
    async def date_page(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        data = await state.get_data()
        if "duration" not in data:
            await callback.answer("Начните запись заново", show_alert=True)
            return
        await state.set_state(Booking.choosing_date)
        await render_date_page(callback.message, state, int(callback.data.split(":", 1)[1]))
        await callback.answer()

    @router.callback_query(Booking.choosing_date, F.data.startswith("date:"))
    async def select_date(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        raw = callback.data.split(":", 1)[1]
        selected = _date_from_callback(raw)
        current = rules()
        today = datetime.now(zone).date()
        closed = set(await asyncio.to_thread(db.list_closed_dates, selected.isoformat(), 1))
        if (
            selected < today
            or selected >= today + timedelta(days=current.booking_horizon_days)
            or is_closed_date(selected, closed, current)
        ):
            await callback.answer("Дата недоступна для записи", show_alert=True)
            return
        await state.update_data(selected_date=raw)
        await state.set_state(Booking.choosing_slot)
        await callback.answer()
        if is_admin(callback.from_user.id):
            await render_slot_page(callback.message, state, 0)
        else:
            await render_period_selection(callback.message, state)

    @router.callback_query(Booking.choosing_slot, F.data.startswith("period:"))
    async def select_period(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        current = rules()
        key = callback.data.split(":", 1)[1]
        valid_keys = {
            period.key
            for period in booking_periods(
                current.user_booking_start_minutes,
                current.user_booking_end_minutes,
            )
        }
        if key not in valid_keys:
            await callback.answer("Этот интервал больше недоступен", show_alert=True)
            await render_period_selection(callback.message, state)
            return
        await state.update_data(slot_period=key)
        await callback.answer()
        await render_slot_page(callback.message, state, 0)

    @router.callback_query(Booking.choosing_slot, F.data == "periods")
    async def return_to_periods(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        await callback.answer()
        await render_period_selection(callback.message, state)

    @router.callback_query(Booking.choosing_slot, F.data.startswith("spage:"))
    async def slot_page(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        await callback.answer()
        await render_slot_page(callback.message, state, int(callback.data.split(":", 1)[1]))

    @router.callback_query(Booking.choosing_slot, F.data.startswith("slot:"))
    async def select_slot(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        raw_time = callback.data.split(":", 1)[1]
        if len(raw_time) != 4 or not raw_time.isdigit():
            await callback.answer("Неверное время", show_alert=True)
            return
        data = await state.get_data()
        selected_date = _date_from_callback(data["selected_date"])
        start_local = datetime.combine(
            selected_date,
            time(hour=int(raw_time[:2]), minute=int(raw_time[2:])),
            zone,
        )
        duration = int(data["duration"])
        current = rules()
        end_local = start_local + timedelta(minutes=duration)
        if not is_admin(callback.from_user.id):
            local_midnight = datetime.combine(selected_date, time.min, zone)
            window_start = local_midnight + timedelta(minutes=current.user_booking_start_minutes)
            window_end = local_midnight + timedelta(minutes=current.user_booking_end_minutes)
            if start_local < window_start or end_local > window_end:
                await callback.answer("Время находится вне доступного интервала", show_alert=True)
                await render_period_selection(callback.message, state)
                return
        if start_local < datetime.now(zone) + timedelta(minutes=current.min_lead_minutes):
            await callback.answer("Этот слот уже недоступен", show_alert=True)
            await render_slot_page(callback.message, state, 0)
            return
        start_at = start_local.astimezone(UTC)
        end_at = start_at + timedelta(minutes=duration)
        if data.get("flow") == "edit":
            request_id = int(data["edit_request_id"])
            try:
                if not await calendar.is_free(start_at, end_at):
                    await callback.answer("Слот уже занят", show_alert=True)
                    await render_slot_page(callback.message, state, 0)
                    return
                updated = await asyncio.to_thread(
                    db.reschedule_pending,
                    request_id,
                    callback.from_user.id,
                    start_at,
                    end_at,
                    current.hold_hours,
                )
            except SlotConflictError:
                await callback.answer("Слот уже зарезервирован", show_alert=True)
                await render_slot_page(callback.message, state, 0)
                return
            except RequestNotEditableError:
                await callback.answer("Заявку уже нельзя изменить", show_alert=True)
                await state.clear()
                return
            except CalendarUnavailable:
                await callback.answer("Google Calendar недоступен", show_alert=True)
                return
            await state.clear()
            await callback.answer("Время изменено")
            await callback.message.edit_text(
                format_request(updated, settings.timezone, include_private=False)
                + f"\n\nНовый слот зарезервирован на {current.hold_hours} ч."
            )
            await notify_admin_changed(bot, updated)
            return
        await state.update_data(start_at=start_at.isoformat())
        await state.set_state(Booking.email)
        await callback.message.edit_text(
            f"Выбрано: {start_local:%d.%m.%Y %H:%M}, {duration} мин (МСК).\n\nВведите email участника:",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()

    @router.message(Booking.email)
    async def receive_email(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 254 or not EMAIL_RE.fullmatch(value):
            await message.answer("Проверьте формат email и введите ещё раз.")
            return
        await state.update_data(email=value)
        await state.set_state(Booking.subject)
        await message.answer("Введите тему встречи:", reply_markup=cancel_keyboard())

    @router.message(Booking.subject)
    async def receive_subject(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if not value or len(value) > 200:
            await message.answer("Тема должна содержать от 1 до 200 символов.")
            return
        await state.update_data(subject=value)
        await state.set_state(Booking.description)
        await message.answer(
            "Добавьте описание или пропустите шаг:",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="Пропустить", callback_data="skip:description")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    async def ask_location(message: Message, state: FSMContext, description: str | None) -> None:
        await state.update_data(description=description)
        await state.set_state(Booking.location)
        await message.answer(
            "Укажите адрес или ссылку на видеовстречу, либо пропустите:",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="Пропустить", callback_data="skip:location")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    @router.message(Booking.description)
    async def receive_description(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 2000:
            await message.answer("Описание должно быть короче 2000 символов.")
            return
        await ask_location(message, state, value or None)

    @router.callback_query(Booking.description, F.data == "skip:description")
    async def skip_description(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message:
            await ask_location(callback.message, state, None)

    async def show_confirmation(message: Message, state: FSMContext, location: str | None) -> None:
        await state.update_data(location=location)
        data = await state.get_data()
        start_time = datetime.fromisoformat(data["start_at"]).astimezone(zone)
        end_time = start_time + timedelta(minutes=int(data["duration"]))
        lines = [
            "<b>Проверьте заявку</b>",
            f"Время: {start_time:%d.%m.%Y %H:%M}–{end_time:%H:%M} (МСК)",
            f"Email: {html.escape(data['email'])}",
            f"Тема: {html.escape(data['subject'])}",
        ]
        if data.get("description"):
            lines.append(f"Описание: {html.escape(data['description'])}")
        if location:
            lines.append(f"Место/ссылка: {html.escape(location)}")
        await state.set_state(Booking.confirm)
        await message.answer(
            "\n".join(lines),
            reply_markup=_keyboard(
                [
                    [
                        InlineKeyboardButton(text="✅ Отправить", callback_data="confirm"),
                        InlineKeyboardButton(text="✖ Отменить", callback_data="abort"),
                    ]
                ]
            ),
        )

    @router.message(Booking.location)
    async def receive_location(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 500:
            await message.answer("Поле должно быть короче 500 символов.")
            return
        await show_confirmation(message, state, value or None)

    @router.callback_query(Booking.location, F.data == "skip:location")
    async def skip_location(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message:
            await show_confirmation(callback.message, state, None)

    @router.callback_query(Booking.confirm, F.data == "confirm")
    async def confirm_request(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.message is None:
            return
        data = await state.get_data()
        start_at = datetime.fromisoformat(data["start_at"]).astimezone(UTC)
        duration = int(data["duration"])
        end_at = start_at + timedelta(minutes=duration)
        current = rules()
        local_start = start_at.astimezone(zone)
        local_end = end_at.astimezone(zone)
        local_midnight = datetime.combine(local_start.date(), time.min, zone)
        outside_user_window = (
            not is_admin(callback.from_user.id)
            and (
                local_start < local_midnight + timedelta(minutes=current.user_booking_start_minutes)
                or local_end > local_midnight + timedelta(minutes=current.user_booking_end_minutes)
            )
        )
        today = datetime.now(zone).date()
        closed = set(await asyncio.to_thread(db.list_closed_dates, local_start.date().isoformat(), 1))
        if (
            not current.booking_enabled
            or duration not in current.durations
            or local_start.date() < today
            or local_start.date() >= today + timedelta(days=current.booking_horizon_days)
            or is_closed_date(local_start.date(), closed, current)
            or local_start < datetime.now(zone) + timedelta(minutes=current.min_lead_minutes)
            or outside_user_window
        ):
            await state.clear()
            await callback.answer("Условия записи изменились", show_alert=True)
            await callback.message.edit_text(
                "Этот слот больше недоступен.",
                reply_markup=home_keyboard(),
            )
            return
        try:
            if not await calendar.is_free(start_at, end_at):
                await callback.answer("Слот уже занят", show_alert=True)
                await state.clear()
                await callback.message.edit_text(
                    "К сожалению, слот уже занят.",
                    reply_markup=home_keyboard(),
                )
                return
            request = await asyncio.to_thread(
                db.create_request,
                telegram_id=callback.from_user.id,
                telegram_name=callback.from_user.full_name,
                telegram_username=callback.from_user.username,
                email=data["email"],
                subject=data["subject"],
                description=data.get("description"),
                location=data.get("location"),
                start_at=start_at,
                end_at=end_at,
                hold_hours=current.hold_hours,
            )
        except SlotConflictError:
            await callback.answer("Слот уже зарезервирован", show_alert=True)
            await state.clear()
            await callback.message.edit_text(
                "Слот уже выбрал другой пользователь.",
                reply_markup=home_keyboard(),
            )
            return
        except CalendarUnavailable:
            await callback.answer("Google Calendar недоступен", show_alert=True)
            return
        await state.clear()
        if automation:
            try:
                notification_rules = load_notification_rules(db, settings)
                await asyncio.to_thread(
                    automation.ensure_new_request_notification,
                    request.id,
                    settings.admin_telegram_id,
                )
                await asyncio.to_thread(
                    automation.ensure_pending_reminder,
                    request.id,
                    settings.admin_telegram_id,
                    notification_rules.pending_reminder_hours,
                )
            except Exception:
                LOGGER.exception("pending_reminder_schedule_failed", extra={"request_id": request.id})
        await callback.answer("Заявка отправлена")
        await callback.message.edit_text(
            format_request(request, settings.timezone, include_private=False)
            + f"\n\nСлот зарезервирован на {current.hold_hours} ч.",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
                ]
            ),
        )
        try:
            await bot.send_message(
                settings.admin_telegram_id,
                "<b>Новая заявка</b>\n\n" + format_request(request, settings.timezone),
                reply_markup=_keyboard(
                    [
                        [
                            InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve:{request.id}"),
                            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request.id}"),
                        ],
                        [InlineKeyboardButton(text="✏️ Изменить / предложить время", callback_data=f"b:admin:req:{request.id}")],
                    ]
                ),
            )
        except Exception:
            LOGGER.exception("admin_notification_failed", extra={"request_id": request.id})

    @router.callback_query(F.data == "my")
    async def my_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await show_my_requests(callback.message, callback.from_user.id)

    @router.message(Command("my"))
    async def my_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            await show_my_requests(message, message.from_user.id)

    @router.callback_query(F.data.startswith("edit:"))
    async def edit_request(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        await state.clear()
        request_id = int(callback.data.split(":", 1)[1])
        request = await asyncio.to_thread(request_owned_pending, request_id, callback.from_user.id)
        if request is None:
            await callback.answer("Заявку уже нельзя изменить", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await render_edit_menu(callback.message, request)

    @router.callback_query(F.data.startswith("editfield:"))
    async def choose_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        _, request_raw, field = callback.data.split(":", 2)
        request_id = int(request_raw)
        request = await asyncio.to_thread(request_owned_pending, request_id, callback.from_user.id)
        if request is None or field not in EDIT_FIELD_LABELS:
            await callback.answer("Заявку уже нельзя изменить", show_alert=True)
            return
        await state.set_state(Editing.value)
        await state.set_data({"edit_request_id": request_id, "edit_field": field})
        rows = [[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]
        prompt = f"Введите новое значение поля «{EDIT_FIELD_LABELS[field]}»:"
        if field in {"description", "location"}:
            rows.insert(0, [InlineKeyboardButton(text="Очистить поле", callback_data=f"editclear:{request_id}:{field}")])
            prompt += "\nПоле можно очистить кнопкой ниже."
        await callback.answer()
        if callback.message:
            await callback.message.answer(prompt, reply_markup=_keyboard(rows))

    @router.message(Editing.value)
    async def receive_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        request_id = int(data["edit_request_id"])
        field = str(data["edit_field"])
        value = (message.text or "").strip()
        error = None
        if field == "email" and (len(value) > 254 or not EMAIL_RE.fullmatch(value)):
            error = "Проверьте формат email."
        elif field in {"telegram_name", "subject"} and (not value or len(value) > 200):
            error = "Значение должно содержать от 1 до 200 символов."
        elif field == "description" and len(value) > 2000:
            error = "Описание должно быть короче 2000 символов."
        elif field == "location" and len(value) > 500:
            error = "Поле должно быть короче 500 символов."
        if error:
            await message.answer(error + " Введите ещё раз.")
            return
        normalized: str | None = value or None
        try:
            updated = await asyncio.to_thread(
                db.update_pending_details,
                request_id,
                message.from_user.id,
                {field: normalized},
            )
        except RequestNotEditableError:
            await state.clear()
            await message.answer("Заявку уже нельзя изменить.")
            return
        await state.clear()
        await message.answer(
            "Изменение сохранено.\n\n" + format_request(updated, settings.timezone, include_private=False),
            reply_markup=_request_edit_keyboard(updated.id),
        )
        await notify_admin_changed(bot, updated)

    @router.callback_query(F.data.startswith("editclear:"))
    async def clear_edit_field(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        _, request_raw, field = callback.data.split(":", 2)
        request_id = int(request_raw)
        if field not in {"description", "location"}:
            await callback.answer("Поле нельзя очистить", show_alert=True)
            return
        try:
            updated = await asyncio.to_thread(
                db.update_pending_details,
                request_id,
                callback.from_user.id,
                {field: None},
            )
        except RequestNotEditableError:
            await callback.answer("Заявку уже нельзя изменить", show_alert=True)
            return
        await state.clear()
        await callback.answer("Поле очищено")
        if callback.message:
            await callback.message.edit_text(
                format_request(updated, settings.timezone, include_private=False),
                reply_markup=_request_edit_keyboard(updated.id),
            )
        await notify_admin_changed(bot, updated)

    @router.callback_query(F.data.startswith("editwhen:"))
    async def edit_when(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        request_id = int(callback.data.split(":", 1)[1])
        request = await asyncio.to_thread(request_owned_pending, request_id, callback.from_user.id)
        if request is None:
            await callback.answer("Заявку уже нельзя изменить", show_alert=True)
            return
        current = rules()
        await state.clear()
        rows = [
            [InlineKeyboardButton(text=f"{duration} мин", callback_data=f"editdur:{request_id}:{duration}")]
            for duration in current.durations
        ]
        rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="abort")])
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите новую длительность:", reply_markup=_keyboard(rows))

    @router.callback_query(F.data.startswith("editdur:"))
    async def edit_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if await block_during_active_form(callback, state):
            return
        _, request_raw, duration_raw = callback.data.split(":", 2)
        request_id = int(request_raw)
        duration = int(duration_raw)
        request = await asyncio.to_thread(request_owned_pending, request_id, callback.from_user.id)
        current = rules()
        if request is None or duration not in current.durations:
            await callback.answer("Длительность недоступна", show_alert=True)
            return
        await state.set_state(Booking.choosing_date)
        await state.set_data(
            {
                "duration": duration,
                "flow": "edit",
                "edit_request_id": request_id,
                "unrestricted_time": is_admin(callback.from_user.id),
            }
        )
        await callback.answer()
        if callback.message:
            await render_date_page(callback.message, state, 0)

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_request(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if await block_during_active_form(callback, state):
            return
        await state.clear()
        request_id = int(callback.data.split(":", 1)[1])
        changed = await asyncio.to_thread(db.cancel_pending, request_id, callback.from_user.id)
        if not changed:
            await callback.answer("Заявку уже нельзя отменить", show_alert=True)
            return
        request = db.get_request(request_id)
        if automation:
            try:
                await asyncio.to_thread(automation.cancel_request_jobs, request_id)
            except Exception:
                LOGGER.exception("cancelled_request_jobs_cleanup_failed", extra={"request_id": request_id})
        await callback.answer("Заявка отменена")
        if callback.message and request:
            await callback.message.edit_text(
                format_request(request, settings.timezone, include_private=False),
                reply_markup=home_keyboard(),
            )
        try:
            await bot.send_message(settings.admin_telegram_id, f"Пользователь отменил заявку №{request_id}.")
        except Exception:
            LOGGER.exception("admin_cancellation_notification_failed", extra={"request_id": request_id})

    @router.callback_query(F.data == "admin:list")
    async def admin_list_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        if await block_during_active_form(callback, state):
            return
        await callback.answer()
        if callback.message:
            await show_admin_requests(callback.message)

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await message.answer("Нет доступа.")
            return
        await show_admin_requests(message)

    @router.callback_query(F.data == "admin:settings")
    async def admin_settings_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        if await block_during_active_form(callback, state):
            return
        await state.clear()
        await callback.answer()
        if callback.message:
            await render_admin_settings(callback.message)

    @router.message(Command("settings"))
    async def admin_settings_command(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await message.answer("Нет доступа.")
            return
        await state.clear()
        await render_admin_settings(message)

    @router.callback_query(F.data == "aset:toggle")
    async def toggle_booking(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        new_value = not rules().booking_enabled
        await asyncio.to_thread(db.set_setting, BOOKING_ENABLED, new_value, callback.from_user.id)
        await callback.answer("Настройка сохранена")
        if callback.message:
            await render_admin_settings(callback.message)

    @router.callback_query(F.data.startswith("aset:value:"))
    async def choose_setting_value(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        key = callback.data.split(":", 2)[2]
        prompts = {
            MIN_LEAD_MINUTES: "Введите минимальный срок до встречи в минутах (0–10080):",
            BOOKING_HORIZON_DAYS: "Введите горизонт записи в днях (1–365):",
            HOLD_HOURS: "Введите срок резерва слота в часах (1–168):",
        }
        if key not in prompts:
            await callback.answer("Неизвестная настройка", show_alert=True)
            return
        await state.set_state(AdminSettings.value)
        await state.set_data({"setting_key": key})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                prompts[key],
                reply_markup=_keyboard([[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]),
            )

    @router.message(AdminSettings.value)
    async def receive_setting_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await message.answer("Нет доступа.")
            await state.clear()
            return
        data = await state.get_data()
        key = str(data["setting_key"])
        raw = (message.text or "").strip()
        try:
            value = validate_value(key, int(raw))
        except (TypeError, ValueError) as exc:
            await message.answer(f"Некорректное значение: {exc}. Введите ещё раз.")
            return
        await asyncio.to_thread(db.set_setting, key, value, message.from_user.id)
        await state.clear()
        await message.answer("Настройка сохранена.")
        await render_admin_settings(message)

    @router.callback_query(F.data == "aset:window")
    async def choose_booking_window(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        current = rules()
        await state.set_state(AdminSettings.booking_window)
        await state.set_data({})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Введите время, когда пользователи могут назначать встречи.\n"
                "Например: 0800-2100 или 08:00-21:00.\n\n"
                "Текущее значение: "
                f"{format_clock_minutes(current.user_booking_start_minutes)}–"
                f"{format_clock_minutes(current.user_booking_end_minutes)}.",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminSettings.booking_window)
    async def receive_booking_window(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await message.answer("Нет доступа.")
            await state.clear()
            return
        try:
            value = parse_booking_window(message.text or "")
        except (TypeError, ValueError) as exc:
            await message.answer(f"Некорректный интервал: {exc}. Введите ещё раз.")
            return
        await asyncio.to_thread(db.set_setting, USER_BOOKING_WINDOW, value, message.from_user.id)
        await state.clear()
        await message.answer(
            "Время записи пользователей сохранено: "
            f"{format_clock_minutes(value[0])}–{format_clock_minutes(value[1])}."
        )
        await render_admin_settings(message)

    @router.callback_query(F.data == "aset:steps")
    async def choose_step(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        rows = [
            [InlineKeyboardButton(text=f"{value} мин", callback_data=f"aset:step:{value}")]
            for value in ALLOWED_STEPS
        ]
        rows.append([InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")])
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите шаг начала слотов:", reply_markup=_keyboard(rows))

    @router.callback_query(F.data.startswith("aset:step:"))
    async def set_step(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        value = validate_value(STEP_MINUTES, int(callback.data.rsplit(":", 1)[1]))
        await asyncio.to_thread(db.set_setting, STEP_MINUTES, value, callback.from_user.id)
        await callback.answer("Шаг сохранён")
        if callback.message:
            await render_admin_settings(callback.message)

    @router.callback_query(F.data == "aset:durations")
    async def choose_durations(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await render_durations(callback.message)

    @router.callback_query(F.data.startswith("aset:duration:"))
    async def toggle_duration(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        value = int(callback.data.rsplit(":", 1)[1])
        enabled = set(rules().durations)
        if value in enabled:
            if len(enabled) == 1:
                await callback.answer("Нельзя отключить последнюю длительность", show_alert=True)
                return
            enabled.remove(value)
        else:
            enabled.add(value)
        normalized = validate_value(DURATIONS, sorted(enabled))
        await asyncio.to_thread(db.set_setting, DURATIONS, normalized, callback.from_user.id)
        await callback.answer("Длительности обновлены")
        if callback.message:
            await render_durations(callback.message)

    @router.callback_query(F.data == "aset:closed")
    async def closed_dates(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        today = datetime.now(zone).date()
        values = await asyncio.to_thread(db.list_closed_dates, today.isoformat(), 30)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"Открыть {date.fromisoformat(value):%d.%m.%Y}",
                    callback_data=f"aset:open:{value.replace('-', '')}",
                )
            ]
            for value in values
        ]
        rows.extend(
            [
                [InlineKeyboardButton(text="➕ Закрыть дату", callback_data="aset:close:add")],
                [InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")],
            ]
        )
        text = "Ближайшие закрытые даты:" if values else "Закрытых будущих дат нет."
        await callback.answer()
        if callback.message:
            await callback.message.answer(text, reply_markup=_keyboard(rows))

    @router.callback_query(F.data == "aset:close:add")
    async def add_closed_date_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.set_state(AdminSettings.closed_date)
        await state.set_data({})
        await callback.answer()
        if callback.message:
            today = datetime.now(zone).date()
            await callback.message.answer(
                "Выберите дату, которую нужно закрыть:",
                reply_markup=calendar_keyboard("adate:closed", today, today, today + timedelta(days=730)),
            )

    @router.callback_query(F.data.startswith("adate:closed:"))
    async def add_closed_date_calendar(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        try:
            _, _, action, raw_value = callback.data.split(":", 3)
            if await state.get_state() != AdminSettings.closed_date.state:
                raise ValueError
            today = datetime.now(zone).date()
            maximum = today + timedelta(days=730)
            if action == "nav":
                shown = month_from_callback(raw_value)
                if not date(today.year, today.month, 1) <= shown <= date(maximum.year, maximum.month, 1):
                    raise ValueError
                await callback.answer()
                if callback.message:
                    await callback.message.edit_reply_markup(
                        reply_markup=calendar_keyboard("adate:closed", shown, today, maximum)
                    )
                return
            if action != "day":
                raise ValueError
            value = date.fromisoformat(raw_value)
            if not today <= value <= maximum:
                raise ValueError
        except (TypeError, ValueError):
            await callback.answer("Дата недоступна. Откройте календарь заново.", show_alert=True)
            return
        changed = await asyncio.to_thread(db.add_closed_date, value.isoformat(), callback.from_user.id)
        await state.clear()
        await callback.answer("Дата закрыта" if changed else "Дата уже закрыта")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Дата закрыта." if changed else "Эта дата уже была закрыта.")
            await render_admin_settings(callback.message)

    @router.message(AdminSettings.closed_date)
    async def receive_closed_date(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await message.answer("Нет доступа.")
            await state.clear()
            return
        try:
            value = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        except ValueError:
            await message.answer("Неверный формат. Введите дату как ДД.ММ.ГГГГ.")
            return
        today = datetime.now(zone).date()
        if value < today or value > today + timedelta(days=730):
            await message.answer("Можно закрыть дату от сегодня до двух лет вперёд.")
            return
        changed = await asyncio.to_thread(db.add_closed_date, value.isoformat(), message.from_user.id)
        await state.clear()
        await message.answer("Дата закрыта." if changed else "Эта дата уже была закрыта.")
        await render_admin_settings(message)

    @router.callback_query(F.data.startswith("aset:open:"))
    async def open_closed_date(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        raw = callback.data.rsplit(":", 1)[1]
        value = _date_from_callback(raw).isoformat()
        changed = await asyncio.to_thread(db.remove_closed_date, value, callback.from_user.id)
        await callback.answer("Дата снова доступна" if changed else "Дата уже доступна")
        if callback.message:
            await render_admin_settings(callback.message)

    @router.callback_query(F.data == "aset:history")
    async def setting_history(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        items = await asyncio.to_thread(db.list_setting_history, 10)
        lines = ["<b>Последние изменения настроек</b>"]
        if not items:
            lines.append("Изменений пока нет.")
        for item in items:
            occurred = item["occurred_at"].astimezone(zone)
            label = SETTING_LABELS.get(item["key"], item["key"])
            new_value = item["new_value"] if item["new_value"] is not None else "по умолчанию"
            lines.append(f"{occurred:%d.%m %H:%M} — {html.escape(label)}: {html.escape(str(new_value))}")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "\n".join(lines),
                reply_markup=_keyboard([[InlineKeyboardButton(text="← Настройки", callback_data="admin:settings")]]),
            )

    @router.callback_query(F.data == "aset:reset:ask")
    async def reset_settings_ask(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Вернуть все настройки записи к значениям из конфигурации сервера? Закрытые даты сохранятся.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="Да, вернуть", callback_data="aset:reset:confirm")],
                        [InlineKeyboardButton(text="Нет", callback_data="admin:settings")],
                    ]
                ),
            )

    @router.callback_query(F.data == "aset:reset:confirm")
    async def reset_settings_confirm(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        count = await asyncio.to_thread(db.reset_settings, callback.from_user.id)
        await callback.answer("Настройки восстановлены")
        if callback.message:
            await callback.message.answer(f"Восстановлены значения по умолчанию ({count} изменений).")
            await render_admin_settings(callback.message)

    @router.callback_query(F.data.startswith("reject:"))
    async def reject_request(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not await require_admin(callback):
            return
        if await block_during_active_form(callback, state):
            return
        request_id = int(callback.data.split(":", 1)[1])
        request = await asyncio.to_thread(db.reject, request_id, callback.from_user.id)
        if request is None:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        if automation:
            try:
                await asyncio.to_thread(automation.cancel_request_jobs, request.id)
            except Exception:
                LOGGER.exception("rejected_request_jobs_cleanup_failed", extra={"request_id": request.id})
        await callback.answer("Заявка отклонена")
        if callback.message:
            await callback.message.edit_text(format_request(request, settings.timezone))
        try:
            await bot.send_message(
                request.telegram_id,
                format_request(request, settings.timezone, include_private=False)
                + "\n\nК сожалению, заявка не согласована.",
                reply_markup=home_keyboard(),
            )
        except Exception:
            LOGGER.exception("user_rejection_notification_failed", extra={"request_id": request.id})

    @router.callback_query(F.data.startswith("approve:"))
    async def approve_request(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not await require_admin(callback):
            return
        if await block_during_active_form(callback, state):
            return
        request_id = int(callback.data.split(":", 1)[1])
        request = await asyncio.to_thread(db.claim_for_approval, request_id, callback.from_user.id)
        if request is None:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        await callback.answer("Проверяю слот и создаю событие…")
        try:
            if not await calendar.is_free(request.start_at, request.end_at):
                await asyncio.to_thread(db.reset_approval, request_id, "google_slot_busy")
                if callback.message:
                    await callback.message.answer(
                        f"Заявку №{request_id} нельзя согласовать: время уже занято в Google Calendar."
                    )
                return
            event_id = await calendar.create_event(request)
            approved = await asyncio.to_thread(
                db.complete_approval,
                request_id,
                callback.from_user.id,
                event_id,
            )
            if automation:
                try:
                    notification_rules = load_notification_rules(db, settings)
                    await asyncio.to_thread(
                        automation.rebuild_request_reminders,
                        approved.id,
                        settings.admin_telegram_id,
                        notification_rules.reminder_minutes,
                    )
                except Exception:
                    LOGGER.exception("meeting_reminder_schedule_failed", extra={"request_id": approved.id})
        except CalendarUnavailable:
            await asyncio.to_thread(db.reset_approval, request_id, "google_unavailable")
            if callback.message:
                await callback.message.answer(
                    f"Google Calendar недоступен. Заявка №{request_id} осталась на согласовании; повторите позже."
                )
            return
        except Exception:
            LOGGER.exception("approval_failed", extra={"request_id": request_id})
            await asyncio.to_thread(db.reset_approval, request_id, "unexpected_error")
            if callback.message:
                await callback.message.answer(
                    f"Неожиданная ошибка для заявки №{request_id}. Заявка возвращена на рассмотрение."
                )
            return
        if callback.message:
            await callback.message.edit_text(format_request(approved, settings.timezone))
        try:
            await bot.send_message(
                approved.telegram_id,
                format_request(approved, settings.timezone, include_private=False)
                + "\n\nВстреча согласована. Приглашение отправлено на ваш email.",
                reply_markup=home_keyboard(),
            )
        except Exception:
            LOGGER.exception("user_approval_notification_failed", extra={"request_id": request_id})

    @router.callback_query(F.data == "abort")
    async def abort(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Действие отменено")
        if callback.message:
            await callback.message.edit_text("Действие отменено.", reply_markup=home_keyboard())

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.message()
    async def unknown_message(message: Message) -> None:
        await message.answer(
            "Я помогу выбрать время для встречи.",
            reply_markup=home_keyboard(),
        )

    return router
