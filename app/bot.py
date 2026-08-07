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

from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.db import Database, SlotConflictError
from app.models import APPROVED, APPROVING, CANCELLED, PENDING, REJECTED, MeetingRequest
from app.slots import available_slots


LOGGER = logging.getLogger(__name__)
DURATIONS = (15, 30, 45, 60, 90)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DATE_PAGE_SIZE = 7
SLOT_PAGE_SIZE = 24


class Booking(StatesGroup):
    choosing_date = State()
    choosing_slot = State()
    email = State()
    subject = State()
    description = State()
    location = State()
    confirm = State()


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Заявки на рассмотрении", callback_data="admin:list")])
    return _keyboard(rows)


def format_request(request: MeetingRequest, timezone_name: str, include_private: bool = True) -> str:
    zone = ZoneInfo(timezone_name)
    start = request.start_at.astimezone(zone)
    end = request.end_at.astimezone(zone)
    status_labels = {
        PENDING: "на согласовании",
        APPROVING: "создаётся в календаре",
        APPROVED: "согласована",
        REJECTED: "отклонена",
        CANCELLED: "отменена",
    }
    lines = [
        f"<b>Заявка №{request.id}</b>",
        f"Статус: {status_labels.get(request.status, request.status)}",
        f"Время: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)",
        f"Тема: {html.escape(request.subject)}",
    ]
    if include_private:
        lines.extend(
            [
                f"Участник: {html.escape(request.telegram_name)}",
                f"Email: {html.escape(request.email)}",
            ]
        )
        if request.description:
            lines.append(f"Описание: {html.escape(request.description)}")
        if request.location:
            lines.append(f"Место/ссылка: {html.escape(request.location)}")
    return "\n".join(lines)


def _date_from_callback(raw: str) -> date:
    return datetime.strptime(raw, "%Y%m%d").date()


