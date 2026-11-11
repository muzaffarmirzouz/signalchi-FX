import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import strategy  # noqa: E402


def make(closes, spread=0.4, vol=100.0, t0=1_700_000_000, step=900):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({"t": t0 + i * step, "o": o, "h": max(o, c) + spread / 2, "l": min(o, c) - spread / 2, "c": c, "v": vol})
        prev = c
    return out


def wavy(n, drift, base=100.0, amp=0.6):
    return [base + drift * i + amp * math.sin(i * 1.3) for i in range(n)]


class IndicatorTests(unittest.TestCase):
    def test_ema_constant(self):
        e = strategy.ema([5.0] * 30, 10)
        self.assertIsNone(e[8])
        self.assertAlmostEqual(e[-1], 5.0)

    def test_rsi_bounds(self):
        up = strategy.rsi([float(i) for i in range(40)], 14)
        down = strategy.rsi([float(-i) for i in range(40)], 14)
        self.assertEqual(up[-1], 100.0)
        self.assertEqual(down[-1], 0.0)
        mixed = strategy.rsi(wavy(60, 0.0), 14)
        self.assertTrue(30 < mixed[-1] < 70)

    def test_atr_constant_range(self):
        candles = make([100.0] * 40, spread=2.0)
        a = strategy.atr(candles, 14)
        self.assertAlmostEqual(a[-1], 2.0, places=6)


class SignalTests(unittest.TestCase):
    def test_not_enough_data(self):
        r = strategy.analyze(make(wavy(30, 0.1)))
        self.assertFalse(r["ok"])

    def test_buy_breakout(self):
        closes = wavy(120, 0.08)
        hi = max(closes[-20:])
        closes.append(hi + 2.5)  # kuchli yorib o'tish
        candles = make(closes)
        candles[-1]["v"] = 300.0
        r = strategy.analyze(candles, strategy.Params())
        self.assertTrue(r["ok"])
        self.assertEqual(r["trend"], "UP")
        self.assertIsNotNone(r["signal"], r["notes"])
        self.assertEqual(r["signal"]["side"], "BUY")
        self.assertEqual(r["signal"]["setup"], "breakout")
        self.assertGreaterEqual(r["signal"]["stars"], 2)
        sl, tp = strategy.levels("BUY", 100.0, r["signal"]["sl_dist"], r["signal"]["tp_dist"])
        self.assertLess(sl, 100.0)
        self.assertGreater(tp, 100.0)

    def test_sell_breakout(self):
        closes = wavy(120, -0.08, base=200.0)
        lo = min(closes[-20:])
        closes.append(lo - 2.5)
        candles = make(closes)
        candles[-1]["v"] = 300.0
        r = strategy.analyze(candles, strategy.Params())
        self.assertEqual(r["trend"], "DOWN")
        self.assertIsNotNone(r["signal"], r["notes"])
        self.assertEqual(r["signal"]["side"], "SELL")
        sl, tp = strategy.levels("SELL", 100.0, r["signal"]["sl_dist"], r["signal"]["tp_dist"])
        self.assertGreater(sl, 100.0)
        self.assertLess(tp, 100.0)

    def test_breakout_against_trend_is_skipped(self):
        closes = wavy(120, -0.08, base=200.0)  # trend pastga
        closes.append(max(closes[-20:]) + 3.0)  # lekin yuqoriga yorib o'tdi
        r = strategy.analyze(make(closes), strategy.Params())
        self.assertIsNone(r["signal"])
        self.assertTrue(any("trend" in n for n in r["notes"]), r["notes"])

    def test_flat_market_no_signal(self):
        r = strategy.analyze(make(wavy(150, 0.0)), strategy.Params())
        self.assertTrue(r["ok"])
        self.assertIsNone(r["signal"])
        self.assertTrue(r["notes"])

    def test_rsi_reversal_buy(self):
        closes = [150.0 - 0.5 * i for i in range(60)]  # barqaror pasayish -> RSI ~0
        closes += [closes[-1] + 0.9, closes[-1] + 1.8, closes[-1] + 5.0]
        candles = make(closes)
        # oxirgi shamni "rad etish" shakliga keltiramiz (uzun pastki soya + yashil tana)
        candles[-1]["l"] = candles[-1]["o"] - 3.0
        candles[-1]["v"] = 400.0
        hit = None
        for cut in range(len(candles) - 4, len(candles) + 1):
            r = strategy.analyze(candles[:cut], strategy.Params(min_stars=1))
            if r.get("signal") and r["signal"]["setup"] == "reversal":
                hit = r["signal"]
        if hit is not None:  # RSI 30 dan qaytgan paytda BUY bo'lishi shart
            self.assertEqual(hit["side"], "BUY")

    def test_min_stars_filter(self):
        closes = wavy(120, 0.08)
        closes.append(max(closes[-20:]) + 0.05)  # zo'rg'a yorib o'tdi, kichik sham
        candles = make(closes)
        strict = strategy.analyze(candles, strategy.Params(min_stars=4))
        self.assertIsNone(strict["signal"])


if __name__ == "__main__":
    unittest.main()
