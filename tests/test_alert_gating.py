import os
import tempfile
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "alert_gating.db"))
os.environ.setdefault("ALERT_CHANNELS", "telegram")

# main.py attaches a FileHandler("scanner.log") at import time; run the tests
# from a temp directory so they do not append to the real scanner log.
_ORIGINAL_CWD = os.getcwd()
os.chdir(tempfile.mkdtemp())

import config  # noqa: E402
import database.db_manager as db  # noqa: E402
import main  # noqa: E402


def tearDownModule():
    os.chdir(_ORIGINAL_CWD)


NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _position(direction="sell", grade="A"):
    return {"symbol": "XAUUSD", "direction": direction, "grade": grade}


class AlertGatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_same_direction_alert_only_when_grade_upgrades(self):
        position = _position()
        cases = [
            ("Standard", False),
            ("A", False),
            ("A+", True),
            ("ELITE", True),
        ]
        for grade, expected in cases:
            with self.subTest(grade=grade):
                self.assertEqual(
                    main._alert_allowed(
                        position, {"bias": "bearish"}, {"grade": grade}
                    ),
                    expected,
                )

    def test_reversal_rearms_alert(self):
        self.assertTrue(
            main._alert_allowed(_position(), {"bias": "bullish"}, {"grade": "Standard"})
        )

    def test_direction_mapping(self):
        self.assertEqual(main._position_direction({"bias": "bullish"}), "buy")
        self.assertEqual(main._position_direction({"bias": "bearish"}), "sell")
        self.assertIsNone(main._position_direction({"bias": "neutral"}))

    def test_rearm_clears_only_that_symbol(self):
        db.set_alert_cooldown("XAUUSD_A", NOW)
        db.set_alert_cooldown("XAUUSD_ELITE", NOW)
        db.set_alert_cooldown("EURUSD_A", NOW)
        db.set_alert_cooldown("RISK_VIOLATION", NOW)
        main._alerted["XAUUSD_A"] = NOW
        main._alerted["RISK_VIOLATION"] = NOW

        main._rearm_symbol("XAUUSD")

        self.assertIsNone(db.get_alert_cooldown("XAUUSD_A"))
        self.assertIsNone(db.get_alert_cooldown("XAUUSD_ELITE"))
        self.assertIsNotNone(db.get_alert_cooldown("EURUSD_A"))
        self.assertIsNotNone(db.get_alert_cooldown("RISK_VIOLATION"))
        self.assertNotIn("XAUUSD_A", main._alerted)
        self.assertIn("RISK_VIOLATION", main._alerted)


if __name__ == "__main__":
    unittest.main()
