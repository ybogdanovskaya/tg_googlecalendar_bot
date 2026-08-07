from __future__ import annotations

import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


MONTH_NAMES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def month_start(value: date) -> date:
    return value.replace(day=1)


def shift_month(value: date, offset: int) -> date:
    number = value.year * 12 + value.month - 1 + offset
    return date(number // 12, number % 12 + 1, 1)


def calendar_keyboard(
    prefix: str,
    shown_month: date,
    minimum: date,
    maximum: date,
) -> InlineKeyboardMarkup:
    shown = month_start(shown_month)
    min_month = month_start(minimum)
    max_month = month_start(maximum)
    previous = shift_month(shown, -1)
    following = shift_month(shown, 1)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="‹" if previous >= min_month else "·",
                callback_data=f"{prefix}:nav:{previous:%Y-%m}" if previous >= min_month else "datepick:noop",
            ),
            InlineKeyboardButton(text=f"{MONTH_NAMES[shown.month]} {shown.year}", callback_data="datepick:noop"),
            InlineKeyboardButton(
                text="›" if following <= max_month else "·",
                callback_data=f"{prefix}:nav:{following:%Y-%m}" if following <= max_month else "datepick:noop",
            ),
        ],
        [InlineKeyboardButton(text=item, callback_data="datepick:noop") for item in WEEKDAYS],
    ]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(shown.year, shown.month):
        row: list[InlineKeyboardButton] = []
        for day_number in week:
            if day_number == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="datepick:noop"))
                continue
            value = date(shown.year, shown.month, day_number)
            available = minimum <= value <= maximum
            row.append(
                InlineKeyboardButton(
                    text=str(day_number) if available else "·",
                    callback_data=f"{prefix}:day:{value.isoformat()}" if available else "datepick:noop",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="abort")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def month_from_callback(value: str) -> date:
    return date.fromisoformat(value + "-01")

