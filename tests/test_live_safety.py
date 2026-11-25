import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "live_safety.db"))
os.environ.setdefault("ALERT_CHANNELS", "telegram")

import config  # noqa: E402
import database.db_manager as db  # noqa: E402
import execution.live_trader as live  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402

GOLD_INFO = {
    "digits": 2,
    "point": 0.01,
    "trade_contract_size": 100.0,
    "trade_tick_size": 0.01,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "filling_mode": 1,
    "trade_stops_level": 0,
    "trade_mode": 4,
}


def _info(tick_value):
    return {**GOLD_INFO, "trade_tick_value": tick_value}


class PositionSizeTests(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()

    def _size(self, balance, tick_value, entry=2000.0, sl=1995.0):
        with mock.patch("risk.risk_manager.get_symbol_info",
                        return_value=_info(tick_value)):
            return self.rm.calculate_position_size(balance, entry, sl, symbol="XAUUSD")

    def test_usd_account_risks_one_percent(self):
        # $10,000 * 1% = $100; $5 SL on 100 oz = $500 per lot -> 0.2 lot.
        with mock.patch.object(config, "MAX_RISK_PER_TRADE", 1.0):
            self.assertAlmostEqual(self._size(10_000, tick_value=1.0), 0.2)

    def test_idr_account_uses_tick_value_in_account_currency(self):
        # 10,000,000 IDR * 1% = 100,000 IDR; 1 tick on 1 lot = 16,000 IDR,
        # 500 ticks -> 8,000,000 IDR per lot -> 0.0125 lot (not 200 lots).
        with mock.patch.object(config, "MAX_RISK_PER_TRADE", 1.0):
            self.assertAlmostEqual(self._size(10_000_000, tick_value=16_000.0), 0.0125)

    def test_invalid_inputs_return_zero_not_min_lot(self):
        self.assertEqual(self._size(0, tick_value=1.0), 0.0)
        self.assertEqual(self._size(10_000, tick_value=1.0, sl=2000.0), 0.0)
        self.assertEqual(self._size(10_000, tick_value=0.0), 0.0)


def _fake_mt5():
    return SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0, ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2, TRADE_RETCODE_INVALID_FILL=10030,
        account_info=lambda: SimpleNamespace(balance=100.0),
    )


ANALYSIS = {
    "bias": "bullish",
    "levels": {"entry": 2000.0, "sl": 1995.0, "tp1": 2010.0, "tp2": 2015.0},
}
SCORING = {"grade": "ELITE", "score": 13}


class OpenLiveTradeTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(live, "MT5_AVAILABLE", True),
            mock.patch.object(live, "mt5", _fake_mt5(), create=True),
            mock.patch.object(config, "LIVE_TRADING_ENABLED", True),
            mock.patch.object(config, "LIVE_SEND_ORDERS", False),
            mock.patch.object(config, "LIVE_MIN_GRADE", "A+"),
            mock.patch.object(config, "LIVE_MAX_SPREAD_POINTS", 0),
            mock.patch.object(config, "LIVE_MAX_DAILY_TRADES", 99),
            mock.patch.object(config, "LIVE_MAX_OPEN_POSITIONS", 5),
            mock.patch.object(config, "LIVE_LOT_MODE", "risk"),
            mock.patch.object(config, "MAX_RISK_PER_TRADE", 1.0),
            mock.patch.object(live, "get_symbol_info", return_value=_info(1.0)),
            mock.patch("risk.risk_manager.get_symbol_info", return_value=_info(1.0)),
            mock.patch.object(live, "get_tick", return_value={
                "bid": 2000.0, "ask": 2000.1, "spread": 0.1, "time": 0}),
            mock.patch.object(live, "count_live_trades_since", return_value=0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_skips_when_positions_cannot_be_read(self):
        with mock.patch.object(live, "get_positions", return_value=None):
            self.assertIsNone(live.open_live_trade("XAUUSD", ANALYSIS, SCORING, RiskManager()))

    def test_skips_when_risk_lot_below_broker_minimum(self):
        # $100 balance -> $1 risk; $5.1 SL costs $510/lot -> 0.002 lot < 0.01.
        with mock.patch.object(live, "get_positions", return_value=[]):
            self.assertIsNone(live.open_live_trade("XAUUSD", ANALYSIS, SCORING, RiskManager()))

    def test_skips_when_balance_unavailable(self):
        live.mt5.account_info = lambda: None
        with mock.patch.object(live, "get_positions", return_value=[]):
            self.assertIsNone(live.open_live_trade("XAUUSD", ANALYSIS, SCORING, RiskManager()))

    def test_sized_trade_still_goes_through_dry_run(self):
        live.mt5.account_info = lambda: SimpleNamespace(balance=100_000.0)
        with mock.patch.object(live, "get_positions", return_value=[]):
            result = live.open_live_trade("XAUUSD", ANALYSIS, SCORING, RiskManager())
        self.assertTrue(result["dry_run"])
        self.assertAlmostEqual(result["lot"], 1.96)


class SyncLiveTradesTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(live, "MT5_AVAILABLE", True),
                  mock.patch.object(live, "get_open_live_trades", return_value=[
                      {"id": 1, "ticket": 555, "symbol": "XAUUSD",
                       "direction": "buy", "entry": 2000.0}])):
            p.start()
            self.addCleanup(p.stop)

    def test_mt5_error_does_not_close_trades(self):
        with mock.patch.object(live, "get_positions", return_value=None), \
             mock.patch.object(live, "close_live_trade") as close:
            self.assertEqual(live.sync_live_trades(), [])
        close.assert_not_called()

    def test_missing_exit_deal_does_not_close_trade(self):
        entry_deal = [{"entry": 0, "price": 2000.0, "profit": 0.0,
                       "swap": 0.0, "commission": -0.5}]
        with mock.patch.object(live, "get_positions", return_value=[]), \
             mock.patch.object(live, "get_position_deals", return_value=entry_deal), \
             mock.patch.object(live, "close_live_trade") as close:
            self.assertEqual(live.sync_live_trades(), [])
        close.assert_not_called()

    def test_exit_deal_closes_trade(self):
        deals = [{"entry": 0, "price": 2000.0, "profit": 0.0, "swap": 0.0, "commission": 0.0},
                 {"entry": 1, "price": 2010.0, "profit": 100.0, "swap": 0.0, "commission": 0.0}]
        with mock.patch.object(live, "get_positions", return_value=[]), \
             mock.patch.object(live, "get_position_deals", return_value=deals), \
             mock.patch.object(live, "close_live_trade", return_value=True) as close:
            events = live.sync_live_trades()
        close.assert_called_once_with(1, 2010.0, 100.0, "win")
        self.assertEqual(len(events), 1)


