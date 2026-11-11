#!/usr/bin/env python3
"""
Backtest — avto-signal strategiyasini (strategy.py) tarixiy shamlarda sinab ko'radi.

Bot jonli rejimda qanday ishlasa, shu yerda ham xuddi shunday:
  * har yopilgan shamda strategy.analyze() chaqiriladi,
  * signal chiqsa, kirish = o'sha shamning yopilish narxi, SL/TP = ATR bo'yicha,
  * tanaffus (AUTO_COOLDOWN_MIN), kunlik limit (AUTO_MAX_PER_DAY) va
    "shu yo'nalishda ochiq signal bor" qoidasi bir xil.

Natija R birligida: SL = -1R, TP = +(TP/SL) R (breakout 1.67R, qaytish 1.33R).
Bir shamda ham SL, ham TP ga tegsa — konservativ hisob: SL birinchi deb olinadi.
Faqat standart kutubxona (aiohttp kerak emas) — kompyuteringizda ham ishlaydi.

Ishlatish:
  python3 backtest.py --symbol XAUUSD --days 30
  python3 backtest.py --symbol BTCUSDT --days 60 --tf 15m --min-stars 3
  python3 backtest.py --symbol XAUUSD --csv exness_xauusd_m15.csv   # MT5'dan eksport qilingan shamlar
  python3 backtest.py --symbol XAUUSD --days 30 --cost-r 0.05       # spred/slippage: har savdoga -0.05R

CSV: sarlavhali (time/open/high/low/close/volume — MT5 "Export" yoki shunga o'xshash). Vaqt: soniya,
millisoniya yoki "2026.11.10 14:15" / ISO ko'rinishida.

Eslatma: o'tmishdagi natija kelajakni kafolatlamaydi. Bitget oltin kontrakti Exness narxidan biroz
farq qiladi, shuning uchun aniq natija uchun MT5 shamlarini --csv bilan bering.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy  # noqa: E402

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
BITGET_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H"}
BITGET_SYMBOLS = {"XAUUSD": "XAUUSDT", "XAGUSD": "XAGUSDT"}
KLINES_API = os.environ.get("KLINES_API", "https://data-api.binance.vision/api/v3/klines")
BITGET_HISTORY_API = os.environ.get("BITGET_HISTORY_API", "https://api.bitget.com/api/v2/mix/market/history-candles")
UA = {"User-Agent": "signalchi-fx-backtest/1.0"}


# ───────────────────────── ma'lumot olish ─────────────────────────

def _get_json(url, params, retries=3):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for k in range(retries):
        try:
            req = urllib.request.Request(full, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if k == retries - 1:
                raise
            time.sleep(1.5 * (k + 1))


def fetch_binance(symbol, tf, start_s, end_s):
    rows, cur = [], start_s * 1000
    while cur < end_s * 1000:
        data = _get_json(KLINES_API, {"symbol": symbol, "interval": tf, "startTime": cur,
                                      "endTime": end_s * 1000, "limit": 1000})
        if not data:
            break
        for k in data:
            rows.append({"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
        nxt = int(data[-1][0]) + TF_SECONDS[tf] * 1000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.2)
    return rows


def fetch_bitget(symbol, tf, start_s, end_s):
    rows, end_ms = [], end_s * 1000
    for _ in range(2000):  # xavfsizlik chegarasi
        data = _get_json(BITGET_HISTORY_API, {
            "symbol": BITGET_SYMBOLS[symbol], "productType": "USDT-FUTURES", "granularity": BITGET_TF[tf],
            "startTime": start_s * 1000, "endTime": end_ms, "limit": 200})
        if str(data.get("code")) != "00000":
            raise RuntimeError(f"Bitget xato: {str(data)[:200]}")
        chunk = data.get("data") or []
        if not chunk:
            break
        for k in chunk:
            rows.append({"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
        oldest = min(int(k[0]) for k in chunk)
        if oldest <= start_s * 1000 or oldest >= end_ms:
            break
        end_ms = oldest - 1
        time.sleep(0.2)
    return rows


def _parse_time(s):
    s = str(s).strip()
    try:
        v = float(s)
        return int(v // 1000) if v > 1e11 else int(v)
    except ValueError:
        pass
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s.replace("Z", ""), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"vaqtni tushunmadim: {s!r}")


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    delim = max(",;\t", key=text.split("\n", 1)[0].count)
    rd = csv.reader(text.splitlines(), delimiter=delim)
    head = [h.strip().lower().strip("<>") for h in next(rd)]

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        raise ValueError(f"CSV'da {names} ustuni yo'q. Topilgan: {head}")

    it, io, ih, il, ic = (col("time", "t", "timestamp", "date", "datetime"), col("open", "o"),
                          col("high", "h"), col("low", "l"), col("close", "c"))
    iv = next((head.index(n) for n in ("volume", "v", "tick_volume", "vol", "tickvol") if n in head), None)
    rows = []
    for r in rd:
        if len(r) <= ic:
            continue
        try:
            rows.append({"t": _parse_time(r[it]), "o": float(r[io]), "h": float(r[ih]), "l": float(r[il]),
                         "c": float(r[ic]), "v": float(r[iv]) if iv is not None and r[iv] else 0.0})
        except ValueError:
            continue
    return rows


def clean(rows, tf):
    """Takrorlarni olib tashlaydi, vaqt bo'yicha tartiblaydi."""
    seen, out = set(), []
    for c in sorted(rows, key=lambda c: c["t"]):
        if c["t"] not in seen:
            seen.add(c["t"])
            out.append(c)
    return out


