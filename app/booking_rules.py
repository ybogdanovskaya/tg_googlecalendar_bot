from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.db import Database


BOOKING_ENABLED = "booking_enabled"
MIN_LEAD_MINUTES = "min_lead_minutes"
BOOKING_HORIZON_DAYS = "booking_horizon_days"
HOLD_HOURS = "hold_hours"
DURATIONS = "durations"
STEP_MINUTES = "step_minutes"
USER_BOOKING_WINDOW = "user_booking_window"
CLOSED_WEEKDAYS = "closed_weekdays"

ALLOWED_DURATIONS = (15, 30, 45, 60, 90)
ALLOWED_STEPS = (5, 10, 15, 30, 60)


@dataclass(frozen=True)
class BookingRules:
    booking_enabled: bool
    min_lead_minutes: int
    booking_horizon_days: int
    hold_hours: int
    durations: tuple[int, ...]
    step_minutes: int
    user_booking_start_minutes: int
    user_booking_end_minutes: int
    closed_weekdays: tuple[int, ...]


def defaults(settings: Settings) -> dict[str, Any]:
    return {
        BOOKING_ENABLED: True,
        MIN_LEAD_MINUTES: settings.min_lead_minutes,
        BOOKING_HORIZON_DAYS: settings.booking_horizon_days,
        HOLD_HOURS: settings.hold_hours,
        DURATIONS: list(ALLOWED_DURATIONS),
        STEP_MINUTES: 15,
        USER_BOOKING_WINDOW: [8 * 60, 21 * 60],
        CLOSED_WEEKDAYS: [],
    }


def format_clock_minutes(value: int) -> str:
    if not 0 <= value <= 24 * 60:
        raise ValueError("Недопустимое время")
    hours, minutes = divmod(value, 60)
    return f"{hours:02d}:{minutes:02d}"


def parse_booking_window(value: str) -> list[int]:
    parts = re.split(r"\s*[-–—]\s*", value.strip())
    if len(parts) != 2:
        raise ValueError("Введите интервал, например 0800-2100")

    def parse_clock(raw: str, *, allow_end_of_day: bool) -> int:
        normalized = raw.strip().replace(":", "")
        if not normalized.isdigit() or len(normalized) not in {1, 2, 3, 4}:
            raise ValueError("Время указывается как 0800 или 08:00")
        if len(normalized) <= 2:
            hours, minutes = int(normalized), 0
        else:
            hours, minutes = int(normalized[:-2]), int(normalized[-2:])
        if allow_end_of_day and hours == 24 and minutes == 0:
            return 24 * 60
        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            raise ValueError("Указано несуществующее время")
        return hours * 60 + minutes

    return validate_value(
        USER_BOOKING_WINDOW,
        [parse_clock(parts[0], allow_end_of_day=False), parse_clock(parts[1], allow_end_of_day=True)],
    )


def validate_value(key: str, value: Any) -> Any:
    if key == BOOKING_ENABLED:
        if not isinstance(value, bool):
            raise ValueError("Значение должно быть включено или выключено")
        return value
    if key == MIN_LEAD_MINUTES:
        number = int(value)
        if not 0 <= number <= 10080:
            raise ValueError("Допустимо от 0 до 10080 минут")
        return number
    if key == BOOKING_HORIZON_DAYS:
        number = int(value)
        if not 1 <= number <= 365:
            raise ValueError("Допустимо от 1 до 365 дней")
        return number
    if key == HOLD_HOURS:
        number = int(value)
        if not 1 <= number <= 168:
            raise ValueError("Допустимо от 1 до 168 часов")
        return number
    if key == STEP_MINUTES:
        number = int(value)
        if number not in ALLOWED_STEPS:
            raise ValueError("Недопустимый шаг слотов")
        return number
    if key == DURATIONS:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Нужен список длительностей")
        normalized = tuple(sorted({int(item) for item in value}))
        if not normalized or any(item not in ALLOWED_DURATIONS for item in normalized):
            raise ValueError("Должна остаться хотя бы одна допустимая длительность")
        return list(normalized)
    if key == USER_BOOKING_WINDOW:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("Нужны время начала и окончания")
        start, end = (int(item) for item in value)
        if not 0 <= start < end <= 24 * 60:
            raise ValueError("Начало должно быть раньше окончания")
        return [start, end]
    if key == CLOSED_WEEKDAYS:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Нужен список дней недели")
        normalized = tuple(sorted({int(item) for item in value}))
        if any(item not in range(7) for item in normalized):
            raise ValueError("Допустимы дни недели от 0 до 6")
        return list(normalized)
    raise ValueError("Неизвестная настройка")


def load_rules(db: Database, settings: Settings) -> BookingRules:
    values = defaults(settings)
    for key, value in db.get_settings().items():
        if key not in values:
            continue
        try:
            values[key] = validate_value(key, value)
        except (TypeError, ValueError):
            continue
    booking_window = values[USER_BOOKING_WINDOW]
    return BookingRules(
        booking_enabled=bool(values[BOOKING_ENABLED]),
        min_lead_minutes=int(values[MIN_LEAD_MINUTES]),
        booking_horizon_days=int(values[BOOKING_HORIZON_DAYS]),
        hold_hours=int(values[HOLD_HOURS]),
        durations=tuple(int(item) for item in values[DURATIONS]),
        step_minutes=int(values[STEP_MINUTES]),
        user_booking_start_minutes=int(booking_window[0]),
        user_booking_end_minutes=int(booking_window[1]),
        closed_weekdays=tuple(int(item) for item in values[CLOSED_WEEKDAYS]),
    )


def is_closed_date(selected_date: Any, closed_dates: set[str], rules: BookingRules) -> bool:
    """Return whether a date is closed by an explicit exception or weekday rule."""
    return selected_date.isoformat() in closed_dates or selected_date.weekday() in rules.closed_weekdays
