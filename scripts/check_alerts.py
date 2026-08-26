#!/usr/bin/env python3
"""
Always-on stock alert check, meant to run on a GitHub Actions schedule.

Unlike the web page, this runs server-side (no browser CORS restrictions), so it hits Yahoo
Finance's screeners directly with no proxy needed. It uses the same criteria as the page:
price under a cap, a big enough % move today, volume well above its 10-day average, an absolute
minimum volume (filters out illiquid stocks), and optionally price above its 30-day average.

State (which symbols are "already alerted") is persisted in data/alert_state.json and committed
back to the repo each run, so restarts/redeploys don't cause duplicate notifications — mirrors
what the page does with localStorage, just using a committed file instead since each Actions run
is a fresh, stateless machine.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

PRICE_CAP = float(os.environ.get("PRICE_CAP", "15"))
CHANGE_THRESHOLD = float(os.environ.get("CHANGE_THRESHOLD", "5"))
VOL_RATIO_THRESHOLD = float(os.environ.get("VOL_RATIO_THRESHOLD", "1.5"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "100000"))
REQUIRE_MA = os.environ.get("REQUIRE_MA", "true").strip().lower() == "true"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

SCREEN_IDS = ["day_gainers", "small_cap_gainers", "most_actives", "aggressive_small_caps"]
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "alert_state.json")


def fetch_json(url, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 - want to retry on anything transient
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
    raise last_err


def fetch_screener(scr_id):
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&lang=en-US&region=US&count=100&scrIds={scr_id}"
    )
    try:
        data = fetch_json(url)
        return data["finance"]["result"][0]["quotes"]
    except Exception as e:  # noqa: BLE001
        print(f"  screener '{scr_id}' failed: {e}")
        return []


def fetch_30d_avg(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2mo&interval=1d"
    try:
        data = fetch_json(url)
        closes = [c for c in data["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 5:
            return None
        last30 = closes[-30:]
        return sum(last30) / len(last30)
    except Exception as e:  # noqa: BLE001
        print(f"  chart fetch for {symbol} failed: {e}")
        return None


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("alerted"), dict):
                return data
        except Exception as e:  # noqa: BLE001
            print(f"  couldn't read existing state, starting fresh: {e}")
    return {"alerted": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def send_ntfy(title, body):
    if not NTFY_TOPIC:
        print("  NTFY_TOPIC not set — skipping push (add it as a repo secret to enable).")
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", "bell,chart_with_upwards_trend")
    req.add_header("Priority", "high")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  ntfy push sent (status {resp.status})")
    except urllib.error.URLError as e:
        print(f"  ntfy push failed: {e}")


def main():
    print(f"Run started {datetime.now(timezone.utc).isoformat()}")
    print(f"Criteria: price<=${PRICE_CAP}, change>={CHANGE_THRESHOLD}%, "
          f"volRatio>={VOL_RATIO_THRESHOLD}x, minVolume>={int(MIN_VOLUME)}, requireMA={REQUIRE_MA}")

    quotes = {}
    for scr_id in SCREEN_IDS:
        for q in fetch_screener(scr_id):
            sym = q.get("symbol")
            if sym:
                quotes[sym] = q
    print(f"Fetched {len(quotes)} unique quotes across {len(SCREEN_IDS)} screeners.")

    if not quotes:
        print("No data from any screener this run (Yahoo may be rate-limiting/blocking this "
              "runner right now) — skipping, will retry next scheduled run.")
        return

    state = load_state()

    # coarse pre-filter before spending extra requests on 30-day averages
    candidates = []
    for sym, q in quotes.items():
        price = q.get("regularMarketPrice")
        chg = q.get("regularMarketChangePercent")
        vol = q.get("regularMarketVolume")
        avg_vol = q.get("averageDailyVolume10Day") or q.get("averageDailyVolume3Month")
        if price is None or chg is None or vol is None or not avg_vol:
            continue
        vol_ratio = vol / avg_vol
        if price <= PRICE_CAP and chg > 0 and vol >= MIN_VOLUME and (
            chg >= CHANGE_THRESHOLD * 0.5 or vol_ratio >= VOL_RATIO_THRESHOLD * 0.6
        ):
            candidates.append(sym)
    print(f"{len(candidates)} candidates need a 30-day average check: {', '.join(candidates) or '(none)'}")

    ma30 = {}
    for sym in candidates:
        avg = fetch_30d_avg(sym)
        if avg is not None:
            ma30[sym] = avg
        time.sleep(0.3)  # be a polite, low-rate client

    matched = []
    for sym in candidates:
        q = quotes[sym]
        price = q["regularMarketPrice"]
        chg = q["regularMarketChangePercent"]
        vol = q["regularMarketVolume"]
        avg_vol = q.get("averageDailyVolume10Day") or q.get("averageDailyVolume3Month")
        vol_ratio = vol / avg_vol
        above_ma = True
        if REQUIRE_MA and sym in ma30:
            above_ma = price > ma30[sym]
        if (price <= PRICE_CAP and chg >= CHANGE_THRESHOLD and vol_ratio >= VOL_RATIO_THRESHOLD
                and vol >= MIN_VOLUME and above_ma):
            matched.append({
                "symbol": sym,
                "name": q.get("shortName") or q.get("longName") or sym,
                "price": price, "chg": chg, "volRatio": vol_ratio,
            })

    matched_symbols = {m["symbol"] for m in matched}
    already = set(state["alerted"].keys())
    new_matches = [m for m in matched if m["symbol"] not in already]
    dropped = already - matched_symbols

    for sym in dropped:
        del state["alerted"][sym]

    now_iso = datetime.now(timezone.utc).isoformat()
    for m in new_matches:
        state["alerted"][m["symbol"]] = {"time": now_iso, "price": m["price"], "name": m["name"]}

    if new_matches:
        lines = [f"{m['symbol']} ${m['price']:.2f}  +{m['chg']:.1f}%  (vol {m['volRatio']:.1f}x avg)"
                 for m in new_matches]
        title = f"{len(new_matches)} new bullish mover{'s' if len(new_matches) != 1 else ''} under ${PRICE_CAP:.0f}"
        body = "\n".join(lines)
        print(f"NEW MATCHES: {', '.join(m['symbol'] for m in new_matches)}")
        send_ntfy(title, body)
    else:
        print("No new matches this run.")

    if dropped:
        print(f"No longer matching (cleared from state): {', '.join(sorted(dropped))}")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
