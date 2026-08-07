from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.db import Database


REMINDER_MINUTES = "reminder_minutes"
PENDING_REMINDER_HOURS = "pending_reminder_hours"
AUTOMATION_ENABLED = "automation_enabled"
DEFAULT_REMINDER_MINUTES = (1440, 60, 5)


@dataclass(frozen=True)
class NotificationRules:
    reminder_minutes: tuple[int, ...]
    pending_reminder_hours: int
    automation_enabled: bool


def defaults(_: Settings) -> dict[str, Any]:
    return {
        REMINDER_MINUTES: list(DEFAULT_REMINDER_MINUTES),
        PENDING_REMINDER_HOURS: 24,
        AUTOMATION_ENABLED: True,
    }


def validate_value(key: str, value: Any) -> Any:
    if key == REMINDER_MINUTES:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Нужен список интервалов")
        normalized = tuple(sorted({int(item) for item in value}, reverse=True))
        if not normalized or len(normalized) > 5 or any(not 1 <= item <= 10080 for item in normalized):
            raise ValueError("Нужно от 1 до 5 интервалов в пределах 1–10080 минут")
        return list(normalized)
    if key == PENDING_REMINDER_HOURS:
        number = int(value)
        if not 1 <= number <= 168:
            raise ValueError("Допустимо от 1 до 168 часов")
        return number
    if key == AUTOMATION_ENABLED:
        if not isinstance(value, bool):
            raise ValueError("Значение должно быть включено или выключено")
        return value
    raise ValueError("Неизвестная настройка уведомлений")


def load_notification_rules(db: Database, settings: Settings) -> NotificationRules:
    values = defaults(settings)
    stored = db.get_settings()
    for key in values:
        if key not in stored:
            continue
        try:
            values[key] = validate_value(key, stored[key])
        except (TypeError, ValueError):
            continue
    return NotificationRules(
        reminder_minutes=tuple(int(item) for item in values[REMINDER_MINUTES]),
        pending_reminder_hours=int(values[PENDING_REMINDER_HOURS]),
        automation_enabled=bool(values[AUTOMATION_ENABLED]),
    )
