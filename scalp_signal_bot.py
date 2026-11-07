#!/usr/bin/env python3
"""
Kripto Scalp Sinyal Botu -> Telegram
====================================
Binance'in herkese acik fiyat verisini (API anahtari gerekmez) her dakika okur,
5 dakikalik grafikte trend yonune geri cekilme (pullback) kurulumu olusunca
Telegram'a GIRIS / SL / TP1 / TP2 / LOT mesaji atar. Emir GIRMEZ, sadece sinyal verir.

Strateji (sohbette kullandigimizla ayni):
  1) Trend (15dk): fiyat EMA50 ustunde ve EMA50 yukari egimliyse sadece ALIS,
     altinda ve asagi egimliyse sadece SATIS.
  2) Kurulum (5dk): fiyat EMA20'ye geri ceker (son 3 mumda RSI7 >= 55 satis icin /
     <= 45 alis icin), sonra trend yonunde EMA20'nin otesinde kapanan donus mumu.
  3) SL: son 6 mumun tepe/dibi + 0.2*ATR. SL mesafesi 0.5-2.5 ATR disindaysa sinyal yok.
  4) TP1 = 1R (yarisini kapat, SL'yi girise cek), TP2 = 2R.
  5) Lot = RISK_USD / (SL mesafesi * kontrat buyuklugu).
  6) Ayni paritede sinyalden sonra 30 dk bekleme, gunde en fazla MAX_SIGNALS_PER_DAY sinyal.
  7) Hacim teyidi: donus mumunun hacmi son 20 mum ortalamasinin VOL_MULT katindan az ise sinyal yok.
  9) Rapor: gonderilen sinyaller signals.json'a yazilir; --report her aksam son 24 saatin
     sonucunu (TP1/TP2/SL/basabas/acik) ve basari oranini Telegram'a gonderir.
  8) Haber filtresi: ForexFactory takviminde USD "High" etkili bir veri (CPI, Fed, NFP...)
     NEWS_BLOCK_MIN dakika icindeyse sinyal yok; 4 saat icindeki onemli veri mesajda uyari olarak yazar.

KURULUM
-------
1) Telegram'da @BotFather'a yaz -> /newbot -> bot TOKEN'ini al.
2) Kendi botuna herhangi bir mesaj at. Sonra tarayicida ac:
     https://api.telegram.org/bot<TOKEN>/getUpdates
   Cikan yazida "chat":{"id": 123456789 ...} -> bu senin CHAT_ID'n.
3) 7/24 acik bir sunucuya (VPS) bu dosyayi koy. Python 3.8+ yeter, ek paket gerekmez.
     export TELEGRAM_TOKEN="123:ABC..."
     export TELEGRAM_CHAT_ID="123456789"
     nohup python3 scalp_signal_bot.py > bot.log 2>&1 &
   (Kalici calismasi icin dosyanin en altindaki systemd ornegine bak.)

Not: Sinyaller Binance fiyatina goredir. Broker (MT5) fiyati birkac dolar farkli olabilir;
seviyeleri o fark kadar kaydir. Egitim/demo amaclidir, yatirim tavsiyesi degildir.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TR = timezone(timedelta(hours=3))  # mesajlardaki saatler Turkiye saati

# ----------------------------- AYARLAR -----------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "HYPEUSDT", "AVAXUSDT"]

# MT5'te 1 lot kac coin? (Sembole sag tik -> Specification'dan kontrol et)
CONTRACT_SIZE = {"BTCUSDT": 1, "ETHUSDT": 1, "XRPUSDT": 1, "SOLUSDT": 1, "HYPEUSDT": 1, "AVAXUSDT": 1}

RISK_USD = 500.0            # islem basi risk (%0.5 / 100k)
MAX_NOTIONAL_USD = 150000.0 # tek islemin max buyuklugu (teminat yetsin diye; ~2 BTC)
COMMISSION_PER_LOT = 25.0   # brokerin lot basi komisyonu (USD), sadece bilgi icin
MAX_SIGNALS_PER_DAY = 8     # gunluk sinyal limiti (gunluk %4.5 kurali icin tampon)
COOLDOWN_MIN = 30           # ayni paritede iki sinyal arasi bekleme
POLL_SECONDS = 60           # kac saniyede bir kontrol
VOL_MULT = 1.2              # donus mumu hacmi >= ortalama hacim x bu katsayi
NEWS_BLOCK_MIN = 30         # onemli haberden once/sonra bu kadar dakika sinyal verme
NEWS_COUNTRIES = {"USD"}    # takip edilen ulke(ler); "All" eklenirse hepsi
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Binance halka acik veri adresleri (biri engelliyse digeri denenir)
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
# -------------------------------------------------------------------


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "scalp-signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def klines(symbol, interval, limit=300):
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            url = f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
            data = http_get_json(url)
            # Son mum henuz kapanmadi -> at
            now_ms = int(time.time() * 1000)
            if data and int(data[-1][6]) > now_ms:
                data = data[:-1]
            return {
                "t": [int(k[0]) for k in data],
                "o": [float(k[1]) for k in data],
                "h": [float(k[2]) for k in data],
                "l": [float(k[3]) for k in data],
                "c": [float(k[4]) for k in data],
                "v": [float(k[5]) for k in data],
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"{symbol} {interval} veri alinamadi: {last_err}")


def klines_since(symbol, start_ms, interval="5m"):
    """start_ms'den simdiye kadar kapanmis mumlar (en fazla 1000)."""
    for host in BINANCE_HOSTS:
        try:
            url = (f"{host}/api/v3/klines?symbol={symbol}&interval={interval}"
                   f"&startTime={int(start_ms)}&limit=1000")
            data = http_get_json(url)
            now_ms = int(time.time() * 1000)
            return [(float(k[2]), float(k[3])) for k in data if int(k[6]) <= now_ms]
        except Exception:  # noqa: BLE001
            continue
    return None


