from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from app.models import SERIES_DAILY, SERIES_MONTHLY, SERIES_WEEKLY


ALLOWED_FREQUENCIES = (SERIES_DAILY, SERIES_WEEKLY, SERIES_MONTHLY)
MAX_SERIES_DAYS = 366
MAX_OCCURRENCES = 367


def generate_occurrences(
    start_at: datetime,
    end_at: datetime,
    frequency: str,
    until_date: date,
) -> list[tuple[datetime, datetime]]:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("series datetimes must be timezone-aware")
    if end_at <= start_at:
        raise ValueError("series end must be after start")
    if frequency not in ALLOWED_FREQUENCIES:
        raise ValueError("unsupported series frequency")
    if until_date < start_at.date():
        raise ValueError("series end date precedes start")
    if until_date > start_at.date() + timedelta(days=MAX_SERIES_DAYS):
        raise ValueError("series is longer than one year")

    duration = end_at - start_at
    current = start_at
    result: list[tuple[datetime, datetime]] = []
    while current.date() <= until_date:
        result.append((current, current + duration))
        if len(result) > MAX_OCCURRENCES:
            raise ValueError("too many occurrences")
        if frequency == SERIES_DAILY:
            current += timedelta(days=1)
        elif frequency == SERIES_WEEKLY:
            current += timedelta(days=7)
        else:
            current = _next_month_with_day(current, start_at.day)
    return result


def _next_month_with_day(value: datetime, day: int) -> datetime:
    year = value.year
    month = value.month
    while True:
        month += 1
        if month == 13:
            month = 1
            year += 1
        if day <= calendar.monthrange(year, month)[1]:
            return value.replace(year=year, month=month, day=day)
