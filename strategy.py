"""
Avto-signal mantig'i (oltin va bitcoin uchun). Faqat standart kutubxona, tarmoq yo'q —
shuning uchun osongina test qilinadi.

Sham (candle) ko'rinishi: {"t": ochilish_vaqti_soniyada, "o": .., "h": .., "l": .., "c": .., "v": ..}
Ro'yxat eskidan yangiga, FAQAT YOPILGAN shamlar.

Ikki turdagi signal:
  breakout  — oxirgi sham oldingi N ta shamning eng yuqorisidan yuqori (BUY) yoki
              eng pastidan past (SELL) yopildi, trend shu tomonga, RSI haddan oshmagan.
  reversal  — RSI 30 dan yuqoriga (BUY) yoki 70 dan pastga (SELL) qaytdi va sham
              shu tomonga yopildi (trendga qarshi, shuning uchun qat'iyroq talab).

Har signalga "kuch" (⭐ 1-4) beriladi; AUTO_MIN_STARS dan past signal yuborilmaydi.
Bu qoidalar foyda kafolatlamaydi — ular faqat ma'lumot beruvchi filtr.
"""

from dataclasses import dataclass


@dataclass
class Params:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_len: int = 14
    atr_len: int = 14
    lookback: int = 20          # breakout uchun oldingi shamlar soni
    sl_atr: float = 1.5         # SL masofasi = sl_atr * ATR
    tp_atr_breakout: float = 2.5
    tp_atr_reversal: float = 2.0
    min_stars: int = 2
    rsi_buy_max: float = 75.0   # breakout BUY da RSI bundan yuqori bo'lsa — kech qolgan
    rsi_sell_min: float = 25.0
    vol_spike: float = 1.3      # hajm o'rtachadan necha barobar katta bo'lsa "kuchli"
    body_atr: float = 0.8       # sham tanasi ATR ning shuncha qismidan katta bo'lsa "kuchli"


# ───────────────────────── indikatorlar ─────────────────────────