def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(values, n):
    """Wilder RSI, her mum icin deger listesi."""
    out = [50.0] * len(values)
    if len(values) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    gain /= n
    loss /= n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return sum(trs[-n:]) / n


def fmt(x):
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 10:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def check_symbol(symbol):
    """Kurulum varsa sinyal sozlugu, yoksa None dondurur."""
    m15 = klines(symbol, "15m")
    m5 = klines(symbol, "5m")

    c15 = m15["c"]
    e50_15 = ema(c15, 50)
    up = c15[-1] > e50_15[-1] and e50_15[-1] > e50_15[-6]
    down = c15[-1] < e50_15[-1] and e50_15[-1] < e50_15[-6]
    if not (up or down):
        return None

    o, h, l, c = m5["o"], m5["h"], m5["l"], m5["c"]
    e20, e50 = ema(c, 20), ema(c, 50)
    r7 = rsi(c, 7)
    a = atr(h, l, c, 14)
    i = len(c) - 1  # son KAPANMIS 5dk mum

    if down:
        pulled_back = max(r7[i - 3:i + 1]) >= 55 and max(h[i - 2:i + 1]) >= e20[i]
        rejection = c[i] < o[i] and c[i] < e20[i] and c[i] < e50[i]
        if not (pulled_back and rejection):
            return None
        entry = c[i]
        sl = max(h[i - 5:i + 1]) + 0.2 * a
        dist = sl - entry
        side = "SAT (SELL)"
        tp1, tp2 = entry - dist, entry - 2 * dist
    else:
        pulled_back = min(r7[i - 3:i + 1]) <= 45 and min(l[i - 2:i + 1]) <= e20[i]
        rejection = c[i] > o[i] and c[i] > e20[i] and c[i] > e50[i]
        if not (pulled_back and rejection):
            return None
        entry = c[i]
        sl = min(l[i - 5:i + 1]) - 0.2 * a
        dist = entry - sl
        side = "AL (BUY)"
        tp1, tp2 = entry + dist, entry + 2 * dist

    if not (0.5 * a <= dist <= 2.5 * a):
        return None

    vols = m5["v"][i - 20:i]
    vol_ratio = m5["v"][i] / (sum(vols) / len(vols)) if vols and sum(vols) > 0 else 0
    if vol_ratio < VOL_MULT:  # hacim teyidi yok
        return None

    cs = CONTRACT_SIZE.get(symbol, 1)
    lot = min(RISK_USD / (dist * cs), MAX_NOTIONAL_USD / (entry * cs))
    risk = lot * dist * cs
    if 2 * risk < 3 * lot * COMMISSION_PER_LOT:  # komisyon kari yiyorsa sinyal verme
        return None
    return {
        "risk": risk, "reward2": 2 * risk, "fee": lot * COMMISSION_PER_LOT,
        "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "dist": dist, "lot": lot, "rsi7": r7[i], "atr": a, "candle_time": m5["t"][i],
        "vol_ratio": vol_ratio,
    }


_news_cache = {"t": 0, "events": None}


def high_impact_events():
    """ForexFactory haftalik takviminden yuksek etkili olaylar: [(datetime_utc, baslik, ulke)]."""
    if _news_cache["events"] is not None and time.time() - _news_cache["t"] < 3600:
        return _news_cache["events"]
    events = []
    try:
        for e in http_get_json(NEWS_URL):
            if e.get("impact") != "High":
                continue
            if "All" not in NEWS_COUNTRIES and e.get("country") not in NEWS_COUNTRIES:
                continue
            dt = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            events.append((dt, e.get("title", "?"), e.get("country", "")))
    except Exception as ex:  # noqa: BLE001
        print(f"Takvim alinamadi: {ex}", file=sys.stderr)
        events = None
    _news_cache.update(t=time.time(), events=events)
    return events


