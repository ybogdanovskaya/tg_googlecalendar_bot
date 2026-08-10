from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import _bool


class ConfigTests(unittest.TestCase):
    def test_boolean_values_are_parsed(self) -> None:
        with patch.dict(os.environ, {"TEST_FLAG": "false"}):
            self.assertFalse(_bool("TEST_FLAG", True))
        with patch.dict(os.environ, {"TEST_FLAG": "yes"}):
            self.assertTrue(_bool("TEST_FLAG", False))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_bool("TEST_FLAG", True))

    def test_invalid_boolean_value_is_rejected(self) -> None:
        with patch.dict(os.environ, {"TEST_FLAG": "maybe"}):
            with self.assertRaises(RuntimeError):
                _bool("TEST_FLAG", True)

