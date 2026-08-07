from __future__ import annotations

import unittest
from datetime import date, time

from app.date_picker import calendar_keyboard, shift_month
from app.input_parsing import parse_time_input


class DatePickerTests(unittest.TestCase):
    def test_month_navigation_crosses_year_boundary(self) -> None:
        self.assertEqual(shift_month(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(shift_month(date(2026, 1, 1), -1), date(2025, 12, 1))

    def test_calendar_disables_days_outside_bounds(self) -> None:
        keyboard = calendar_keyboard(
            "test",
            date(2026, 8, 1),
            date(2026, 8, 7),
            date(2026, 8, 10),
        )
        callbacks = {
            button.text: button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.text in {"6", "7", "10", "11"}
        }
        self.assertNotIn("6", callbacks)
        self.assertEqual(callbacks["7"], "test:day:2026-08-07")
        self.assertEqual(callbacks["10"], "test:day:2026-08-10")
        self.assertNotIn("11", callbacks)

    def test_time_can_be_entered_without_colon(self) -> None:
        self.assertEqual(parse_time_input("930"), time(9, 30))
        self.assertEqual(parse_time_input("0930"), time(9, 30))
        self.assertEqual(parse_time_input("9.30"), time(9, 30))
        self.assertEqual(parse_time_input("9 30"), time(9, 30))
        self.assertEqual(parse_time_input("09:30"), time(9, 30))
        with self.assertRaises(ValueError):
            parse_time_input("2460")


if __name__ == "__main__":
    unittest.main()
