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
"""

import os
import json
import html
import asyncio
import logging
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

# Metallar gold-api.com'dan, qolganlari Binance'dan olinadi
METALS = {"XAUUSD": "XAU", "XAGUSD": "XAG"}
ALIASES = {
    "XAU": "XAUUSD", "GOLD": "XAUUSD", "OLTIN": "XAUUSD", "XAUUSD": "XAUUSD",
    "XAG": "XAGUSD", "SILVER": "XAGUSD", "KUMUSH": "XAGUSD", "XAGUSD": "XAGUSD",
}

http = None          # aiohttp ClientSession
zones = []           # [{id, symbol, low, high, note, state}]
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
        "🎯 <b>Narx zonaga yetdi!</b>",
        "",
        f"📈 Juftlik: <b>{esc(z['symbol'])}</b>",
        f"💰 Narx: <b>{fmt(price)}</b>",
        f"📍 Zona: <b>{zone_text(z)}</b>",
        how,
    ]
    if z.get("note"):
        lines.append(f"📝 {esc(z['note'])}")
    lines += ["", f"<i>#{z['id']} zona ro'yxatdan o'chirildi.</i>"]
    return "\n".join(lines)


# ───────────────────────── saqlash ─────────────────────────

def load_zones():
    global zones, next_id
    try:
        data = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
        zones, next_id = data.get("zones", []), data.get("next_id", 1)
        log.info("%d ta zona yuklandi", len(zones))
    except FileNotFoundError:
        zones, next_id = [], 1
    except Exception:
        log.exception("zones.json o'qib bo'lmadi, bo'sh ro'yxat bilan boshlanadi")
        zones, next_id = [], 1


def save_zones():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ZONES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next_id": next_id, "zones": zones}, ensure_ascii=False, indent=1), encoding="utf-8")
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


async def send(chat_id, text: str):
    await tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


async def broadcast(text: str):
    for chat_id in CHAT_IDS:
        await send(chat_id, text)


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
    if not zones:
        return
    prices = await fetch_prices(z["symbol"] for z in zones)
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
    if changed or fired:
        save_zones()
    for z, price, how in fired:
        log.info("Signal: #%s %s %s", z["id"], z["symbol"], price)
        await broadcast(signal_text(z, price, how))


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
    "<code>/zona XAUUSD 4150 4160</code> — narx shu oraliqqa kirsa signal\n"
    "<code>/zona XAUUSD 4200</code> — narx shu darajani kesib o'tsa signal\n"
    "<code>/zona oltin 4100 4110 support</code> — izoh bilan\n\n"
    "<b>Boshqa buyruqlar:</b>\n"
    "/zonalar — faol zonalar ro'yxati\n"
    "<code>/ochir 3</code> — 3-raqamli zonani o'chirish\n"
    "/tozala — hamma zonani o'chirish\n"
    "<code>/narx XAUUSD</code> — hozirgi narx\n\n"
    "Oltin: <code>XAUUSD</code> (yoki XAU, oltin, gold). Kumush: <code>XAGUSD</code>.\n"
    "Kripto: BTC, ETH va boshqalar (Binance).\n"
    "Signal bir marta keladi, keyin zona avtomatik o'chiriladi.\n"
    "TradingView alertlari ham avvalgidek ishlayveradi."
)


async def cmd_zona(chat_id, args):
    global next_id
    if len(args) < 2 or not is_num(args[1]):
        return await send(chat_id, "Namuna: <code>/zona XAUUSD 4150 4160</code> yoki <code>/zona XAUUSD 4150</code>")
    symbol = norm_symbol(args[0])
    a = parse_num(args[1])
    rest = args[2:]
    b = a
    if rest and is_num(rest[0]):
        b = parse_num(rest[0])
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
    zones.append(z)
    next_id += 1
    save_zones()

    msg = [
        f"✅ <b>#{z['id']} zona qo'shildi</b>",
        f"📈 {esc(symbol)}",
        f"📍 Zona: <b>{zone_text(z)}</b>",
        f"💰 Hozirgi narx: {fmt(price)}",
    ]
    if z["note"]:
        msg.append(f"📝 {esc(z['note'])}")
    if z["state"] == "in":
        msg.append("\n⚠️ Narx hozir zona ichida. Zonadan chiqib, qayta kirsa signal beraman.")
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
        lines.append(f"<b>#{z['id']}</b> {esc(z['symbol'])}: {zone_text(z)}{now}{note}")
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


async def cmd_help(chat_id, _):
    await send(chat_id, HELP)


COMMANDS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/zona": cmd_zona,
    "/zonalar": cmd_zonalar,
    "/ochir": cmd_ochir,
    "/tozala": cmd_tozala,
    "/narx": cmd_narx,
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
        {"command": "zona", "description": "Zona qo'shish: /zona XAUUSD 4150 4160"},
        {"command": "zonalar", "description": "Faol zonalar ro'yxati"},
        {"command": "ochir", "description": "Zonani o'chirish: /ochir 3"},
        {"command": "tozala", "description": "Hamma zonani o'chirish"},
        {"command": "narx", "description": "Hozirgi narx: /narx XAUUSD"},
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
    lines = ["🔔 <b>TradingView signal</b>", ""]
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