def create_router(settings: Settings, db: Database, calendar: CalendarClient) -> Router:
    router = Router()
    zone = ZoneInfo(settings.timezone)

    def is_admin(telegram_id: int) -> bool:
        return telegram_id == settings.admin_telegram_id

    async def show_home(message: Message, telegram_id: int) -> None:
        await message.answer(
            "Выберите действие:",
            reply_markup=main_menu(is_admin(telegram_id)),
        )

    async def ensure_consent(callback: CallbackQuery) -> bool:
        if db.has_consent(callback.from_user.id, settings.privacy_policy_version):
            return True
        await callback.answer("Сначала примите условия", show_alert=True)
        return False

    async def render_date_page(target: Message, state: FSMContext, page: int) -> None:
        today = datetime.now(zone).date()
        max_page = (settings.booking_horizon_days - 1) // DATE_PAGE_SIZE
        page = max(0, min(page, max_page))
        start_offset = page * DATE_PAGE_SIZE
        rows: list[list[InlineKeyboardButton]] = []
        for offset in range(start_offset, min(start_offset + DATE_PAGE_SIZE, settings.booking_horizon_days)):
            value = today + timedelta(days=offset)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=value.strftime("%d.%m, %A").replace("Monday", "пн").replace("Tuesday", "вт").replace("Wednesday", "ср").replace("Thursday", "чт").replace("Friday", "пт").replace("Saturday", "сб").replace("Sunday", "вс"),
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
        await target.edit_text("Выберите дату:", reply_markup=_keyboard(rows))

    async def load_slots(selected_date: date, duration: int) -> list[datetime]:
        day_start = datetime.combine(selected_date, time.min, zone)
        day_end = day_start + timedelta(days=1)
        google_busy, local_busy = await asyncio.gather(
            calendar.busy(day_start, day_end),
            asyncio.to_thread(db.active_intervals, day_start, day_end),
        )
        return available_slots(
            local_date=selected_date,
            duration_minutes=duration,
            busy_intervals=google_busy + local_busy,
            now=datetime.now(UTC),
            timezone_name=settings.timezone,
            min_lead_minutes=settings.min_lead_minutes,
        )

    async def render_slot_page(target: Message, state: FSMContext, page: int) -> None:
        data = await state.get_data()
        selected_date = _date_from_callback(data["selected_date"])
        duration = int(data["duration"])
        try:
            slots = await load_slots(selected_date, duration)
        except CalendarUnavailable:
            await target.edit_text(
                "Google Calendar сейчас недоступен. Попробуйте позже.",
                reply_markup=_keyboard([[InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")]]),
            )
            return
        if not slots:
            await target.edit_text(
                "На эту дату нет свободных слотов.",
                reply_markup=_keyboard([[InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")]]),
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
        rows.append([InlineKeyboardButton(text="← К датам", callback_data=f"dpage:{data.get('date_page', 0)}")])
        await target.edit_text(
            f"Свободно {selected_date:%d.%m.%Y}, длительность {duration} мин:\n"
            "Время указано по Москве.",
            reply_markup=_keyboard(rows),
        )

    async def show_my_requests(message: Message, telegram_id: int) -> None:
        requests = await asyncio.to_thread(db.list_user_requests, telegram_id)
        if not requests:
            await message.answer("У вас пока нет заявок.", reply_markup=main_menu(is_admin(telegram_id)))
            return
        await message.answer("Последние заявки:")
        for request in requests:
            buttons = None
            if request.status == PENDING:
                buttons = _keyboard(
                    [[InlineKeyboardButton(text="Отменить заявку", callback_data=f"cancel:{request.id}")]]
                )
            await message.answer(format_request(request, settings.timezone, include_private=False), reply_markup=buttons)

    async def show_admin_requests(message: Message) -> None:
        requests = await asyncio.to_thread(db.list_pending)
        if not requests:
            await message.answer("Нет заявок на рассмотрении.")
            return
        for request in requests:
            buttons = None
            if request.status == PENDING:
                buttons = _keyboard(
                    [[
                        InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve:{request.id}"),
                        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request.id}"),
                    ]]
                )
            await message.answer(format_request(request, settings.timezone), reply_markup=buttons)

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
            "Бот использует имя из Telegram, email и введённые вами сведения "
            "только для записи на встречу, уведомлений и создания события в Google Calendar. "
            "Содержимое календа вам не показывается. Данные пилота хранятся на сервере в России.\n\n"
            "Нажимая «Согласен», вы подтверждаете согласие на обработку этих данных."
        )
        await message.answer(
            text,
            reply_markup=_keyboard([[InlineKeyboardButton(text="✅ Согласен", callback_data="consent:yes")]]),
        )

    @router.callback_query(F.data == "consent:yes")
    async def consent(callback: CallbackQuery) -> None:
        await asyncio.to_thread(db.set_consent, callback.from_user.id, settings.privacy_policy_version)
        await callback.answer("Согласие сохранено")
        if callback.message:
            await callback.message.edit_text("Согласие принято.")
            await show_home(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "book")
    async def begin_booking(callback: CallbackQuery, state: FSMContext) -> None:
        if not await ensure_consent(callback) or callback.message is None:
            return
        await state.clear()
        rows = [[InlineKeyboardButton(text=f"{duration} мин", callback_data=f"dur:{duration}")] for duration in DURATIONS]
        rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="abort")])
        await callback.message.edit_text("Выберите длительность встречи:", reply_markup=_keyboard(rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("dur:"))
    async def select_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        duration = int(callback.data.split(":", 1)[1])
        if duration not in DURATIONS:
            await callback.answer("Неверная длительность", show_alert=True)
            return
        await state.set_state(Booking.choosing_date)
        await state.update_data(duration=duration)
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
        today = datetime.now(zone).date()
        if selected < today or selected >= today + timedelta(days=settings.booking_horizon_days):
            await callback.answer("Дата вне горизонта записи", show_alert=True)
            return
        await state.update_data(selected_date=raw)
        await state.set_state(Booking.choosing_slot)
        await callback.answer()
        await render_slot_page(callback.message, state, 0)

    @router.callback_query(Booking.choosing_slot, F.data.startswith("spage:"))
    async def slot_page(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        await callback.answer()
        await render_slot_page(callback.message, state, int(callback.data.split(":", 1)[1]))

    @router.callback_query(Booking.choosing_slot, F.data.startswith("slot:"))
    async def select_slot(callback: CallbackQuery, state: FSMContext) -> None:
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
        if start_local < datetime.now(zone) + timedelta(minutes=settings.min_lead_minutes):
            await callback.answer("Этот слот уже недоступен", show_alert=True)
            await render_slot_page(callback.message, state, 0)
            return
        await state.update_data(start_at=start_local.astimezone(UTC).isoformat())
        await state.set_state(Booking.email)
        await callback.message.edit_text(
            f"Выбрано: {start_local:%d.%m.%Y %H:%M}, {duration} мин (МСК).\n\nВведите email участника:"
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
        await message.answer("Введите тему встречи:")

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
            reply_markup=_keyboard([[InlineKeyboardButton(text="Пропустить", callback_data="skip:description")]]),
        )

    async def ask_location(message: Message, state: FSMContext, description: str | None) -> None:
        await state.update_data(description=description)
        await state.set_state(Booking.location)
        await message.answer(
            "Укажите адрес или ссылку на видеовстречу, либо пропустите:",
            reply_markup=_keyboard([[InlineKeyboardButton(text="Пропустить", callback_data="skip:location")]]),
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
        start = datetime.fromisoformat(data["start_at"]).astimezone(zone)
        end = start + timedelta(minutes=int(data["duration"]))
        lines = [
            "<b>Проверьте заявку</b>",
            f"Время: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)",
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
                [[
                    InlineKeyboardButton(text="✅ Отправить", callback_data="confirm"),
                    InlineKeyboardButton(text="✖ Отменить", callback_data="abort"),
                ]]
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
        end_at = start_at + timedelta(minutes=int(data["duration"]))
        try:
            if not await calendar.is_free(start_at, end_at):
                await callback.answer("Слот уже занят", show_alert=True)
                await state.clear()
                await callback.message.edit_text("К сожалению, слот уже занят. Начните запись заново: /start")
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
                hold_hours=settings.hold_hours,
            )
        except SlotConflictError:
            await callback.answer("Слот уже зарезервирован", show_alert=True)
            await state.clear()
            await callback.message.edit_text("Слот уже выбрал другой пользователь. Начните запись заново: /start")
            return
        except CalendarUnavailable:
            await callback.answer("Google Calendar недоступен", show_alert=True)
            return
        await state.clear()
        await callback.answer("Заявка отправлена")
        await callback.message.edit_text(
            format_request(request, settings.timezone, include_private=False)
            + f"\n\nСлот зарезервирован на {settings.hold_hours} часа."
        )
        admin_buttons = _keyboard(
            [[
                InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve:{request.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request.id}"),
            ]]
        )
        try:
            await bot.send_message(
                settings.admin_telegram_id,
                "<b>Новая заявка</b>\n\n" + format_request(request, settings.timezone),
                reply_markup=admin_buttons,
            )
        except Exception:
            LOGGER.exception("admin_notification_failed", extra={"request_id": request.id})

    @router.callback_query(F.data == "my")
    async def my_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_my_requests(callback.message, callback.from_user.id)

    @router.message(Command("my"))
    async def my_command(message: Message) -> None:
        if message.from_user:
            await show_my_requests(message, message.from_user.id)

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_request(callback: CallbackQuery, bot: Bot) -> None:
        request_id = int(callback.data.split(":", 1)[1])
        changed = await asyncio.to_thread(db.cancel_pending, request_id, callback.from_user.id)
        if not changed:
            await callback.answer("Заявку уже нельзя отменить", show_alert=True)
            return
        request = db.get_request(request_id)
        await callback.answer("Заявка отменена")
        if callback.message:
            await callback.message.edit_text(format_request(request, settings.timezone, include_private=False))
        try:
            await bot.send_message(settings.admin_telegram_id, f"Пользователь отменил заявку №{request_id}.")
        except Exception:
            LOGGER.exception("admin_cancellation_notification_failed", extra={"request_id": request_id})

    @router.callback_query(F.data == "admin:list")
    async def admin_list_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
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

    @router.callback_query(F.data.startswith("reject:"))
    async def reject_request(callback: CallbackQuery, bot: Bot) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        request_id = int(callback.data.split(":", 1)[1])
        request = await asyncio.to_thread(db.reject, request_id, callback.from_user.id)
        if request is None:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        await callback.answer("Заявка отклонена")
        if callback.message:
            await callback.message.edit_text(format_request(request, settings.timezone))
        try:
            await bot.send_message(
                request.telegram_id,
                format_request(request, settings.timezone, include_private=False) + "\n\nК сожалению, заявка не согласована.",
            )
        except Exception:
            LOGGER.exception("user_rejection_notification_failed", extra={"request_id": request.id})

    @router.callback_query(F.data.startswith("approve:"))
    async def approve_request(callback: CallbackQuery, bot: Bot) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
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
            approved = await asyncio.to_thread(db.complete_approval, request_id, callback.from_user.id, event_id)
        except CalendarUnavailable:
            await asyncio.to_thread(db.reset_approval, request_id, "google_unavailable")
            if callback.message:
                await callback.message.answer(
                    f"Google Calendar недоступен. Заявка №{request_id} осталась на согласовании; повторите позже."
                )
            return
        except Exception:
            LOGGER.exception("approval_failed", extra={"request_id": request_id})
            if callback.message:
                await callback.message.answer(
                    f"Неожиданная ошибка для заявки №{request_id}. Не нажимайте кнопку повторно до проверки лога."
                )
            return
        if callback.message:
            await callback.message.edit_text(format_request(approved, settings.timezone))
        try:
            await bot.send_message(
                approved.telegram_id,
                format_request(approved, settings.timezone, include_private=False)
                + "\n\nВстреча согласована. Приглашение отправлено на ваш email.",
            )
        except Exception:
            LOGGER.exception("user_approval_notification_failed", extra={"request_id": request_id})

    @router.callback_query(F.data == "abort")
    async def abort(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Запись отменена")
        if callback.message:
            await callback.message.edit_text("Запись отменена. Вернуться в меню: /start")

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.message()
    async def unknown_message(message: Message) -> None:
        await message.answer(
            "Я помогу выбрать время для встречи. Откройте меню командой /start."
        )

    return router
