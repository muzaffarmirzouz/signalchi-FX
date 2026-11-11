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

---

## 🤖 Avto-signal (oltin + bitcoin)

Bot endi zona yozmasangiz ham **XAUUSD** va **BTCUSDT** shamlarini o'zi tahlil qilib signal beradi
(`strategy.py`). Shamlar: oltin — Bitget, bitcoin — Binance. Kirish narxi esa botning odatdagi
manbasidan olinadi (MT5/Exness tuzatishi bilan).

**Qoidalar (15 daqiqalik yopilgan sham bo'yicha):**

| Setap | Shart |
|---|---|
| **Breakout** | sham oldingi 20 shamning eng yuqorisidan (BUY) / eng pastidan (SELL) yopildi, EMA20/EMA50 trendi shu tomonga, RSI haddan oshmagan (BUY < 75, SELL > 25) |
| **RSI qaytishi** | RSI 30 dan yuqoriga (BUY) yoki 70 dan pastga (SELL) qaytdi va sham shu tomonga yopildi (trendga qarshi, shuning uchun qat'iy) |

Har signalga **kuch (⭐1–4)**: katta sham tanasi, hajm portlashi, sham chekkada yopilishi, soya (rad etish) uchun ball.
`AUTO_MIN_STARS` (standart 2) dan past signallar yuborilmaydi.
**SL = 1.5 × ATR**, **TP = 2.5 × ATR** (breakout) yoki 2 × ATR (qaytish). Signal chiqqach, mavjud kuzatuv ishlaydi:
+50/+100 pips, SL yoki TP urilganda signal postiga reply bo'ladi.

**Himoyalar:** bir juftlik+yo'nalishda 60 daqiqa tanaffus, kuniga ≤ 5 signal/juftlik, shu yo'nalishda ochiq signal
bo'lsa yangisi chiqmaydi, eski (bot o'chib yoqilgandan keyingi) shamlar signal bermaydi.

### Rejimlar (Telegram'dan boshqariladi)

| Buyruq | Ma'nosi |
|---|---|
| `/avto test` | **Standart.** Signal faqat adminga (`ADMIN_ID` / shaxsiy `CHAT_ID`) boradi, "🧪 TEST" belgisi bilan |
| `/avto kanal` | Signal `CHAT_ID` dagi hammaga (kanal) boradi |
| `/avto off` | Avto-signal o'chiq |
| `/avto` | Holat: rejim, qabul qiluvchilar, bugungi signallar soni |
| `/tahlil` | Hozirgi trend, RSI, ATR va **signal bo'lmasa sababi** (sinash uchun foydali) |

Rejim `auto_state.json` ga saqlanadi (Railway Volume uchun `DATA_DIR=/data` qo'ying, aks holda qayta deploy'da
rejim standartga — `test` ga — qaytadi).

### Qo'shimcha o'zgaruvchilar (hammasi ixtiyoriy)

`AUTO_MODE` (test/kanal/off) · `AUTO_SYMBOLS` (`XAUUSD,BTCUSDT`) · `AUTO_TF` (`5m`/`15m`/`30m`/`1h`) ·
`AUTO_MIN_STARS` · `AUTO_COOLDOWN_MIN` · `AUTO_MAX_PER_DAY` · `AUTO_TEST_CHAT` ·
`AUTO_ENTRY` (`pending` standart / `market`) · `AUTO_PULLBACK_ATR` (0.5) · `AUTO_PENDING_MIN` (120)

### ⏳ Kutiladigan kirish (pending) → ✅ Signal AKTIV

Standart (`AUTO_ENTRY=pending`): strategiya signal topganda bot darrov kirishni aytmaydi, balki
**qulayroq kirish narxini** (signal narxidan ATR × 0.5 pastda BUY, yuqorida SELL) belgilaydi:

1. `⏳🟢 SIGNAL KUTILMOQDA: BUY` — kirish, SL, TP yoziladi.
2. Narx kirishga yetganda `✅🟢 SIGNAL AKTIV: BUY` (signal postiga reply) — **shundan boshlab** +pips / TP / SL kuzatiladi.
3. Yetmasa `❌ Signal bekor bo'ldi` (reply): 120 daqiqa o'tdi, narx kirishsiz TP tomonga ketib qoldi yoki kirishdan SL ga sakrab o'tdi.

`/signallar` — kutayotgan va ochiq signallar, `/yopish 5` — kutayotgan signalni qo'lda bekor qilish.
`AUTO_ENTRY=market` — eski usul (signal chiqqan zahoti aktiv).

> Pending avtomatik yaxshiroq emas: kirish narxi yaxshi, lekin kuchli harakatlar kirishsiz ketib qoladi.
> Qaysi biri yaxshiroq — `python3 backtest.py --symbol XAUUSD --csv ... --compare` bilan **o'z ma'lumotingizda** tekshiring.

### Test

```
python3 tests/test_strategy.py
```

> ⚠️ Bu qoidalar foyda kafolatlamaydi. Kanalga ochishdan oldin kamida 1–2 hafta test rejimida
> signallarni kuzating (`/signallar` va TP/SL natijalari) va kerak bo'lsa `AUTO_MIN_STARS` ni oshiring.
> Bu moliyaviy maslahat emas.

---

## 📈 Backtest (strategiyani o'tmishda sinash)

Kanalga ochishdan **oldin** shuni ishga tushiring: avto-signal qoidalari o'tgan kunlarda qancha ishlaganini ko'rsatadi.
Faqat Python (aiohttp kerak emas), o'z kompyuteringizda ishlaydi.

```bash
python3 backtest.py --symbol XAUUSD --days 30
python3 backtest.py --symbol BTCUSDT --days 60
python3 backtest.py --symbol XAUUSD --csv exness_xauusd_m15.csv   # MT5'dan eksport (eng aniq: Exness narxi)
python3 backtest.py --symbol XAUUSD --days 30 --min-stars 3 --cost-r 0.05
python3 backtest.py --symbol XAUUSD --days 30 --compare   # market va pending yonma-yon
python3 backtest.py --symbol XAUUSD --days 30 --entry market --pullback 0.3 --pending-min 90
```

- Jonli bot bilan **bir xil qoidalar**: tanaffus, kunlik limit, "shu yo'nalishda ochiq signal bor" holati.
- Natija **R** birligida: SL = −1R, TP = +1.67R (breakout) / +1.33R (qaytish). `--cost-r` spred/slippage uchun.
- Bir shamda ham SL, ham TP ga tegsa — **SL birinchi** deb olinadi (konservativ).
- Hisobot: umumiy, BUY/SELL, breakout/qaytish bo'yicha va **kuch (⭐) bo'yicha** — `AUTO_MIN_STARS` ni shundan tanlang.
- MT5'da shamlarni eksport qilish: *Tools → History Center* (M15 → Export) yoki grafikdan "Save as CSV".
- `python3 tests/test_backtest.py` — testlar.

**Natijani qanday o'qish:** `o'rtacha R > 0` va `PF > 1.2` bo'lsa va savdolar soni ≥ 30–50 bo'lsa, qoida o'sha davrda
ishlagan. Aks holda (ayniqsa `o'rtacha ≤ 0`) kanalga ochmang. O'tmishdagi natija kelajakni kafolatlamaydi,
va Bitget oltin narxi Exness'dan biroz farq qiladi.

---

## 🔌 MT5 (Exness) narxini botga ulash — narx 1:1

Bot narxni odatda Bitget/Binance'dan oladi, ular Exness'dan 2–5$ farq qilishi mumkin (oltinda). `mt5/ZonaPriceFeed.mq5`
EA'si MT5'dagi **Exness Bid/Ask** narxini botga har 3 soniyada yuboradi. Ulangach bot **hamma narxni** (signal kirishi,
pips, SL/TP, zonalar) to'g'ridan-to'g'ri Exness'dan oladi — `/tuzat` kerak emas. EA **savdo qilmaydi**, faqat narxni yuboradi.

**O'rnatish (kompyuterdagi MT5, telefon emas):**

1. Railway'da `WEBHOOK_SECRET` qiymatini va domeningizni (`https://....up.railway.app`) toping.
2. MT5: **File → Open Data Folder → MQL5 → Experts** ga `mt5/ZonaPriceFeed.mq5` ni nusxalang. **MetaEditor** (F4) da oching → **Compile** (F7), xatosiz bo'lsin.
3. MT5: **Tools → Options → Expert Advisors** → *Allow WebRequest for listed URL* ni belgilang va `https://SIZNING-APP.up.railway.app` ni qo'shing (faqat domen, `/price` siz).
4. **Navigator → Expert Advisors → ZonaPriceFeed** ni istalgan grafikka (masalan XAUUSD) sudrab tashlang. **Inputs** da:
   - `InpUrl` = `https://SIZNING-APP.up.railway.app/price?key=WEBHOOK_SECRET`
   - `InpSymbols` = `XAUUSD:XAUUSD,BTCUSD:BTCUSDT` (`BrokerNomi:BotNomi`; broker nomi Market Watch'dagi bilan bir xil bo'lsin, ba'zan `XAUUSDm`)
   - **Common** → *Allow Algo Trading*; yuqoridagi **Algo Trading** tugmasi yashil bo'lsin.
5. Botga `/mt5` yozing: `🟢 XAUUSD: Bid ... · 3s oldin` va boshqa manba bilan farq ko'rinsa — ulandi.

**Bilib qo'ying:**
- EA faqat **kompyuter/VPS dagi MT5** da ishlaydi (telefondagi MT5 EA ishga tushirmaydi). MT5 yoqiq turishi kerak.
- MT5 o'chsa yoki bozor yopiq bo'lsa, 60 soniyadan keyin bot avtomatik Bitget/Binance (+`/tuzat`) ga qaytadi, EA eski tickni yubormaydi.
- Signal **shamlari** (breakout/RSI hisobi) hozircha Bitget/Binance'dan; **narxlar** (kirish, pips, SL/TP) MT5'dan.
- Xato bo'lsa MT5 *Experts* jurnalida sabab yoziladi: `URL ruxsat etilmagan` (3-qadam), `403` (key noto'g'ri).

---

## 📊 Natijalar va kunlik hisobot

Bot har signalning natijasini o'zi yozib boradi: **TP**, **SL**, vaqt tugashi yoki kirishga yetmay bekor bo'lishi.
Har kuni **23:00 da** (Toshkent vaqti) hisobot yuboradi:

```
📊 Kunlik hisobot — 11.11.2026 · 🧪 TEST rejimi

Yopilgan signallar: 10 ta → ✅ TP: 5 · ❌ SL: 5
🎯 Yutuq: 50% · Jami natija: +3.3R

🟢 BUY: 5 ta (TP 3, SL 2)
🔴 SELL: 5 ta (TP 2, SL 3)
📈 XAUUSD: 7 ta (TP 4, SL 3) · +235 pips
📈 BTCUSDT: 3 ta (TP 1, SL 2) · −40 pips

🏆 Eng yaxshisi: ... · 💔 Eng yomoni: ...
⏳ Kirish narxiga yetmay bekor bo'lgan: 2 ta
🔄 Hali ochiq: XAUUSD BUY · hozir +10 pips
```

- Hisobot **hamma** signalni sanaydi (yutgan ham, yutqazgan ham). Jami natija **R** da: SL = −1R, TP = +1.3…1.7R.
  Pips faqat har juftlik uchun alohida (oltin va bitcoin pips'larini qo'shib bo'lmaydi).
- Test rejimida hisobot faqat adminga, `/avto kanal` rejimida `CHAT_ID` dagi hammaga (kanalga) boradi.
- **Qo'lda buyruq:** `/hisobot` (bugun, hozirgacha) · `/hisobot kecha` · `/hisobot 7` (oxirgi 7 kun).
- Zona/TradingView/`/buy` `/sell` signallari asosiy statistikaga kirmaydi, alohida bitta qator bo'ladi.
- Sozlash: `REPORT_HOUR` (standart 23), `TZ_HOURS` (standart 5 — Toshkent).

> ⚠️ **Muhim:** natijalar `zones.json`, hisobot belgisi `auto_state.json` da saqlanadi. Railway'da **Volume** ulab
> `DATA_DIR=/data` qo'ymasangiz, har qayta deploy/restartda tarix o'chadi va hisobot to'liq bo'lmaydi.

## 📡 MT4/MT5 indikatoridan signal (`/indi`)

Indikatori strelka chizadigan MT4 (yoki MT5) dan signalni botga yuborish: `mt4/ZonaIndiFeed.mq4` (MQL4; MT5 uchun `iCustom`/`CopyBuffer` ga moslash kerak).

1. Faylni MT4: *File → Open Data Folder → MQL4 → Experts* ga qo'ying, MetaEditor'da **Compile**.
2. *Tools → Options → Expert Advisors*: **Allow WebRequest** va Railway domeningizni qo'shing.
3. EA'ni signal chizadigan grafikka (masalan XAUUSD M15) tashlang. Parametrlar: `InpUrl` (`.../indi?key=WEBHOOK_SECRET`), `InpIndicator` (indikator nomi), `InpBuyBuffer`/`InpSellBuffer`.
4. `InpScan=true` bo'lsa ishga tushganda *Experts* jurnalida har bufer nechta qiymat berganini yozadi — strelka buferini shundan topasiz.
- Faqat **yopilgan sham** (`InpShift=1`) o'qiladi. SL/TP = ATR × koeffitsiyent (bot o'z narxiga qo'yadi).
- Hisobotda indikator signallari avto-signaldan **alohida** qatorda chiqadi (`📡 Indikator signallari`).
