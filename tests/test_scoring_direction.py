import unittest

import pandas as pd

from scanner.market_analyzer import (
    calculate_entry_levels,
    detect_liquidity_sweep,
    fvgs_for_bias,
)
from scanner.scoring_engine import score_setup


def _bars(rows):
    """rows: list of (open, high, low, close)."""
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])


def _trend_bars(n=30, start=2000.0, step=0.0, spread=2.0):
    rows = []
    price = start
    for _ in range(n):
        rows.append((price, price + spread, price - spread, price))
        price += step
    return _bars(rows)


def _fvg(kind, active=True):
    return {"type": kind, "top": 1.0, "bottom": 0.5, "filled": not active,
            "active": active}


class LiquiditySweepTests(unittest.TestCase):
    def setUp(self):
        # Last bar wicks above the swing high at 2010 and closes back below it.
        self.df = _bars([(2000, 2002, 1998, 2000)] * 4 + [(2005, 2012, 2003, 2006)])
        self.highs = [(1, 2010.0)]
        self.lows = [(2, 1990.0)]

    def test_bearish_sweep_is_ignored_for_bullish_bias(self):
        self.assertIsNone(detect_liquidity_sweep(self.df, self.highs, self.lows, "bullish"))

    def test_bearish_sweep_counts_for_bearish_bias(self):
        sweep = detect_liquidity_sweep(self.df, self.highs, self.lows, "bearish")
        self.assertEqual(sweep["type"], "bearish_sweep")

    def test_bullish_sweep_counts_for_bullish_bias(self):
        df = _bars([(2000, 2002, 1998, 2000)] * 4 + [(1995, 1997, 1987, 1994)])
        sweep = detect_liquidity_sweep(df, self.highs, self.lows, "bullish")
        self.assertEqual(sweep["type"], "bullish_sweep")


class FvgDirectionTests(unittest.TestCase):
    def test_only_bias_aligned_active_gaps(self):
        fvgs = [_fvg("bearish_fvg"), _fvg("bullish_fvg", active=False), _fvg("bullish_fvg")]
        self.assertEqual(fvgs_for_bias(fvgs, "bullish"), [fvgs[2]])
        self.assertEqual(fvgs_for_bias(fvgs, "bearish"), [fvgs[0]])
        self.assertEqual(fvgs_for_bias(fvgs, "neutral"), [])


class RewardToStructureTests(unittest.TestCase):
    def test_rr_reflects_room_to_nearest_resistance(self):
        df = _trend_bars()
        lows = [(5, 1997.0)]
        # Resistance 1R above entry -> rr 1, not the hardcoded 2.0.
        levels = calculate_entry_levels(df, "bullish", [(8, 2003.0)], lows, digits=2)
        self.assertAlmostEqual(levels["rr"], round(3.0 / levels["risk"], 2), places=1)
        self.assertLess(levels["rr"], 2)

    def test_rr_open_space_when_no_opposing_structure(self):
        df = _trend_bars()
        levels = calculate_entry_levels(df, "bearish", [(5, 2003.0)], [], digits=2)
        self.assertEqual(levels["rr"], 3.0)


class ScoreSetupTests(unittest.TestCase):
    def _analysis(self, **overrides):
        base = {"bias": "bullish", "bos": None, "liquidity_sweep": None,
                "fvgs": [], "retest": None, "rejection": None,
                "session": "london", "levels": {"rr": 1.2}}
        base.update(overrides)
        return base

    def test_bias_and_session_alone_are_not_a_setup(self):
        # Before the fix the hardcoded rr=2.0 made this 4 points = Standard.
        result = score_setup(self._analysis())
        self.assertEqual(result["score"], 3)
        self.assertIsNone(result["grade"])

    def test_counter_bias_sweep_and_fvg_add_nothing(self):
        result = score_setup(self._analysis(
            liquidity_sweep={"type": "bearish_sweep", "level": 1.0},
            fvgs=[_fvg("bearish_fvg")],
        ))
        self.assertEqual(result["score"], 3)

    def test_aligned_sweep_fvg_and_room_score(self):
        result = score_setup(self._analysis(
            liquidity_sweep={"type": "bullish_sweep", "level": 1.0},
            fvgs=[_fvg("bullish_fvg")],
            levels={"rr": 2.5},
        ))
        self.assertEqual(result["score"], 2 + 2 + 2 + 1 + 1)
        self.assertEqual(result["grade"], "A")


if __name__ == "__main__":
    unittest.main()
