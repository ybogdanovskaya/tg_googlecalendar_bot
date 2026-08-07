from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import SERIES_DAILY, SERIES_MONTHLY, SERIES_WEEKLY
from app.recurrence import generate_occurrences


class RecurrenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = ZoneInfo("Europe/Moscow")

    def test_daily_crosses_year_boundary(self) -> None:
        start = datetime(2026, 12, 30, 10, 0, tzinfo=self.zone)
        values = generate_occurrences(start, start + timedelta(minutes=30), SERIES_DAILY, date(2027, 1, 2))
        self.assertEqual([item[0].date() for item in values], [date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)])

    def test_weekly_keeps_local_time(self) -> None:
        start = datetime(2026, 8, 7, 9, 15, tzinfo=self.zone)
        values = generate_occurrences(start, start + timedelta(minutes=45), SERIES_WEEKLY, date(2026, 8, 30))
        self.assertEqual(len(values), 4)
        self.assertTrue(all(item[0].hour == 9 and item[0].minute == 15 for item in values))

    def test_monthly_skips_month_without_day(self) -> None:
        start = datetime(2027, 1, 31, 12, 0, tzinfo=self.zone)
        values = generate_occurrences(start, start + timedelta(minutes=30), SERIES_MONTHLY, date(2027, 5, 31))
        self.assertEqual([item[0].date() for item in values], [date(2027, 1, 31), date(2027, 3, 31), date(2027, 5, 31)])

    def test_leap_day_skips_non_leap_years(self) -> None:
        start = datetime(2028, 2, 29, 12, 0, tzinfo=self.zone)
        values = generate_occurrences(start, start + timedelta(minutes=30), SERIES_MONTHLY, date(2028, 5, 31))
        self.assertEqual([item[0].day for item in values], [29, 29, 29, 29])

    def test_series_is_limited_to_one_year(self) -> None:
        start = datetime(2026, 8, 7, 12, 0, tzinfo=self.zone)
        with self.assertRaises(ValueError):
            generate_occurrences(start, start + timedelta(minutes=30), SERIES_DAILY, start.date() + timedelta(days=367))


if __name__ == "__main__":
    unittest.main()
