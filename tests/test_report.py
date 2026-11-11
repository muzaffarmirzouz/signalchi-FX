import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import report  # noqa: E402


def e(result, pips, r=None, side="BUY", symbol="XAUUSD", auto=True, closed=1000.0):
    return {"result": result, "pips": pips, "r": r, "side": side, "symbol": symbol, "auto": auto, "closed": closed}


class AggregateTests(unittest.TestCase):
    def test_counts_and_winrate(self):
        entries = [e("TP", 120, 1.67), e("TP", 110, 1.67, "SELL"), e("SL", -70, -1.0), e("SL", -72, -1.0, "SELL"),
                   e("SL", -69, -1.0, symbol="BTCUSDT")]
        a = report.aggregate(entries)
        self.assertEqual((a["total"]["n"], a["total"]["tp"], a["total"]["sl"]), (5, 2, 3))
        self.assertAlmostEqual(a["win_rate"], 0.4)
        self.assertAlmostEqual(a["r_total"], 1.67 * 2 - 3.0)
        self.assertEqual(a["side"]["BUY"]["n"], 3)
        self.assertEqual(a["side"]["SELL"]["n"], 2)
        self.assertEqual(a["symbol"]["XAUUSD"]["n"], 4)
        self.assertEqual(a["symbol"]["BTCUSDT"]["sl"], 1)
        self.assertEqual(a["best"]["pips"], 120)
        self.assertEqual(a["worst"]["pips"], -72)

    def test_nofill_and_manual_are_separate(self):
        entries = [e("TP", 100, 1.5), e("NOFILL", 0.0), e("NOFILL", 0.0), e("PIPS", 150, auto=False)]
        a = report.aggregate(entries)
        self.assertEqual(a["total"]["n"], 1)
        self.assertEqual(a["nofill"], 2)
        self.assertEqual(a["manual"]["n"], 1)
        self.assertEqual(a["manual"]["pips"], 150)

    def test_timeout_is_other(self):
        a = report.aggregate([e("TIMEOUT", 10, 0.1), e("TP", 100, 1.5)])
        self.assertEqual((a["total"]["other"], a["total"]["tp"]), (1, 1))

    def test_empty(self):
        a = report.aggregate([])
        self.assertEqual(a["total"]["n"], 0)
        self.assertIsNone(a["win_rate"])
        self.assertIsNone(a["best"])


class TimeTests(unittest.TestCase):
    def test_local_day_bounds_tashkent(self):
        # 2023-11-14 22:13 UTC = 2023-11-15 03:13 Toshkent -> kun 14-noyabr 19:00 UTC dan boshlanadi
        start, end = report.local_day_bounds(1_700_000_000, 5)
        self.assertEqual(start, 1_699_988_400)
        self.assertEqual(end - start, 86400)
        self.assertTrue(start <= 1_700_000_000 < end)

    def test_in_range_and_prune(self):
        entries = [e("TP", 1, closed=50.0), e("TP", 1, closed=150.0), e("TP", 1, closed=250.0)]
        self.assertEqual(len(report.in_range(entries, 100, 200)), 1)
        old = [e("TP", 1, closed=0.0), e("TP", 1, closed=99 * 86400.0)]
        self.assertEqual(len(report.prune(old, 100 * 86400.0, keep_days=90)), 1)


if __name__ == "__main__":
    unittest.main()