class DryRunParsingTests(unittest.TestCase):
    def _dry_run_for(self, value):
        with mock.patch.dict(os.environ, {"LIVE_TRADING_DRY_RUN": value}):
            return importlib.reload(config).LIVE_TRADING_DRY_RUN

    def tearDown(self):
        importlib.reload(config)

    def test_only_explicit_false_disables_dry_run(self):
        for value in ("true", "1", "yes", "on", "true ", "", "garbage"):
            self.assertTrue(self._dry_run_for(value), value)
        for value in ("false", "FALSE", " false "):
            self.assertFalse(self._dry_run_for(value), value)


class DatabaseRiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        conn = db.get_connection()
        for table in ("trades", "paper_trades", "live_trades"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def _set_closed(self, table, row_id, status, closed_at):
        conn = db.get_connection()
        conn.execute(
            f"UPDATE {table} SET status = ?, closed_at = ? WHERE id = ?",
            (status, closed_at, row_id),
        )
        conn.commit()
        conn.close()

    def _live(self, status, closed_at):
        row_id = db.create_live_trade("XAUUSD", "buy", 0.01, 2000.0, 1995.0, 2010.0)
        self._set_closed("live_trades", row_id, status, closed_at)

    def _paper(self, status, closed_at, n):
        row = db.create_paper_trade(
            "XAUUSD", "A", "buy", "A", 7, "[]", "london", "M15", 0.01,
            2000.0, 1995.0, 2010.0, 2015.0, 5.0, f"2026-01-01 00:{n:02d}:00",
            signal_key=f"k{n}",
        )
        row_id = row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row
        self._set_closed("paper_trades", row_id, status, closed_at)

    def test_paper_win_does_not_reset_live_losing_streak(self):
        self._live("loss", "2026-01-01 10:00:00")
        self._live("loss", "2026-01-01 11:00:00")
        self._live("loss", "2026-01-01 12:00:00")
        self._paper("win", "2026-01-01 13:00:00", 1)
        self.assertEqual(len(db.get_recent_losses(3)), 3)

    def test_same_signal_losing_in_paper_and_live_counts_once(self):
        self._live("loss", "2026-01-01 10:00:00")
        self._paper("loss", "2026-01-01 10:00:00", 2)
        self.assertEqual(len(db.get_recent_losses(3)), 1)

    def test_daily_count_defaults_to_utc_date(self):
        db.create_live_trade("XAUUSD", "buy", 0.01, 2000.0, 1995.0, 2010.0)
        utc_today = datetime.now(timezone.utc).date()
        self.assertEqual(db.count_live_trades_since(),
                         db.count_live_trades_since(utc_today))
        self.assertEqual(db.count_live_trades_since(), 1)


if __name__ == "__main__":
    unittest.main()
