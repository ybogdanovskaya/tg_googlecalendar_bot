from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


Interval = tuple[datetime, datetime]


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
) -> list[datetime]:
    zone = ZoneInfo(timezone_name)
    day_start = datetime.combine(local_date, time.min, zone)
    day_end = day_start + timedelta(days=1)
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
