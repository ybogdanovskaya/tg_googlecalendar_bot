from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.booking_rules import DURATIONS, MIN_LEAD_MINUTES, load_rules, validate_value
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


if __name__ == "__main__":
    unittest.main()