# ───────────────────────── simulyatsiya ─────────────────────────

def resolve_trade(candles, i, side, entry, sl, tp, max_hours):
    """i-shamda ochilgan savdo qanday tugaganini topadi: (natija, chiqish_indeksi, chiqish_narxi).
    natija: 'TP' | 'SL' | 'TIMEOUT' | 'OPEN' (ma'lumot tugadi)."""
    t0 = candles[i]["t"]
    for j in range(i + 1, len(candles)):
        b = candles[j]
        if side == "BUY":
            sl_hit, tp_hit = b["l"] <= sl, b["h"] >= tp
        else:
            sl_hit, tp_hit = b["h"] >= sl, b["l"] <= tp
        if sl_hit:  # ikkalasi bir shamda bo'lsa ham SL birinchi (konservativ)
            return "SL", j, sl
        if tp_hit:
            return "TP", j, tp
        if b["t"] - t0 > max_hours * 3600:
            return "TIMEOUT", j, b["c"]
    return "OPEN", len(candles) - 1, candles[-1]["c"]


def simulate(candles, params=None, cooldown_min=60, max_per_day=5, max_hours=48, cost_r=0.0, window=300):
    params = params or strategy.Params()
    need = max(params.ema_slow, params.rsi_len, params.atr_len, params.lookback) + 5
    trades, last_sig, day_count, open_trades = [], {}, {}, []
    for i in range(need, len(candles)):
        res = strategy.analyze(candles[max(0, i - window + 1):i + 1], params)
        sig = res.get("signal") if res.get("ok") else None
        if not sig:
            continue
        bar = candles[i]
        side = key = sig["side"]
        if bar["t"] - last_sig.get(key, -10**12) < cooldown_min * 60:
            continue
        day = datetime.fromtimestamp(bar["t"], timezone.utc).strftime("%Y-%m-%d")
        if day_count.get(day, 0) >= max_per_day:
            continue
        if any(o["side"] == side and o["exit_idx"] > i for o in open_trades):
            continue
        entry = bar["c"]
        sl, tp = strategy.levels(side, entry, sig["sl_dist"], sig["tp_dist"])
        outcome, j, px = resolve_trade(candles, i, side, entry, sl, tp, max_hours)
        move = (px - entry) if side == "BUY" else (entry - px)
        r = move / sig["sl_dist"] - cost_r
        t = {"t": bar["t"], "side": side, "setup": sig["setup"], "stars": sig["stars"], "entry": entry,
             "sl": sl, "tp": tp, "outcome": outcome, "exit_idx": j, "exit_t": candles[j]["t"], "r": r}
        open_trades.append(t)
        last_sig[key] = bar["t"]
        day_count[day] = day_count.get(day, 0) + 1
        trades.append(t)
    return trades


