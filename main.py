"""
Zona signal bot — ikki xil ishlaydi:

1) TradingView alert -> webhook (/webhook) -> Telegram
2) Telegram'da /zona buyrug'i bilan zona yozasiz, bot narxni o'zi kuzatadi
   (Binance narxlari) va narx zonaga yetganda signal beradi.

Railway Variables:
  BOT_TOKEN       - @BotFather bergan token
  CHAT_ID         - signal boradigan ID (bir nechta bo'lsa vergul bilan)
  WEBHOOK_SECRET  - o'zingiz o'ylab topgan maxfiy so'z
  ADMIN_ID        - (ixtiyoriy) botga buyruq bera oladigan odam ID'si.
                    Berilmasa, CHAT_ID dagi shaxsiy (minus'siz) ID'lar admin bo'ladi.
  CHECK_INTERVAL  - (ixtiyoriy) narxni necha soniyada tekshirish, standart 10
  DATA_DIR        - (ixtiyoriy) zonalar saqlanadigan papka (Railway Volume, masalan /data)
  PIP_TARGETS     - (ixtiyoriy) signaldan keyin qaysi pips'larda xabar berish, standart "50,100,150,200"
  TRACK_HOURS     - (ixtiyoriy) signal necha soat kuzatiladi, standart 48
  PIP_SIZES       - (ixtiyoriy) 1 pip qiymati, masalan "XAUUSD:0.1,BTCUSDT:1"
  FEED_MAX_AGE    - (ixtiyoriy) MT5 narxi necha soniyagacha yangi hisoblanadi, standart 60

Avto-signal (strategy.py) — bot oltin va bitcoin shamlarini o'zi tahlil qilib signal beradi:
  AUTO_MODE       - "test" (standart: signal faqat adminga) | "kanal" (CHAT_ID ga) | "off"
  AUTO_SYMBOLS    - kuzatiladigan juftliklar, standart "XAUUSD,BTCUSDT"
  AUTO_TF         - sham taymfreymi: 5m, 15m (standart), 30m, 1h
  AUTO_MIN_STARS  - eng kam signal kuchi (1-4), standart 2
  AUTO_COOLDOWN_MIN - bir juftlik+yo'nalishda signallar orasidagi tanaffus, standart 60
  AUTO_MAX_PER_DAY  - bir juftlikka kuniga eng ko'p signal, standart 5
  AUTO_TEST_CHAT  - (ixtiyoriy) test signallari boradigan ID; bo'lmasa ADMIN_ID
  AUTO_ENTRY      - "pending" (standart: avval ⏳ kutilmoqda, narx kirishga yetganda ✅ AKTIV) | "market" (darrov kirish)
  AUTO_PULLBACK_ATR - pending kirish narxi signal narxidan ATR ning shuncha qismi qulayroq, standart 0.5
  AUTO_PENDING_MIN  - kutish muddati (daqiqa), standart 120; shundan keyin signal bekor bo'ladi
Rejimni Telegram'dan o'zgartirish: /avto test | /avto kanal | /avto off

MT5 (Exness) narxi: MT5'dagi ZonaPriceFeed EA har 3 soniyada /price ga narx yuboradi.
Yangi MT5 narxi bo'lsa, bot shuni ishlatadi, bo'lmasa Bitget / gold-api / Binance'ga o'tadi.
"""

import os
import json
import html
import asyncio
import logging
import math
import re
import time
from collections import deque
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

import strategy
import report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zona-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_ID", "").split(",") if c.strip()]
SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
PORT = int(os.environ.get("PORT", 8080))
CHECK_INTERVAL = float(os.environ.get("CHECK_INTERVAL", 10))
ADMIN_IDS = {a.strip() for a in os.environ.get("ADMIN_ID", "").split(",") if a.strip()} or {
    c for c in CHAT_IDS if not c.startswith("-")
}
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
ZONES_FILE = DATA_DIR / "zones.json"
PRICE_API = os.environ.get("PRICE_API", "https://data-api.binance.vision/api/v3/ticker/price")
METAL_API = os.environ.get("METAL_API", "https://api.gold-api.com/price")
METAL_CACHE = float(os.environ.get("METAL_CACHE", 5))
BITGET_API = os.environ.get("BITGET_API", "https://api.bitget.com/api/v2/mix/market/ticker")
# Bitget'dagi oltin/kumush kontraktlari. "index" — spot oltin indeksi (Exness kabi spot brokerlarga yaqin),
# "last" — kontraktning o'z narxi.
BITGET_SYMBOLS = {"XAUUSD": "XAUUSDT", "XAGUSD": "XAGUSDT"}
GOLD_PRICE_FIELD = os.environ.get("GOLD_PRICE_FIELD", "index").lower()
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PIP_TARGETS = sorted({int(x) for x in os.environ.get("PIP_TARGETS", "50,100,150,200").split(",") if x.strip().isdigit()})
TRACK_HOURS = float(os.environ.get("TRACK_HOURS", 48))
# XAUUSD: 1 pip = 0.10$ (100 pips = 10$). XAGUSD: 1 pip = 0.01$.
PIP_SIZES = {"XAUUSD": 0.1, "XAGUSD": 0.01}
for _item in os.environ.get("PIP_SIZES", "").split(","):
    if ":" in _item:
        _k, _v = _item.split(":", 1)
        PIP_SIZES[_k.strip().upper()] = float(_v)

# ── avto-signal sozlamalari ──
AUTO_FILE = DATA_DIR / "auto_state.json"
AUTO_SYMBOLS = [s for s in (x.strip().upper() for x in os.environ.get("AUTO_SYMBOLS", "XAUUSD,BTCUSDT").split(",")) if s]
AUTO_TF = os.environ.get("AUTO_TF", "15m").strip().lower()
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
if AUTO_TF not in TF_SECONDS:
    AUTO_TF = "15m"
AUTO_INTERVAL = float(os.environ.get("AUTO_INTERVAL", 60))
AUTO_COOLDOWN = float(os.environ.get("AUTO_COOLDOWN_MIN", 60)) * 60
AUTO_MAX_PER_DAY = int(os.environ.get("AUTO_MAX_PER_DAY", 5))
AUTO_MIN_STARS = int(os.environ.get("AUTO_MIN_STARS", 2))
AUTO_TEST_CHATS = [c.strip() for c in os.environ.get("AUTO_TEST_CHAT", "").split(",") if c.strip()]
AUTO_ENTRY = os.environ.get("AUTO_ENTRY", "pending").strip().lower()
if AUTO_ENTRY not in ("pending", "market"):
    AUTO_ENTRY = "pending"
AUTO_PULLBACK_ATR = float(os.environ.get("AUTO_PULLBACK_ATR", 0.5))
AUTO_PENDING_MIN = float(os.environ.get("AUTO_PENDING_MIN", 120))
# kunlik hisobot: mahalliy soat (Toshkent = UTC+5) bo'yicha, har kuni REPORT_HOUR:00 da
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", 23))
TZ_HOURS = float(os.environ.get("TZ_HOURS", 5))
KLINES_API = os.environ.get("KLINES_API", "https://data-api.binance.vision/api/v3/klines")
BITGET_CANDLES_API = os.environ.get("BITGET_CANDLES_API", "https://api.bitget.com/api/v2/mix/market/candles")
BITGET_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H"}
AUTO_DISCLAIMER = "⚠️ Bu moliyaviy maslahat emas. Risk — o'zingizda, SL'siz savdo qilmang."

# Metallar Bitget (zaxira: gold-api.com) dan, qolganlari Binance'dan olinadi
METALS = {"XAUUSD": "XAU", "XAGUSD": "XAG"}
ALIASES = {
    "XAU": "XAUUSD", "GOLD": "XAUUSD", "OLTIN": "XAUUSD", "XAUUSD": "XAUUSD",
    "XAG": "XAGUSD", "SILVER": "XAGUSD", "KUMUSH": "XAGUSD", "XAGUSD": "XAGUSD",
}

http = None          # aiohttp ClientSession
zones = []           # [{id, symbol, low, high, note, state, side}]
trades = []          # signal chiqqandan keyin kuzatilayotganlar
pendings = []        # avto-signal: kirish narxiga yetishini kutayotganlar
history = []         # yopilgan signallar natijasi (kunlik hisobot uchun)
next_id = 1
tasks = []
metal_cache = {}     # {"XAUUSD": (vaqt, narx, manba)}
offsets = {}         # /tuzat bilan: {"XAUUSD": [0.85, "Bitget"]} — Exness narxi va manba orasidagi farq
feed_prices = {}     # MT5'dan kelgan narxlar: {"XAUUSD": (vaqt, bid, ask)}
FEED_MAX_AGE = float(os.environ.get("FEED_MAX_AGE", 60))
last_source = {}     # {"XAUUSD": "MT5"} — manba almashganini logga yozish uchun
# TradingView signalida BUY/SELL'ni aniqlash uchun oxirgi ~40 daqiqa narxlari
price_history = {}   # {"XAUUSD": deque([(vaqt, narx), ...])}
HISTORY_SYMBOLS = {"XAUUSD"}
recent_tv = {}       # takroriy TradingView xabarlarini o'tkazib yuborish: {xabar: vaqt}


# ───────────────────────── yordamchi funksiyalar ─────────────────────────

def esc(v) -> str:
    return html.escape(str(v), quote=False)


def fmt(p: float) -> str:
    """64000 -> '64 000', 0.00001234 -> '0.00001234'"""
    d = 2 if p >= 100 else 4 if p >= 1 else 8
    s = f"{p:,.{d}f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


