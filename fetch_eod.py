"""
fetch_eod.py - daily end-of-day (EOD) prices for your watchlist.

Runs every weekday evening on GitHub's computers (see .github/workflows/eod.yml).
Order of sources, per stock:
  1. NSE full daily file (has delivery %)
  2. NSE daily file (new format)
  3. BSE daily file
  4. Yahoo Finance (last resort)
Indices (NIFTY 50, NIFTY BANK): NSE index file, then Yahoo.

What it writes (all inside this repository):
  README.md               - the page you read on your phone
  watchlist.txt           - your stock list; edit it on GitHub to change stocks
  data/eod_history.csv    - one row per stock per day, building up over time
  data/status.json        - health report of every source (used to diagnose problems)
"""

import csv
import datetime as dt
import io
import json
import os
import sys
import time
import traceback
import zipfile

import requests

DEFAULT_WATCHLIST = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ICICIBANK",
                     "SBIN", "ITC", "LT", "NIFTY 50", "NIFTY BANK"]
INDICES = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK"}   # name -> Yahoo code
WATCHLIST_FILE = "watchlist.txt"
HISTORY_FILE = "data/eod_history.csv"
STATUS_FILE = "data/status.json"
FIELDS = ["date", "symbol", "open", "high", "low", "close", "prev_close",
          "change_pct", "volume", "delivery_pct", "source"]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(IST)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
SOURCES = []          # health report rows


# ------------------------------------------------------------------ helpers

def report(source, ok, date=None, detail=""):
    SOURCES.append({"source": source, "ok": ok, "date": str(date) if date else None,
                    "detail": detail})
    print(f"  {'OK  ' if ok else 'FAIL'} {source}: {date or '-'} {detail}")


def num(x):
    try:
        x = str(x).replace(",", "").strip()
        return float(x) if x not in ("", "-", "nan", "None") else None
    except Exception:
        return None


def recent_weekdays(n=7):
    """Newest first. Today counts only after 18:00 IST (files appear in the evening)."""
    day = NOW.date() if NOW.hour >= 18 else NOW.date() - dt.timedelta(days=1)
    out = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= dt.timedelta(days=1)
    return out


def get(session, url, timeout=25):
    """(content, problem) - exactly one is None."""
    for attempt in range(2):
        try:
            r = session.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            problem = "timed out (often means blocked)"
        except requests.exceptions.ConnectionError as e:
            problem = f"could not connect ({type(e).__name__})"
        except Exception as e:
            problem = f"error: {e}"
        else:
            if r.status_code == 200 and r.content:
                return r.content, None
            if r.status_code == 404:
                return None, "404"
            problem = f"HTTP {r.status_code}" + (" (refused)" if r.status_code in (401, 403) else "")
        time.sleep(3)
    return None, problem


def read_csv(content):
    if content[:2] == b"PK":                      # zipped
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            content = z.read(z.namelist()[0])
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if r]


def latest_file(session, name, url_for):
    """Try recent weekdays until a file downloads. Returns (date, rows) or (None, None)."""
    misses = 0
    for day in recent_weekdays():
        content, problem = get(session, url_for(day))
        if content is not None:
            try:
                return day, read_csv(content)
            except Exception as e:
                report(name, False, day, f"downloaded but unreadable: {e}")
                return None, None
        if problem != "404":
            report(name, False, None, problem)
            return None, None
        misses += 1
        time.sleep(0.5)
    report(name, False, None, f"404 on all {misses} recent weekdays - the file address may have changed")
    return None, None


# ------------------------------------------------------------------ sources

STOCK_SERIES = {"EQ", "BE", "A", "B", "T", "X", "XT", "M", "MT", "Z", ""}