def summarize(trades):
    """Yopilgan savdolar (OPEN emas) bo'yicha statistika."""
    done = [t for t in trades if t["outcome"] != "OPEN"]
    n = len(done)
    if n == 0:
        return {"n": 0}
    wins = [t for t in done if t["r"] > 0]
    losses = [t for t in done if t["r"] <= 0]
    gross_w, gross_l = sum(t["r"] for t in wins), -sum(t["r"] for t in losses)
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in done:
        eq += t["r"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        streak = streak + 1 if t["r"] <= 0 else 0
        max_streak = max(max_streak, streak)
    return {
        "n": n,
        "tp": sum(t["outcome"] == "TP" for t in done),
        "sl": sum(t["outcome"] == "SL" for t in done),
        "timeout": sum(t["outcome"] == "TIMEOUT" for t in done),
        "win_rate": len(wins) / n,
        "avg_r": sum(t["r"] for t in done) / n,
        "total_r": eq,
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "max_dd_r": max_dd,
        "max_losing_streak": max_streak,
        "open": len(trades) - n,
    }


def fmt_row(label, s):
    if not s["n"]:
        return f"{label:<22} savdo yo'q"
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (f"{label:<22} {s['n']:>4} ta | yutuq {s['win_rate'] * 100:>3.0f}% (TP {s['tp']}, SL {s['sl']}, vaqt {s['timeout']}) | "
            f"o'rtacha {s['avg_r']:+.2f}R | jami {s['total_r']:+.1f}R | PF {pf} | maks. tushish {s['max_dd_r']:.1f}R | "
            f"ketma-ket SL ≤ {s['max_losing_streak']}")


def report(symbol, tf, candles, trades):
    t0 = datetime.fromtimestamp(candles[0]["t"], timezone.utc).strftime("%Y-%m-%d")
    t1 = datetime.fromtimestamp(candles[-1]["t"], timezone.utc).strftime("%Y-%m-%d")
    days = max(1, (candles[-1]["t"] - candles[0]["t"]) / 86400)
    lines = [f"═══ {symbol} · {tf} · {t0} → {t1} ({days:.0f} kun, {len(candles)} sham) ═══", ""]
    lines.append(fmt_row("HAMMASI", summarize(trades)))
    lines.append("")
    for side in ("BUY", "SELL"):
        lines.append(fmt_row(f"  {side}", summarize([t for t in trades if t["side"] == side])))
    for setup in ("breakout", "reversal"):
        lines.append(fmt_row(f"  {setup}", summarize([t for t in trades if t["setup"] == setup])))
    lines.append("")
    lines.append("Kuch bo'yicha (AUTO_MIN_STARS ni tanlash uchun):")
    for st in (2, 3, 4):
        lines.append(fmt_row(f"  ≥ {st}⭐", summarize([t for t in trades if t["stars"] >= st])))
    s = summarize(trades)
    if s["n"]:
        lines += ["", f"Kuniga o'rtacha {s['n'] / days:.1f} ta signal."]
        if s["n"] < 30:
            lines.append("⚠️ Savdolar soni 30 dan kam — statistika ishonchsiz. Ko'proq kun oling.")
        if s["avg_r"] <= 0:
            lines.append("⚠️ O'rtacha natija ≤ 0: bu davrda strategiya foyda bermadi. Kanalga ochmang.")
    return "\n".join(lines)


# ───────────────────────── CLI ─────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="signalchi-FX avto-signal backtest")
    ap.add_argument("--symbol", default="XAUUSD", help="XAUUSD, BTCUSDT, ...")
    ap.add_argument("--tf", default="15m", choices=sorted(TF_SECONDS))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--csv", help="tayyor shamlar fayli (tarmoqsiz)")
    ap.add_argument("--min-stars", type=int, default=2)
    ap.add_argument("--cooldown-min", type=float, default=60)
    ap.add_argument("--max-per-day", type=int, default=5)
    ap.add_argument("--max-hours", type=float, default=48, help="savdo shuncha soatdan keyin yopiladi")
    ap.add_argument("--cost-r", type=float, default=0.0, help="har savdoga spred/slippage (R birligida)")
    ap.add_argument("--json", action="store_true", help="savdolar ro'yxatini JSON qilib chiqarish")
    a = ap.parse_args(argv)

    sym = a.symbol.upper()
    if a.csv:
        candles = clean(load_csv(a.csv), a.tf)
    else:
        end = int(time.time())
        start = end - a.days * 86400
        fetch = fetch_bitget if sym in BITGET_SYMBOLS else fetch_binance
        try:
            candles = clean(fetch(sym, a.tf, start, end), a.tf)
        except Exception as e:
            sys.exit(f"Shamlarni olib bo'lmadi: {e}\nTarmoq yopiq bo'lsa, --csv bilan fayl bering.")
    candles = [c for c in candles if c["t"] + TF_SECONDS[a.tf] <= time.time() + 2]
    if len(candles) < 120:
        sys.exit(f"Shamlar yetarli emas ({len(candles)}). Kamida ~120 ta kerak.")

    params = strategy.Params(min_stars=a.min_stars)
    trades = simulate(candles, params, a.cooldown_min, a.max_per_day, a.max_hours, a.cost_r)
    print(report(sym, a.tf, candles, trades))
    if a.json:
        print(json.dumps(trades, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
