from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class SlotPeriod:
    key: str
    title: str
    start_minutes: int
    end_minutes: int


def booking_periods(window_start_minutes: int, window_end_minutes: int) -> tuple[SlotPeriod, ...]:
    if not 0 <= window_start_minutes < window_end_minutes <= 24 * 60:
        raise ValueError("invalid booking window")
    definitions = (
        ("morning", "🌅 Утро", 0, 12 * 60),
        ("day", "☀️ День", 12 * 60, 17 * 60),
        ("evening", "🌆 Вечер", 17 * 60, 24 * 60),
    )
    result: list[SlotPeriod] = []
    for key, title, period_start, period_end in definitions:
        start = max(window_start_minutes, period_start)
        end = min(window_end_minutes, period_end)
        if start < end:
            result.append(SlotPeriod(key, title, start, end))
    return tuple(result)


def slot_in_period(slot: datetime, period: SlotPeriod) -> bool:
    local_minutes = slot.hour * 60 + slot.minute
    return period.start_minutes <= local_minutes < period.end_minutes


def overlaps(start: datetime, end: datetime, intervals: list[Interval]) -> bool:
    return any(start < busy_end and end > busy_start for busy_start, busy_end in intervals)


def available_slots(
    *,
    local_date: date,
    duration_minutes: int,
    busy_intervals: list[Interval],
    now: datetime,
    timezone_name: str,
    min_lead_minutes: int,
    step_minutes: int = 15,
    window_start_minutes: int = 0,
    window_end_minutes: int = 24 * 60,
) -> list[datetime]:
    if not 0 <= window_start_minutes < window_end_minutes <= 24 * 60:
        raise ValueError("invalid booking window")
    zone = ZoneInfo(timezone_name)
    local_midnight = datetime.combine(local_date, time.min, zone)
    day_start = local_midnight + timedelta(minutes=window_start_minutes)
    day_end = local_midnight + timedelta(minutes=window_end_minutes)
    earliest = now.astimezone(zone) + timedelta(minutes=min_lead_minutes)
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    normalized_busy = [(start.astimezone(UTC), end.astimezone(UTC)) for start, end in busy_intervals]
    result: list[datetime] = []
    cursor = day_start
    while cursor + duration <= day_end:
        if cursor >= earliest:
            start_utc = cursor.astimezone(UTC)
            end_utc = (cursor + duration).astimezone(UTC)
            if not overlaps(start_utc, end_utc, normalized_busy):
                result.append(cursor)
        cursor += step
    return result
