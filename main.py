"""
TradingView zona signal bot
TradingView alert -> webhook -> shu server -> Telegram

Railway'da ishlaydi. Kerakli o'zgaruvchilar (Variables):
  BOT_TOKEN       - @BotFather bergan token
  CHAT_ID         - signal boradigan chat/kanal/guruh ID (bir nechta bo'lsa vergul bilan)
  WEBHOOK_SECRET  - o'zingiz o'ylab topgan maxfiy so'z (begonalar signal yubora olmasligi uchun)
"""

import os
import json
import html
import logging

from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zona-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_ID", "").split(",") if c.strip()]
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", 8080))

# TradingView JSON'dagi maydonlar -> Telegram'da chiqadigan nomlar
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


def esc(v) -> str:
    return html.escape(str(v))


def format_message(data) -> str:
    """JSON (dict) yoki oddiy matndan Telegram xabarini yasaydi."""
    if not isinstance(data, dict):
        return f"🔔 <b>TradingView signal</b>\n\n{esc(data)}"

    lines = ["🔔 <b>Zona signali</b>", ""]
    used = set()
    for key, label in FIELDS:
        if data.get(key) not in (None, ""):
            lines.append(f"{label}: <b>{esc(data[key])}</b>")
            used.add(key)
    # Qo'shimcha maydonlar bo'lsa ular ham chiqadi
    for key, value in data.items():
        if key in used or key in SKIP or value in (None, ""):
            continue
        lines.append(f"• {esc(key)}: {esc(value)}")
    return "\n".join(lines)


async def send_telegram(app: web.Application, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    session: ClientSession = app["http"]
    for chat_id in CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with session.post(url, json=payload) as r:
                if r.status != 200:
                    log.error("Telegram xato (%s): %s", chat_id, await r.text())
        except Exception as e:  # tarmoq xatosi signalni to'xtatib qo'ymasin
            log.exception("Telegram'ga yuborib bo'lmadi (%s): %s", chat_id, e)


async def webhook(request: web.Request) -> web.Response:
    raw = (await request.text()).strip()

    # Xabar JSON bo'lsa o'qiymiz, bo'lmasa oddiy matn sifatida qoldiramiz
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = raw

    # Maxfiy so'zni tekshirish: URL'da ?key=... yoki JSON ichida "secret"
    if SECRET:
        given = request.query.get("key", "")
        if not given and isinstance(data, dict):
            given = str(data.get("secret", ""))
        if given != SECRET:
            log.warning("Noto'g'ri maxfiy so'z, so'rov rad etildi")
            return web.Response(status=403, text="forbidden")

    if not data:
        return web.Response(status=400, text="bo'sh xabar")

    log.info("Signal keldi: %s", raw[:300])
    await send_telegram(request.app, format_message(data))
    return web.Response(text="ok")


async def health(_: web.Request) -> web.Response:
    return web.Response(text="Zona bot ishlayapti ✅")


async def on_startup(app: web.Application) -> None:
    app["http"] = ClientSession(timeout=ClientTimeout(total=15))
    if not BOT_TOKEN or not CHAT_IDS:
        log.error("BOT_TOKEN yoki CHAT_ID berilmagan!")
    if not SECRET:
        log.warning("WEBHOOK_SECRET berilmagan — har kim signal yubora oladi!")


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook", webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
