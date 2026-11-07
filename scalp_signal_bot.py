#!/usr/bin/env python3
"""
Kripto Sinyal Botu (coklu zaman dilimi) -> Telegram
===================================================
Her saat basi son kapanan 1 saatlik mumlara bakar. Emir GIRMEZ, sadece sinyal verir;
hangi sinyale girilecegine kullanici karar verir.

Strateji:
  1) Yon: 4 saatlik kapanis EMA50 ustunde ve EMA50 yukari egimli -> LONG adayi,
     altinda ve asagi egimli -> SHORT adayi.
  2) Giris: 1 saatlik mum son 10 mumun tepesinin ustunde (long) / dibinin altinda (short) kapanir.
  3) Skor (x/4): 4s + gunluk (EMA20) + haftalik (10 haftalik ort.) + aylik (6 aylik ort.)
     yonlerinden kaci sinyal yonunde. 4/4 en guclu.
  4) Stop %2 (STOP_PCT). +1R'de stop girise, sonra tepe/dipten %1,5 takip (TRAIL_PCT).
  5) Lot = BAKIYE x RISK_PCT / stop mesafesi.
  6) Ayni coinde 6 saat bekleme, gunde en fazla MAX_SIGNALS_PER_DAY sinyal.
  7) Haber filtresi: ForexFactory'de USD "High" etkili veri +-30 dk icindeyse sinyal yok.
  8) Gonderilen sinyaller signals.json'a yazilir; --report son 24 saatin sonucunu gonderir.

Kullanim:  --once (saatlik tarama) | --report (gunluk rapor) | --test (Telegram testi)
Egitim/demo amaclidir, yatirim tavsiyesi degildir.
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
SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "AVAX", "HYPE", "DOGE", "LINK", "BNB", "ADA",
           "SUI", "LTC", "DOT", "NEAR", "APT", "ARB", "OP", "TRX", "UNI", "ATOM"]
BALANCE = 1000.0            # hesap bakiyesi (USD) - lot hesabi icin
RISK_PCT = 0.01             # islem basi risk (%1 = 10 $)
STOP_PCT = 0.02             # stop mesafesi (%2)
TRAIL_PCT = 0.015           # +1R sonrasi takip mesafesi (%1.5)
BREAKOUT_BARS = 10          # 1s kirilim penceresi
MAX_SIGNALS_PER_DAY = 8     # gunluk ust sinir
COOLDOWN_HOURS = 6          # ayni coinde iki sinyal arasi
FEE_PCT = 0.00035           # komisyon+spread (pozisyon buyuklugunun orani, tur)
NEWS_BLOCK_MIN = 30
NEWS_COUNTRIES = {"USD"}
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
# -------------------------------------------------------------------


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def klines(symbol, interval, limit=300, start_ms=None):
    """Kapanmis mumlar: [{t(open), o, h, l, c, T(close)}]"""
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            url = f"{host}/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
            if start_ms:
                url += f"&startTime={int(start_ms)}"
            data = http_get_json(url)
            now_ms = int(time.time() * 1000)
            return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "T": int(k[6])} for k in data if int(k[6]) <= now_ms]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"{symbol} {interval} veri alinamadi: {last_err}")


def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def period_closes(days, key):
    """Gunluk mumlardan tamamlanmis periyot kapanislari (hafta/ay). Son (devam eden) periyot atilir."""
    out, cur_key = [], None
    for d in days:
        k = key(d["t"])
        if k != cur_key:
            out.append(d["c"])
            cur_key = k
        else:
            out[-1] = d["c"]
    return out[:-1]  # son periyot henuz bitmedi


def week_key(ms):
    return int((ms / 86400000 + 3) // 7)  # Pazartesi baslangicli hafta


def month_key(ms):
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return d.year * 12 + d.month


def higher_tf_bias(symbol):
    """{'4s': +1/-1/0, 'gun': ±1, 'hafta': ±1/0, 'ay': ±1/0}"""
    h4 = klines(symbol, "4h", 120)
    c4 = [k["c"] for k in h4]
    e4 = ema(c4, 50)
    if c4[-1] > e4[-1] and e4[-1] > e4[-4]:
        b4 = 1
    elif c4[-1] < e4[-1] and e4[-1] < e4[-4]:
        b4 = -1
    else:
        b4 = 0
    days = klines(symbol, "1d", 400)
    dc = [k["c"] for k in days]
    de = ema(dc, 20)
    b_day = 1 if dc[-1] > de[-1] else -1
    wk = period_closes(days, week_key)
    b_wk = (1 if wk[-1] > sum(wk[-10:]) / 10 else -1) if len(wk) >= 10 else 0
    mo = period_closes(days, month_key)
    b_mo = (1 if mo[-1] > sum(mo[-6:]) / 6 else -1) if len(mo) >= 6 else 0
    return {"4s": b4, "gun": b_day, "hafta": b_wk, "ay": b_mo}


def check_symbol(symbol):
    """Son kapanan 1s mumda kurulum varsa sinyal sozlugu, yoksa None."""
    h1 = klines(symbol, "1h", BREAKOUT_BARS + 5)
    if len(h1) < BREAKOUT_BARS + 1:
        return None
    last = h1[-1]
    prev = h1[-BREAKOUT_BARS - 1:-1]
    hh, ll = max(k["h"] for k in prev), min(k["l"] for k in prev)
    if not (last["c"] > hh or last["c"] < ll):
        return None
    bias = higher_tf_bias(symbol)
    direction = bias["4s"]
    if direction == 0 or (direction > 0 and last["c"] <= hh) or (direction < 0 and last["c"] >= ll):
        return None
    score = sum(1 for v in bias.values() if v == direction)
    entry = last["c"]
    dist = entry * STOP_PCT
    risk_usd = BALANCE * RISK_PCT
    qty = risk_usd / dist
    return {
        "symbol": symbol, "dir": direction, "entry": entry,
        "sl": entry - direction * dist, "tp1": entry + direction * dist, "tp2": entry + 2 * direction * dist,
        "qty": qty, "notional": qty * entry, "risk": risk_usd, "score": score, "bias": bias,
        "candle_close": last["T"],
    }


# ----------------------------- HABER -----------------------------
_news_cache = {"t": 0, "events": None}


def high_impact_events():
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
    """(engelle_mi, uyari_metni)"""
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
        return False, f"Yaklasan onemli veri: {cc} {title} - {dt.astimezone(TR).strftime('%d.%m %H:%M')} TR"
    return False, ""


# ----------------------------- TELEGRAM -----------------------------
def find_chat_id():
    try:
        res = http_get_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates").get("result", [])
        for upd in reversed(res):
            chat = (upd.get("message") or upd.get("my_chat_member") or {}).get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates hatasi: {e}", file=sys.stderr)
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


def fmt(x):
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 10:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


ARROW = {1: "⬆️", -1: "⬇️", 0: "➖"}


def signal_text(s):
    yon = "AL (LONG)" if s["dir"] > 0 else "SAT (SHORT)"
    b = s["bias"]
    tfs = f"Ay {ARROW[b['ay']]}  Hafta {ARROW[b['hafta']]}  Gun {ARROW[b['gun']]}  4s {ARROW[b['4s']]}"
    guc = {4: "GUCLU", 3: "IYI", 2: "ZAYIF"}.get(s["score"], "COK ZAYIF")
    t = datetime.fromtimestamp((s["candle_close"] + 1) / 1000, tz=TR).strftime("%H:%M")
    return (
        f"📊 {s['symbol']}USD — {yon}\n"
        f"Skor: {s['score']}/4 ({guc})   {tfs}\n"
        f"Giris: {fmt(s['entry'])} (1s kapanis {t} TR)\n"
        f"SL: {fmt(s['sl'])} (%{STOP_PCT*100:.0f})\n"
        f"+1R: {fmt(s['tp1'])} -> stopu girise cek, sonra %{TRAIL_PCT*100:.1f} takip\n"
        f"2R: {fmt(s['tp2'])}\n"
        f"Miktar: {s['qty']:.4f} {s['symbol']} (~{s['notional']:.0f}$)  Risk: {s['risk']:.0f}$  "
        f"Komisyon ~{s['notional']*FEE_PCT:.2f}$\n"
        + (f"📰 {s['news']}\n" if s.get("news") else "")
        + "⚠️ Fiyat Binance'e gore; MT5 farkini kaydir. Lot = miktar / MT5 kontrat buyuklugu."
    )


# ----------------------------- KAYIT -----------------------------
def load_signals():
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_signals(rows):
    cutoff = time.time() * 1000 - 30 * 86400_000
    with open(SIGNALS_FILE, "w") as f:
        json.dump([r for r in rows if r["t"] >= cutoff], f, indent=1)


def tr_day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TR).date().isoformat()


# ----------------------------- TARAMA -----------------------------
def run_once():
    rows = load_signals()
    now_ms = time.time() * 1000
    today = tr_day(now_ms)
    sent_today = sum(1 for r in rows if tr_day(r["t"]) == today)
    candidates = []
    for sym in SYMBOLS:
        try:
            s = check_symbol(sym)
        except Exception as e:  # noqa: BLE001
            print(f"{sym} hata: {e}", file=sys.stderr)
            continue
        if not s:
            print(f"{sym}: kurulum yok")
            continue
        if now_ms - s["candle_close"] > 3600_000:
            print(f"{sym}: mum eski, atlandi")
            continue
        if any(r["symbol"] == sym and r["t"] == s["candle_close"] for r in rows):
            print(f"{sym}: zaten gonderildi")
            continue
        if any(r["symbol"] == sym and s["candle_close"] - r["t"] < COOLDOWN_HOURS * 3600_000 for r in rows):
            print(f"{sym}: bekleme suresi")
            continue
        candidates.append(s)
    candidates.sort(key=lambda x: -x["score"])  # once en guclu skor
    block, news = news_check() if candidates else (False, "")
    for s in candidates:
        if sent_today >= MAX_SIGNALS_PER_DAY:
            print(f"{s['symbol']}: gunluk sinir doldu")
            continue
        if block:
            print(f"{s['symbol']}: haber nedeniyle atlandi ({news})")
            continue
        s["news"] = news
        send_telegram(signal_text(s))
        rows.append({"t": s["candle_close"], "symbol": s["symbol"], "dir": s["dir"], "entry": s["entry"],
                     "score": s["score"], "risk": s["risk"]})
        sent_today += 1
        print(f"{s['symbol']}: SINYAL gonderildi (skor {s['score']}/4)")
    save_signals(rows)


# ----------------------------- RAPOR -----------------------------
def evaluate(sig):
    """Stop %2; +1R'de stop girise; sonra %1.5 takip. Ayni mumda stop once sayilir. -> (etiket, R)"""
    bars = klines(sig["symbol"], "1h", 1000, start_ms=sig["t"])
    e, d = sig["entry"], sig["entry"] * STOP_PCT
    direction = sig["dir"]
    st, peak, be = e - direction * d, e, False
    for k in bars:
        if (direction > 0 and k["l"] <= st) or (direction < 0 and k["h"] >= st):
            ex = min(k["o"], st) if direction > 0 else max(k["o"], st)
            r = direction * (ex - e) / d
            if not be:
                return "SL", -1.0
            return ("basabas", r) if abs(r) < 0.05 else ("takip stop", r)
        peak = max(peak, k["h"]) if direction > 0 else min(peak, k["l"])
        if not be and (peak >= e + d if direction > 0 else peak <= e - d):
            be = True
        if be:
            ts = max(e, peak * (1 - TRAIL_PCT)) if direction > 0 else min(e, peak * (1 + TRAIL_PCT))
            st = max(st, ts) if direction > 0 else min(st, ts)
    last = bars[-1]["c"] if bars else e
    return "acik", direction * (last - e) / d


