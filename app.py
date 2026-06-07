import os
import time
import json
import threading
import datetime
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar

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
_refreshing = False
_last_refresh = 0.0
_refresh_started = 0.0


def _log(msg):
    print(f"[ROTATION] {msg}", flush=True)


def _build_session():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "text/html,application/json,*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    for u in ("https://fc.yahoo.com",):
        try:
            opener.open(u, timeout=8).read()
        except Exception:
            pass
    crumb = None
    try:
        r = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        crumb = r.read().decode("utf-8").strip()
    except Exception as e:
        _log(f"crumb request failed: {type(e).__name__}: {e}")
    if crumb and "<" not in crumb and len(crumb) < 40:
        _log("crumb acquired OK")
        return opener, crumb
    _log("no valid crumb; proceeding without")
    return opener, None


def _fetch_raw(ticker, opener, crumb):
    path = f"/v8/finance/chart/{ticker}?range=3mo&interval=1d"
    if crumb:
        path += "&crumb=" + urllib.parse.quote(crumb)
    last_err = None
    for host in YAHOO_HOSTS:
        url = host + path
        try:
            with opener.open(url, timeout=10) as resp:
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
            last_err = "no usable closes in response"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} from {host}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} ({host})"
    _log(f"{ticker} fetch failed -> {last_err}")
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
    _log("refresh cycle started")
    opener, crumb = _build_session()
    raws = {}
    ok_count = 0
    for ticker, name in SECTORS:
        raws[ticker] = _fetch_raw(ticker, opener, crumb)
        if raws[ticker]:
            ok_count += 1
    _log(f"fetched {ok_count}/{len(SECTORS)} tickers")

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
            _log("state updated -> LIVE")
        else:
            if _state["status"] != "live":
                _state["status"] = "error"
            _log("refresh incomplete -> data not fully available")


def _run_refresh():
    global _refreshing, _last_refresh
    try:
        _refresh_once()
    except Exception as e:
        _log(f"refresh error: {type(e).__name__}: {e}")
    finally:
        _last_refresh = time.monotonic()
        with _lock:
            _refreshing = False


def _maybe_refresh(force=False, blocking=False):
    global _refreshing, _refresh_started
    now = time.monotonic()
    with _lock:
        if _refreshing and (now - _refresh_started) > 90:
            _log("previous refresh looked stuck -> resetting flag")
            _refreshing = False
        if _refreshing:
            return
        stale = (_state["status"] != "live") or (now - _last_refresh > REFRESH_SECONDS)
        if not force and not stale:
            return
        _refreshing = True
        _refresh_started = now
    if blocking:
        _run_refresh()
    else:
        threading.Thread(target=_run_refresh, daemon=True).start()


def _background_loop():
    while True:
        _maybe_refresh(force=True)
        time.sleep(REFRESH_SECONDS)


def _start_background():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_background_loop, daemon=True).start()


@app.route("/")
def index():
    _maybe_refresh()
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    with _lock:
        have_data = _state["status"] == "live"
    _maybe_refresh(blocking=not have_data)
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
