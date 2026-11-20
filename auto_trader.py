"""
Binance USDⓈ-M Futures otomatik islem modulu (scalp_signal_bot.py tarafindan kullanilir).

Varsayilan olarak Binance DEMO TRADING (sahte para) ortamina baglanir:
    https://demo-fapi.binance.com
Gercek hesaba gecmek icin BINANCE_LIVE=1 ortam degiskeni gerekir (bilincli secim).

Kurallar (botun test edilmis duzeni):
  - Sadece 4/4 skorlu sinyal, ayni anda TEK pozisyon.
  - Pozisyon POSITION_USD (450 $) buyuklugunde, LEVERAGE kaldirac, ISOLATED marjin.
  - Giris piyasa emriyle. Hemen ardindan borsada:
      * STOP_MARKET (closePosition) -> girisin %2 otesi (zarar kes)
      * TRAILING_STOP_MARKET (reduceOnly) -> +%2'de devreye girer, %1.5 geri cekilmede kapatir
        (devreye girdiginde stop zaten girisin ustundedir -> basabas korumasi)
  - Gunluk gerceklesen sonuc (kar/zarar + komisyon + funding) -DAILY_STOP_USD ise yeni islem yok.
  - Kapanan pozisyonun artakalan emirleri iptal edilir, Telegram'a sonuc gonderilir.

API anahtari sadece islem (trade) yetkili olmali; para cekme yetkisi KAPALI olmali.
"""

import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
LIVE = os.environ.get("BINANCE_LIVE", "") == "1"
BASE = "https://fapi.binance.com" if LIVE else "https://demo-fapi.binance.com"

LEVERAGE = 5
CALLBACK_RATE = 1.5  # % takip mesafesi


class BinanceError(Exception):
    pass


def enabled():
    return bool(API_KEY and API_SECRET)


def _request(method, path, params=None, signed=True):
    params = dict(params or {})
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
    query = urllib.parse.urlencode(params)
    if signed:
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        query += f"&signature={sig}"
    url = f"{BASE}{path}?{query}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": API_KEY, "User-Agent": "signal-bot"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise BinanceError(f"{method} {path} -> HTTP {e.code}: {body[:300]}") from None


# --------------------------- yardimcilar ---------------------------
_filters = {}


def symbol_filters(symbol):
    """(stepSize, tickSize, minNotional)"""
    if not _filters:
        info = _request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info.get("symbols", []):
            f = {x["filterType"]: x for x in s.get("filters", [])}
            step = float(f.get("MARKET_LOT_SIZE", f.get("LOT_SIZE", {})).get("stepSize", "0.001"))
            tick = float(f.get("PRICE_FILTER", {}).get("tickSize", "0.01"))
            mn = float(f.get("MIN_NOTIONAL", {}).get("notional", "5"))
            _filters[s["symbol"]] = (step, tick, mn)
    if symbol not in _filters:
        raise BinanceError(f"{symbol} futures'ta yok")
    return _filters[symbol]


def _decimals(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def round_step(x, step):
    return round(math.floor(x / step + 1e-9) * step, _decimals(step))


def round_tick(x, tick):
    return round(round(x / tick) * tick, _decimals(tick))


def open_positions():
    try:
        data = _request("GET", "/fapi/v2/positionRisk")
    except BinanceError:
        data = _request("GET", "/fapi/v3/positionRisk")
    return {p["symbol"]: float(p["positionAmt"]) for p in data if abs(float(p.get("positionAmt", 0))) > 0}


def today_realized(start_ms):
    """Bugun (start_ms'den beri) gerceklesen kar/zarar + komisyon + funding (USDT)."""
    rows = _request("GET", "/fapi/v1/income", {"startTime": int(start_ms), "limit": 1000})
    keep = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
    return sum(float(r["income"]) for r in rows if r.get("incomeType") in keep)


def symbol_realized(symbol, start_ms):
    rows = _request("GET", "/fapi/v1/income", {"symbol": symbol, "startTime": int(start_ms), "limit": 1000})
    keep = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
    return sum(float(r["income"]) for r in rows if r.get("incomeType") in keep)


def cancel_all(symbol):
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
        try:
            _request("DELETE", path, {"symbol": symbol})
        except BinanceError as e:
            print(f"iptal uyarisi {symbol} {path}: {e}", file=sys.stderr)


# --------------------------- islem ---------------------------
def open_trade(symbol, direction, position_usd, stop_pct):
    """Piyasa emriyle pozisyon acar, borsada SL + takip stopu kurar. -> dict"""
    sym = symbol + "USDT"
    step, tick, min_notional = symbol_filters(sym)
    try:
        _request("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
    except BinanceError as e:
        if "-4046" not in str(e):  # zaten ISOLATED
            print(f"marginType uyarisi: {e}", file=sys.stderr)
    _request("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

    price = float(_request("GET", "/fapi/v1/ticker/price", {"symbol": sym}, signed=False)["price"])
    qty = round_step(position_usd / price, step)
    if qty * price < min_notional:
        raise BinanceError(f"{sym}: miktar cok kucuk ({qty} ~ {qty*price:.2f}$ < {min_notional}$)")

    side = "BUY" if direction > 0 else "SELL"
    close_side = "SELL" if direction > 0 else "BUY"
    order = _request("POST", "/fapi/v1/order", {"symbol": sym, "side": side, "type": "MARKET",
                                                 "quantity": qty, "newOrderRespType": "RESULT"})
    entry = float(order.get("avgPrice") or 0) or price
    filled = float(order.get("executedQty") or qty)

    sl = round_tick(entry * (1 - direction * stop_pct), tick)
    act = round_tick(entry * (1 + direction * stop_pct), tick)
    try:
        _request("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": sym, "side": close_side,
                                                "type": "STOP_MARKET", "triggerPrice": sl,
                                                "closePosition": "true", "workingType": "MARK_PRICE"})
    except BinanceError as e:
        # Stop konulamadiysa pozisyonu korumasiz birakma: hemen kapat
        _request("POST", "/fapi/v1/order", {"symbol": sym, "side": close_side, "type": "MARKET",
                                            "quantity": filled, "reduceOnly": "true"})
        raise BinanceError(f"{sym}: stop emri konulamadi, pozisyon kapatildi ({e})") from None
    trail_note = ""
    try:
        _request("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": sym, "side": close_side,
                                                "type": "TRAILING_STOP_MARKET", "quantity": filled,
                                                "reduceOnly": "true", "activatePrice": act,
                                                "callbackRate": CALLBACK_RATE})
    except BinanceError as e:
        trail_note = f"Takip stopu kurulamadi ({e}); stop yine de yerinde."
        print(trail_note, file=sys.stderr)
    return {"symbol": symbol, "dir": direction, "qty": filled, "entry": entry, "sl": sl, "act": act,
            "opened": int(time.time() * 1000), "trail_note": trail_note}