def daily_report():
    now_ms = time.time() * 1000
    rows = [r for r in load_signals() if r["t"] >= now_ms - 86400_000]
    if not rows:
        send_telegram("📋 Gunluk rapor: son 24 saatte sinyal gonderilmedi.")
        return
    lines, win, loss, be_n, open_n, total_r = [], 0, 0, 0, 0, 0.0
    by_score = {}
    for r in rows:
        try:
            label, rr = evaluate(r)
        except Exception as e:  # noqa: BLE001
            label, rr = f"veri yok ({e})", 0.0
        if label == "SL":
            loss += 1
        elif label == "basabas":
            be_n += 1
        elif label == "takip stop":
            win += 1
        else:
            open_n += 1
        total_r += rr
        sc = by_score.setdefault(r.get("score", 0), [0, 0.0])
        sc[0] += 1
        sc[1] += rr
        t = datetime.fromtimestamp(r["t"] / 1000, tz=TR).strftime("%H:%M")
        yon = "AL" if r["dir"] > 0 else "SAT"
        lines.append(f"{t} {r['symbol']} {yon} ({r.get('score', '?')}/4) -> {label} ({rr:+.1f}R)")
    closed = win + loss + be_n
    rate = f"%{100 * win / closed:.0f}" if closed else "-"
    risk = rows[0].get("risk", BALANCE * RISK_PCT)
    score_line = "  ".join(f"{k}/4: {v[0]} isl {v[1]:+.1f}R" for k, v in sorted(by_score.items(), reverse=True))
    text = (
        "📋 Gunluk rapor (son 24 saat)\n"
        f"Sinyal: {len(rows)} | Kar: {win} | Basabas: {be_n} | SL: {loss} | Acik: {open_n}\n"
        f"Kazanma orani (kapananlar): {rate}\n"
        f"Toplam: {total_r:+.1f}R (~{total_r * risk:+.0f}$, komisyon haric)\n"
        f"Skora gore: {score_line}\n\n"
        + "\n".join(lines)
        + "\n\nNot: Sinyale tam uyuldugu ve stopun kurala gore tasindigi varsayildi (Binance fiyati)."
    )
    send_telegram(text)


if __name__ == "__main__":
    if "--report" in sys.argv:
        daily_report()
    elif "--test" in sys.argv:
        send_telegram("✅ Test: bot Telegram'a ulasabiliyor.")
    else:
        run_once()