def ema(values, n):
    """Oddiy EMA. Birinchi n-1 qiymat None."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, n=14):
    """Wilder RSI. Birinchi n qiymat None."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n

    def val(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100 - 100 / (1 + ag / al)

    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = val(ag, al)
    return out


def atr(candles, n=14):
    """Wilder ATR. Birinchi n qiymat None."""
    out = [None] * len(candles)
    if len(candles) <= n:
        return out
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = candles[i - 1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(candles)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ───────────────────────── tahlil ─────────────────────────

def stars_text(n: int) -> str:
    return "⭐" * max(1, min(4, n))


def analyze(candles, p: Params = None):
    """
    Oxirgi YOPILGAN shamni tahlil qiladi.
    Qaytaradi: {
      "ok": bool, "why": str (ok=False bo'lsa sabab),
      "t", "price", "trend", "rsi", "atr", "hi", "lo", "ema_fast", "ema_slow", "vol_ratio",
      "signal": None yoki {"side","setup","stars","sl_dist","tp_dist","reason"},
      "notes": [signal chiqmagani sababi...]
    }
    """
    p = p or Params()
    need = max(p.ema_slow, p.rsi_len, p.atr_len, p.lookback) + 5
    if len(candles) < need:
        return {"ok": False, "why": f"shamlar yetarli emas ({len(candles)}/{need})"}

    closes = [c["c"] for c in candles]
    ef, es = ema(closes, p.ema_fast), ema(closes, p.ema_slow)
    rs, at = rsi(closes, p.rsi_len), atr(candles, p.atr_len)
    last, prev = candles[-1], candles[-2]
    a, r, r_prev = at[-1], rs[-1], rs[-2]
    if None in (a, r, r_prev, ef[-1], es[-1]) or a <= 0:
        return {"ok": False, "why": "indikatorlar hisoblanmadi"}

    window = candles[-1 - p.lookback:-1]
    hi, lo = max(c["h"] for c in window), min(c["l"] for c in window)

    if ef[-1] > es[-1] and last["c"] > es[-1]:
        trend = "UP"
    elif ef[-1] < es[-1] and last["c"] < es[-1]:
        trend = "DOWN"
    else:
        trend = "FLAT"

    vols = [c.get("v") or 0 for c in candles[-1 - p.lookback:-1]]
    avg_v = sum(vols) / len(vols) if vols else 0
    vol_ratio = (last.get("v") or 0) / avg_v if avg_v > 0 else None

    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    out = {
        "ok": True, "t": last["t"], "price": last["c"], "trend": trend, "rsi": r, "atr": a,
        "hi": hi, "lo": lo, "ema_fast": ef[-1], "ema_slow": es[-1], "vol_ratio": vol_ratio,
        "signal": None, "notes": [],
    }

    def bonus(side):
        """Qo'shimcha kuch ballari: katta tana, hajm portlashi, sham yopilishi chekkaga yaqin."""
        s, why = 0, []
        if body >= p.body_atr * a:
            s += 1
            why.append("katta sham tanasi")
        if vol_ratio is not None and vol_ratio >= p.vol_spike:
            s += 1
            why.append(f"hajm {vol_ratio:.1f}x")
        if rng > 0:
            pos = (last["c"] - last["l"]) / rng
            if (side == "BUY" and pos >= 0.75) or (side == "SELL" and pos <= 0.25):
                s += 1
                why.append("sham chekkada yopildi")
        return s, why

    cands = []

    # 1) Breakout
    if last["c"] > hi:
        if trend != "UP":
            out["notes"].append(f"yuqoriga breakout bor, lekin trend {trend} — o'tkazib yuborildi")
        elif r >= p.rsi_buy_max:
            out["notes"].append(f"yuqoriga breakout, lekin RSI {r:.0f} juda baland (kech)")
        else:
            s, why = bonus("BUY")
            cands.append(("BUY", "breakout", 1 + s,
                          f"{p.lookback} sham maksimumi ({hi:.2f}) yuqoriga yorib o'tildi, trend yuqoriga, RSI {r:.0f}"
                          + (f"; {', '.join(why)}" if why else ""), p.tp_atr_breakout))
    elif last["c"] < lo:
        if trend != "DOWN":
            out["notes"].append(f"pastga breakout bor, lekin trend {trend} — o'tkazib yuborildi")
        elif r <= p.rsi_sell_min:
            out["notes"].append(f"pastga breakout, lekin RSI {r:.0f} juda past (kech)")
        else:
            s, why = bonus("SELL")
            cands.append(("SELL", "breakout", 1 + s,
                          f"{p.lookback} sham minimumi ({lo:.2f}) pastga yorib o'tildi, trend pastga, RSI {r:.0f}"
                          + (f"; {', '.join(why)}" if why else ""), p.tp_atr_breakout))

    # 2) RSI reversal (trendga qarshi — qat'iyroq)
    if r_prev < 30 <= r and last["c"] > last["o"]:
        s, why = bonus("BUY")
        low_wick = min(last["o"], last["c"]) - last["l"]
        if low_wick >= body and body > 0:
            s += 1
            why.append("pastki soya (rad etish)")
        cands.append(("BUY", "reversal", 1 + s,
                      f"RSI {r_prev:.0f} → {r:.0f}: sotilib ketgan hududdan qaytdi"
                      + (f"; {', '.join(why)}" if why else ""), p.tp_atr_reversal))
    elif r_prev > 70 >= r and last["c"] < last["o"]:
        s, why = bonus("SELL")
        up_wick = last["h"] - max(last["o"], last["c"])
        if up_wick >= body and body > 0:
            s += 1
            why.append("yuqori soya (rad etish)")
        cands.append(("SELL", "reversal", 1 + s,
                      f"RSI {r_prev:.0f} → {r:.0f}: haddan ortiq sotib olingan hududdan qaytdi"
                      + (f"; {', '.join(why)}" if why else ""), p.tp_atr_reversal))

    if cands:
        side, setup, stars, reason, tp_mult = max(cands, key=lambda c: c[2])
        if stars < p.min_stars:
            out["notes"].append(f"{side} {setup} topildi, lekin kuchi {stars}⭐ < {p.min_stars}⭐ (yuborilmadi)")
        else:
            out["signal"] = {
                "side": side, "setup": setup, "stars": stars, "reason": reason,
                "sl_dist": p.sl_atr * a, "tp_dist": tp_mult * a,
            }
    if not out["signal"] and not out["notes"]:
        out["notes"].append("shart bajarilmadi (breakout ham, RSI qaytishi ham yo'q)")
    return out


def levels(side: str, entry: float, sl_dist: float, tp_dist: float):
    """(sl, tp) narxlari."""
    if side == "BUY":
        return entry - sl_dist, entry + tp_dist
    return entry + sl_dist, entry - tp_dist
