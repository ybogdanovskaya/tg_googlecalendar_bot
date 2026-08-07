from __future__ import annotations

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


def defaults(settings: Settings) -> dict[str, Any]:
    return {
        BOOKING_ENABLED: True,
        MIN_LEAD_MINUTES: settings.min_lead_minutes,
        BOOKING_HORIZON_DAYS: settings.booking_horizon_days,
        HOLD_HOURS: settings.hold_hours,
        DURATIONS: list(ALLOWED_DURATIONS),
        STEP_MINUTES: 15,
    }


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
    return BookingRules(
        booking_enabled=bool(values[BOOKING_ENABLED]),
        min_lead_minutes=int(values[MIN_LEAD_MINUTES]),
        booking_horizon_days=int(values[BOOKING_HORIZON_DAYS]),
        hold_hours=int(values[HOLD_HOURS]),
        durations=tuple(int(item) for item in values[DURATIONS]),
        step_minutes=int(values[STEP_MINUTES]),
    )
