"""
Kunlik hisobot uchun sof (tarmoqsiz) hisob-kitob. main.py shu yerdan foydalanadi.

history yozuvi (main.record_history yozadi):
  {"id", "symbol", "side", "auto": bool, "setup", "stars", "entry", "exit",
   "result": "TP" | "SL" | "TIMEOUT" | "PIPS" | "NOFILL", "pips", "r", "opened", "closed"}
NOFILL — pending signal kirish narxiga yetmay bekor bo'lgan (savdo ochilmagan, pips yo'q).
"""

DAY = 86400


def local_day_bounds(now: float, tz_hours: float):
    """Joriy mahalliy kunning (boshi, oxiri) — UTC soniyalarda."""
    off = tz_hours * 3600
    start = int((now + off) // DAY) * DAY - off
    return start, start + DAY


def in_range(entries, start, end):
    return [e for e in entries if start <= e.get("closed", 0) < end]


def _bucket():
    return {"n": 0, "tp": 0, "sl": 0, "other": 0, "pips": 0.0}


def _add(b, e):
    b["n"] += 1
    b["pips"] += e.get("pips") or 0.0
    r = e.get("result")
    if r == "TP":
        b["tp"] += 1
    elif r == "SL":
        b["sl"] += 1
    else:
        b["other"] += 1


def aggregate(entries, auto_only=True):
    """Yopilgan signallar statistikasi. auto_only=True: asosiy statistika faqat avto-signallar;
    qo'lda/zona/TradingView signallar `manual` bo'limida alohida."""
    auto = [e for e in entries if e.get("auto") and e.get("result") != "NOFILL"]
    indi = [e for e in entries if e.get("src") == "indikator" and e.get("result") != "NOFILL"]
    manual = [e for e in entries if not e.get("auto") and e.get("src") != "indikator" and e.get("result") != "NOFILL"]
    main_set = auto if auto_only else auto + manual
    out = {
        "total": _bucket(), "side": {"BUY": _bucket(), "SELL": _bucket()}, "symbol": {},
        "r_total": 0.0, "best": None, "worst": None,
        "nofill": sum(1 for e in entries if e.get("auto") and e.get("result") == "NOFILL"),
        "manual": _bucket(), "indi": _bucket(), "indi_r": 0.0, "indi_strong": _bucket(), "indi_strong_r": 0.0,
    }
    for e in main_set:
        _add(out["total"], e)
        if e.get("side") in out["side"]:
            _add(out["side"][e["side"]], e)
        _add(out["symbol"].setdefault(e.get("symbol", "?"), _bucket()), e)
        out["r_total"] += e.get("r") or 0.0
    if main_set:
        out["best"] = max(main_set, key=lambda e: e.get("pips") or 0.0)
        out["worst"] = min(main_set, key=lambda e: e.get("pips") or 0.0)
    for e in manual:
        _add(out["manual"], e)
    for e in indi:
        _add(out["indi"], e)
        out["indi_r"] += e.get("r") or 0.0
        if e.get("strong"):
            _add(out["indi_strong"], e)
            out["indi_strong_r"] += e.get("r") or 0.0
    n = out["total"]["n"]
    out["win_rate"] = out["total"]["tp"] / n if n else None
    return out


def prune(entries, now, keep_days=90):
    cutoff = now - keep_days * DAY
    return [e for e in entries if e.get("closed", now) >= cutoff]