def news_check():
    """(engelle_mi, uyari_metni) dondurur."""
    events = high_impact_events()
    if events is None:
        return False, "Haber takvimi alinamadi, onemli veri saatlerini kendin kontrol et."
    now = datetime.now(timezone.utc)
    for dt, title, cc in events:
        mins = (dt - now).total_seconds() / 60
        if -NEWS_BLOCK_MIN <= mins <= NEWS_BLOCK_MIN:
            return True, f"{cc} {title} ({mins:+.0f} dk)"
    upcoming = sorted((dt, t, c) for dt, t, c in events if 0 < (dt - now).total_seconds() <= 4 * 3600)
    if upcoming:
        dt, title, cc = upcoming[0]
        tr = dt.astimezone(TR).strftime("%d.%m %H:%M TR")
        return False, f"Yaklasan onemli veri: {cc} {title} - {tr}. Pozisyonu oncesinde kapatmayi dusun."
    return False, ""


def find_chat_id():
    """CHAT_ID girilmemisse: bota en son 'Baslat' diyen sohbetin ID'sini bul."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        res = http_get_json(url).get("result", [])
        for upd in reversed(res):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                print(f"CHAT_ID bulundu: {chat['id']}")
                return str(chat["id"])
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates hatasi: {e}", file=sys.stderr)
    print("CHAT_ID bulunamadi: Telegram'da bota /start yaz.", file=sys.stderr)
    return ""


def send_telegram(text):
    global TELEGRAM_CHAT_ID
    if TELEGRAM_TOKEN and not TELEGRAM_CHAT_ID:
        TELEGRAM_CHAT_ID = find_chat_id()
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM AYARLI DEGIL]\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print(f"Telegram hatasi: {e}", file=sys.stderr)


def signal_text(s):
    name = s["symbol"].replace("USDT", "USD")
    t = datetime.fromtimestamp(s["candle_time"] / 1000 + 300, tz=TR).strftime("%H:%M TR")
    return (
        f"📊 {name} — {s['side']}\n"
        f"Giris: {fmt(s['entry'])} (piyasa)\n"
        f"SL: {fmt(s['sl'])}\n"
        f"TP1 (1R, yarisini kapat): {fmt(s['tp1'])}\n"
        f"TP2 (2R): {fmt(s['tp2'])}\n"
        f"Lot: {s['lot']:.2f}  (SL'de -{s['risk']:.0f}$ | TP2'de +{s['reward2']:.0f}$ | komisyon ~{s['fee']:.0f}$)\n"
        f"Stop mesafesi: {fmt(s['dist'])} | ATR5m: {fmt(s['atr'])} | RSI7: {s['rsi7']:.0f} | Hacim: {s['vol_ratio']:.1f}x ort.\n"
        f"Mum kapanisi: {t}\n"
        + (f"📰 {s['news']}\n" if s.get("news") else "")
        + f"⚠️ Fiyat Binance'e gore; MT5 farkini kaydir. Gunluk -%4.5 / toplam -%9 kuralini unutma."
    )


def main():
    send_telegram(f"✅ Scalp sinyal botu basladi: {', '.join(SYMBOLS)}")
    last_signal_at = {s: 0.0 for s in SYMBOLS}
    last_candle = {s: 0 for s in SYMBOLS}
    day, sent_today = datetime.now(timezone.utc).date(), 0

    while True:
        today = datetime.now(timezone.utc).date()
        if today != day:
            day, sent_today = today, 0

        for sym in SYMBOLS:
            try:
                if sent_today >= MAX_SIGNALS_PER_DAY:
                    break
                if time.time() - last_signal_at[sym] < COOLDOWN_MIN * 60:
                    continue
                s = check_symbol(sym)
                if s and s["candle_time"] != last_candle[sym]:
                    block, s["news"] = news_check()
                    if block:
                        print(f"{sym}: haber nedeniyle atlandi ({s['news']})")
                        last_candle[sym] = s["candle_time"]
                        continue
                    send_telegram(signal_text(s))
                    log_signal(s)
                    last_candle[sym] = s["candle_time"]
                    last_signal_at[sym] = time.time()
                    sent_today += 1
                    print(f"{datetime.now()} sinyal: {sym} {s['side']}")
            except Exception as e:  # noqa: BLE001
                print(f"{datetime.now()} {sym} hata: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


def load_signals():
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def log_signal(s):
    """Gonderilen sinyali dosyaya ekler (son 30 gun tutulur)."""
    rows = load_signals()
    rows.append({
        "t": s["candle_time"] + 300_000,  # mum kapanis zamani (ms)
        "symbol": s["symbol"], "dir": "short" if "SAT" in s["side"] else "long",
        "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
        "lot": round(s["lot"], 4), "risk": round(s["risk"], 2),
    })
    cutoff = time.time() * 1000 - 30 * 86400_000
    rows = [r for r in rows if r["t"] >= cutoff]
    with open(SIGNALS_FILE, "w") as f:
        json.dump(rows, f, indent=1)


def evaluate(sig):
    """Sinyalin sonucu: (etiket, R). Ayni mumda hem SL hem TP varsa SL sayilir (temkinli).
    TP1'de yarisi kapanir ve SL girise cekilir: TP2 = +1.5R, TP1 sonrasi basabas = +0.5R."""
    bars = klines_since(sig["symbol"], sig["t"])
    if bars is None:
        return "veri yok", 0.0
    short = sig["dir"] == "short"
    tp1_hit = False
    for hi, lo in bars:
        sl_hit = hi >= sig["sl"] if short else lo <= sig["sl"]
        t1 = lo <= sig["tp1"] if short else hi >= sig["tp1"]
        t2 = lo <= sig["tp2"] if short else hi >= sig["tp2"]
        be = hi >= sig["entry"] if short else lo <= sig["entry"]
        if not tp1_hit:
            if sl_hit:
                return "SL", -1.0
            if t1:
                tp1_hit = True
                if t2:
                    return "TP2", 1.5
        else:
            if t2:
                return "TP2", 1.5
            if be:
                return "TP1 + basabas", 0.5
    return ("TP1 (acik)", 0.5) if tp1_hit else ("acik", 0.0)


