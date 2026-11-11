//+------------------------------------------------------------------+
//| ZonaIndiFeed.mq4                                                 |
//| MT4 indikatori strelkasini signalchi-FX botiga yuboradi (/indi). |
//| Faqat YOPILGAN shamdagi signal olinadi (repaint'dan himoya).      |
//| DIQQAT: kompilyatsiya qilib sinalmagan — xato chiqsa yuboring.    |
//+------------------------------------------------------------------+
#property strict

extern string InpUrl       = "https://SIZNING-APP.up.railway.app/indi?key=MAXFIY_SOZ";
extern string InpBotSymbol = "XAUUSD";                 // botdagi nomi (bo'sh bo'lsa grafik simvoli)
extern string InpIndicator = "BrainTrend2SigALERT5";   // Navigator'dagi indikator nomi (fayl nomi, .ex4 siz)
extern int    InpBuyBuffer  = 0;                       // BUY strelka buferi raqami
extern int    InpSellBuffer = 1;                       // SELL strelka buferi raqami
extern int    InpShift      = 1;                       // 1 = oxirgi YOPILGAN sham (0 ishlatmang: repaint)
extern int    InpAtrPeriod  = 14;
extern double InpSlAtr      = 1.5;                     // SL masofasi = ATR * shu son
extern double InpTpAtr      = 2.0;                     // TP masofasi = ATR * shu son
extern bool   InpScan       = true;                    // ishga tushganda bufer tahlilini jurnalga yozadi

//--- "Kuchli signal": strelka + tasdiq nuqtasi (masalan FL03: bufer 0 = ko'k nuqta, bufer 1 = sariq nuqta)
extern string InpConfirm       = "FL03";               // tasdiq indikatori nomi ("" = o'chirilgan)
extern int    InpConfBuyBuffer  = 0;                   // BUY ni tasdiqlovchi nuqta buferi
extern int    InpConfSellBuffer = 1;                   // SELL ni tasdiqlovchi nuqta buferi
extern int    InpConfWindow     = 3;                   // nuqta strelkadan oldin/bir vaqtda necha shamgacha bo'lsa ham hisoblanadi
extern bool   InpOnlyStrong     = false;               // true = faqat kuchli signallar yuboriladi

datetime g_lastBar = 0;

bool Valid(double v)
{
   return(v != EMPTY_VALUE && v != 0.0 && MathAbs(v) < 1.0e9);
}

//--- Hamma buferlarni skanerlaydi: qaysi bufer strelka (kam-kam qiymat) ekanini ko'rsatadi
void ScanBuffers()
{
   Print("ZonaIndiFeed SKAN: ", InpIndicator, " ", Symbol(), " so'nggi 300 sham");
   for(int b = 0; b < 8; b++)
   {
      int cnt = 0;
      string last = "";
      for(int i = 1; i <= 300; i++)
      {
         double v = iCustom(NULL, 0, InpIndicator, b, i);
         if(Valid(v)) { cnt++; if(last == "") last = TimeToString(Time[i]) + " = " + DoubleToString(v, Digits); }
      }
      Print("  bufer ", b, ": ", cnt, " ta qiymat (300 shamdan)", (last != "" ? "; oxirgisi " + last : ""));
   }
   Print("Strelka buferida odatda 5-60 ta qiymat bo'ladi; 300 ga yaqin bo'lsa u chiziq/rang buferi.");
}

int OnInit()
{
   if(StringFind(InpUrl, "SIZNING-APP") >= 0 || StringFind(InpUrl, "MAXFIY_SOZ") >= 0)
      Print("ZonaIndiFeed: InpUrl ni Railway manzilingiz va WEBHOOK_SECRET bilan to'ldiring.");
   if(InpShift < 1)
      Print("ZonaIndiFeed: InpShift 1 dan kichik — signal qayta chizilishi mumkin!");
   if(InpScan) { ScanBuffers(); ScanConfirm(); }
   return(INIT_SUCCEEDED);
}

//--- Tasdiq indikatorida (buffer) oxirgi InpConfWindow ta yopilgan shamdan birida qiymat bormi
bool HasConfirm(int buffer)
{
   if(StringLen(InpConfirm) == 0) return(false);
   for(int i = InpShift; i < InpShift + InpConfWindow; i++)
      if(Valid(iCustom(NULL, 0, InpConfirm, buffer, i))) return(true);
   return(false);
}

void ScanConfirm()
{
   if(StringLen(InpConfirm) == 0) return;
   Print("ZonaIndiFeed SKAN (tasdiq): ", InpConfirm);
   for(int b = 0; b < 2; b++)
   {
      int cnt = 0;
      string last = "";
      for(int i = 1; i <= 300; i++)
      {
         double v = iCustom(NULL, 0, InpConfirm, b, i);
         if(Valid(v)) { cnt++; if(last == "") last = TimeToString(Time[i]) + " = " + DoubleToString(v, Digits); }
      }
      Print("  tasdiq bufer ", b, ": ", cnt, " ta qiymat (300 shamdan)", (last != "" ? "; oxirgisi " + last : ""));
   }
}

void Send(string side, double atr, datetime barTime, bool strong)
{
   string sym = (StringLen(InpBotSymbol) > 0 ? InpBotSymbol : Symbol());
   string json = "{\"symbol\":\"" + sym + "\",\"side\":\"" + side + "\",\"name\":\"" + InpIndicator +
                 "\",\"strong\":" + (strong ? "true" : "false") + ",\"bar\":\"" + IntegerToString((int)barTime) + "\",\"sl_dist\":" + DoubleToString(atr * InpSlAtr, 5) +
                 ",\"tp_dist\":" + DoubleToString(atr * InpTpAtr, 5) + "}";
   char post[], result[];
   string resHeaders;
   int n = StringToCharArray(json, post, 0, StringLen(json));   // oxirgi nol baytsiz
   ArrayResize(post, n);
   ResetLastError();
   int code = WebRequest("POST", InpUrl, "Content-Type: application/json\r\n", 5000, post, result, resHeaders);
   if(code == -1)
   {
      int err = GetLastError();
      Print("ZonaIndiFeed: WebRequest xato ", err, err == 4060 ? " — Tools>Options>Expert Advisors da URL ruxsat bering" : "");
      return;
   }
   Print("ZonaIndiFeed: ", side, strong ? " (KUCHLI)" : "", " yuborildi, bot javobi (", code, "): ", CharArrayToString(result));
   if(code == 403) Print("ZonaIndiFeed: key= WEBHOOK_SECRET bilan bir xil emas.");
}

void OnTick()
{
   if(Time[0] == g_lastBar) return;          // faqat yangi sham ochilganda
   bool first = (g_lastBar == 0);
   g_lastBar = Time[0];
   if(first) return;                         // ulanganda eski strelkani yubormaymiz

   double b = iCustom(NULL, 0, InpIndicator, InpBuyBuffer,  InpShift);
   double s = iCustom(NULL, 0, InpIndicator, InpSellBuffer, InpShift);
   bool hasBuy = Valid(b), hasSell = Valid(s);
   if(hasBuy == hasSell) return;             // signal yo'q yoki ikkalasi birdan (shubhali) — o'tkazamiz

   double atr = iATR(NULL, 0, InpAtrPeriod, InpShift);
   if(atr <= 0) return;
   bool strong = HasConfirm(hasBuy ? InpConfBuyBuffer : InpConfSellBuffer);
   if(InpOnlyStrong && !strong) return;
   Send(hasBuy ? "BUY" : "SELL", atr, Time[InpShift], strong);
}
