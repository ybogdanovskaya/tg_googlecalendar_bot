from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.booking_rules import (
    CLOSED_WEEKDAYS,
    DURATIONS,
    MIN_LEAD_MINUTES,
    USER_BOOKING_WINDOW,
    load_rules,
    parse_booking_window,
    validate_value,
)
from datetime import date
from app.booking_rules import is_closed_date
from app.db import Database


class BookingRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "rules.sqlite3")
        self.db.initialize()
        self.settings = SimpleNamespace(
            min_lead_minutes=120,
            booking_horizon_days=30,
            hold_hours=24,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_database_override_is_loaded_without_restart(self) -> None:
        self.assertEqual(load_rules(self.db, self.settings).min_lead_minutes, 120)
        self.db.set_setting(MIN_LEAD_MINUTES, 180, 1)
        self.assertEqual(load_rules(self.db, self.settings).min_lead_minutes, 180)

    def test_invalid_persisted_value_falls_back_to_default(self) -> None:
        self.db.set_setting(MIN_LEAD_MINUTES, -100, 1)
        self.assertEqual(load_rules(self.db, self.settings).min_lead_minutes, 120)

    def test_at_least_one_duration_is_required(self) -> None:
        with self.assertRaises(ValueError):
            validate_value(DURATIONS, [])

    def test_user_booking_window_defaults_to_eight_until_twenty_one(self) -> None:
        rules = load_rules(self.db, self.settings)
        self.assertEqual(rules.user_booking_start_minutes, 8 * 60)
        self.assertEqual(rules.user_booking_end_minutes, 21 * 60)

    def test_user_booking_window_can_be_changed_without_restart(self) -> None:
        self.db.set_setting(USER_BOOKING_WINDOW, [9 * 60, 20 * 60], 1)
        rules = load_rules(self.db, self.settings)
        self.assertEqual(
            (rules.user_booking_start_minutes, rules.user_booking_end_minutes),
            (9 * 60, 20 * 60),
        )

    def test_booking_window_accepts_time_with_or_without_colons(self) -> None:
        self.assertEqual(parse_booking_window("0800-2100"), [8 * 60, 21 * 60])
        self.assertEqual(parse_booking_window("08:00–21:00"), [8 * 60, 21 * 60])

    def test_booking_window_rejects_reversed_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_booking_window("2100-0800")

    def test_weekend_rules_are_loaded_and_close_matching_dates(self) -> None:
        self.db.set_setting(CLOSED_WEEKDAYS, [5, 6], 1)
        rules = load_rules(self.db, self.settings)
        self.assertTrue(is_closed_date(date(2026, 8, 8), set(), rules))
        self.assertTrue(is_closed_date(date(2026, 8, 9), set(), rules))
        self.assertFalse(is_closed_date(date(2026, 8, 10), set(), rules))


if __name__ == "__main__":
    unittest.main()
