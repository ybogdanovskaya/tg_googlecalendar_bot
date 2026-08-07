from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.booking_rules import load_rules
from app.automation_store import AutomationStore
from app.bot import EMAIL_RE, _keyboard, cancel_keyboard, format_request, home_keyboard
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.date_picker import calendar_keyboard, month_from_callback
from app.db import Database, RequestNotEditableError, SlotConflictError
from app.input_parsing import parse_time_input
from app.models import CHANGE_CANCEL, CHANGE_RESCHEDULE, PENDING
from app.notification_rules import load_notification_rules


class ReleaseBFlow(StatesGroup):
    admin_value = State()
    alternative_date = State()
    alternative_time = State()
    move_date = State()
    move_time = State()
    manual_subject = State()
    manual_date = State()
    manual_time = State()
    manual_email = State()
    manual_description = State()
    manual_location = State()
    manual_review = State()
    manual_edit_date = State()
    manual_edit_value = State()


LOGGER = logging.getLogger(__name__)


def create_release_b_router(
    settings: Settings,
    db: Database,
    calendar: CalendarClient,
    automation: AutomationStore | None = None,
) -> Router:
    router = Router(name="release-b")
    zone = ZoneInfo(settings.timezone)

    def is_admin(user_id: int) -> bool:
        return user_id == settings.admin_telegram_id

    async def require_admin(callback: CallbackQuery) -> bool:
        if is_admin(callback.from_user.id):
            return True
        await callback.answer("Нет доступа", show_alert=True)
        return False

    def parse_date(value: str) -> datetime:
        parsed = datetime.strptime(value.strip(), "%d.%m.%Y").date()
        return datetime.combine(parsed, time.min, zone)

    def parse_start(day: str, value: str) -> datetime:
        parsed_time = parse_time_input(value)
        return datetime.combine(datetime.fromisoformat(day).date(), parsed_time, zone)

    async def show_date_picker(
        message: Message,
        prefix: str,
        prompt: str,
        minimum: datetime,
        maximum: datetime,
    ) -> None:
        await message.answer(
            prompt,
            reply_markup=calendar_keyboard(prefix, minimum.date(), minimum.date(), maximum.date()),
        )

    @router.callback_query(F.data.startswith("bdate:"))
    async def date_picker_choice(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            _, kind, action, raw_value = callback.data.split(":", 3)
            expected_states = {
                "alternative": ReleaseBFlow.alternative_date.state,
                "move": ReleaseBFlow.move_date.state,
                "manual": ReleaseBFlow.manual_date.state,
                "manualedit": ReleaseBFlow.manual_edit_date.state,
            }
            if kind not in expected_states or await state.get_state() != expected_states[kind]:
                raise ValueError
            if kind in {"alternative", "manual", "manualedit"} and not is_admin(callback.from_user.id):
                await callback.answer("Нет доступа", show_alert=True)
                return
            today = datetime.now(zone).date()
            if kind in {"alternative", "move"}:
                horizon = load_rules(db, settings).booking_horizon_days
                minimum, maximum = today, today + timedelta(days=horizon - 1)
            else:
                minimum, maximum = today, today + timedelta(days=366)
            if action == "nav":
                shown = month_from_callback(raw_value)
                if not date(minimum.year, minimum.month, 1) <= shown <= date(maximum.year, maximum.month, 1):
                    raise ValueError
                await callback.answer()
                if callback.message:
                    await callback.message.edit_reply_markup(
                        reply_markup=calendar_keyboard(f"bdate:{kind}", shown, minimum, maximum)
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
        if kind == "manualedit":
            data = await state.get_data()
            try:
                current = datetime.fromisoformat(str(data["start_at"])).astimezone(zone)
                changed = datetime.combine(selected, current.timetz().replace(tzinfo=None), zone)
                if changed <= datetime.now(zone):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                await callback.answer("Выберите будущее время встречи.", show_alert=True)
                return
            await state.update_data(day=selected.isoformat(), start_at=changed.isoformat())
            await state.set_state(ReleaseBFlow.manual_review)
            await callback.answer(f"Выбрано: {selected:%d.%m.%Y}")
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
                await render_manual_review(callback.message, state)
            return
        await state.update_data(day=selected.isoformat())
        next_states = {
            "alternative": ReleaseBFlow.alternative_time,
            "move": ReleaseBFlow.move_time,
            "manual": ReleaseBFlow.manual_time,
        }
        await state.set_state(next_states[kind])
        await callback.answer(f"Выбрано: {selected:%d.%m.%Y}")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "Введите время, например 930 или 1430 (МСК):",
                reply_markup=cancel_keyboard(),
            )

    def validate_user_time(start: datetime) -> None:
        rules = load_rules(db, settings)
        today = datetime.now(zone).date()
        if start < datetime.now(zone) + timedelta(minutes=rules.min_lead_minutes):
            raise ValueError("Слишком близкое время")
        if start.date() >= today + timedelta(days=rules.booking_horizon_days):
            raise ValueError("Дата вне горизонта записи")

    async def notify_change_admin(bot: Bot, change_id: int) -> None:
        change = db.get_change_request(change_id)
        request = db.get_request(change.request_id) if change else None
        if change is None or request is None:
            return
        kind = "отмену" if change.change_type == CHANGE_CANCEL else "перенос"
        text = f"<b>Запрошен {kind} встречи</b>\n\n" + format_request(request, settings.timezone)
        if change.proposed_start_at and change.proposed_end_at:
            start = change.proposed_start_at.astimezone(zone)
            end = change.proposed_end_at.astimezone(zone)
            text += f"\nПредложено: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)"
        await bot.send_message(
            settings.admin_telegram_id,
            text,
            reply_markup=_keyboard(
                [[
                    InlineKeyboardButton(text="✅ Выполнить", callback_data=f"b:change:approve:{change.id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"b:change:reject:{change.id}"),
                ]]
            ),
        )

    @router.callback_query(F.data.startswith("b:admin:req:"))
    async def admin_request_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        request_id = int(callback.data.rsplit(":", 1)[1])
        request = db.get_request(request_id)
        if request is None or request.status != PENDING:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                format_request(request, settings.timezone)
                + "\n\nМожно изменить текстовые поля сразу или предложить пользователю другое время.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="📝 Тема", callback_data=f"b:admin:field:{request_id}:subject")],
                        [
                            InlineKeyboardButton(text="📄 Описание", callback_data=f"b:admin:field:{request_id}:description"),
                            InlineKeyboardButton(text="📍 Место/ссылка", callback_data=f"b:admin:field:{request_id}:location"),
                        ],
                        [InlineKeyboardButton(text="🕐 Предложить время", callback_data=f"b:alt:start:{request_id}")],
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("b:admin:field:"))
    async def admin_field(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        _, _, _, request_raw, field = callback.data.split(":", 4)
        if field not in {"subject", "description", "location"}:
            await callback.answer("Поле недоступно", show_alert=True)
            return
        await state.set_state(ReleaseBFlow.admin_value)
        await state.set_data({"request_id": int(request_raw), "field": field})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Введите новое значение. Для очистки описания или места отправьте дефис «-».",
                reply_markup=cancel_keyboard(),
            )

    @router.message(ReleaseBFlow.admin_value)
    async def admin_value(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        field = str(data["field"])
        value = (message.text or "").strip()
        if field == "subject" and (not value or len(value) > 200):
            await message.answer("Тема должна содержать от 1 до 200 символов.")
            return
        limits = {"description": 2000, "location": 500}
        if field in limits and len(value) > limits[field]:
            await message.answer("Значение слишком длинное.")
            return
        normalized = None if field != "subject" and value == "-" else value
        try:
            request = await asyncio.to_thread(
                db.admin_update_pending_details,
                int(data["request_id"]),
                message.from_user.id,
                {field: normalized},
            )
        except RequestNotEditableError:
            await state.clear()
            await message.answer("Заявка уже обработана.", reply_markup=home_keyboard())
            return
        await state.clear()
        await message.answer("Изменение сохранено.\n\n" + format_request(request, settings.timezone), reply_markup=home_keyboard())
        await bot.send_message(
            request.telegram_id,
            "Администратор уточнил данные заявки.\n\n" + format_request(request, settings.timezone, include_private=False),
            reply_markup=home_keyboard(),
        )

    @router.callback_query(F.data.startswith("b:alt:start:"))
    async def alternative_start(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        request_id = int(callback.data.rsplit(":", 1)[1])
        durations = load_rules(db, settings).durations
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите длительность альтернативы:",
                reply_markup=_keyboard(
                    [[InlineKeyboardButton(text=f"{item} мин", callback_data=f"b:alt:dur:{request_id}:{item}")] for item in durations]
                    + [[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]
                ),
            )

    @router.callback_query(F.data.startswith("b:alt:dur:"))
    async def alternative_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        _, _, _, request_raw, duration_raw = callback.data.split(":", 4)
        await state.set_state(ReleaseBFlow.alternative_date)
        await state.set_data({"request_id": int(request_raw), "duration": int(duration_raw)})
        await callback.answer()
        if callback.message:
            today = datetime.now(zone)
            horizon = load_rules(db, settings).booking_horizon_days
            await show_date_picker(
                callback.message,
                "bdate:alternative",
                "Выберите дату альтернативы:",
                today,
                today + timedelta(days=horizon - 1),
            )

    @router.message(ReleaseBFlow.alternative_date)
    async def alternative_date(message: Message, state: FSMContext) -> None:
        try:
            day = parse_date(message.text or "")
            if day.date() < datetime.now(zone).date():
                raise ValueError
        except ValueError:
            await message.answer("Введите будущую дату в формате ДД.ММ.ГГГГ.")
            return
        await state.update_data(day=day.date().isoformat())
        await state.set_state(ReleaseBFlow.alternative_time)
        await message.answer("Введите время по Москве, например 930 или 1430:", reply_markup=cancel_keyboard())

    @router.message(ReleaseBFlow.alternative_time)
    async def alternative_time(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        try:
            start = parse_start(str(data["day"]), message.text or "")
            validate_user_time(start)
        except ValueError as exc:
            await message.answer(f"Время недоступно: {exc or 'введите, например, 930 или 1430'}.")
            return
        end = start + timedelta(minutes=int(data["duration"]))
        try:
            if not await calendar.is_free(start, end):
                raise SlotConflictError("Google slot busy")
            alternative = await asyncio.to_thread(
                db.create_alternative,
                int(data["request_id"]),
                message.from_user.id,
                start,
                end,
                load_rules(db, settings).hold_hours,
            )
        except (SlotConflictError, CalendarUnavailable):
            await message.answer("Это время занято или календарь временно недоступен. Введите другое время.")
            return
        except (RequestNotEditableError, ValueError):
            await state.clear()
            await message.answer(
                "Нельзя добавить альтернативу: заявка обработана или уже сохранены три варианта.",
                reply_markup=home_keyboard(),
            )
            return
        await state.clear()
        request = db.get_request(alternative.request_id)
        count = len(db.list_offered_alternatives(alternative.request_id))
        rows = []
        if count < 3:
            rows.append(
                [InlineKeyboardButton(text="➕ Добавить ещё", callback_data=f"b:alt:start:{alternative.request_id}")]
            )
        rows.append(
            [InlineKeyboardButton(text="📨 Отправить пользователю", callback_data=f"b:alt:send:{alternative.request_id}")]
        )
        await message.answer(
            f"Альтернатива {count}/3 сохранена: {start:%d.%m.%Y %H:%M}–{end:%H:%M}.",
            reply_markup=_keyboard(rows),
        )

    @router.callback_query(F.data.startswith("b:alt:send:"))
    async def send_alternatives(callback: CallbackQuery, bot: Bot) -> None:
        if not await require_admin(callback):
            return
        request_id = int(callback.data.rsplit(":", 1)[1])
        request = db.get_request(request_id)
        alternatives = db.list_offered_alternatives(request_id)
        if request is None or not alternatives:
            await callback.answer("Нет активных альтернатив", show_alert=True)
            return
        rows = []
        for item in alternatives:
            start = item.start_at.astimezone(zone)
            end = item.end_at.astimezone(zone)
            rows.append([InlineKeyboardButton(text=f"{start:%d.%m %H:%M}–{end:%H:%M}", callback_data=f"b:alt:accept:{item.id}")])
        rows.append([InlineKeyboardButton(text="Не подходит", callback_data=f"b:alt:decline:{request_id}")])
        await bot.send_message(
            request.telegram_id,
            "Администратор предложил другое время. Выберите подходящий вариант:",
            reply_markup=_keyboard(rows),
        )
        await callback.answer("Отправлено")

    @router.callback_query(F.data.startswith("b:alt:accept:"))
    async def accept_alternative(callback: CallbackQuery, bot: Bot) -> None:
        alternative_id = int(callback.data.rsplit(":", 1)[1])
        alternative = db.get_alternative(alternative_id)
        if alternative is None:
            await callback.answer("Вариант недоступен", show_alert=True)
            return
        try:
            if not await calendar.is_free(alternative.start_at, alternative.end_at):
                raise SlotConflictError
            request = await asyncio.to_thread(
                db.accept_alternative,
                alternative_id,
                callback.from_user.id,
                load_rules(db, settings).hold_hours,
            )
        except (SlotConflictError, RequestNotEditableError, CalendarUnavailable):
            await callback.answer("Вариант уже недоступен", show_alert=True)
            return
        await callback.answer("Новое время подтверждено")
        if callback.message:
            await callback.message.edit_text("Новое время подтверждено.\n\n" + format_request(request, settings.timezone, include_private=False), reply_markup=home_keyboard())
        await bot.send_message(
            settings.admin_telegram_id,
            f"Пользователь подтвердил альтернативу для заявки №{request.id}.\n\n"
            + format_request(request, settings.timezone),
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

    @router.callback_query(F.data.startswith("b:alt:decline:"))
    async def decline_alternatives(callback: CallbackQuery, bot: Bot) -> None:
        request_id = int(callback.data.rsplit(":", 1)[1])
        try:
            await asyncio.to_thread(db.decline_alternatives, request_id, callback.from_user.id)
        except RequestNotEditableError:
            await callback.answer("Предложение уже недоступно", show_alert=True)
            return
        await callback.answer("Ответ отправлен")
        if callback.message:
            await callback.message.edit_text("Предложенные варианты отклонены.", reply_markup=home_keyboard())
        await bot.send_message(settings.admin_telegram_id, f"Пользователь отклонил альтернативы для заявки №{request_id}.")

    @router.callback_query(F.data.startswith("b:cancel:") & ~F.data.startswith("b:cancel:confirm:"))
    async def ask_cancel(callback: CallbackQuery) -> None:
        request_id = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отправить администратору запрос на отмену этой встречи? Событие не удалится без решения администратора.",
                reply_markup=_keyboard([[InlineKeyboardButton(text="Да, запросить", callback_data=f"b:cancel:confirm:{request_id}")], [InlineKeyboardButton(text="Нет", callback_data="home")]]),
            )

    @router.callback_query(F.data.startswith("b:cancel:confirm:"))
    async def confirm_cancel(callback: CallbackQuery, bot: Bot) -> None:
        request_id = int(callback.data.rsplit(":", 1)[1])
        try:
            change = await asyncio.to_thread(db.create_change_request, request_id, callback.from_user.id, CHANGE_CANCEL)
        except RequestNotEditableError:
            await callback.answer("Запрос уже создан или встреча недоступна", show_alert=True)
            return
        await notify_change_admin(bot, change.id)
        await callback.answer("Запрос отправлен")
        if callback.message:
            await callback.message.edit_text("Запрос на отмену отправлен. Встреча пока остаётся в календаре.", reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("b:move:") & ~F.data.startswith("b:move:dur:"))
    async def move_start(callback: CallbackQuery) -> None:
        request_id = int(callback.data.rsplit(":", 1)[1])
        durations = load_rules(db, settings).durations
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новую длительность:",
                reply_markup=_keyboard([[InlineKeyboardButton(text=f"{item} мин", callback_data=f"b:move:dur:{request_id}:{item}")] for item in durations] + [[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]),
            )

    @router.callback_query(F.data.startswith("b:move:dur:"))
    async def move_duration(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, _, request_raw, duration_raw = callback.data.split(":", 4)
        await state.set_state(ReleaseBFlow.move_date)
        await state.set_data({"request_id": int(request_raw), "duration": int(duration_raw)})
        await callback.answer()
        if callback.message:
            today = datetime.now(zone)
            horizon = load_rules(db, settings).booking_horizon_days
            await show_date_picker(
                callback.message,
                "bdate:move",
                "Выберите новую дату:",
                today,
                today + timedelta(days=horizon - 1),
            )

    @router.message(ReleaseBFlow.move_date)
    async def move_date(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            await state.clear()
            return
        try:
            day = parse_date(message.text or "")
        except ValueError:
            await message.answer("Введите дату в формате ДД.ММ.ГГГГ.")
            return
        await state.update_data(day=day.date().isoformat())
        await state.set_state(ReleaseBFlow.move_time)
        await message.answer("Введите новое время, например 930 или 1430 (МСК):", reply_markup=cancel_keyboard())

    @router.message(ReleaseBFlow.move_time)
    async def move_time(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            await state.clear()
            return
        data = await state.get_data()
        try:
            start = parse_start(str(data["day"]), message.text or "")
            validate_user_time(start)
        except ValueError as exc:
            await message.answer(f"Время недоступно: {exc or 'проверьте формат'}.")
            return
        end = start + timedelta(minutes=int(data["duration"]))
        request = db.get_request(int(data["request_id"]))
        try:
            if request is None or not await calendar.is_free(start, end):
                raise SlotConflictError
            local_conflicts = await asyncio.to_thread(db.active_intervals, start, end, request.id)
            if local_conflicts:
                raise SlotConflictError
            change = await asyncio.to_thread(db.create_change_request, request.id, message.from_user.id, CHANGE_RESCHEDULE, start, end)
        except (SlotConflictError, RequestNotEditableError, CalendarUnavailable):
            await message.answer("Слот занят, календарь недоступен или запрос уже существует.")
            return
        await state.clear()
        await notify_change_admin(bot, change.id)
        await message.answer("Запрос на перенос отправлен. Старое время остаётся действующим до решения администратора.", reply_markup=home_keyboard())

    @router.callback_query(F.data == "b:changes")
    async def list_changes(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        changes = db.list_pending_changes()
        await callback.answer()
        if callback.message:
            if not changes:
                await callback.message.answer("Нет запросов на перенос или отмену.", reply_markup=home_keyboard())
            for change in changes:
                request = db.get_request(change.request_id)
                if request:
                    await callback.message.answer(
                        ("Отмена" if change.change_type == CHANGE_CANCEL else "Перенос") + f" — запрос №{change.id}\n\n" + format_request(request, settings.timezone),
                        reply_markup=_keyboard([[InlineKeyboardButton(text="✅ Выполнить", callback_data=f"b:change:approve:{change.id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"b:change:reject:{change.id}")]]),
                    )

    @router.callback_query(F.data.startswith("b:change:reject:"))
    async def reject_change(callback: CallbackQuery, bot: Bot) -> None:
        if not await require_admin(callback):
            return
        change = await asyncio.to_thread(db.reject_change, int(callback.data.rsplit(":", 1)[1]), callback.from_user.id)
        if change is None:
            await callback.answer("Запрос уже обработан", show_alert=True)
            return
        await callback.answer("Отклонено")
        if callback.message:
            await callback.message.edit_text("Запрос отклонён.", reply_markup=home_keyboard())
        await bot.send_message(change.requested_by, "Запрос на изменение встречи отклонён.", reply_markup=home_keyboard())

    @router.callback_query(F.data.startswith("b:change:approve:"))
    async def approve_change(callback: CallbackQuery, bot: Bot) -> None:
        if not await require_admin(callback):
            return
        change_id = int(callback.data.rsplit(":", 1)[1])
        change = await asyncio.to_thread(db.claim_change, change_id, callback.from_user.id)
        if change is None:
            await callback.answer("Запрос уже обработан", show_alert=True)
            return
        request = db.get_request(change.request_id)
        try:
            if request is None or not request.google_event_id:
                raise CalendarUnavailable("event id missing")
            if change.change_type == CHANGE_CANCEL:
                # Отсутствующее событие уже соответствует цели отмены; операция идемпотентна.
                await calendar.delete_event(request.google_event_id)
            else:
                moved = replace(request, start_at=change.proposed_start_at, end_at=change.proposed_end_at)
                await calendar.update_event(moved)
            _, updated = await asyncio.to_thread(db.complete_change, change.id, callback.from_user.id)
            if automation:
                try:
                    if change.change_type == CHANGE_CANCEL:
                        await asyncio.to_thread(automation.cancel_request_jobs, updated.id)
                    else:
                        notification_rules = load_notification_rules(db, settings)
                        await asyncio.to_thread(
                            automation.rebuild_request_reminders,
                            updated.id,
                            settings.admin_telegram_id,
                            notification_rules.reminder_minutes,
                        )
                except Exception:
                    LOGGER.exception("change_reminder_rebuild_failed", extra={"request_id": updated.id})
        except Exception:
            LOGGER.exception("meeting_change_approval_failed", extra={"change_id": change.id})
            await asyncio.to_thread(db.reset_change, change.id, "google_error")
            await callback.answer("Google Calendar недоступен; запрос сохранён", show_alert=True)
            return
        await callback.answer("Выполнено")
        if callback.message:
            await callback.message.edit_text("Изменение выполнено.\n\n" + format_request(updated, settings.timezone), reply_markup=home_keyboard())
        await bot.send_message(updated.telegram_id, "Изменение встречи согласовано.\n\n" + format_request(updated, settings.timezone, include_private=False), reply_markup=home_keyboard())

    async def ask_manual_description(target: Message, state: FSMContext) -> None:
        await state.set_state(ReleaseBFlow.manual_description)
        await target.answer(
            "Введите описание встречи или нажмите «Пропустить»: ",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="Пропустить", callback_data="b:manual:skip:description")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    async def ask_manual_location(target: Message, state: FSMContext) -> None:
        await state.set_state(ReleaseBFlow.manual_location)
        await target.answer(
            "Введите адрес или ссылку на встречу либо нажмите «Пропустить»: ",
            reply_markup=_keyboard(
                [
                    [InlineKeyboardButton(text="Пропустить", callback_data="b:manual:skip:location")],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    async def ask_manual_blocking(target: Message, state: FSMContext) -> None:
        await state.set_state(ReleaseBFlow.manual_review)
        await target.answer(
            "Должна ли встреча блокировать это время для заявок пользователей?",
            reply_markup=_keyboard(
                [
                    [
                        InlineKeyboardButton(text="🔒 Да, время занято", callback_data="b:manual:block:1"),
                        InlineKeyboardButton(text="🟢 Нет, время свободно", callback_data="b:manual:block:0"),
                    ],
                    [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                ]
            ),
        )

    def manual_summary(data: dict[str, object]) -> str:
        start = datetime.fromisoformat(str(data["start_at"])).astimezone(zone)
        end = start + timedelta(minutes=int(data["duration"]))
        guest = html.escape(str(data.get("email") or "только я"))
        blocking = "да" if bool(data.get("blocks_calendar")) else "нет"
        lines = [
            "<b>Проверьте встречу</b>",
            f"Тема: {html.escape(str(data['subject']))}",
            f"Время: {start:%d.%m.%Y %H:%M}–{end:%H:%M} (МСК)",
            f"Участник: {guest}",
            f"Блокирует время: {blocking}",
        ]
        if data.get("description"):
            lines.append(f"Описание: {html.escape(str(data['description']))}")
        if data.get("location"):
            lines.append(f"Место/ссылка: {html.escape(str(data['location']))}")
        return "\n".join(lines)

    async def manual_conflict(start: datetime, end: datetime) -> bool:
        local = await asyncio.to_thread(db.active_intervals, start, end)
        if local:
            return True
        return not await calendar.is_free(start, end)

    async def render_manual_review(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            start = datetime.fromisoformat(str(data["start_at"]))
            end = start + timedelta(minutes=int(data["duration"]))
            conflict = await manual_conflict(start, end)
        except (KeyError, TypeError, ValueError):
            await state.clear()
            await message.answer("Черновик устарел. Начните создание встречи заново.", reply_markup=home_keyboard())
            return
        except CalendarUnavailable:
            await message.answer(
                "Google Calendar временно недоступен. Черновик сохранён — попробуйте вернуться к проверке ещё раз.",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="b:manual:edit:review")],
                        [InlineKeyboardButton(text="✏️ Изменить", callback_data="b:manual:edit")],
                        [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                    ]
                ),
            )
            return
        if conflict:
            note = "\n\n⚠️ Время пересекается с другой встречей. Создание возможно только после отдельного подтверждения."
            rows = [[InlineKeyboardButton(text="⚠️ Создать с пересечением", callback_data="b:manual:create:1")]]
        else:
            note = "\n\nПересечений не найдено."
            rows = [[InlineKeyboardButton(text="✅ Создать встречу", callback_data="b:manual:create:0")]]
        rows.extend(
            [
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="b:manual:edit")],
                [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
            ]
        )
        await state.set_state(ReleaseBFlow.manual_review)
        await message.answer(manual_summary(data) + note, reply_markup=_keyboard(rows))

    @router.callback_query(F.data == "b:manual")
    async def manual_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.clear()
        await state.set_state(ReleaseBFlow.manual_subject)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите тему новой встречи:", reply_markup=cancel_keyboard())

    @router.message(ReleaseBFlow.manual_subject)
    async def manual_subject(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        value = (message.text or "").strip()
        if not value or len(value) > 200:
            await message.answer("Тема должна содержать от 1 до 200 символов.")
            return
        await state.update_data(subject=value)
        await state.set_state(ReleaseBFlow.manual_date)
        today = datetime.now(zone)
        await show_date_picker(
            message,
            "bdate:manual",
            "Выберите дату встречи:",
            today,
            today + timedelta(days=366),
        )

    @router.message(ReleaseBFlow.manual_date)
    async def manual_date(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        try:
            day = parse_date(message.text or "")
            if day.date() < datetime.now(zone).date():
                raise ValueError
        except ValueError:
            await message.answer("Введите сегодняшнюю или будущую дату в формате ДД.ММ.ГГГГ.")
            return
        await state.update_data(day=day.date().isoformat())
        await state.set_state(ReleaseBFlow.manual_time)
        await message.answer("Введите время начала, например 930 или 1430 (МСК):", reply_markup=cancel_keyboard())

    @router.message(ReleaseBFlow.manual_time)
    async def manual_time(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        try:
            start = parse_start(str(data["day"]), message.text or "")
            if start <= datetime.now(zone):
                raise ValueError
        except ValueError:
            await message.answer("Введите будущее время, например 930 или 1430. Для администратора минимального срока нет.")
            return
        await state.update_data(start_at=start.isoformat())
        durations = load_rules(db, settings).durations
        await message.answer(
            "Выберите длительность:",
            reply_markup=_keyboard(
                [[InlineKeyboardButton(text=f"{item} мин", callback_data=f"b:manual:duration:{item}")] for item in durations]
                + [[InlineKeyboardButton(text="✖ Отменить", callback_data="abort")]]
            ),
        )

    @router.callback_query(F.data.startswith("b:manual:duration:"))
    async def manual_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        duration = int(callback.data.rsplit(":", 1)[1])
        if duration not in load_rules(db, settings).durations:
            await callback.answer("Длительность недоступна", show_alert=True)
            return
        await state.update_data(duration=duration)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Добавить гостя по email?",
                reply_markup=_keyboard(
                    [
                        [
                            InlineKeyboardButton(text="✉️ Ввести email", callback_data="b:manual:guest:email"),
                            InlineKeyboardButton(text="👤 Только я", callback_data="b:manual:guest:none"),
                        ],
                        [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                    ]
                ),
            )

    @router.callback_query(F.data == "b:manual:guest:email")
    async def manual_guest_email(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.set_state(ReleaseBFlow.manual_email)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите email гостя:", reply_markup=cancel_keyboard())

    @router.callback_query(F.data == "b:manual:guest:none")
    async def manual_guest_none(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(email=None)
        await callback.answer()
        if callback.message:
            await ask_manual_description(callback.message, state)

    @router.message(ReleaseBFlow.manual_email)
    async def manual_email(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        value = (message.text or "").strip()
        if len(value) > 254 or not EMAIL_RE.fullmatch(value):
            await message.answer("Введите корректный email.")
            return
        await state.update_data(email=value)
        await ask_manual_description(message, state)

    @router.callback_query(F.data == "b:manual:skip:description")
    async def manual_description_skip(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(description=None)
        await callback.answer()
        if callback.message:
            await ask_manual_location(callback.message, state)

    @router.message(ReleaseBFlow.manual_description)
    async def manual_description(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        value = (message.text or "").strip()
        if len(value) > 2000:
            await message.answer("Описание не должно превышать 2000 символов.")
            return
        await state.update_data(description=value or None)
        await ask_manual_location(message, state)

    @router.callback_query(F.data == "b:manual:skip:location")
    async def manual_location_skip(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(location=None)
        await callback.answer()
        if callback.message:
            await ask_manual_blocking(callback.message, state)

    @router.message(ReleaseBFlow.manual_location)
    async def manual_location(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        value = (message.text or "").strip()
        if len(value) > 500:
            await message.answer("Адрес или ссылка не должны превышать 500 символов.")
            return
        await state.update_data(location=value or None)
        await ask_manual_blocking(message, state)

    @router.callback_query(F.data.startswith("b:manual:block:"))
    async def manual_blocking(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        blocks_calendar = callback.data.rsplit(":", 1)[1] == "1"
        await state.update_data(blocks_calendar=blocks_calendar)
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await render_manual_review(callback.message, state)

    @router.callback_query(F.data == "b:manual:edit")
    async def manual_edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        data = await state.get_data()
        required = {"subject", "day", "start_at", "duration", "blocks_calendar"}
        if not required.issubset(data):
            await callback.answer("Черновик устарел. Начните создание встречи заново.", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Что изменить в черновике встречи?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="📝 Тема", callback_data="b:manual:edit:value:subject")],
                        [
                            InlineKeyboardButton(text="📅 Дата", callback_data="b:manual:edit:date"),
                            InlineKeyboardButton(text="🕐 Время", callback_data="b:manual:edit:value:time"),
                        ],
                        [InlineKeyboardButton(text="⏱ Длительность", callback_data="b:manual:edit:duration")],
                        [InlineKeyboardButton(text="✉️ Участник", callback_data="b:manual:edit:email")],
                        [
                            InlineKeyboardButton(text="📄 Описание", callback_data="b:manual:edit:value:description"),
                            InlineKeyboardButton(text="📍 Место", callback_data="b:manual:edit:value:location"),
                        ],
                        [InlineKeyboardButton(text="🔒 Занято/свободно", callback_data="b:manual:edit:block")],
                        [InlineKeyboardButton(text="← К проверке", callback_data="b:manual:edit:review")],
                    ]
                ),
            )

    @router.callback_query(F.data == "b:manual:edit:review")
    async def manual_edit_review(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await render_manual_review(callback.message, state)

    @router.callback_query(F.data == "b:manual:edit:date")
    async def manual_edit_date(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        today = datetime.now(zone)
        await state.set_state(ReleaseBFlow.manual_edit_date)
        await callback.answer()
        if callback.message:
            await show_date_picker(
                callback.message,
                "bdate:manualedit",
                "Выберите новую дату встречи:",
                today,
                today + timedelta(days=366),
            )

    @router.callback_query(F.data.startswith("b:manual:edit:value:"))
    async def manual_edit_value_prompt(callback: CallbackQuery, state: FSMContext) -> None:
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
        await state.update_data(manual_edit_field=field)
        await state.set_state(ReleaseBFlow.manual_edit_value)
        await callback.answer()
        if callback.message:
            await callback.message.answer(prompts[field], reply_markup=cancel_keyboard())

    @router.message(ReleaseBFlow.manual_edit_value)
    async def manual_edit_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not is_admin(message.from_user.id):
            await state.clear()
            return
        data = await state.get_data()
        field = str(data.get("manual_edit_field") or "")
        raw = (message.text or "").strip()
        try:
            if field == "subject":
                if not 1 <= len(raw) <= 200:
                    raise ValueError
                updates: dict[str, object] = {"subject": raw}
            elif field == "time":
                start = parse_start(str(data["day"]), raw)
                if start <= datetime.now(zone):
                    raise ValueError
                updates = {"start_at": start.isoformat()}
            elif field == "email":
                email = raw.lower()
                if len(email) > 254 or not EMAIL_RE.fullmatch(email):
                    raise ValueError
                updates = {"email": email}
            elif field == "description":
                if len(raw) > 2000:
                    raise ValueError
                updates = {"description": None if raw == "-" else raw or None}
            elif field == "location":
                if len(raw) > 500:
                    raise ValueError
                updates = {"location": None if raw == "-" else raw or None}
            else:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            await message.answer("Значение не подходит. Проверьте формат и длину.", reply_markup=cancel_keyboard())
            return
        await state.update_data(**updates)
        await state.set_state(ReleaseBFlow.manual_review)
        await render_manual_review(message, state)

    @router.callback_query(F.data == "b:manual:edit:duration")
    async def manual_edit_duration(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Выберите новую длительность:",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text=f"{item} мин", callback_data=f"b:manual:edit:setduration:{item}")]
                        for item in load_rules(db, settings).durations
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("b:manual:edit:setduration:"))
    async def manual_edit_set_duration(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        try:
            duration = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Длительность недоступна", show_alert=True)
            return
        if duration not in load_rules(db, settings).durations:
            await callback.answer("Длительность недоступна", show_alert=True)
            return
        await state.update_data(duration=duration)
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await render_manual_review(callback.message, state)

    @router.callback_query(F.data == "b:manual:edit:email")
    async def manual_edit_email(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Изменить участника:",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="👤 Только я", callback_data="b:manual:edit:setemail:self")],
                        [InlineKeyboardButton(text="✉️ Ввести email", callback_data="b:manual:edit:value:email")],
                    ]
                ),
            )

    @router.callback_query(F.data == "b:manual:edit:setemail:self")
    async def manual_edit_email_self(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(email=None)
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await render_manual_review(callback.message, state)

    @router.callback_query(F.data == "b:manual:edit:block")
    async def manual_edit_block(callback: CallbackQuery) -> None:
        if not await require_admin(callback):
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Должна встреча блокировать время?",
                reply_markup=_keyboard(
                    [
                        [InlineKeyboardButton(text="🔒 Да", callback_data="b:manual:edit:setblock:1")],
                        [InlineKeyboardButton(text="🟢 Нет", callback_data="b:manual:edit:setblock:0")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("b:manual:edit:setblock:"))
    async def manual_edit_set_block(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        await state.update_data(blocks_calendar=callback.data.endswith(":1"))
        await callback.answer("Проверяю календарь…")
        if callback.message:
            await render_manual_review(callback.message, state)

    @router.callback_query(F.data.startswith("b:manual:create:"))
    async def manual_create(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_admin(callback):
            return
        allow_overlap = callback.data.rsplit(":", 1)[1] == "1"
        data = await state.get_data()
        try:
            start = datetime.fromisoformat(str(data["start_at"]))
            end = start + timedelta(minutes=int(data["duration"]))
        except (KeyError, TypeError, ValueError):
            await state.clear()
            await callback.answer("Черновик устарел. Начните создание заново.", show_alert=True)
            return
        if start <= datetime.now(zone):
            await state.clear()
            await callback.answer("Время начала уже прошло. Создайте встречу заново.", show_alert=True)
            return
        try:
            conflict = await manual_conflict(start, end)
            if conflict and not allow_overlap:
                await callback.answer("Слот уже занят. Подтвердите создание с пересечением.", show_alert=True)
                if callback.message:
                    await callback.message.answer(
                        manual_summary(data) + "\n\n⚠️ Пока вы проверяли данные, время стало занято.",
                        reply_markup=_keyboard(
                            [
                                [InlineKeyboardButton(text="⚠️ Создать с пересечением", callback_data="b:manual:create:1")],
                                [InlineKeyboardButton(text="✖ Отменить", callback_data="abort")],
                            ]
                        ),
                    )
                return
            draft = await asyncio.to_thread(
                db.create_admin_draft,
                admin_id=callback.from_user.id,
                admin_name=callback.from_user.full_name,
                admin_username=callback.from_user.username,
                email=data.get("email"),
                subject=str(data["subject"]),
                description=data.get("description"),
                location=data.get("location"),
                start_at=start,
                end_at=end,
                blocks_calendar=bool(data.get("blocks_calendar")),
                allow_overlap=allow_overlap,
            )
            try:
                event_id = await calendar.create_event(draft)
                created = await asyncio.to_thread(db.complete_approval, draft.id, callback.from_user.id, event_id)
                if automation:
                    try:
                        notification_rules = load_notification_rules(db, settings)
                        await asyncio.to_thread(
                            automation.rebuild_request_reminders,
                            created.id,
                            settings.admin_telegram_id,
                            notification_rules.reminder_minutes,
                        )
                    except Exception:
                        LOGGER.exception("manual_reminder_schedule_failed", extra={"request_id": created.id})
            except Exception:
                await asyncio.to_thread(db.fail_admin_draft, draft.id, "calendar_create_failed")
                raise
        except (CalendarUnavailable, SlotConflictError):
            await callback.answer("Не удалось создать встречу: календарь недоступен или слот занят.", show_alert=True)
            return
        except Exception:
            LOGGER.exception("admin_manual_meeting_failed")
            await callback.answer("Не удалось создать встречу. Ошибка записана в журнал.", show_alert=True)
            return
        await state.clear()
        await callback.answer("Встреча создана")
        if callback.message:
            guest_note = "Приглашение гостю отправлено Google Calendar." if created.email else "Встреча создана только для вас."
            await callback.message.edit_text(
                "Встреча создана в Google Calendar.\n\n" + format_request(created, settings.timezone) + f"\n\n{guest_note}",
                reply_markup=home_keyboard(),
            )

    return router
