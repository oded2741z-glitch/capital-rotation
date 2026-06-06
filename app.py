import os
import time
import json
import threading
import datetime
import urllib.request

from flask import Flask, render_template, jsonify

SECTORS = [
    ("XLK", "Technology"),
    ("XLV", "Health Care"),
    ("XLY", "Consumer Disc."),
    ("XLB", "Materials"),
    ("XLC", "Comm. Services"),
    ("XLI", "Industrials"),
    ("XLRE", "Real Estate"),
    ("XLF", "Financials"),
    ("XLP", "Consumer Staples"),
    ("XLU", "Utilities"),
    ("XLE", "Energy"),
]

PERIOD_DAYS = {"1D": 1, "1W": 7, "1M": 30}

REFRESH_SECONDS = 15 * 60

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "status": "loading",
    "updated": None,
    "periods": {"1D": [], "1W": [], "1M": []},
}
_started = False


def _fetch_raw(ticker):
    path = f"/v8/finance/chart/{ticker}?range=3mo&interval=1d"
    for host in YAHOO_HOSTS:
        url = host + path
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            result = payload["chart"]["result"][0]
            timestamps = result["timestamp"]
            indicators = result["indicators"]
            values = indicators["quote"][0].get("close", [])
            if "adjclose" in indicators:
                adj = indicators["adjclose"][0].get("adjclose")
                if adj:
                    values = adj
            closes = []
            for ts, c in zip(timestamps, values):
                if c is None:
                    continue
                d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
                closes.append((d, float(c)))
            if len(closes) >= 2:
                return closes
        except Exception:
            continue
    return None


def _pct_from_closes(closes, days):
    if not closes or len(closes) < 2:
        return None
    closes = sorted(closes, key=lambda x: x[0])
    end_date, end_price = closes[-1]
    if days <= 1:
        start_price = closes[-2][1]
    else:
        target = end_date - datetime.timedelta(days=days)
        start_price = closes[0][1]
        for d, c in closes:
            if d <= target:
                start_price = c
            else:
                break
    if start_price == 0:
        return None
    return ((end_price - start_price) / start_price) * 100


def _refresh_once():
    raws = {}
    for ticker, name in SECTORS:
        raws[ticker] = _fetch_raw(ticker)

    new_periods = {"1D": [], "1W": [], "1M": []}
    ok = True
    for period, days in PERIOD_DAYS.items():
        rows = []
        for ticker, name in SECTORS:
            pct = _pct_from_closes(raws.get(ticker), days)
            if pct is None:
                ok = False
                break
            rows.append({"ticker": ticker, "name": name, "pct": round(pct, 2)})
        if not ok:
            break
        new_periods[period] = rows

    with _lock:
        if ok:
            _state["status"] = "live"
            _state["periods"] = new_periods
            _state["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            if _state["status"] != "live":
                _state["status"] = "error"


def _background_loop():
    while True:
        try:
            _refresh_once()
        except Exception:
            pass
        time.sleep(REFRESH_SECONDS)


def _start_background():
    global _started
    if _started:
        return
    _started = True
    thread = threading.Thread(target=_background_loop, daemon=True)
    thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({
            "status": _state["status"],
            "updated": _state["updated"],
            "periods": _state["periods"],
        })


_start_background()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)