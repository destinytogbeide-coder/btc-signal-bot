import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import websocket

WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
GRANULARITY = 60
NEWS_LOOKBACK_MIN = 120
NEWS_BLOCK_MIN = 90

# News is a FILTER only. This bot never places trades.
HIGH_IMPACT = [
    "cpi", "consumer price index", "pce", "personal consumption",
    "nonfarm", "non-farm", "payroll", "fomc", "federal reserve",
    "fed decision", "interest rate", "rate decision", "powell",
    "unemployment", "jobs report", "retail sales", "gdp",
    "producer price", "ppi", "ism", "jolts", "core inflation"
]
GOLD_KEYWORDS = [
    "gold", "xau", "bullion", "precious metals", "safe haven",
    "dollar", "usd", "treasury", "yield", "fed", "inflation"
]

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for value in values[period:]:
        e = value * k + e * (1 - k)
    return e

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

def find_xau_symbol(ws):
    ws.send(json.dumps({"active_symbols": "full", "req_id": 1}))
    while True:
        data = json.loads(ws.recv())
        if data.get("error"):
            raise RuntimeError(data["error"])
        if data.get("msg_type") == "active_symbols":
            matches = []
            for s in data.get("active_symbols", []):
                text = " ".join(str(s.get(k, "")) for k in (
                    "underlying_symbol", "underlying_symbol_name",
                    "display_name", "symbol"
                )).lower()
                if "btcusd" in text or ("gold" in text and "usd" in text):
                    matches.append(s)
            if not matches:
                raise RuntimeError(
                    "Could not find BTCUSD/Gold in Deriv active_symbols. "
                    "Run active_symbols manually to verify whether BTCUSD is available on this endpoint."
                )
            s = matches[0]
            return s.get("underlying_symbol") or s.get("symbol"), s

def get_candles(ws, symbol):
    ws.send(json.dumps({
        "ticks_history": symbol,
        "end": "latest",
        "count": 120,
        "style": "candles",
        "granularity": GRANULARITY,
        "req_id": 2
    }))
    while True:
        data = json.loads(ws.recv())
        if data.get("error"):
            raise RuntimeError(data["error"])
        if data.get("msg_type") == "candles":
            return [
                {
                    "epoch": int(c["epoch"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"])
                }
                for c in data.get("candles", [])
            ]

def fetch_news():
    # Google News RSS is used as a public headline source; no API key required.
    query = urllib.parse.quote("(BTCUSD OR gold) (Fed OR CPI OR PCE OR NFP OR inflation OR USD OR yields)")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        items = re.findall(r"<item>(.*?)</item>", raw, flags=re.S | re.I)
        now = time.time()
        found = []
        for item in items[:30]:
            title_m = re.search(r"<title>(.*?)</title>", item, flags=re.S | re.I)
            date_m = re.search(r"<pubDate>(.*?)</pubDate>", item, flags=re.S | re.I)
            if not title_m:
                continue
            title = re.sub(r"<.*?>", "", title_m.group(1)).strip()
            published = 0
            if date_m:
                try:
                    published = datetime.strptime(
                        date_m.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    published = 0
            age_min = (now - published) / 60 if published else 999999
            low = title.lower()
            impact = any(k in low for k in HIGH_IMPACT)
            gold = any(k in low for k in GOLD_KEYWORDS)
            if gold and age_min <= NEWS_LOOKBACK_MIN:
                found.append({
                    "title": title,
                    "age_minutes": round(age_min, 1),
                    "high_impact": impact
                })
        return found
    except Exception as e:
        return [{"error": f"News feed unavailable: {e}"}]

def analyze(candles, news):
    if len(candles) < SLOW_EMA + 2:
        return {
            "signal": "WAIT",
            "reason": "Insufficient M1 candle history for EMA9/EMA21."
        }

    closes = [c["close"] for c in candles]
    previous = closes[:-1]
    ema9_prev = ema(previous, FAST_EMA)
    ema21_prev = ema(previous, SLOW_EMA)
    ema9_now = ema(closes, FAST_EMA)
    ema21_now = ema(closes, SLOW_EMA)
    r = rsi(closes, RSI_PERIOD)

    technical = "WAIT"
    if ema9_prev <= ema21_prev and ema9_now > ema21_now and r > 50:
        technical = "BUY"
    elif ema9_prev >= ema21_prev and ema9_now < ema21_now and r < 50:
        technical = "SELL"

    news_error = any("error" in n for n in news)
    high_news = [n for n in news if n.get("high_impact")]
    news_status = "UNAVAILABLE" if news_error else ("HIGH_IMPACT_NEARBY" if high_news else "CLEAR")

    final_signal = "WAIT" if news_status != "CLEAR" else technical
    if news_status == "HIGH_IMPACT_NEARBY":
        reason = "Technical setup filtered because recent high-impact gold/USD news was detected."
    elif news_status == "UNAVAILABLE":
        reason = "Technical setup withheld because the news feed is unavailable."
    elif technical == "WAIT":
        reason = "No confirmed EMA9/EMA21 crossover with RSI confirmation."
    else:
        reason = "Technical setup confirmed and no recent high-impact news conflict detected."

    return {
        "instrument": "BTCUSD",
        "timeframe": "M1",
        "signal": final_signal,
        "technical_signal": technical,
        "entry": closes[-1],
        "ema9": round(ema9_now, 5),
        "ema21": round(ema21_now, 5),
        "rsi14": round(r, 2),
        "news_status": news_status,
        "news_items": news[:5],
        "risk_reward": "1:2",
        "risk": "$1 target risk (position sizing must be calculated from the actual BTCUSD contract)",
        "reward": "$2 target reward",
        "execution": "MANUAL_ONLY",
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

def main():
    while True:
        try:
            print("Connecting to Deriv public market-data WebSocket...")
            ws = websocket.create_connection(WS_URL, timeout=30)
            print("CONNECTED TO DERIV")

            symbol, meta = find_xau_symbol(ws)
            print("BTCUSD symbol:", symbol, "|", meta.get("underlying_symbol_name", ""))

            candles = get_candles(ws, symbol)
            news = fetch_news()
            result = analyze(candles, news)

            print(json.dumps(result, indent=2))
            with open("current_signal.json", "w") as f:
                json.dump(result, f, indent=2)

            # Subscribe to ticks and update one-minute candles.
            ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            current = None

            while True:
                data = json.loads(ws.recv())
                if data.get("error"):
                    raise RuntimeError(data["error"])
                if data.get("msg_type") != "tick":
                    continue

                epoch = int(data["tick"]["epoch"])
                price = float(data["tick"]["quote"])
                minute = epoch // 60 * 60

                if current is None:
                    current = {"epoch": minute, "open": price, "high": price, "low": price, "close": price}
                elif minute == current["epoch"]:
                    current["high"] = max(current["high"], price)
                    current["low"] = min(current["low"], price)
                    current["close"] = price
                else:
                    candles.append(current)
                    candles = candles[-120:]
                    current = {"epoch": minute, "open": price, "high": price, "low": price, "close": price}

                    news = fetch_news()
                    result = analyze(candles, news)
                    print(json.dumps(result, indent=2))
                    with open("current_signal.json", "w") as f:
                        json.dump(result, f, indent=2)

        except KeyboardInterrupt:
            print("\nStopped. No trades were placed.")
            break
        except Exception as e:
            print("ERROR:", e)
            print("Retrying in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    main()
