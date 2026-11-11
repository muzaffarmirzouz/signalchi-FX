//+------------------------------------------------------------------+
//| ZonaPriceFeed.mq5                                                |
//| MT5 (Exness) narxini signalchi-FX botiga yuboradi.               |
//| SAVDO QILMAYDI: faqat narxni o'qiydi va botga POST qiladi.       |
//+------------------------------------------------------------------+
#property copyright "signalchi-FX"
#property version   "1.00"
#property strict

// Bot manzili: Railway domeni + /price?key=WEBHOOK_SECRET
input string InpUrl           = "https://SIZNING-APP.up.railway.app/price?key=MAXFIY_SOZ";
// "BrokerNomi:BotNomi" juftliklari, vergul bilan. Exness'da nomlar odatda XAUUSD va BTCUSD
// (hisob turiga qarab XAUUSDm, XAUUSDz bo'lishi mumkin — Market Watch'dagi nomni yozing).
input string InpSymbols       = "XAUUSD:XAUUSD,BTCUSD:BTCUSDT";
input int    InpIntervalSec   = 3;    // necha soniyada yuborish
input int    InpMaxTickAgeSec = 20;   // tick shundan eski bo'lsa (bozor yopiq) yuborilmaydi

string   g_broker[];
string   g_bot[];
int      g_count   = 0;
datetime g_lastLog = 0;
int      g_sent    = 0;

int OnInit()
{
   if(StringFind(InpUrl, "SIZNING-APP") >= 0 || StringFind(InpUrl, "MAXFIY_SOZ") >= 0)
   {
      Print("ZonaPriceFeed: InpUrl ni o'zingizning Railway manzilingiz va WEBHOOK_SECRET bilan to'ldiring.");
      return INIT_PARAMETERS_INCORRECT;
   }

   string items[];
   int n = StringSplit(InpSymbols, ',', items);
   ArrayResize(g_broker, MathMax(n, 1));
   ArrayResize(g_bot, MathMax(n, 1));
   g_count = 0;

   for(int i = 0; i < n; i++)
   {
      string it = items[i];
      StringTrimLeft(it);
      StringTrimRight(it);
      if(it == "") continue;

      string pair[];
      int m = StringSplit(it, ':', pair);
      string b = pair[0];
      string t = (m > 1 ? pair[1] : pair[0]);
      StringTrimLeft(b);  StringTrimRight(b);
      StringTrimLeft(t);  StringTrimRight(t);

      if(!SymbolSelect(b, true))
      {
         PrintFormat("ZonaPriceFeed: '%s' simvoli topilmadi. Market Watch'dagi aniq nomni yozing (masalan XAUUSDm).", b);
         continue;
      }
      g_broker[g_count] = b;
      g_bot[g_count]    = t;
      g_count++;
   }

   if(g_count == 0)
   {
      Print("ZonaPriceFeed: birorta ham simvol topilmadi, to'xtadi.");
      return INIT_FAILED;
   }

   EventSetTimer(MathMax(1, InpIntervalSec));
   PrintFormat("ZonaPriceFeed ishga tushdi: %d ta simvol, har %d soniyada. Eslatma: Tools > Options > Expert Advisors da WebRequest uchun URL ruxsat etilgan bo'lishi kerak.",
               g_count, InpIntervalSec);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   for(int i = 0; i < g_count; i++)
      SendPrice(i);
}

void SendPrice(const int idx)
{
   MqlTick tick;
   if(!SymbolInfoTick(g_broker[idx], tick)) return;
   if(tick.bid <= 0) return;
   // Bozor yopiq bo'lsa eski narxni "yangi" qilib yubormaymiz — bot zaxira manbaga o'tadi.
   if(InpMaxTickAgeSec > 0 && (TimeCurrent() - tick.time) > InpMaxTickAgeSec) return;

   int    digits = (int)SymbolInfoInteger(g_broker[idx], SYMBOL_DIGITS);
   double ask    = (tick.ask > 0 ? tick.ask : tick.bid);
   string json   = StringFormat("{\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s}",
                                g_bot[idx],
                                DoubleToString(tick.bid, digits),
                                DoubleToString(ask, digits));

   char post[];
   char result[];
   string resHeaders;
   StringToCharArray(json, post, 0, StringLen(json), CP_UTF8);
   ArrayResize(post, StringLen(json));   // oxiridagi nol bayt bo'lmasin

   ResetLastError();
   int code = WebRequest("POST", InpUrl, "Content-Type: application/json\r\n", 3000, post, result, resHeaders);

   if(code == 200)
   {
      g_sent++;
      return;
   }

   // Xatoni har daqiqada bir marta yozamiz
   if(TimeLocal() - g_lastLog < 60) return;
   g_lastLog = TimeLocal();
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4060)
         Print("ZonaPriceFeed: URL ruxsat etilmagan. Tools > Options > Expert Advisors > 'Allow WebRequest for listed URL' ga shu manzilning boshini (https://...up.railway.app) qo'shing.");
      else
         PrintFormat("ZonaPriceFeed: WebRequest xato, kod %d", err);
   }
   else if(code == 403)
      Print("ZonaPriceFeed: bot 403 qaytardi — InpUrl dagi key= WEBHOOK_SECRET bilan bir xil emas.");
   else
      PrintFormat("ZonaPriceFeed: bot javobi HTTP %d", code);
}