def daily_report():
    now_ms = time.time() * 1000
    rows = [r for r in load_signals() if r["t"] >= now_ms - 86400_000]
    if not rows:
        send_telegram("📋 Gunluk rapor: son 24 saatte sinyal gonderilmedi.")
        return
    lines, wins, losses, total_r, total_usd = [], 0, 0, 0.0, 0.0
    for r in rows:
        label, rr = evaluate(r)
        if label.startswith("TP"):
            wins += 1
        elif label == "SL":
            losses += 1
        total_r += rr
        total_usd += rr * r.get("risk", RISK_USD)
        t = datetime.fromtimestamp(r["t"] / 1000, tz=TR).strftime("%H:%M")
        name = r["symbol"].replace("USDT", "USD")
        yon = "SAT" if r["dir"] == "short" else "AL"
        lines.append(f"{t} {name} {yon} -> {label} ({rr:+.1f}R)")
    decided = wins + losses
    rate = f"%{100 * wins / decided:.0f}" if decided else "-"
    text = (
        f"📋 Gunluk rapor (son 24 saat)\n"
        f"Sinyal: {len(rows)} | TP1+ : {wins} | SL: {losses} | Acik: {len(rows) - decided}\n"
        f"Basari orani: {rate}\n"
        f"Toplam: {total_r:+.1f}R (~{total_usd:+.0f}$, komisyon haric)\n\n"
        + "\n".join(lines)
        + "\n\nNot: Binance fiyatina gore, sinyale tam uyuldugu varsayilarak hesaplandi."
    )
    send_telegram(text)


def run_once():
    """GitHub Actions icin: tek tur kontrol. Sadece son 5 dk icinde kapanan mumdaki
    kurulumlari gonderir (her 5 dk'lik calismada ayni sinyal tekrar gitmesin diye)."""
    now_ms = time.time() * 1000
    for sym in SYMBOLS:
        try:
            s = check_symbol(sym)
            if not s:
                print(f"{sym}: kurulum yok")
                continue
            closed_ago = now_ms - (s["candle_time"] + 300_000)
            if 0 <= closed_ago < 300_000:
                block, s["news"] = news_check()
                if block:
                    print(f"{sym}: haber nedeniyle atlandi ({s['news']})")
                    continue
                send_telegram(signal_text(s))
                log_signal(s)
                print(f"{sym}: SINYAL gonderildi ({s['side']})")
            else:
                print(f"{sym}: kurulum eski mumda, atlandi")
        except Exception as e:  # noqa: BLE001
            print(f"{sym} hata: {e}", file=sys.stderr)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    elif "--report" in sys.argv:
        daily_report()
    elif "--test" in sys.argv:
        send_telegram("✅ Test: bot Telegram'a ulasabiliyor.")
    else:
        main()

# ---------------- systemd ornegi (/etc/systemd/system/scalpbot.service) ----------------
# [Unit]
# Description=Scalp Signal Bot
# After=network-online.target
#
# [Service]
# Environment=TELEGRAM_TOKEN=123:ABC...
# Environment=TELEGRAM_CHAT_ID=123456789
# ExecStart=/usr/bin/python3 /root/scalp_signal_bot.py
# Restart=always
#
# [Install]
# WantedBy=multi-user.target
#
# Sonra: sudo systemctl daemon-reload && sudo systemctl enable --now scalpbot
