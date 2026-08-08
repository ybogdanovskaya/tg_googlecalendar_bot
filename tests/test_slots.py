from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from app.slots import available_slots, booking_periods, slot_in_period


class AvailableSlotsTests(unittest.TestCase):
    def test_slot_must_end_before_midnight(self) -> None:
        slots = available_slots(
            local_date=date(2026, 8, 8),
            duration_minutes=90,
            busy_intervals=[],
            now=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            timezone_name="Europe/Moscow",
            min_lead_minutes=0,
        )
        self.assertEqual(slots[-1].strftime("%H:%M"), "22:30")

    def test_busy_interval_is_excluded(self) -> None:
        # 09:00–10:00 Moscow equals 06:00–07:00 UTC.
        slots = available_slots(
            local_date=date(2026, 8, 8),
            duration_minutes=30,
            busy_intervals=[
                (
                    datetime(2026, 8, 8, 6, 0, tzinfo=UTC),
                    datetime(2026, 8, 8, 7, 0, tzinfo=UTC),
                )
            ],
            now=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            timezone_name="Europe/Moscow",
            min_lead_minutes=0,
        )
        values = {slot.strftime("%H:%M") for slot in slots}
        self.assertNotIn("08:45", values)
        self.assertNotIn("09:00", values)
        self.assertNotIn("09:30", values)
        self.assertIn("10:00", values)

    def test_minimum_lead_is_applied(self) -> None:
        slots = available_slots(
            local_date=date(2026, 8, 8),
            duration_minutes=15,
            busy_intervals=[],
            now=datetime(2026, 8, 8, 7, 7, tzinfo=UTC),  # 10:07 Moscow
            timezone_name="Europe/Moscow",
            min_lead_minutes=120,
        )
        self.assertEqual(slots[0].strftime("%H:%M"), "12:15")

    def test_user_window_limits_start_and_requires_meeting_to_finish_by_end(self) -> None:
        slots = available_slots(
            local_date=date(2026, 8, 8),
            duration_minutes=90,
            busy_intervals=[],
            now=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            timezone_name="Europe/Moscow",
            min_lead_minutes=0,
            window_start_minutes=8 * 60,
            window_end_minutes=21 * 60,
        )
        self.assertEqual(slots[0].strftime("%H:%M"), "08:00")
        self.assertEqual(slots[-1].strftime("%H:%M"), "19:30")

    def test_periods_follow_configured_window(self) -> None:
        periods = booking_periods(9 * 60, 20 * 60)
        self.assertEqual(
            [(item.key, item.start_minutes, item.end_minutes) for item in periods],
            [
                ("morning", 9 * 60, 12 * 60),
                ("day", 12 * 60, 17 * 60),
                ("evening", 17 * 60, 20 * 60),
            ],
        )
        morning_slot = datetime(2026, 8, 8, 11, 45)
        self.assertTrue(slot_in_period(morning_slot, periods[0]))
        self.assertFalse(slot_in_period(morning_slot, periods[1]))


if __name__ == "__main__":
    unittest.main()
