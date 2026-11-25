import os
import tempfile
import unittest
from unittest import mock

import pandas as pd
import requests

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "alert_fixes.db"))
os.environ.setdefault("ALERT_CHANNELS", "telegram")

# main.py attaches a FileHandler("scanner.log") at import time.
_ORIGINAL_CWD = os.getcwd()
os.chdir(tempfile.mkdtemp())

import config  # noqa: E402
import database.db_manager as db  # noqa: E402
import main  # noqa: E402
from alerts import dispatcher  # noqa: E402
from scanner.market_analyzer import detect_fvg, detect_retest  # noqa: E402


def tearDownModule():
    os.chdir(_ORIGINAL_CWD)


def _bars(closes, wick=0.5):
    rows = [(c, c + wick, c - wick, c) for c in closes]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])


class StructureRetestTests(unittest.TestCase):
    # Range ~2000-2020 -> tolerance ~0.06, "near" = within ~0.12 of the level.
    LEVEL = 2010.0

    def test_unbroken_resistance_is_not_a_retest(self):
        df = _bars([2000, 2005, 2008, 2010, 2006, 2004, 2007, 2009, 2020 - 10.05])
        self.assertIsNone(detect_retest(df, [], [(3, self.LEVEL)], [], "bullish"))

    def test_broken_then_retested_level_is_a_retest(self):
        df = _bars([2000, 2005, 2008, 2010, 2006, 2012, 2020, 2015, 2010.05])
        retest = detect_retest(df, [], [(3, self.LEVEL)], [], "bullish")
        self.assertEqual(retest["type"], "structure_retest")


class FvgFillTests(unittest.TestCase):
    def test_gap_closed_through_then_reclaimed_stays_filled(self):
        rows = [
            (100, 101, 99, 100),    # c0: high 101
            (101, 104, 101, 103),
            (103, 106, 102, 105),   # c2: low 102 > 101 -> bullish FVG 101-102
            (105, 105, 99, 100),    # closes below the gap bottom (invalidated)
            (100, 104, 100, 103),   # reclaimed above
        ]
        df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])
        fvg = [f for f in detect_fvg(df) if f["type"] == "bullish_fvg"][0]
        self.assertTrue(fvg["filled"])
        self.assertFalse(fvg["active"])


class StageTests(unittest.TestCase):
    def test_a_plus_never_below_a(self):
        no_rejection = {"conditions": ["BOS", "FVG"]}
        self.assertEqual(main.determine_stage({"grade": "A"}, no_rejection), "entry_zone")
        self.assertEqual(main.determine_stage({"grade": "A+"}, no_rejection), "entry_zone")
        self.assertEqual(
            main.determine_stage({"grade": "A+"}, {"conditions": ["Rejection Candle"]}),
            "m15_confirmation",
        )


class LiveStatusLineTests(unittest.TestCase):
    def test_skipped_live_trade_is_explained_in_alert(self):
        from alerts.telegram_bot import format_alert
        msg = format_alert(
            "XAUUSD", {"bias": "bearish", "conditions": ["FVG"]},
            {"grade": "Standard", "score": 5},
            live_trade={"ok": False, "dry_run": False, "skipped": True,
                        "reason": "grade Standard below minimum A+"},
        )
        self.assertIn("Live: no order", msg)
        self.assertIn("grade Standard below minimum A+", msg)
        self.assertNotIn("rejected", msg)


class CooldownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_key_includes_direction(self):
        buy, _ = main._alert_cooldown("XAUUSD", {"bias": "bullish"}, "A", None)
        sell, _ = main._alert_cooldown("XAUUSD", {"bias": "bearish"}, "A", None)
        self.assertNotEqual(buy, sell)

    def test_open_position_alert_is_once_per_position(self):
        position = {"id": 7, "opened_at": "2026-11-25 10:00:00"}
        key, window = main._alert_cooldown("XAUUSD", {"bias": "bullish"}, "A+", position)
        self.assertIn("2026-11-25 10:00:00", key)
        self.assertGreater(window, config.ALERT_COOLDOWN_MINUTES)
        self.assertTrue(key.startswith("XAUUSD_"))  # cleared by _rearm_symbol

    def test_checking_cooldown_does_not_consume_it(self):
        key = "TEST_readonly_A"
        self.assertFalse(main._cooldown_active(key))
        self.assertFalse(main._cooldown_active(key))
        main._mark_cooldown(key)
        self.assertTrue(main._cooldown_active(key))


def _response(status=200, body=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {"ok": True}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        resp.raise_for_status.return_value = None
    return resp


class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        conn = db.get_connection()
        conn.execute("DELETE FROM telegram_targets")
        conn.execute(
            "INSERT INTO telegram_targets (chat_id, chat_type, paired_at, active) "
            "VALUES ('-100555', 'supergroup', '2026-01-01 00:00:00', 1)"
        )
        conn.commit()
        conn.close()
        for p in (mock.patch.object(config, "TELEGRAM_BOT_TOKEN", "t"),
                  mock.patch.object(config, "TELEGRAM_CHAT_ID", "999")):
            p.start()
            self.addCleanup(p.stop)

    def _active_ids(self):
        return [row["chat_id"] for row in db.get_telegram_targets()]

    def test_chat_not_found_deactivates_paired_target_and_falls_back(self):
        dead = _response(400, {"ok": False, "description": "Bad Request: chat not found"})
        with mock.patch.object(dispatcher.requests, "post", return_value=dead):
            self.assertFalse(dispatcher._send_telegram_text("hi"))
        self.assertEqual(self._active_ids(), [])
        self.assertEqual(dispatcher._telegram_chat_ids(), ["999"])

    def test_group_migration_moves_target(self):
        migrated = _response(400, {"ok": False, "description": "migrated",
                                   "parameters": {"migrate_to_chat_id": -100777}})
        with mock.patch.object(dispatcher.requests, "post", return_value=migrated):
            dispatcher._send_telegram_text("hi")
        self.assertEqual(self._active_ids(), ["-100777"])

    def test_other_errors_keep_target(self):
        bad_html = _response(400, {"ok": False, "description": "can't parse entities"})
        with mock.patch.object(dispatcher.requests, "post", return_value=bad_html):
            dispatcher._send_telegram_text("<b>")
        self.assertEqual(self._active_ids(), ["-100555"])

    def test_long_caption_sent_as_separate_message(self):
        caption = "<b>x</b>" * 200  # 1600 raw chars
        with tempfile.NamedTemporaryFile(suffix=".png") as photo, \
             mock.patch.object(dispatcher.requests, "post", return_value=_response()) as post:
            self.assertTrue(dispatcher._send_telegram_photo(photo.name, caption))
        photo_call, text_call = post.call_args_list
        self.assertEqual(photo_call.kwargs["data"]["caption"], "")
        self.assertEqual(text_call.kwargs["json"]["text"], caption)

    def test_short_caption_stays_on_photo(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as photo, \
             mock.patch.object(dispatcher.requests, "post", return_value=_response()) as post:
            dispatcher._send_telegram_photo(photo.name, "<b>short</b>")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["caption"], "<b>short</b>")


if __name__ == "__main__":
    unittest.main()