def nse_full(session, stocks):
    day, rows = latest_file(session, "NSE full file (delivery %)", lambda d:
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
    if not rows:
        return {}
    out = {}
    for r in rows:
        s = r.get("SYMBOL", "").upper()
        if s in stocks and r.get("SERIES", "") in STOCK_SERIES and s not in out:
            out[s] = {"date": day, "open": num(r.get("OPEN_PRICE")), "high": num(r.get("HIGH_PRICE")),
                      "low": num(r.get("LOW_PRICE")), "close": num(r.get("CLOSE_PRICE")),
                      "prev_close": num(r.get("PREV_CLOSE")), "volume": num(r.get("TTL_TRD_QNTY")),
                      "delivery_pct": num(r.get("DELIV_PER"))}
    report("NSE full file (delivery %)", bool(out), day, f"{len(out)} of {len(stocks)} stocks")
    return out


def udiff(session, name, url_for, stocks):
    """NSE and BSE both publish this same 'new format' layout."""
    day, rows = latest_file(session, name, url_for)
    if not rows:
        return {}
    out = {}
    for r in rows:
        s = r.get("TckrSymb", "").upper()
        if s in stocks and r.get("SctySrs", "") in STOCK_SERIES and s not in out:
            out[s] = {"date": day, "open": num(r.get("OpnPric")), "high": num(r.get("HghPric")),
                      "low": num(r.get("LwPric")), "close": num(r.get("ClsPric")),
                      "prev_close": num(r.get("PrvsClsgPric")), "volume": num(r.get("TtlTradgVol")),
                      "delivery_pct": None}
    report(name, bool(out), day, f"{len(out)} of {len(stocks)} stocks")
    return out


def nse_indices(session, indices):
    day, rows = latest_file(session, "NSE index file", lambda d:
        f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv")
    if not rows:
        return {}
    out = {}
    for r in rows:
        n = r.get("Index Name", "").upper()
        if n in indices:
            close = num(r.get("Closing Index Value"))
            chg = num(r.get("Change(%)"))
            prev = round(close / (1 + chg / 100), 2) if close and chg is not None else None
            out[n] = {"date": day, "open": num(r.get("Open Index Value")),
                      "high": num(r.get("High Index Value")), "low": num(r.get("Low Index Value")),
                      "close": close, "prev_close": prev, "volume": None, "delivery_pct": None}
    report("NSE index file", bool(out), day, f"{len(out)} of {len(indices)} indices")
    return out


def yahoo(symbols):
    """symbols: {our name: yahoo code}. Last resort only."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        raw = yf.download(list(symbols.values()), period="10d", interval="1d", progress=False,
                          auto_adjust=False, group_by="ticker", threads=False)
    except Exception as e:
        report("Yahoo Finance", False, None, f"error: {e}")
        return {}
    out = {}
    for name, code in symbols.items():
        try:
            d = raw[code].dropna(subset=["Close"])
        except Exception:
            continue
        if len(d):
            last = d.iloc[-1]
            prev = float(d.iloc[-2]["Close"]) if len(d) > 1 else None
            out[name] = {"date": d.index[-1].date(), "open": float(last["Open"]),
                         "high": float(last["High"]), "low": float(last["Low"]),
                         "close": float(last["Close"]), "prev_close": prev,
                         "volume": float(last["Volume"]) if last["Volume"] == last["Volume"] else None,
                         "delivery_pct": None}
    dates = sorted({str(v["date"]) for v in out.values()})
    report("Yahoo Finance", bool(out), dates[-1] if dates else None,
           f"{len(out)} of {len(symbols)} (backup source)")
    return out


# ------------------------------------------------------------------ main

def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "w") as f:
            f.write("# One NSE symbol per line. Indices allowed: NIFTY 50, NIFTY BANK\n")
            f.write("\n".join(DEFAULT_WATCHLIST) + "\n")
    items = []
    for line in open(WATCHLIST_FILE):
        s = line.split("#")[0].strip().upper()
        if s and s not in items:
            items.append(s)
    return items or DEFAULT_WATCHLIST


def pick(symbol, sources):
    """Newest date wins; ties go to the higher-priority source."""
    best = None
    for name, data in sources:
        row = data.get(symbol)
        if row and row.get("close"):
            if best is None or row["date"] > best[1]["date"]:
                best = (name, row)
    return best


def save_history(rows):
    os.makedirs("data", exist_ok=True)
    old = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            old = list(csv.DictReader(f))
    keep = {(r["date"], r["symbol"]): r for r in old}
    for r in rows:
        keep[(r["date"], r["symbol"])] = r
    merged = sorted(keep.values(), key=lambda r: (r["date"], r["symbol"]))
    with open(HISTORY_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(merged)


def fmt(x, nd=2):
    return "" if x in (None, "") else f"{float(x):,.{nd}f}"


def write_readme(rows, missing):
    newest = max((r["date"] for r in rows), default="-")
    lines = ["# NSE end-of-day watchlist", "",
             f"Updated {NOW:%d %b %Y, %H:%M} IST · latest trading day **{newest}**", "",
             "| Stock | Close | Chg % | Deliv % | Date | Source |",
             "|---|---:|---:|---:|---|---|"]
    for r in rows:
        stale = " ⚠" if r["date"] != newest else ""
        lines.append(f"| {r['symbol']} | {fmt(r['close'])} | {fmt(r['change_pct'])} | "
                     f"{fmt(r['delivery_pct'], 1)} | {r['date']}{stale} | {r['source']} |")
    for s in missing:
        lines.append(f"| {s} | no data | | | | all sources failed |")
    lines += ["", "⚠ = older than the latest day (that source had not published yet)", "",
              "### Source health", "", "| Source | Result | Date | Detail |", "|---|---|---|---|"]
    for s in SOURCES:
        lines.append(f"| {s['source']} | {'✅' if s['ok'] else '❌'} | {s['date'] or '-'} | {s['detail']} |")
    lines += ["", "To change stocks, edit `watchlist.txt` in this repository."]
    with open("README.md", "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    items = load_watchlist()
    indices = {s: INDICES[s] for s in items if s in INDICES}
    stocks = [s for s in items if s not in INDICES]
    print(f"Run at {NOW:%Y-%m-%d %H:%M} IST for {len(items)} symbols")

    nse = requests.Session()
    nse.headers.update(HEADERS)
    nse.headers["Referer"] = "https://www.nseindia.com/"
    bse = requests.Session()
    bse.headers.update(HEADERS)
    bse.headers["Referer"] = "https://www.bseindia.com/"

    tried = []
    steps = [
        ("NSE full", lambda: nse_full(nse, stocks)),
        ("NSE new", lambda: udiff(nse, "NSE daily file (new format)", lambda d:
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
            stocks)),
        ("BSE", lambda: udiff(bse, "BSE daily file", lambda d:
            f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV",
            stocks)),
        ("NSE index", lambda: nse_indices(nse, indices) if indices else {}),
    ]
    for name, fn in steps:
        try:
            tried.append((name, fn()))
        except Exception as e:
            report(name, False, None, f"crashed: {e}")
            tried.append((name, {}))

    # Yahoo only for symbols the exchanges could not supply for the newest day
    exchange_dates = [r["date"] for _, d in tried for r in d.values()]
    newest = max(exchange_dates) if exchange_dates else None
    need = {}
    for s in items:
        got = pick(s, tried)
        if got is None or (newest and got[1]["date"] < newest):
            need[s] = INDICES.get(s, s + ".NS")
    if need:
        try:
            tried.append(("Yahoo", yahoo(need)))
        except Exception as e:
            report("Yahoo Finance", False, None, f"crashed: {e}")

    rows, missing = [], []
    for s in items:
        got = pick(s, tried)
        if got is None:
            missing.append(s)
            continue
        src, r = got
        chg = round((r["close"] - r["prev_close"]) / r["prev_close"] * 100, 2) \
            if r.get("prev_close") else None
        rows.append({"date": str(r["date"]), "symbol": s, "open": r.get("open"),
                     "high": r.get("high"), "low": r.get("low"), "close": r["close"],
                     "prev_close": r.get("prev_close"), "change_pct": chg,
                     "volume": r.get("volume"), "delivery_pct": r.get("delivery_pct"),
                     "source": src})

    save_history(rows)
    write_readme(rows, missing)
    status = {"run_at_ist": NOW.isoformat(timespec="minutes"), "symbols": items,
              "got": len(rows), "missing": missing, "sources": SOURCES}
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=1)

    print(f"Done: {len(rows)} of {len(items)} symbols. Missing: {missing or 'none'}")
    if not rows:
        print("No data from any source - marking this run as failed.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        os.makedirs("data", exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump({"run_at_ist": NOW.isoformat(timespec="minutes"), "crash": traceback.format_exc(),
                       "sources": SOURCES}, f, indent=1)
        traceback.print_exc()
        sys.exit(1)
