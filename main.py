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
"""

import os
import json
import html
import asyncio
import logging
import math
import re
import time
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

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
METAL_CACHE = float(os.environ.get("METAL_CACHE", 30))  # gold-api har ~30 soniyada yangilanadi
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PIP_TARGETS = sorted({int(x) for x in os.environ.get("PIP_TARGETS", "50,100,150,200").split(",") if x.strip().isdigit()})
TRACK_HOURS = float(os.environ.get("TRACK_HOURS", 48))
# XAUUSD: 1 pip = 0.10$ (100 pips = 10$). XAGUSD: 1 pip = 0.01$.
PIP_SIZES = {"XAUUSD": 0.1, "XAGUSD": 0.01}
for _item in os.environ.get("PIP_SIZES", "").split(","):
    if ":" in _item:
        _k, _v = _item.split(":", 1)
        PIP_SIZES[_k.strip().upper()] = float(_v)

# Metallar gold-api.com'dan, qolganlari Binance'dan olinadi
METALS = {"XAUUSD": "XAU", "XAGUSD": "XAG"}
ALIASES = {
    "XAU": "XAUUSD", "GOLD": "XAUUSD", "OLTIN": "XAUUSD", "XAUUSD": "XAUUSD",
    "XAG": "XAGUSD", "SILVER": "XAGUSD", "KUMUSH": "XAGUSD", "XAGUSD": "XAGUSD",
}

http = None          # aiohttp ClientSession
zones = []           # [{id, symbol, low, high, note, state, side}]
trades = []          # signal chiqqandan keyin kuzatilayotganlar
next_id = 1
tasks = []
metal_cache = {}     # {"XAUUSD": (vaqt, narx)}


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
    sl, tp = find_level(z.get("note"), "SL"), find_level(z.get("note"), "TP")
    # noto'g'ri tomondagi SL/TP e'tiborga olinmaydi
    if sl is not None and ((side == "BUY" and sl >= price) or (side == "SELL" and sl <= price)):
        sl = None
    if tp is not None and ((side == "BUY" and tp <= price) or (side == "SELL" and tp >= price)):
        tp = None
    t = {
        "id": z["id"], "symbol": z["symbol"], "side": side, "entry": price,
        "pip": pip_size(z["symbol"], price), "sl": sl, "tp": tp, "hit": [], "best": 0.0,
        "opened": time.time(), "msgs": {str(k): v for k, v in msg_ids.items()},
    }
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
        return "\n".join(text), True

    # 2) TP
    if t["tp"] is not None and ((t["side"] == "BUY" and price >= t["tp"]) or (t["side"] == "SELL" and price <= t["tp"])):
        tp_pips = pips_of(t, t["tp"])
        return "\n".join([f"🏆 <b>TP urildi! {fmt_pips(tp_pips)} pips</b> 🔥", trade_head(t),
                          f"Kirish: {fmt(t['entry'])} → TP: {fmt(t['tp'])}"]), True

    # 3) pips maqsadlari (50, 100, ...)
    new_hits = [p for p in PIP_TARGETS if p not in t["hit"] and move >= p]
    if new_hits:
        t["hit"].extend(new_hits)
        top = max(new_hits)
        last = t["tp"] is None and top >= PIP_TARGETS[-1]
        title = f"🏁 <b>Signal yakunlandi: +{top} pips</b> 🔥" if last else f"✅ <b>+{top} pips</b> 🔥"
        return "\n".join([title, trade_head(t), line]), last

    # 4) vaqt tugadi
    if time.time() - t["opened"] > TRACK_HOURS * 3600:
        log.info("#%s kuzatuv vaqti tugadi (eng yaxshi %.0f pips)", t["id"], t["best"])
        return None, True
    return None, False


# ───────────────────────── saqlash ─────────────────────────

def load_zones():
    global zones, next_id
    try:
        data = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
        zones, next_id = data.get("zones", []), data.get("next_id", 1)
        trades[:] = data.get("trades", [])
        log.info("%d ta zona, %d ta kuzatilayotgan signal yuklandi", len(zones), len(trades))
    except FileNotFoundError:
        zones, next_id = [], 1
    except Exception:
        log.exception("zones.json o'qib bo'lmadi, bo'sh ro'yxat bilan boshlanadi")
        zones, next_id = [], 1


def save_zones():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ZONES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next_id": next_id, "zones": zones, "trades": trades}, ensure_ascii=False, indent=1), encoding="utf-8")
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


async def broadcast(text: str, reply_to: dict = None):
    """Hamma CHAT_ID ga yuboradi, {chat_id: message_id} qaytaradi."""
    ids = {}
    for chat_id in CHAT_IDS:
        mid = await send(chat_id, text, reply_to=(reply_to or {}).get(str(chat_id)))
        if mid:
            ids[str(chat_id)] = mid
    return ids


async def fetch_metal(symbol: str):
    """XAUUSD/XAGUSD narxi gold-api.com'dan (30 soniya keshlanadi)."""
    now = asyncio.get_running_loop().time()
    cached = metal_cache.get(symbol)
    if cached and now - cached[0] < METAL_CACHE:
        return cached[1]
    try:
        async with http.get(f"{METAL_API}/{METALS[symbol]}") as r:
            if r.status != 200:
                raise ValueError(f"{r.status}: {await r.text()}")
            price = float((await r.json())["price"])
        metal_cache[symbol] = (now, price)
        return price
    except Exception as e:
        log.warning("%s narxi olinmadi: %s", symbol, e)
        return cached[1] if cached and now - cached[0] < 120 else None


async def fetch_prices(symbols):
    """{'BTCUSDT': 64500.1, 'XAUUSD': 4165.2, ...}. Topilmaganlar natijada bo'lmaydi."""
    symbols = sorted(set(symbols))
    out = {}
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

async def check_once():
    if not zones and not trades:
        return
    prices = await fetch_prices([z["symbol"] for z in zones] + [t["symbol"] for t in trades])
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
            await broadcast(text, reply_to=t.get("msgs"))
        if done:
            closed.add(t["id"])
    if closed:
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
    "<code>/yopish 3</code> — signal kuzatuvini to'xtatish\n\n"
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
        f"💰 Hozirgi narx: {fmt(price)}",
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
        return await send(chat_id, f"❌ {esc(symbol)} topilmadi.")
    await send(chat_id, f"💰 <b>{esc(symbol)}</b>: {fmt(p)}")


async def cmd_signallar(chat_id, _):
    if not trades:
        return await send(chat_id, "Hozir kuzatilayotgan signal yo'q.")
    prices = await fetch_prices(t["symbol"] for t in trades)
    lines = [f"📊 <b>Kuzatilayotgan signallar ({len(trades)} ta)</b>", ""]
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
    before = len(trades)
    trades[:] = [t for t in trades if t["id"] not in ids]
    save_zones()
    n = before - len(trades)
    await send(chat_id, f"⏹ {n} ta signal kuzatuvi to'xtatildi." if n else "Bunday raqamli signal topilmadi.")


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
    "/yopish": cmd_yopish,
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
        {"command": "yopish", "description": "Signal kuzatuvini to'xtatish: /yopish 3"},
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

    log.info("TradingView signal: %s", raw[:300])
    await broadcast(format_message(data))
    return web.Response(text="ok")


async def health(_):
    return web.Response(text=f"Zona bot ishlayapti ✅ ({len(zones)} ta faol zona)")


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
    if BOT_TOKEN:
        tasks.append(asyncio.create_task(poll_loop()))
    tasks.append(asyncio.create_task(price_loop()))


async def on_cleanup(app):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await http.close()


def create_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook", webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