def parse_num(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


def is_num(s: str) -> bool:
    try:
        parse_num(s)
        return True
    except ValueError:
        return False


def norm_symbol(s: str) -> str:
    """oltin -> XAUUSD, btc -> BTCUSDT, eth/usdt -> ETHUSDT"""
    s = s.upper().replace("/", "").replace("-", "").replace("_", "").strip()
    if s in ALIASES:
        return ALIASES[s]
    if len(s) <= 5:
        s += "USDT"
    return s


def zone_text(z) -> str:
    if z["low"] == z["high"]:
        return f"{fmt(z['low'])} (daraja)"
    return f"{fmt(z['low'])} – {fmt(z['high'])}"


SIDE_WORDS = {"BUY": "BUY", "B": "BUY", "LONG": "BUY", "SELL": "SELL", "S": "SELL", "SHORT": "SELL"}


def side_label(side) -> str:
    return {"BUY": "🟢 BUY", "SELL": "🔴 SELL"}.get(side, "⚪️ aniqlanmagan")


def side_header(side) -> str:
    if side == "BUY":
        return "🟢🟢🟢 <b>DIQQAT BUY</b> 🟢🟢🟢"
    if side == "SELL":
        return "🔴🔴🔴 <b>DIQQAT SELL</b> 🔴🔴🔴"
    return "🎯 <b>DIQQAT! Narx zonaga yetdi</b>"


# ───────────────────────── zona mantig'i ─────────────────────────

def zone_state(z, price: float) -> str:
    if z["low"] == z["high"]:
        return "above" if price >= z["low"] else "below"
    if z["low"] <= price <= z["high"]:
        return "in"
    return "above" if price > z["high"] else "below"


def check_zone(z, price: float):
    """(signal_bormi, qanday_yetdi, yangi_holat) qaytaradi."""
    prev = z.get("state")
    new = zone_state(z, price)
    if prev is None or prev == new:
        return False, "", new
    if z["low"] == z["high"]:
        how = "⬆️ Pastdan yuqoriga kesib o'tdi" if new == "above" else "⬇️ Yuqoridan pastga kesib o'tdi"
        return True, how, new
    if new == "in":
        how = "⬇️ Yuqoridan tushib kirdi" if prev == "above" else "⬆️ Pastdan ko'tarilib kirdi"
        return True, how, new
    if prev in ("above", "below"):  # tez harakatda zonani bir tekshiruvda sakrab o'tib ketdi
        how = "⚡️ Zonani tez kesib o'tdi (hozir " + ("yuqorisida)" if new == "above" else "pastida)")
        return True, how, new
    return False, "", new  # zonadan chiqdi — signal yo'q


def signal_text(z, price: float, how: str) -> str:
    lines = [
        side_header(z.get("side")),
        "",
        f"📈 Juftlik: <b>{esc(z['symbol'])}</b>",
        f"💰 Narx: <b>{fmt(price)}</b>",
        f"📍 Zona: <b>{zone_text(z)}</b>",
        how,
    ]
    if z.get("note"):
        lines.append(f"📝 {esc(z['note'])}")
    return "\n".join(lines)


# ───────────────────────── signaldan keyingi kuzatuv (pips) ─────────────────────────

def pip_size(symbol: str, price: float) -> float:
    if symbol in PIP_SIZES:
        return PIP_SIZES[symbol]
    # kripto: narxning ~0.01% i atrofida (BTC 66000 -> 1, ETH 3400 -> 0.1)
    return 10 ** (math.floor(math.log10(price)) - 4)


def find_level(note: str, word: str):
    m = re.search(rf"\b{word}\s*[:=]?\s*(\d+(?:[.,]\d+)?)", note or "", re.IGNORECASE)
    return parse_num(m.group(1)) if m else None


def pips_of(t, price: float) -> float:
    d = (price - t["entry"]) if t["side"] == "BUY" else (t["entry"] - price)
    return d / t["pip"]


def fmt_pips(p: float) -> str:
    return f"{'+' if p >= 0 else '−'}{abs(p):.0f}"


def open_trade(z, price: float, msg_ids: dict):
    side = z.get("side")
    if side not in ("BUY", "SELL"):
        return None
    sl = z["sl"] if z.get("sl") is not None else find_level(z.get("note"), "SL")
    tp = z["tp"] if z.get("tp") is not None else find_level(z.get("note"), "TP")
    # noto'g'ri tomondagi SL/TP e'tiborga olinmaydi
    if sl is not None and ((side == "BUY" and sl >= price) or (side == "SELL" and sl <= price)):
        sl = None
    if tp is not None and ((side == "BUY" and tp <= price) or (side == "SELL" and tp >= price)):
        tp = None
    t = {
        "id": z["id"], "symbol": z["symbol"], "side": side, "entry": price,
        "pip": pip_size(z["symbol"], price), "sl": sl, "tp": tp, "hit": [], "best": 0.0,
        "opened": time.time(), "msgs": {str(k): v for k, v in msg_ids.items()},
        "chats": [str(k) for k in msg_ids],  # kuzatuv xabarlari faqat signal borgan chatlarga boradi
    }
    if z.get("auto"):
        t["auto"] = True
        t["setup"], t["stars"] = z.get("setup"), z.get("stars")
    trades.append(t)
    return t


def trade_head(t) -> str:
    icon = "🟢" if t["side"] == "BUY" else "🔴"
    return f"{icon} {esc(t['symbol'])} {t['side']} signal bo'yicha"


def check_trade(t, price: float):
    """(xabar yoki None, yopilsinmi) qaytaradi."""
    move = pips_of(t, price)
    t["best"] = max(t["best"], move)
    line = f"Kirish: {fmt(t['entry'])} → Hozir: {fmt(price)}"

    # 1) SL
    if t["sl"] is not None and ((t["side"] == "BUY" and price <= t["sl"]) or (t["side"] == "SELL" and price >= t["sl"])):
        sl_pips = pips_of(t, t["sl"])
        text = [f"❌ <b>SL urildi ({fmt_pips(sl_pips)} pips)</b>", trade_head(t),
                f"Kirish: {fmt(t['entry'])} → SL: {fmt(t['sl'])}"]
        if t["best"] >= 1:
            text.append(f"Eng yaxshi natija: {fmt_pips(t['best'])} pips")
        t["result"], t["exit"] = "SL", t["sl"]
        return "\n".join(text), True

    # 2) TP
    if t["tp"] is not None and ((t["side"] == "BUY" and price >= t["tp"]) or (t["side"] == "SELL" and price <= t["tp"])):
        tp_pips = pips_of(t, t["tp"])
        t["result"], t["exit"] = "TP", t["tp"]
        return "\n".join([f"🏆 <b>TP urildi! {fmt_pips(tp_pips)} pips</b> 🔥", trade_head(t),
                          f"Kirish: {fmt(t['entry'])} → TP: {fmt(t['tp'])}"]), True

    # 3) pips maqsadlari (50, 100, ...)
    new_hits = [p for p in PIP_TARGETS if p not in t["hit"] and move >= p]
    if new_hits:
        t["hit"].extend(new_hits)
        top = max(new_hits)
        last = t["tp"] is None and top >= PIP_TARGETS[-1]
        title = f"🏁 <b>Signal yakunlandi: +{top} pips</b> 🔥" if last else f"✅ <b>+{top} pips</b> 🔥"
        if last:
            t["result"], t["exit"] = "PIPS", price
        return "\n".join([title, trade_head(t), line]), last

    # 4) vaqt tugadi
    if time.time() - t["opened"] > TRACK_HOURS * 3600:
        log.info("#%s kuzatuv vaqti tugadi (eng yaxshi %.0f pips)", t["id"], t["best"])
        t["result"], t["exit"] = "TIMEOUT", price
        return "\n".join([f"⌛ <b>Kuzatuv muddati tugadi ({TRACK_HOURS:.0f} soat): {fmt_pips(move)} pips</b>",
                          trade_head(t), line]), True
    return None, False


# ───────────────────────── saqlash ─────────────────────────

def load_zones():
    global zones, next_id
    try:
        data = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
        zones, next_id = data.get("zones", []), data.get("next_id", 1)
        trades[:] = data.get("trades", [])
        pendings[:] = data.get("pendings", [])
        history[:] = data.get("history", [])
        offsets.clear()
        offsets.update({k: v for k, v in data.get("offsets", {}).items() if isinstance(v, list)})
        log.info("%d ta zona, %d ta kuzatilayotgan signal yuklandi", len(zones), len(trades))
    except FileNotFoundError:
        zones, next_id = [], 1
    except Exception:
        log.exception("zones.json o'qib bo'lmadi, bo'sh ro'yxat bilan boshlanadi")
        zones, next_id = [], 1


def save_zones():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ZONES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next_id": next_id, "zones": zones, "trades": trades, "pendings": pendings, "history": history, "offsets": offsets}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ZONES_FILE)


# ───────────────────────── tashqi API'lar ─────────────────────────

async def tg(method: str, **payload):
    try:
        async with http.post(f"{TG_API}/{method}", json=payload) as r:
            data = await r.json()
            if not data.get("ok"):
                log.error("Telegram %s xato: %s", method, data)
            return data
    except Exception as e:
        log.exception("Telegram %s ishlamadi: %s", method, e)
        return {"ok": False}


async def send(chat_id, text: str, reply_to=None):
    payload = dict(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    data = await tg("sendMessage", **payload)
    return (data.get("result") or {}).get("message_id") if isinstance(data, dict) else None


async def broadcast(text: str, reply_to: dict = None, chats=None):
    """Hamma CHAT_ID ga (yoki berilgan `chats` ga) yuboradi, {chat_id: message_id} qaytaradi."""
    ids = {}
    for chat_id in (chats or CHAT_IDS):
        mid = await send(chat_id, text, reply_to=(reply_to or {}).get(str(chat_id)))
        if mid:
            ids[str(chat_id)] = mid
    return ids


async def fetch_bitget(symbol: str):
    """Bitget'dan oltin/kumush narxi. Eskirgan (2 daqiqadan eski) narxni qaytarmaydi."""
    params = {"symbol": BITGET_SYMBOLS[symbol], "productType": "USDT-FUTURES"}
    async with http.get(BITGET_API, params=params) as r:
        data = await r.json()
    if str(data.get("code")) != "00000" or not data.get("data"):
        raise ValueError(f"Bitget javobi: {data}")
    d = data["data"][0] if isinstance(data["data"], list) else data["data"]
    ts = float(d.get("ts") or 0) / 1000
    if ts and time.time() - ts > 120:
        raise ValueError(f"Bitget narxi eskirgan ({time.time() - ts:.0f}s)")
    if GOLD_PRICE_FIELD == "index" and d.get("indexPrice"):
        return float(d["indexPrice"])
    return float(d.get("bidPr") or d["lastPr"])


async def fetch_goldapi(symbol: str):
    """gold-api.com — zaxira manba. 10 daqiqadan eski narxni qabul qilmaydi."""
    from datetime import datetime, timezone
    async with http.get(f"{METAL_API}/{METALS[symbol]}") as r:
        if r.status != 200:
            raise ValueError(f"{r.status}: {await r.text()}")
        data = await r.json()
    upd = data.get("updatedAt")
    if upd:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(upd.replace("Z", "+00:00"))).total_seconds()
        if age > 600:
            raise ValueError(f"gold-api narxi eskirgan ({age / 3600:.1f} soat)")
    return float(data["price"])


async def fetch_metal_raw(symbol: str):
    """(narx, manba) — tuzatishsiz. Avval Bitget, bo'lmasa gold-api."""
    now = asyncio.get_running_loop().time()
    cached = metal_cache.get(symbol)
    if cached and now - cached[0] < METAL_CACHE:
        return cached[1], cached[2]
    for name, fn in (("Bitget", fetch_bitget), ("gold-api.com", fetch_goldapi)):
        try:
            price = await fn(symbol)
            metal_cache[symbol] = (now, price, name)
            return price, name
        except Exception as e:
            log.warning("%s: %s narxi olinmadi: %s", name, symbol, e)
    if cached and now - cached[0] < 120:
        return cached[1], cached[2]
    return None, None


async def fetch_metal(symbol: str):
    """Exness'ga moslangan narx: manba narxi + /tuzat farqi."""
    price, src = await fetch_metal_raw(symbol)
    if price is None:
        return None
    return price + offset_for(symbol, src)


def offset_for(symbol: str, src) -> float:
    """Tuzatish faqat u hisoblangan manbaga qo'llanadi (Bitget farqi gold-api'ga qo'shilmaydi)."""
    o = offsets.get(symbol)
    if isinstance(o, list) and len(o) == 2 and o[1] == src:
        return float(o[0])
    return 0.0


def feed_price(symbol: str):
    """MT5'dan kelgan yangi narx (bid) yoki None."""
    f = feed_prices.get(symbol)
    if f and time.time() - f[0] <= FEED_MAX_AGE:
        return f[1]
    return None


def price_source(symbol: str) -> str:
    if feed_price(symbol) is not None:
        return "Exness MT5"
    if symbol in METALS:
        c = metal_cache.get(symbol)
        name = c[2] if c else "Bitget"
        off = offset_for(symbol, name)
        return f"{name}, Exness'ga moslangan ({off:+.2f})" if off else name
    return "Binance"


async def fetch_prices(symbols):
    """{'BTCUSDT': 64500.1, 'XAUUSD': 4165.2, ...}. Topilmaganlar natijada bo'lmaydi."""
    symbols = sorted(set(symbols))
    out = {}
    # 1) MT5 (Exness) narxi bo'lsa — eng aniq manba
    for s in symbols:
        p = feed_price(s)
        if p is not None:
            out[s] = p
        src = price_source(s)
        if last_source.get(s) != src:
            if s in last_source:
                log.warning("%s narx manbasi o'zgardi: %s -> %s", s, last_source[s], src)
            last_source[s] = src
    symbols = [s for s in symbols if s not in out]
    for s in [s for s in symbols if s in METALS]:
        p = await fetch_metal(s)
        if p is not None:
            out[s] = p
    crypto = [s for s in symbols if s not in METALS]
    if crypto:
        try:
            out.update(await fetch_binance(crypto))
        except Exception as e:
            log.warning("Binance narxlari olinmadi: %s", e)
    return out


async def fetch_binance(symbols):
    """{'BTCUSDT': 64500.1, ...}. Topilmagan juftliklar natijada bo'lmaydi."""
    if not symbols:
        return {}

    async def get(params):
        async with http.get(PRICE_API, params=params) as r:
            if r.status != 200:
                raise ValueError(f"{r.status}: {await r.text()}")
            data = await r.json()
            data = data if isinstance(data, list) else [data]
            return {d["symbol"]: float(d["price"]) for d in data}

    if len(symbols) == 1:
        try:
            return await get({"symbol": symbols[0]})
        except ValueError:
            return {}
    try:
        return await get({"symbols": json.dumps(symbols, separators=(",", ":"))})
    except ValueError:
        # bittasi noto'g'ri bo'lsa, qolganlarini alohida olamiz
        out = {}
        for s in symbols:
            try:
                out.update(await get({"symbol": s}))
            except ValueError:
                log.warning("%s narxi olinmadi", s)
        return out


# ───────────────────────── narx kuzatuvchi ─────────────────────────

def shift_history(symbol: str, delta: float):
    """/tuzat farqi o'zgarganda eslab qolingan narxlarni ham suramiz, aks holda yo'nalish xato aniqlanadi."""
    h = price_history.get(symbol)
    if h and delta:
        price_history[symbol] = deque(((t, p + delta) for t, p in h), maxlen=h.maxlen)


def remember_prices(prices: dict):
    now = time.time()
    for sym, p in prices.items():
        h = price_history.setdefault(sym, deque(maxlen=300))
        h.append((now, p))


def detect_side(symbol: str, price: float, data: dict):
    """Narx zonaga qayerdan kelganiga qarab: yuqoridan tushgan -> BUY, pastdan chiqqan -> SELL."""
    h = price_history.get(symbol)
    if h:
        now = time.time()
        # 3-15 daqiqa oldingi narxlar o'rtachasi bilan solishtiramiz
        old = [p for t, p in h if 180 <= now - t <= 900]
        if old:
            ref = sum(old) / len(old)
            if abs(price - ref) >= price * 0.00005:  # oltinda ~0.2$ dan katta farq
                return "BUY" if price < ref else "SELL"
    # zaxira: shamning ochilishi bilan yopilishini solishtirish
    try:
        o, c = float(data.get("open")), float(data.get("price"))
        if c < o:
            return "BUY"
        if c > o:
            return "SELL"
    except (TypeError, ValueError):
        pass
    return None


async def check_once():
    wanted = ([z["symbol"] for z in zones] + [t["symbol"] for t in trades]
              + [p["symbol"] for p in pendings] + list(HISTORY_SYMBOLS))
    prices = await fetch_prices(wanted)
    remember_prices(prices)
    if not zones and not trades and not pendings:
        return
    fired, changed = [], False
    for z in zones:
        price = prices.get(z["symbol"])
        if price is None:
            continue
        hit, how, new = check_zone(z, price)
        if new != z.get("state"):
            z["state"], changed = new, True
        if hit:
            fired.append((z, price, how))
    if fired:
        ids = {z["id"] for z, _, _ in fired}
        zones[:] = [z for z in zones if z["id"] not in ids]
    for z, price, how in fired:
        log.info("Signal: #%s %s %s", z["id"], z["symbol"], price)
        msg_ids = await broadcast(signal_text(z, price, how))
        open_trade(z, price, msg_ids)

    # kutilayotgan avto-signallar: kirish narxiga yetdimi / bekor bo'ldimi
    if pendings and await check_pendings(prices):
        changed = True

    # ochiq signallarni kuzatish: +50, +100 pips, SL, TP
    closed = set()
    for t in list(trades):
        price = prices.get(t["symbol"])
        if price is None:
            continue
        before = (t["best"], len(t["hit"]))
        text, done = check_trade(t, price)
        if (t["best"], len(t["hit"])) != before:
            changed = True
        if text:
            log.info("#%s: %s", t["id"], text.splitlines()[0])
            await broadcast(text, reply_to=t.get("msgs"), chats=t.get("chats"))
        if done:
            closed.add(t["id"])
    if closed:
        for t in trades:
            if t["id"] in closed:
                record_history(t)
        trades[:] = [t for t in trades if t["id"] not in closed]
    if changed or fired or closed:
        save_zones()


async def price_loop():
    while True:
        try:
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Narx tekshiruvida xato")
        await asyncio.sleep(CHECK_INTERVAL)


# ───────────────────────── avto-signal (oltin + bitcoin) ─────────────────────────

PARAMS = strategy.Params(min_stars=AUTO_MIN_STARS)
auto = {"mode": os.environ.get("AUTO_MODE", "test").strip().lower(), "last_bar": {}, "last_sig": {}, "day": "", "count": {}}
if auto["mode"] not in ("test", "kanal", "off"):
    auto["mode"] = "test"


def auto_load():
    try:
        data = json.loads(AUTO_FILE.read_text(encoding="utf-8"))
        if data.get("mode") in ("test", "kanal", "off"):
            auto["mode"] = data["mode"]
        auto["last_report"] = data.get("last_report", "")
        for k in ("last_bar", "last_sig", "count"):
            if isinstance(data.get(k), dict):
                auto[k] = data[k]
        auto["day"] = data.get("day", "")
        log.info("Avto-signal holati yuklandi: rejim=%s", auto["mode"])
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("auto_state.json o'qib bo'lmadi, standart holat")


def auto_save():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(auto, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(AUTO_FILE)


def test_targets():
    return AUTO_TEST_CHATS or sorted(a for a in ADMIN_IDS if not a.startswith("-"))


def auto_targets():
    if auto["mode"] == "kanal":
        return list(CHAT_IDS)
    if auto["mode"] == "test":
        return test_targets()
    return []


async def fetch_candles(symbol: str, tf: str = None, limit: int = 200):
    """Yopilgan shamlar, eskidan yangiga. Oltin/kumush — Bitget, qolganlari — Binance."""
    tf = tf or AUTO_TF
    step = TF_SECONDS[tf]
    rows = []
    if symbol in BITGET_SYMBOLS:
        params = {"symbol": BITGET_SYMBOLS[symbol], "productType": "USDT-FUTURES",
                  "granularity": BITGET_TF[tf], "limit": str(limit)}
        async with http.get(BITGET_CANDLES_API, params=params) as r:
            data = await r.json()
        if str(data.get("code")) != "00000" or not data.get("data"):
            raise ValueError(f"Bitget sham javobi: {str(data)[:200]}")
        for k in data["data"]:
            rows.append({"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    else:
        async with http.get(KLINES_API, params={"symbol": symbol, "interval": tf, "limit": limit}) as r:
            if r.status != 200:
                raise ValueError(f"Binance klines {r.status}: {(await r.text())[:200]}")
            data = await r.json()
        for k in data:
            rows.append({"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    rows.sort(key=lambda c: c["t"])
    now = time.time()
    return [c for c in rows if c["t"] + step <= now + 2]  # hali shakllanayotgan shamni tashlaymiz


def auto_signal_text(symbol, price, sig, sl, tp, test: bool) -> str:
    ps = pip_size(symbol, price)
    rr = sig["tp_dist"] / sig["sl_dist"]
    setup = {"breakout": "Breakout (yorib o'tish)", "reversal": "RSI qaytishi"}.get(sig["setup"], sig["setup"])
    lines = [
        side_header(sig["side"]),
        "🤖 Avto-signal" + (" · 🧪 <b>TEST rejimi</b>" if test else ""),
        "",
        f"📈 Juftlik: <b>{esc(symbol)}</b> · {AUTO_TF}",
        f"💰 Kirish: <b>{fmt(price)}</b>",
        f"🛑 SL: <b>{fmt(sl)}</b> ({fmt_pips(-sig['sl_dist'] / ps)} pips)",
        f"🎯 TP: <b>{fmt(tp)}</b> ({fmt_pips(sig['tp_dist'] / ps)} pips)",
        f"⚖️ Risk : Foyda = 1 : {rr:.2f}",
        f"📊 Setap: {setup} · Kuch: {strategy.stars_text(sig['stars'])}",
        f"📝 {esc(sig['reason'])}",
        "",
        AUTO_DISCLAIMER,
    ]
    return "\n".join(lines)


def _rr_lines(symbol, entry, sl, tp, sl_dist, tp_dist):
    ps = pip_size(symbol, entry)
    return [
        f"🛑 SL: <b>{fmt(sl)}</b> ({fmt_pips(-sl_dist / ps)} pips)",
        f"🎯 TP: <b>{fmt(tp)}</b> ({fmt_pips(tp_dist / ps)} pips)",
        f"⚖️ Risk : Foyda = 1 : {tp_dist / sl_dist:.2f}",
    ]


def pending_text(p, price, test: bool) -> str:
    icon = "🟢" if p["side"] == "BUY" else "🔴"
    setup = {"breakout": "Breakout (yorib o'tish)", "reversal": "RSI qaytishi"}.get(p["setup"], p["setup"])
    way = "tushganda" if p["side"] == "BUY" else "ko'tarilganda"
    return "\n".join([
        f"⏳{icon} <b>SIGNAL KUTILMOQDA: {p['side']}</b>",
        "🤖 Avto-signal" + (" · 🧪 <b>TEST rejimi</b>" if test else ""),
        "",
        f"📈 Juftlik: <b>{esc(p['symbol'])}</b> · {AUTO_TF}",
        f"💰 Hozirgi narx: {fmt(price)}",
        f"📥 Kirish: <b>{fmt(p['entry'])}</b> — narx shu darajaga {way} signal AKTIV bo'ladi",
        *_rr_lines(p["symbol"], p["entry"], p["sl"], p["tp"], p["sl_dist"], p["tp_dist"]),
        f"📊 Setap: {setup} · Kuch: {strategy.stars_text(p['stars'])}",
        f"📝 {esc(p['reason'])}",
        "",
        f"⏱ {AUTO_PENDING_MIN:.0f} daqiqada yetmasa yoki narx kirishsiz {fmt(p['tp'])} ga ketsa — signal bekor bo'ladi.",
        AUTO_DISCLAIMER,
    ])


def activated_text(p, price) -> str:
    icon = "🟢" if p["side"] == "BUY" else "🔴"
    return "\n".join([
        f"✅{icon} <b>SIGNAL AKTIV: {p['side']}</b>",
        "",
        f"📈 {esc(p['symbol'])} · kirish narxi yetdi: <b>{fmt(price)}</b>",
        *_rr_lines(p["symbol"], price, p["sl"], p["tp"], abs(price - p["sl"]), abs(p["tp"] - price)),
        "",
        "Endi bot +pips, TP va SL ni kuzatib boradi.",
    ])


async def cancel_pending(p, reason: str, count: bool = True):
    log.info("Pending #%s %s %s bekor: %s", p["id"], p["symbol"], p["side"], reason)
    if count:
        history.append({"id": p["id"], "symbol": p["symbol"], "side": p["side"], "auto": True, "setup": p.get("setup"),
                        "stars": p.get("stars"), "entry": p.get("entry"), "exit": None, "result": "NOFILL",
                        "pips": 0.0, "r": None, "opened": p.get("created", time.time()), "closed": time.time()})
    text = (f"❌ <b>Signal bekor bo'ldi</b>\n{esc(p['symbol'])} {p['side']} (kirish {fmt(p['entry'])})\n"
            f"Sabab: {esc(reason)}\n<i>Pozitsiya ochmang — bu signal endi kuchda emas.</i>")
    await broadcast(text, reply_to=p.get("msgs"), chats=p.get("chats"))


async def check_pendings(prices: dict) -> bool:
    """Kutilayotgan signallar: kirish narxiga yetdi -> aktiv; muddat tugadi/narx ketib qoldi -> bekor."""
    now, done = time.time(), set()
    for p in list(pendings):
        price = prices.get(p["symbol"])
        buy = p["side"] == "BUY"
        if now > p["expires"]:
            await cancel_pending(p, f"{AUTO_PENDING_MIN:.0f} daqiqa ichida narx {fmt(p['entry'])} ga yetmadi")
            done.add(p["id"])
            continue
        if price is None:
            continue
        reached = price <= p["entry"] if buy else price >= p["entry"]
        if reached:
            beyond_sl = price <= p["sl"] if buy else price >= p["sl"]
            if beyond_sl:
                await cancel_pending(p, f"narx kirishdan sakrab SL ({fmt(p['sl'])}) dan o'tib ketdi")
            else:
                ids = await broadcast(activated_text(p, price), reply_to=p.get("msgs"), chats=p.get("chats"))
                ids = ids or {str(c): None for c in p.get("chats", [])}  # xabar ketmasa ham kuzatuv o'sha chatlarga
                z = {"id": p["id"], "symbol": p["symbol"], "side": p["side"], "sl": p["sl"], "tp": p["tp"], "auto": True,
                     "setup": p.get("setup"), "stars": p.get("stars")}
                open_trade(z, price, ids)
                log.info("Pending #%s %s %s AKTIV narx=%s", p["id"], p["symbol"], p["side"], price)
            done.add(p["id"])
            continue
        missed = price >= p["tp"] if buy else price <= p["tp"]
        if missed:
            await cancel_pending(p, f"narx kirishga tushmay, TP ({fmt(p['tp'])}) tomonga ketib qoldi")
            done.add(p["id"])
    if done:
        pendings[:] = [p for p in pendings if p["id"] not in done]
        save_zones()
    return bool(done)


async def auto_check_once():
    global next_id
    if auto["mode"] == "off":
        return
    targets = auto_targets()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if auto["day"] != today:
        auto["day"], auto["count"] = today, {}
    changed = False
    for sym in AUTO_SYMBOLS:
        try:
            res = strategy.analyze(await fetch_candles(sym), PARAMS)
        except Exception as e:
            log.warning("Avto-signal: %s shamlari olinmadi: %s", sym, e)
            continue
        if not res.get("ok") or auto["last_bar"].get(sym) == res["t"]:
            continue  # ma'lumot yetarli emas yoki bu shamni allaqachon ko'rganmiz
        auto["last_bar"][sym] = res["t"]
        changed = True
        sig = res["signal"]
        if not sig:
            continue
        step = TF_SECONDS[AUTO_TF]
        if time.time() - (res["t"] + step) > step:
            log.info("Avto-signal: %s eski sham (%s), o'tkazib yuborildi", sym, sig["side"])
            continue
        key = f"{sym}|{sig['side']}"
        if time.time() - auto["last_sig"].get(key, 0) < AUTO_COOLDOWN:
            log.info("Avto-signal: %s tanaffusda, o'tkazib yuborildi", key)
            continue
        if auto["count"].get(sym, 0) >= AUTO_MAX_PER_DAY:
            log.info("Avto-signal: %s bugungi limit tugadi", sym)
            continue
        if any(x["symbol"] == sym and x["side"] == sig["side"] for x in trades + pendings):
            log.info("Avto-signal: %s uchun shu yo'nalishda signal hali ochiq/kutilmoqda", key)
            continue
        if not targets:
            log.warning("Avto-signal: %s signal bor, lekin yuboriladigan chat yo'q (rejim=%s)", key, auto["mode"])
            continue
        price = (await fetch_prices([sym])).get(sym) or res["price"]
        if AUTO_ENTRY == "pending" and AUTO_PULLBACK_ATR > 0:
            pb = AUTO_PULLBACK_ATR * res["atr"]
            entry = price - pb if sig["side"] == "BUY" else price + pb
            sl, tp = strategy.levels(sig["side"], entry, sig["sl_dist"], sig["tp_dist"])
            p = {"id": next_id, "symbol": sym, "side": sig["side"], "setup": sig["setup"], "stars": sig["stars"],
                 "reason": sig["reason"], "entry": entry, "sl": sl, "tp": tp, "sl_dist": sig["sl_dist"],
                 "tp_dist": sig["tp_dist"], "created": time.time(), "expires": time.time() + AUTO_PENDING_MIN * 60}
            msg_ids = await broadcast(pending_text(p, price, auto["mode"] == "test"), chats=targets)
            if not msg_ids:
                continue
            p["msgs"], p["chats"] = msg_ids, [str(c) for c in msg_ids]
            pendings.append(p)
            next_id += 1
            auto["last_sig"][key] = time.time()
            auto["count"][sym] = auto["count"].get(sym, 0) + 1
            log.info("Avto-signal KUTILMOQDA #%s %s %s narx=%s kirish=%s SL=%s TP=%s kuch=%s rejim=%s", p["id"], sym,
                     sig["side"], price, round(entry, 2), round(sl, 2), round(tp, 2), sig["stars"], auto["mode"])
            save_zones()
            continue
        sl, tp = strategy.levels(sig["side"], price, sig["sl_dist"], sig["tp_dist"])
        z = {"id": next_id, "symbol": sym, "side": sig["side"], "sl": sl, "tp": tp, "auto": True,
             "setup": sig["setup"], "stars": sig["stars"]}
        msg_ids = await broadcast(auto_signal_text(sym, price, sig, sl, tp, auto["mode"] == "test"), chats=targets)
        if not msg_ids:
            continue
        open_trade(z, price, msg_ids)
        next_id += 1
        auto["last_sig"][key] = time.time()
        auto["count"][sym] = auto["count"].get(sym, 0) + 1
        log.info("Avto-signal #%s %s %s narx=%s SL=%s TP=%s kuch=%s rejim=%s", z["id"], sym, sig["side"],
                 price, round(sl, 2), round(tp, 2), sig["stars"], auto["mode"])
        save_zones()
    if changed:
        auto_save()


async def auto_loop():
    await asyncio.sleep(5)  # birinchi narxlar kelib olsin
    while True:
        try:
            await auto_check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Avto-signal siklida xato")
        await asyncio.sleep(AUTO_INTERVAL)


async def cmd_avto(chat_id, args):
    mode = (args[0].lower() if args else "")
    if mode in ("test", "kanal", "off"):
        if mode == "kanal" and not CHAT_IDS:
            return await send(chat_id, "❌ CHAT_ID berilmagan, kanalga yuborib bo'lmaydi.")
        auto["mode"] = mode
        auto_save()
    elif mode:
        return await send(chat_id, "Namuna: <code>/avto test</code>, <code>/avto kanal</code> yoki <code>/avto off</code>")
    names = {"test": "🧪 TEST — signal faqat adminga", "kanal": "📢 KANAL — signal CHAT_ID dagi hammaga",
             "off": "⏸ O'CHIQ"}
    tg_list = auto_targets()
    now = time.time()
    bars = []
    for s in AUTO_SYMBOLS:
        t = auto["last_bar"].get(s)
        bars.append(f"{esc(s)}: " + (f"{(now - t) / 60:.0f} daq oldingi sham" if t else "hali tekshirilmagan"))
    lines = [
        f"🤖 <b>Avto-signal</b>: {names[auto['mode']]}",
        f"📈 Kuzatiladi: {esc(', '.join(AUTO_SYMBOLS))} · {AUTO_TF}",
        f"⭐ Eng kam kuch: {AUTO_MIN_STARS} · tanaffus {AUTO_COOLDOWN / 60:.0f} daq · kuniga ≤ {AUTO_MAX_PER_DAY}/juftlik",
        (f"📥 Kirish: ⏳ kutiladi (ATR × {AUTO_PULLBACK_ATR:g} qulayroq narx, {AUTO_PENDING_MIN:.0f} daq) · "
         f"hozir kutayotgan: {len(pendings)}" if AUTO_ENTRY == "pending" and AUTO_PULLBACK_ATR > 0
         else "📥 Kirish: darrov (market)"),
        f"📬 Qabul qiluvchilar: {esc(', '.join(tg_list)) if tg_list else 'yo‘q'}",
        f"📊 Bugun: {esc(', '.join(f'{k} {v}' for k, v in auto['count'].items()) or 'signal bo‘lmadi')}",
        "🕒 " + "; ".join(bars),
        "",
        "<code>/avto test</code> · <code>/avto kanal</code> · <code>/avto off</code> · <code>/tahlil</code>",
    ]
    await send(chat_id, "\n".join(lines))


async def cmd_tahlil(chat_id, args):
    """Hozirgi bozor holati — signal bo'lmasa ham sababini ko'rsatadi (test uchun)."""
    symbols = [norm_symbol(a) for a in args] or AUTO_SYMBOLS
    for sym in symbols:
        try:
            candles = await fetch_candles(sym)
            res = strategy.analyze(candles, PARAMS)
        except Exception as e:
            await send(chat_id, f"❌ {esc(sym)}: shamlar olinmadi ({esc(e)})")
            continue
        if not res.get("ok"):
            await send(chat_id, f"❌ {esc(sym)}: {esc(res.get('why'))}")
            continue
        trend = {"UP": "⬆️ yuqoriga", "DOWN": "⬇️ pastga", "FLAT": "➡️ yon (aniq emas)"}[res["trend"]]
        age = (time.time() - (res["t"] + TF_SECONDS[AUTO_TF])) / 60
        vr = f"{res['vol_ratio']:.1f}x" if res["vol_ratio"] is not None else "—"
        lines = [
            f"📊 <b>{esc(sym)}</b> · {AUTO_TF}",
            f"💰 Oxirgi sham yopilishi: {fmt(res['price'])} ({age:.0f} daq oldin)",
            f"📈 Trend: {trend} (EMA{PARAMS.ema_fast}/{PARAMS.ema_slow})",
            f"RSI: {res['rsi']:.0f} · ATR: {fmt(res['atr'])} · Hajm: {vr}",
            f"{PARAMS.lookback} sham oralig'i: {fmt(res['lo'])} – {fmt(res['hi'])}",
        ]
        if age > 3 * TF_SECONDS[AUTO_TF] / 60:
            lines.append("⏸ Ma'lumot eski — bozor yopiq bo'lishi mumkin.")
        sig = res["signal"]
        if sig:
            lines.append(f"🔔 <b>Hozir signal bor:</b> {sig['side']} · {sig['setup']} · {strategy.stars_text(sig['stars'])}")
            lines.append(f"📝 {esc(sig['reason'])}")
        else:
            lines.append("🔕 Signal yo'q: " + esc("; ".join(res["notes"])))
        await send(chat_id, "\n".join(lines))


# ───────────────────────── natijalar tarixi va kunlik hisobot ─────────────────────────

def record_history(t):
    """Yopilgan signal natijasini saqlaydi (kunlik hisobot shundan tuziladi)."""
    exit_px = t.get("exit")
    pips = pips_of(t, exit_px) if exit_px is not None else 0.0
    sl_pips = abs(t["entry"] - t["sl"]) / t["pip"] if t.get("sl") is not None else None
    history.append({
        "id": t["id"], "symbol": t["symbol"], "side": t["side"], "auto": bool(t.get("auto")),
        "setup": t.get("setup"), "stars": t.get("stars"), "entry": t["entry"], "exit": exit_px,
        "result": t.get("result", "?"), "pips": round(pips, 1),
        "r": round(pips / sl_pips, 2) if sl_pips else None, "opened": t["opened"], "closed": time.time(),
    })
    history[:] = report.prune(history, time.time())


def _row(b) -> str:
    extra = f", boshqa {b['other']}" if b["other"] else ""
    return f"{b['n']} ta (TP {b['tp']}, SL {b['sl']}{extra})"


def report_text(entries, title: str, live=None) -> str:
    """live: {"open": [(trade, pips_now)], "pending": n} — faqat bugungi hisobotda."""
    a = report.aggregate(entries)
    t = a["total"]
    lines = [f"📊 <b>{esc(title)}</b>" + (" · 🧪 TEST rejimi" if auto["mode"] == "test" else ""), ""]
    if t["n"] == 0:
        lines.append("Shu davrda yopilgan avto-signal bo'lmadi.")
    else:
        lines += [
            f"Yopilgan signallar: <b>{t['n']}</b> ta → ✅ TP: <b>{t['tp']}</b> · ❌ SL: <b>{t['sl']}</b>"
            + (f" · ⌛ boshqa: {t['other']}" if t["other"] else ""),
            f"🎯 Yutuq: <b>{a['win_rate'] * 100:.0f}%</b> · Jami natija: <b>{a['r_total']:+.1f}R</b>",
            "",
            f"🟢 BUY: {_row(a['side']['BUY'])}",
            f"🔴 SELL: {_row(a['side']['SELL'])}",
        ]
        for sym, b in sorted(a["symbol"].items()):
            lines.append(f"📈 {esc(sym)}: {_row(b)} · <b>{fmt_pips(b['pips'])} pips</b>")
        best, worst = a["best"], a["worst"]
        if best and best["result"] != "SL" and (best.get("pips") or 0) > 0:
            lines.append(f"\n🏆 Eng yaxshisi: {esc(best['symbol'])} {best['side']} {fmt_pips(best['pips'])} pips")
        if worst and (worst.get("pips") or 0) < 0:
            lines.append(f"💔 Eng yomoni: {esc(worst['symbol'])} {worst['side']} {fmt_pips(worst['pips'])} pips")
    if a["nofill"]:
        lines.append(f"⏳ Kirish narxiga yetmay bekor bo'lgan: {a['nofill']} ta")
    if a["manual"]["n"]:
        lines.append(f"✍️ Qo'lda/zona signallar: {a['manual']['n']} ta · {fmt_pips(a['manual']['pips'])} pips")
    if live is not None:
        for tr, p in live.get("open", []):
            lines.append(f"🔄 Hali ochiq: {esc(tr['symbol'])} {tr['side']} · hozir {fmt_pips(p)} pips")
        if live.get("pending"):
            lines.append(f"⏳ Kirishni kutayotgan: {live['pending']} ta")
    if 0 < t["n"] < 30:
        lines += ["", "<i>Namuna kichik (30 dan kam savdo) — natijaga ishonib bo'lmaydi.</i>"]
    lines += ["", AUTO_DISCLAIMER]
    return "\n".join(lines)


async def build_report(start: float, end: float, title: str, live: bool = False) -> str:
    entries = report.in_range(history, start, end)
    snap = None
    if live:
        opened = [t for t in trades if t.get("auto")]
        prices = await fetch_prices([t["symbol"] for t in opened]) if opened else {}
        snap = {"open": [(t, pips_of(t, prices[t["symbol"]])) for t in opened if t["symbol"] in prices],
                "pending": len(pendings)}
    return report_text(entries, title, snap)


async def send_daily_report(now: float = None):
    now = now or time.time()
    start, end = report.local_day_bounds(now, TZ_HOURS)
    day = time.strftime("%d.%m.%Y", time.gmtime(start + TZ_HOURS * 3600))
    text = await build_report(start, min(end, now + 1), f"Kunlik hisobot — {day}", live=True)
    chats = auto_targets() or test_targets()
    if not chats:
        return log.warning("Kunlik hisobot: yuboriladigan chat yo'q")
    await broadcast(text, chats=chats)
    log.info("Kunlik hisobot yuborildi: %s", day)


async def report_loop():
    while True:
        try:
            now = time.time()
            lt = time.gmtime(now + TZ_HOURS * 3600)
            today = time.strftime("%Y-%m-%d", lt)
            if lt.tm_hour >= REPORT_HOUR and auto.get("last_report") != today:
                auto["last_report"] = today  # avval belgilaymiz: xato bo'lsa ham takror-takror yubormasin
                auto_save()
                await send_daily_report(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Kunlik hisobotda xato")
        await asyncio.sleep(30)


async def cmd_hisobot(chat_id, args):
    """/hisobot — bugun (hozirgacha) · /hisobot kecha · /hisobot 7 — oxirgi 7 kun."""
    now = time.time()
    start, end = report.local_day_bounds(now, TZ_HOURS)
    arg = (args[0].lower() if args else "")
    if arg in ("kecha", "yesterday"):
        start, end, title, live = start - 86400, start, "Kechagi hisobot", False
    elif arg.isdigit() and 1 <= int(arg) <= 90:
        n = int(arg)
        start, end, title, live = start - (n - 1) * 86400, now + 1, f"Oxirgi {n} kunlik hisobot", n == 1
    elif arg:
        return await send(chat_id, "Namuna: <code>/hisobot</code>, <code>/hisobot kecha</code>, <code>/hisobot 7</code>")
    else:
        end, title, live = now + 1, "Bugungi hisobot (hozirgacha)", True
    await send(chat_id, await build_report(start, end, title, live))


# ───────────────────────── Telegram buyruqlari ─────────────────────────

HELP = (
    "🤖 <b>Zona signal bot</b>\n\n"
    "<b>Zona qo'shish:</b>\n"
    "<code>/buy XAUUSD 4150 4160</code> — BUY zona (signal: DIQQAT BUY)\n"
    "<code>/sell XAUUSD 4250 4260</code> — SELL zona (signal: DIQQAT SELL)\n"
    "<code>/buy XAUUSD 4150 4160 SL 4140 TP 4200</code> — SL/TP bilan\n"
    "<code>/zona XAUUSD 4150 4160</code> — yo'nalishni bot o'zi aniqlaydi:\n"
    "    zona narxdan pastda → BUY, yuqorida → SELL\n\n"
    "<b>Boshqa buyruqlar:</b>\n"
    "/zonalar — faol zonalar ro'yxati\n"
    "<code>/ochir 3</code> — 3-raqamli zonani o'chirish\n"
    "/tozala — hamma zonani o'chirish\n"
    "<code>/narx XAUUSD</code> — hozirgi narx\n"
    "/signallar — signal chiqqandan keyin necha pips yurgani\n"
    "<code>/tuzat XAUUSD 4181.35</code> — narxni Exness'ga moslash\n"
    "<code>/yopish 3</code> — signal kuzatuvini to'xtatish\n\n"
    "<b>Avto-signal (oltin + bitcoin):</b>\n"
    "/avto — holat · <code>/avto test</code> (faqat sizga) · <code>/avto kanal</code> (hammaga) · <code>/avto off</code>\n"
    "/tahlil — hozirgi bozor holati va signal bo'lmasa sababi\n"
    "/mt5 — MT5 (Exness) narxi ulanganmi va farqi qancha\n"
    "/hisobot — bugungi natija (TP/SL) · <code>/hisobot kecha</code> · <code>/hisobot 7</code> (oxirgi 7 kun). "
    "Avtomatik hisobot har kuni 23:00 da keladi.\n\n"
    "<b>Signaldan keyin:</b> bot narxni kuzatadi va +50, +100 pips yurganda "
    "signal postiga reply qilib yozadi. Izohda SL/TP bo'lsa, urilganini ham yozadi.\n\n"
    "Oltin: <code>XAUUSD</code> (yoki XAU, oltin, gold). Kumush: <code>XAGUSD</code>.\n"
    "Kripto: BTC, ETH va boshqalar (Binance).\n"
    "XAUUSD'da 1 pip = 0.10$ (100 pips = 10$).\n"
    "TradingView alertlari ham avvalgidek ishlayveradi."
)


async def cmd_buy(chat_id, args):
    await cmd_zona(chat_id, args, side="BUY")


async def cmd_sell(chat_id, args):
    await cmd_zona(chat_id, args, side="SELL")


async def cmd_zona(chat_id, args, side=None):
    global next_id
    if len(args) < 2 or not is_num(args[1]):
        cmd = {"BUY": "/buy", "SELL": "/sell"}.get(side, "/zona")
        return await send(chat_id, f"Namuna: <code>{cmd} XAUUSD 4150 4160</code> yoki <code>{cmd} XAUUSD 4150</code>")
    symbol = norm_symbol(args[0])
    a = parse_num(args[1])
    rest = args[2:]
    b = a
    if rest and is_num(rest[0]):
        b = parse_num(rest[0])
        rest = rest[1:]
    # /zona XAUUSD 4150 4160 buy — yo'nalish so'z bilan ham yozilishi mumkin
    if side is None and rest and rest[0].upper() in SIDE_WORDS:
        side = SIDE_WORDS[rest[0].upper()]
        rest = rest[1:]
    low, high = min(a, b), max(a, b)
    if low <= 0:
        return await send(chat_id, "Narx 0 dan katta bo'lishi kerak.")

    price = (await fetch_prices([symbol])).get(symbol)
    if price is None:
        if symbol in METALS:
            return await send(chat_id, f"❌ {esc(symbol)} narxini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
        return await send(chat_id, f"❌ <b>{esc(symbol)}</b> topilmadi. Oltin uchun <code>XAUUSD</code>, kripto uchun Binance nomini yozing (masalan BTCUSDT).")

    z = {"id": next_id, "symbol": symbol, "low": low, "high": high, "note": " ".join(rest)[:200]}
    z["state"] = zone_state(z, price)
    auto = side is None
    if auto:  # zona narxdan pastda — support (BUY), yuqorida — resistance (SELL)
        side = {"above": "BUY", "below": "SELL"}.get(z["state"])
    z["side"] = side
    zones.append(z)
    next_id += 1
    save_zones()

    msg = [
        f"✅ <b>#{z['id']} zona qo'shildi</b>",
        f"📈 {esc(symbol)}",
        f"🧭 Yo'nalish: <b>{side_label(side)}</b>" + (" (avtomatik)" if auto and side else ""),
        f"📍 Zona: <b>{zone_text(z)}</b>",
        f"💰 Hozirgi narx: {fmt(price)} <i>({price_source(symbol)})</i>",
    ]
    if z["note"]:
        msg.append(f"📝 {esc(z['note'])}")
    if z["state"] == "in":
        msg.append("\n⚠️ Narx hozir zona ichida. Zonadan chiqib, qayta kirsa signal beraman.")
    if side is None:
        msg.append("⚠️ Yo'nalishni aniqlab bo'lmadi. BUY yoki SELL kerak bo'lsa, "
                   f"<code>/ochir {z['id']}</code> qilib, <code>/buy</code> yoki <code>/sell</code> bilan qayta qo'shing.")
    await send(chat_id, "\n".join(msg))


async def cmd_zonalar(chat_id, _):
    if not zones:
        return await send(chat_id, "Faol zona yo'q. Qo'shish: <code>/zona XAUUSD 4150 4160</code>")
    prices = await fetch_prices(z["symbol"] for z in zones)
    lines = [f"📋 <b>Faol zonalar ({len(zones)} ta)</b>", ""]
    for z in zones:
        p = prices.get(z["symbol"])
        now = f" · hozir {fmt(p)}" if p is not None else ""
        note = f"\n    📝 {esc(z['note'])}" if z.get("note") else ""
        mark = {"BUY": "🟢 BUY", "SELL": "🔴 SELL"}.get(z.get("side"), "⚪️")
        lines.append(f"<b>#{z['id']}</b> {mark} {esc(z['symbol'])}: {zone_text(z)}{now}{note}")
    lines.append("\nO'chirish: <code>/ochir raqam</code>")
    await send(chat_id, "\n".join(lines))


async def cmd_ochir(chat_id, args):
    ids = {int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()}
    if not ids:
        return await send(chat_id, "Namuna: <code>/ochir 3</code>")
    before = len(zones)
    zones[:] = [z for z in zones if z["id"] not in ids]
    removed = before - len(zones)
    save_zones()
    await send(chat_id, f"🗑 {removed} ta zona o'chirildi." if removed else "Bunday raqamli zona topilmadi.")


async def cmd_tozala(chat_id, _):
    n = len(zones)
    zones.clear()
    save_zones()
    await send(chat_id, f"🗑 Hamma zonalar o'chirildi ({n} ta).")


async def cmd_narx(chat_id, args):
    if not args:
        return await send(chat_id, "Namuna: <code>/narx XAUUSD</code>")
    symbol = norm_symbol(args[0])
    p = (await fetch_prices([symbol])).get(symbol)
    if p is None:
        if symbol in METALS:
            return await send(chat_id, f"❌ {esc(symbol)} narxini hozir olib bo'lmadi. Bozor yopiq bo'lishi mumkin yoki birozdan keyin urinib ko'ring.")
        return await send(chat_id, f"❌ {esc(symbol)} topilmadi.")
    await send(chat_id, f"💰 <b>{esc(symbol)}</b>: {fmt(p)}\n<i>Manba: {price_source(symbol)}</i>")


async def cmd_signallar(chat_id, _):
    if not trades and not pendings:
        return await send(chat_id, "Hozir kuzatilayotgan signal yo'q.")
    prices = await fetch_prices([t["symbol"] for t in trades] + [p["symbol"] for p in pendings])
    lines = []
    if pendings:
        lines += [f"⏳ <b>Kirishni kutayotgan ({len(pendings)} ta)</b>", ""]
        for p in pendings:
            now_p = prices.get(p["symbol"])
            left = max(0, (p["expires"] - time.time()) / 60)
            icon = "🟢" if p["side"] == "BUY" else "🔴"
            lines.append(f"<b>#{p['id']}</b> {icon} {esc(p['symbol'])} {p['side']} — kirish {fmt(p['entry'])}"
                         + (f" · hozir {fmt(now_p)}" if now_p is not None else "") + f" · {left:.0f} daq qoldi")
        lines.append("")
    if not trades:
        lines.append("Bekor qilish: <code>/yopish raqam</code>")
        return await send(chat_id, "\n".join(lines))
    lines += [f"📊 <b>Kuzatilayotgan signallar ({len(trades)} ta)</b>", ""]
    for t in trades:
        p = prices.get(t["symbol"])
        now = f" · hozir {fmt_pips(pips_of(t, p))} pips" if p is not None else ""
        extra = "".join([f" · SL {fmt(t['sl'])}" if t["sl"] else "", f" · TP {fmt(t['tp'])}" if t["tp"] else ""])
        icon = "🟢" if t["side"] == "BUY" else "🔴"
        lines.append(f"<b>#{t['id']}</b> {icon} {esc(t['symbol'])} {t['side']} @ {fmt(t['entry'])}{now}{extra}")
    lines.append("\nKuzatuvni to'xtatish: <code>/yopish raqam</code>")
    await send(chat_id, "\n".join(lines))


async def cmd_yopish(chat_id, args):
    ids = {int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()}
    if not ids:
        return await send(chat_id, "Namuna: <code>/yopish 3</code>")
    before = len(trades) + len(pendings)
    for p in [p for p in pendings if p["id"] in ids]:
        await cancel_pending(p, "admin bekor qildi (/yopish)", count=False)
    pendings[:] = [p for p in pendings if p["id"] not in ids]
    trades[:] = [t for t in trades if t["id"] not in ids]
    save_zones()
    n = before - len(trades) - len(pendings)
    await send(chat_id, f"⏹ {n} ta signal kuzatuvi to'xtatildi." if n else "Bunday raqamli signal topilmadi.")


async def cmd_tuzat(chat_id, args):
    """/tuzat XAUUSD 4181.35 — Exness'dagi hozirgi narxni yozasiz, bot farqni eslab qoladi."""
    if len(args) < 2:
        lines = ["Exness'dagi hozirgi <b>Bid</b> (Продажа) narxini yozing:",
                 "<code>/tuzat XAUUSD 4181.35</code>", "", "Tuzatishni o'chirish: <code>/tuzat XAUUSD off</code>"]
        if offsets:
            lines += ["", "Hozirgi tuzatishlar:"] + [f"• {esc(k)}: {v[0]:+.2f} ({esc(v[1])})" for k, v in offsets.items()]
        return await send(chat_id, "\n".join(lines))
    symbol = norm_symbol(args[0])
    if symbol not in METALS:
        return await send(chat_id, "Tuzatish faqat XAUUSD va XAGUSD uchun.")
    if args[1].lower() in ("off", "0", "reset", "ochir"):
        c = metal_cache.get(symbol)
        shift_history(symbol, -offset_for(symbol, c[2] if c else "Bitget"))
        offsets.pop(symbol, None)
        save_zones()
        return await send(chat_id, f"✅ {esc(symbol)} tuzatishi o'chirildi.")
    if not is_num(args[1]):
        return await send(chat_id, "Namuna: <code>/tuzat XAUUSD 4181.35</code>")
    metal_cache.pop(symbol, None)  # eng yangi narx bilan solishtiramiz
    raw, src = await fetch_metal_raw(symbol)
    if raw is None:
        return await send(chat_id, "❌ Hozir narxni olib bo'lmadi, birozdan keyin urinib ko'ring.")
    exness = parse_num(args[1])
    off = exness - raw
    if abs(off) > raw * 0.01:
        return await send(chat_id, f"⚠️ Farq juda katta ({off:+.2f}). Narxni to'g'ri yozdingizmi? {esc(src)}: {fmt(raw)}")
    shift_history(symbol, round(off, 3) - offset_for(symbol, src))
    offsets[symbol] = [round(off, 3), src]
    save_zones()
    await send(chat_id, "\n".join([
        f"✅ <b>{esc(symbol)} Exness'ga moslandi</b>",
        f"{esc(src)}: {fmt(raw)}",
        f"Exness: {fmt(exness)}",
        f"Farq: <b>{off:+.2f}</b> — endi hamma narxlarga qo'shiladi.",
        "",
        "<i>Farq vaqt o'tishi bilan biroz o'zgaradi — kuniga bir marta qayta /tuzat qilib turing.</i>",
    ]))


async def cmd_mt5(chat_id, args):
    """MT5 narx ulanishi: narx yangimi, boshqa manba bilan farqi qancha."""
    syms = [norm_symbol(a) for a in args] or sorted(set(AUTO_SYMBOLS) | set(feed_prices))
    lines = ["🔌 <b>MT5 narx ulanishi</b>", ""]
    if not feed_prices:
        lines += ["❌ MT5 dan hali narx kelmagan.",
                  "MT5 da ZonaPriceFeed EA ishlayotganini, URL ruxsat etilganini va key to'g'riligini tekshiring (README: MT5 bo'limi).", ""]
    for s in syms:
        f = feed_prices.get(s)
        if f is None:
            lines.append(f"⚪️ {esc(s)}: MT5 narxi kelmayapti")
            continue
        age = time.time() - f[0]
        other, src = None, None
        try:
            if s in METALS:
                other, src = await fetch_metal_raw(s)
            else:
                other, src = (await fetch_binance([s])).get(s), "Binance"
        except Exception:
            pass
        icon = "🟢" if age <= FEED_MAX_AGE else "🟡 eski"
        row = f"{icon} {esc(s)}: Bid <b>{fmt(f[1])}</b> / Ask {fmt(f[2])} · {age:.0f}s oldin"
        if other:
            row += f"\n    {esc(src)} bilan farq: {f[1] - other:+.2f}"
        lines.append(row)
    lines += ["", "MT5 narxi yangi bo'lsa, bot hamma narxlarni (signal, pips, SL/TP) shundan oladi — /tuzat kerak emas."]
    await send(chat_id, "\n".join(lines))


async def cmd_help(chat_id, _):
    await send(chat_id, HELP)


COMMANDS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/zona": cmd_zona,
    "/buy": cmd_buy,
    "/sell": cmd_sell,
    "/zonalar": cmd_zonalar,
    "/ochir": cmd_ochir,
    "/tozala": cmd_tozala,
    "/narx": cmd_narx,
    "/signallar": cmd_signallar,
    "/tuzat": cmd_tuzat,
    "/yopish": cmd_yopish,
    "/avto": cmd_avto,
    "/tahlil": cmd_tahlil,
    "/mt5": cmd_mt5,
    "/hisobot": cmd_hisobot,
}


async def handle_message(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))
    if user_id not in ADMIN_IDS and str(chat_id) not in ADMIN_IDS:
        return await send(
            chat_id,
            f"⛔️ Sizga ruxsat yo'q.\nSizning ID: <code>{esc(user_id)}</code>\n"
            "Bot egasi bo'lsangiz, Railway'da <b>ADMIN_ID</b> ga shu raqamni yozing.",
        )
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    fn = COMMANDS.get(cmd)
    if fn is None:
        return await send(chat_id, "Bunday buyruq yo'q. /help ni bosing.")
    await fn(chat_id, parts[1:])


async def poll_loop():
    await tg("deleteWebhook")
    await tg("setMyCommands", commands=[
        {"command": "buy", "description": "BUY zona: /buy XAUUSD 4150 4160"},
        {"command": "sell", "description": "SELL zona: /sell XAUUSD 4250 4260"},
        {"command": "zona", "description": "Zona (yo'nalish avtomatik): /zona XAUUSD 4150 4160"},
        {"command": "zonalar", "description": "Faol zonalar ro'yxati"},
        {"command": "ochir", "description": "Zonani o'chirish: /ochir 3"},
        {"command": "tozala", "description": "Hamma zonani o'chirish"},
        {"command": "narx", "description": "Hozirgi narx: /narx XAUUSD"},
        {"command": "signallar", "description": "Kuzatilayotgan signallar (pips)"},
        {"command": "tuzat", "description": "Narxni Exness'ga moslash: /tuzat XAUUSD 4181.35"},
        {"command": "yopish", "description": "Signal kuzatuvini to'xtatish: /yopish 3"},
        {"command": "avto", "description": "Avto-signal: /avto test | kanal | off"},
        {"command": "tahlil", "description": "Oltin/BTC hozirgi tahlili"},
        {"command": "mt5", "description": "MT5 narx ulanishi holati"},
        {"command": "hisobot", "description": "Natijalar: /hisobot, /hisobot kecha, /hisobot 7"},
        {"command": "help", "description": "Yordam"},
    ])
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            async with http.post(f"{TG_API}/getUpdates", json=params, timeout=ClientTimeout(total=45)) as r:
                data = await r.json()
            if not data.get("ok"):
                log.error("getUpdates xato: %s", data)
                await asyncio.sleep(5)
                continue
            for upd in data["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg:
                    try:
                        await handle_message(msg)
                    except Exception:
                        log.exception("Buyruqni bajarishda xato")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling xato")
            await asyncio.sleep(5)


# ───────────────────────── TradingView webhook ─────────────────────────

FIELDS = [
    ("signal", "🚦 Signal"),
    ("ticker", "📈 Juftlik"),
    ("price", "💰 Narx"),
    ("zona", "📍 Zona"),
    ("interval", "⏱ Taymfreym"),
    ("vaqt", "🕒 Vaqt"),
    ("izoh", "📝 Izoh"),
]
SKIP = {"secret", "key"}


def format_message(data) -> str:
    if not isinstance(data, dict):
        return f"🔔 <b>TradingView signal</b>\n\n{esc(data)}"
    sig = str(data.get("signal", "")).upper()
    side = "BUY" if ("BUY" in sig or "LONG" in sig) else "SELL" if ("SELL" in sig or "SHORT" in sig) else None
    lines = [side_header(side) if side else "🔔 <b>TradingView signal</b>", ""]
    used = set()
    for key, label in FIELDS:
        if data.get(key) not in (None, ""):
            lines.append(f"{label}: <b>{esc(data[key])}</b>")
            used.add(key)
    for key, value in data.items():
        if key in used or key in SKIP or value in (None, ""):
            continue
        lines.append(f"• {esc(key)}: {esc(value)}")
    return "\n".join(lines)


def tv_signal_text(z, price: float) -> str:
    lines = [
        side_header(z.get("side")),
        "",
        f"📈 Juftlik: <b>{esc(z['symbol'])}</b>",
        f"💰 Narx: <b>{fmt(price)}</b>",
    ]
    if z.get("zona"):
        lines.append(f"📍 Zona: <b>{esc(z['zona'])}</b>")
    if z.get("note"):
        lines.append(f"📝 {esc(z['note'])}")
    return "\n".join(lines)


async def handle_tv_signal(data: dict) -> str:
    """TradingView signalini /buy, /sell dagi kabi qayta ishlaydi: signal + pips kuzatuvi."""
    global next_id
    ticker = str(data.get("ticker") or data.get("symbol") or "").split(":")[-1]
    if not ticker:
        await broadcast(format_message(data))
        return "ticker yo'q, oddiy xabar yuborildi"
    symbol = norm_symbol(ticker)

    # Narx: botning o'z manbasi (Exness'ga moslangan) — pips shu bilan hisoblanadi
    our = (await fetch_prices([symbol])).get(symbol)
    try:
        tv_price = float(data.get("price"))
    except (TypeError, ValueError):
        tv_price = None
    price = our if our is not None else tv_price
    if price is None:
        await broadcast(format_message(data))
        return "narx yo'q, oddiy xabar yuborildi"

    sig = str(data.get("signal", "")).upper()
    side = next((SIDE_WORDS[w] for w in re.findall(r"[A-Z]+", sig) if w in SIDE_WORDS), None)
    auto = side is None
    if auto:
        side = detect_side(symbol, price, data)

    # TradingView ba'zan aynan bir xabarni ikki marta yuboradi
    key = json.dumps(data, sort_keys=True, ensure_ascii=False)
    now = time.time()
    for k in [k for k, t in recent_tv.items() if now - t > 60]:
        recent_tv.pop(k, None)
    if key in recent_tv:
        return "takroriy signal, o'tkazib yuborildi"
    recent_tv[key] = now

    z = {"id": next_id, "symbol": symbol, "side": side,
         "zona": str(data.get("zona") or "").strip(), "note": str(data.get("izoh") or "").strip()[:200]}
    next_id += 1
    msg_ids = await broadcast(tv_signal_text(z, price))
    tracked = our is not None and open_trade(z, price, msg_ids) is not None
    save_zones()
    log.info("TradingView signal #%s %s %s%s narx=%s kuzatuv=%s", z["id"], symbol, side,
             " (avto)" if auto else "", price, tracked)
    return "ok"


async def webhook(request):
    raw = (await request.text()).strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = raw

    if SECRET:
        given = request.query.get("key", "")
        if not given and isinstance(data, dict):
            given = str(data.get("secret", ""))
        if given != SECRET:
            log.warning("Noto'g'ri maxfiy so'z, so'rov rad etildi")
            return web.Response(status=403, text="forbidden")

    if not data:
        return web.Response(status=400, text="bo'sh xabar")

    log.info("TradingView xabari: %s", raw[:300])
    if isinstance(data, dict):
        result = await handle_tv_signal(data)
    else:
        await broadcast(format_message(data))
        result = "ok"
    return web.Response(text=result)


async def price_feed(request):
    """MT5 EA narx yuboradi: {"symbol": "XAUUSD", "bid": 4181.2, "ask": 4181.4}"""
    if SECRET and request.query.get("key", "") != SECRET:
        return web.Response(status=403, text="forbidden")
    try:
        data = json.loads((await request.text()).strip().strip("\x00"))  # ba'zi EA'lar oxiriga nol bayt qo'shadi
        symbol = norm_symbol(str(data["symbol"]))
        bid = float(data["bid"])
        ask = float(data.get("ask") or bid)
        if bid <= 0:
            raise ValueError("bid <= 0")
    except Exception as e:
        return web.Response(status=400, text=f"xato: {e}")
    first = symbol not in feed_prices
    feed_prices[symbol] = (time.time(), bid, ask)
    if first:
        log.info("MT5 narxi kela boshladi: %s %s", symbol, bid)
    return web.Response(text="ok")


async def health(_):
    feeds = ", ".join(f"{s}={fmt(v[1])} ({time.time() - v[0]:.0f}s oldin)" for s, v in feed_prices.items())
    return web.Response(text=f"Zona bot ishlayapti ✅ ({len(zones)} ta faol zona)\nMT5 narxi: {feeds or 'kelmayapti'}\nAvto-signal: {auto['mode']}")


# ───────────────────────── ishga tushirish ─────────────────────────

async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=15))
    if not BOT_TOKEN or not CHAT_IDS:
        log.error("BOT_TOKEN yoki CHAT_ID berilmagan!")
    if not SECRET:
        log.warning("WEBHOOK_SECRET berilmagan — har kim signal yubora oladi!")
    if not ADMIN_IDS:
        log.warning("ADMIN_ID yo'q — botga hech kim buyruq bera olmaydi")
    load_zones()
    auto_load()
    if BOT_TOKEN:
        tasks.append(asyncio.create_task(poll_loop()))
    tasks.append(asyncio.create_task(price_loop()))
    tasks.append(asyncio.create_task(auto_loop()))
    tasks.append(asyncio.create_task(report_loop()))


async def on_cleanup(app):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await http.close()


def create_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook", webhook)
    app.router.add_post("/price", price_feed)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)  # URL'dagi kalit logga tushmasin
