import csv
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backtest  # noqa: E402
import strategy  # noqa: E402


def bars(closes, t0=1_700_000_000, step=900, spread=0.4):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"t": t0 + i * step, "o": prev, "h": max(prev, c) + spread / 2,
                    "l": min(prev, c) - spread / 2, "c": c, "v": 100.0})
        prev = c
    return out


def wavy(n, drift, base=100.0, amp=0.6):
    return [base + drift * i + amp * math.sin(i * 1.3) for i in range(n)]


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.c = bars([100.0] * 5)

    def test_tp_hit(self):
        c = self.c + [{"t": self.c[-1]["t"] + 900, "o": 100, "h": 106, "l": 99.5, "c": 105, "v": 1}]
        out, j, px = backtest.resolve_trade(c, 4, "BUY", 100, 95, 105, 48)
        self.assertEqual((out, px), ("TP", 105))

    def test_sl_first_when_both_in_one_bar(self):
        c = self.c + [{"t": self.c[-1]["t"] + 900, "o": 100, "h": 106, "l": 94, "c": 100, "v": 1}]
        out, _, px = backtest.resolve_trade(c, 4, "BUY", 100, 95, 105, 48)
        self.assertEqual((out, px), ("SL", 95))

    def test_sell_tp(self):
        c = self.c + [{"t": self.c[-1]["t"] + 900, "o": 100, "h": 100.5, "l": 94, "c": 95, "v": 1}]
        out, _, px = backtest.resolve_trade(c, 4, "SELL", 100, 105, 95, 48)
        self.assertEqual((out, px), ("TP", 95))

    def test_timeout_and_open(self):
        c = self.c + [{"t": self.c[-1]["t"] + 900 * k, "o": 100, "h": 100.1, "l": 99.9, "c": 100, "v": 1} for k in range(1, 4)]
        self.assertEqual(backtest.resolve_trade(c, 4, "BUY", 100, 90, 110, 48)[0], "OPEN")
        self.assertEqual(backtest.resolve_trade(c, 4, "BUY", 100, 90, 110, 0.5)[0], "TIMEOUT")


class SummaryTests(unittest.TestCase):
    def test_summary_math(self):
        mk = lambda o, r: {"outcome": o, "r": r}
        s = backtest.summarize([mk("TP", 1.5), mk("SL", -1), mk("SL", -1), mk("TP", 1.5), mk("OPEN", 0)])
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["open"], 1)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["total_r"], 1.0)
        self.assertAlmostEqual(s["profit_factor"], 1.5)
        self.assertAlmostEqual(s["max_dd_r"], 2.0)
        self.assertEqual(s["max_losing_streak"], 2)

    def test_empty(self):
        self.assertEqual(backtest.summarize([])["n"], 0)


class SimulateTests(unittest.TestCase):
    def _series(self, move):
        """120 ta o'sish shami + breakout shami + `move(breakout_close, k)` bo'yicha davomi."""
        closes = wavy(120, 0.08)
        brk = max(closes[-20:]) + 2.0
        closes.append(brk)
        closes += [move(brk, k) for k in range(1, 40)]
        c = bars(closes)
        c[120]["v"] = 300.0
        return c

    def _at_breakout(self, c, **kw):
        kw.setdefault("cooldown_min", 0)  # oldingi (117-sham) signal tanaffusi aralashmasin
        trades = [t for t in backtest.simulate(c, strategy.Params(), **kw) if t["t"] == c[120]["t"]]
        self.assertEqual(len(trades), 1, "breakout shamida aynan bitta signal bo'lishi kerak")
        return trades[0]

    def test_winning_breakout(self):
        t = self._at_breakout(self._series(lambda b, k: b + 1.0 * k))
        self.assertEqual((t["side"], t["outcome"]), ("BUY", "TP"))
        self.assertGreater(t["r"], 1.0)

    def test_losing_breakout(self):
        t = self._at_breakout(self._series(lambda b, k: b - 1.0 * k))
        self.assertEqual(t["outcome"], "SL")
        self.assertAlmostEqual(t["r"], -1.0, places=6)

    def test_cost_reduces_r(self):
        c = self._series(lambda b, k: b + 1.0 * k)
        a = self._at_breakout(c)["r"]
        b = self._at_breakout(c, cost_r=0.1)["r"]
        self.assertAlmostEqual(a - b, 0.1)

    def test_cooldown_blocks_repeat(self):
        c = self._series(lambda b, k: b + 1.0 * k)
        strict = backtest.simulate(c, strategy.Params(), cooldown_min=10_000)
        self.assertEqual(len([t for t in strict if t["side"] == "BUY"]), 1)


class CsvTests(unittest.TestCase):
    def test_load_csv_mt5_style(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["<DATE>", "time", "open", "high", "low", "close", "tick_volume"])
            w.writerow(["x", "2026.11.10 14:15", "4100.5", "4102", "4099", "4101.2", "250"])
            w.writerow(["x", "2026.11.10 14:30", "4101.2", "4103", "4100", "4102.5", "300"])
            path = f.name
        try:
            rows = backtest.load_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["t"] - rows[0]["t"], 900)
        self.assertEqual(rows[0]["c"], 4101.2)

    def test_cli_end_to_end_with_csv(self):
        closes = wavy(300, 0.05, base=4000.0, amp=6.0)
        c = bars(closes, t0=1_700_000_000)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            for x in c:
                w.writerow([x["t"], x["o"], x["h"], x["l"], x["c"], x["v"]])
            path = f.name
        try:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                backtest.main(["--symbol", "XAUUSD", "--csv", path, "--min-stars", "1"])
        finally:
            os.unlink(path)
        out = buf.getvalue()
        self.assertIn("HAMMASI", out)
        self.assertIn("Kuch bo'yicha", out)


if __name__ == "__main__":
    unittest.main()
