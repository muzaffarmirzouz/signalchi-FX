# TradingView Zona Signal Bot

TradingView'da chizgan zonangizga narx yetganda Telegram'ga signal yuboradi.

```
TradingView alert  ──webhook──►  Railway (main.py)  ──►  Telegram
```

## 1. Telegram tomoni

1. **@BotFather** → `/newbot` → token oling.
2. Signal qayerga borishini hal qiling:
   - **O'zingizga:** botga `/start` yozing, keyin **@userinfobot** orqali ID'ingizni oling.
   - **Kanal/guruhga:** botni admin qiling. ID `-100...` bilan boshlanadi (masalan, kanaldagi postni **@getidsbot** ga forward qilib bilasiz).

## 2. Railway'ga qo'yish

1. Shu fayllarni yangi GitHub repoga yuklang (`main.py`, `requirements.txt`, `Procfile`).
2. Railway → **New Project → Deploy from GitHub repo**.
3. **Variables** bo'limiga qo'shing:

| Nomi | Qiymati |
|---|---|
| `BOT_TOKEN` | BotFather bergan token |
| `CHAT_ID` | Signal boradigan ID (bir nechta bo'lsa: `123,-100456`) |
| `WEBHOOK_SECRET` | O'zingiz o'ylagan maxfiy so'z, masalan `zona2026xyz` |

4. **Settings → Networking → Generate Domain** bosing. Sizga shunaqa manzil beradi:
   `https://zona-bot-production.up.railway.app`
5. Brauzerda shu manzilni oching — "Zona bot ishlayapti ✅" chiqsa, tayyor.

## 3. TradingView tomoni

1. Chartda zonani **Rectangle** (to'rtburchak) yoki **Horizontal Line** bilan chizing.
2. Chizmaga o'ng tugma → **Add alert on Rectangle** (yoki Alt+A).
3. **Condition:** `Entering` (zonaga kirdi) — yoki chiziq uchun `Crossing`.
4. **Trigger:** `Only once` (bir marta) yoki `Once per bar close` (har sham yopilganda).
5. **Notifications** → **Webhook URL** ni yoqing va yozing:

```
https://SIZNING-MANZIL.up.railway.app/webhook?key=zona2026xyz
```

6. **Message** maydoniga shuni qo'ying (zona va izohni har alert uchun o'zgartirasiz):

```json
{
  "signal": "SOTISH zonasi",
  "ticker": "{{ticker}}",
  "price": "{{close}}",
  "zona": "65000 - 66000",
  "interval": "{{interval}}",
  "vaqt": "{{timenow}}",
  "izoh": "Qarshilik zonasi, reaksiyani kuting"
}
```

Telegram'ga shunday keladi:

```
🔔 Zona signali

🚦 Signal: SOTISH zonasi
📈 Juftlik: BTCUSDT
💰 Narx: 65012.5
📍 Zona: 65000 - 66000
⏱ Taymfreym: 15
🕒 Vaqt: 2026-09-30T10:15:00Z
📝 Izoh: Qarshilik zonasi, reaksiyani kuting
```

Message'ga JSON emas, oddiy matn yozsangiz ham ishlaydi — shunchaki o'sha matn keladi.

## Eslatmalar

- `{{timenow}}` UTC vaqtda keladi (Toshkent vaqti = UTC + 5).
- Maydonlardan keraksizini o'chirib tashlashingiz yoki o'zingiznikini qo'shishingiz mumkin — bot hammasini ko'rsatadi.
- Maxfiy so'z (`key`) noto'g'ri bo'lsa bot so'rovni rad etadi, shuning uchun manzilni hech kimga bermang.
- TradingView webhook faqat standart portlarga (443) yuboradi — Railway domeni shunga mos.
