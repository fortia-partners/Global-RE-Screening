#!/usr/bin/env python3
"""
build_detail.py — per-company detail files for the drawer.

    python build_detail.py                  # every name in the snapshot
    python build_detail.py --mock            # offline synthetic
    python build_detail.py --only PLD,SGRO.L

Writes data/co/{TICKER}.json, one per company, so the main snapshot stays small
and the browser fetches detail only when a row is opened.

WHAT IS AND IS NOT IN HERE
--------------------------
Prices and volume: 10 years, weekly, plus 2 years daily. Real.

Financials: whatever Yahoo carries, which is about 4 annual and 5 quarterly
periods — not 10 years. No free source has 10 years of statements. The quality
block reports exactly how many periods came back, so a short history is visible
rather than silent.

Point-in-time multiples: TRAILING only. Price on each date over the earnings
that had actually been *reported* by that date, using the filing date rather than
the period end. A historical FORWARD multiple needs the consensus estimate as it
stood on each past date, which is a paid point-in-time estimates database. Using
today's forward estimate against past prices produces look-ahead bias — it looks
convincing and it is worthless, so it is not computed here at all.

Non-recurring adjustment: earnings are normalised by stripping unusual items and,
for fair-value reporters, property revaluation gains and losses. For an IFRS REIT
this is the difference between a meaningful multiple and noise, since revaluations
routinely swamp operating profit.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CO = DATA / "co"
CACHE = ROOT / ".cache"
CO.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(exist_ok=True)

SLEEP = 0.30
PRICE_YEARS = 10

# Income-statement lines, in preference order. Yahoo's normalised labels drift
# between tickers, so each concept lists several candidates.
IS_ROWS = {
    "revenue":    ["Total Revenue", "Operating Revenue"],
    "op_income":  ["Operating Income", "Total Operating Income As Reported"],
    "ebit":       ["EBIT"],
    "ebitda":     ["EBITDA", "Normalized EBITDA"],
    "interest":   ["Interest Expense", "Interest Expense Non Operating"],
    "pretax":     ["Pretax Income"],
    "tax":        ["Tax Provision"],
    "net_income": ["Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest"],
    "net_cont":   ["Net Income From Continuing Operation Net Minority Interest",
                   "Net Income Continuous Operations"],
    "unusual":    ["Total Unusual Items", "Total Unusual Items Excluding Goodwill"],
    "eps":        ["Diluted EPS", "Basic EPS"],
    "shares":     ["Diluted Average Shares", "Basic Average Shares"],
}
BS_ROWS = {
    "assets":     ["Total Assets"],
    "prop":       ["Investment Properties", "Net PPE", "Gross PPE"],
    "debt":       ["Total Debt"],
    "cash":       ["Cash Cash Equivalents And Short Term Investments",
                   "Cash And Cash Equivalents"],
    "equity":     ["Stockholders Equity", "Total Equity Gross Minority Interest"],
    "pref":       ["Preferred Stock"],
    "shares_out": ["Ordinary Shares Number", "Share Issued"],
}
CF_ROWS = {
    "ocf":        ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex":      ["Capital Expenditure"],
    "fcf":        ["Free Cash Flow"],
    "divs_paid":  ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    "acq":        ["Net Business Purchase And Sale", "Purchase Of Business"],
    "disposals":  ["Net PPE Purchase And Sale", "Sale Of PPE"],
}


def pick(df, names):
    """Fetch a statement line by label, tolerating Yahoo's label drift."""
    if df is None or getattr(df, "empty", True):
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for want in names:
        w = want.strip().lower()
        if w in idx:
            return df.loc[idx[w]]
    for want in names:                                  # loosen to substring
        w = want.strip().lower()
        for lab, orig in idx.items():
            if w in lab:
                return df.loc[orig]
    return None


def frame_to_periods(df, rowmap) -> list[dict]:
    """Turn a statement DataFrame into a list of periods, newest first."""
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for col in df.columns:
        per = {"end": pd.Timestamp(col).strftime("%Y-%m-%d")}
        for key, names in rowmap.items():
            s = pick(df, names)
            v = None
            if s is not None and col in s.index:
                x = s[col]
                if pd.notna(x):
                    v = float(x)
            per[key] = v
        out.append(per)
    return out


def normalise_earnings(periods: list[dict], fair_value: bool) -> list[dict]:
    """
    Strip non-recurring items from each period's earnings.

    Two adjustments:
      1. Unusual items, where Yahoo tags them.
      2. For fair-value reporters, the gap between net income and operating
         income net of interest and tax — which for a REIT is dominated by
         property revaluation. Under IFRS this line moves earnings by multiples
         of operating profit and reverses sign year to year, so an unadjusted
         P/E on an IFRS REIT is close to meaningless.
    """
    for p in periods:
        ni = p.get("net_income")
        adj, basis = ni, "reported"
        if ni is not None:
            un = p.get("unusual") or 0.0
            if un:
                adj, basis = ni - un, "ex-unusual"
            if fair_value:
                op, itr, tax = p.get("op_income"), p.get("interest"), p.get("tax")
                if op is not None:
                    core = op - (itr or 0.0) - (tax or 0.0)
                    # only treat it as revaluation when the gap is large; small
                    # gaps are ordinary consolidation noise
                    if abs(ni - core) > max(abs(core) * 0.30, 1.0):
                        adj, basis = core, "ex-revaluation"
        p["adj_income"] = adj
        p["adj_basis"] = basis
        sh = p.get("shares")
        p["adj_eps"] = (adj / sh) if (adj is not None and sh) else None
        p["rep_eps"] = p.get("eps")
    return periods


def pit_trailing_eps(quarters: list[dict], lag_days: int = 55) -> pd.Series:
    """
    Trailing-twelve-month adjusted EPS, stepped on the date the market plausibly
    knew it.

    Yahoo gives period-end dates, not filing dates. Results land weeks after the
    period closes, so each figure is dated forward by `lag_days`. Skipping this
    step would date earnings to the period end and quietly reintroduce the
    look-ahead bias the trailing construction exists to avoid.
    """
    rows = [q for q in quarters if q.get("adj_eps") is not None]
    if len(rows) < 4:
        return pd.Series(dtype=float)
    rows = sorted(rows, key=lambda q: q["end"])
    pts = {}
    for i in range(3, len(rows)):
        ttm = sum(rows[j]["adj_eps"] for j in range(i - 3, i + 1))
        known = pd.Timestamp(rows[i]["end"]) + pd.Timedelta(days=lag_days)
        pts[known] = ttm
    return pd.Series(pts).sort_index()


def multiple_history(close: pd.Series, eps: pd.Series) -> list[dict]:
    """Price ÷ trailing adjusted EPS, sampled monthly, using only prior earnings."""
    if eps.empty or close.empty:
        return []
    c = close.copy()
    if isinstance(c.index, pd.DatetimeIndex) and c.index.tz is not None:
        c.index = c.index.tz_localize(None)
    monthly = c.resample("ME").last().dropna()
    out = []
    for dt, px in monthly.items():
        prior = eps[eps.index <= dt]
        if prior.empty:
            continue
        e = float(prior.iloc[-1])
        if e and e > 0:
            out.append({"d": dt.strftime("%Y-%m"), "pe": round(float(px) / e, 2)})
    return out


def series_block(df: pd.DataFrame) -> dict:
    """10y weekly plus 2y daily OHLCV, rounded to keep the payload small."""
    c = df["Close"].dropna()
    if isinstance(c.index, pd.DatetimeIndex) and c.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
        c = df["Close"].dropna()
    last = c.index[-1]

    wk = df.resample("W").agg({"Close": "last", "Volume": "sum"}).dropna()
    wk = wk[wk.index >= last - pd.Timedelta(days=365 * PRICE_YEARS)]
    dy = df[df.index >= last - pd.Timedelta(days=365 * 2)]

    def enc(idx, px, vol, digits):
        return {
            "t": [pd.Timestamp(i).strftime("%Y-%m-%d") for i in idx],
            "p": [round(float(v), digits) for v in px],
            "v": [int(v) if pd.notna(v) else 0 for v in vol],
        }
    dg = 3 if float(c.iloc[-1]) < 10 else 2
    return {"w": enc(wk.index, wk["Close"], wk["Volume"], dg),
            "d": enc(dy.index, dy["Close"].ffill(), dy["Volume"].fillna(0), dg)}


def quality(det: dict) -> dict:
    """
    Per-company coverage, so a gap is visible instead of rendering as a blank cell.
    """
    q = {"flags": [], "ok": True}
    na, nq = len(det.get("annual", [])), len(det.get("quarterly", []))
    q["annual_periods"], q["quarterly_periods"] = na, nq
    wk = det.get("px", {}).get("w", {}).get("t", [])
    q["price_weeks"] = len(wk)
    q["price_from"] = wk[0] if wk else None
    q["mult_points"] = len(det.get("mult", []))

    if na == 0:
        q["flags"].append("No annual statements returned")
    elif na < 3:
        q["flags"].append(f"Only {na} annual periods — trend metrics unreliable")
    if nq < 4:
        q["flags"].append(f"Only {nq} quarterly periods — no point-in-time multiple")
    if len(wk) < 260:
        q["flags"].append(f"Price history {len(wk)} weeks, under 5 years")
    if not det.get("mult"):
        q["flags"].append("No trailing multiple series — earnings negative or missing")
    bases = {p.get("adj_basis") for p in det.get("annual", [])}
    if "ex-revaluation" in bases:
        q["flags"].append("Earnings normalised for property revaluation")
    if "ex-unusual" in bases:
        q["flags"].append("Earnings normalised for unusual items")
    q["ok"] = not any(f.startswith(("No ", "Only ")) for f in q["flags"])
    return q


# ── fetch ───────────────────────────────────────────────────────────────────
def fetch_one(ticker: str, fair_value: bool):
    import yfinance as yf
    tk = yf.Ticker(ticker)
    hist = tk.history(period=f"{PRICE_YEARS}y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty:
        return None
    det = {"t": ticker, "px": series_block(hist)}
    try:
        det["annual"] = frame_to_periods(tk.income_stmt, IS_ROWS)
        det["quarterly"] = frame_to_periods(tk.quarterly_income_stmt, IS_ROWS)
        det["bs"] = frame_to_periods(tk.balance_sheet, BS_ROWS)
        det["cf"] = frame_to_periods(tk.cashflow, CF_ROWS)
    except Exception as e:
        print(f"    {ticker}: statements partial — {type(e).__name__}", file=sys.stderr)
        det.setdefault("annual", []); det.setdefault("quarterly", [])
        det.setdefault("bs", []); det.setdefault("cf", [])

    normalise_earnings(det["annual"], fair_value)
    normalise_earnings(det["quarterly"], fair_value)
    eps = pit_trailing_eps(det["quarterly"])
    det["mult"] = multiple_history(hist["Close"].dropna(), eps)
    det["q"] = quality(det)
    return det


def synth_one(ticker: str, fair_value: bool):
    rng = random.Random(ticker + "|det")
    n = 252 * PRICE_YEARS
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    n = len(idx)          # pandas may return periods-1 depending on version; trust the index
    px0 = rng.uniform(4, 180)
    drift, vol = rng.uniform(-0.02, 0.07) / 252, rng.uniform(0.010, 0.022)
    s = [px0]
    for _ in range(n - 1):
        s.append(max(0.5, s[-1] * (1 + drift + rng.gauss(0, vol))))
    close = pd.Series(s, index=idx)
    volume = pd.Series([rng.uniform(3e4, 5e6) * (1 + abs(rng.gauss(0, .5))) for _ in range(n)], index=idx)
    hist = pd.DataFrame({"Close": close, "Volume": volume})

    det = {"t": ticker, "px": series_block(hist)}
    shares = rng.uniform(5e7, 8e8)
    rev0 = px0 * shares * rng.uniform(0.06, 0.22)

    def periods(k, step):
        out = []
        for i in range(k):
            end = pd.Timestamp.today().normalize() - pd.Timedelta(days=step * i)
            gr = (1 + rng.uniform(-0.04, 0.09)) ** (-i)
            rev = rev0 * gr / (4 if step < 200 else 1)
            op = rev * rng.uniform(0.30, 0.62)
            itr = op * rng.uniform(0.15, 0.55)
            tax = max(0.0, (op - itr) * rng.uniform(0.0, 0.22))
            core = op - itr - tax
            reval = core * rng.gauss(0, 1.6) if fair_value else core * rng.gauss(0, 0.15)
            ni = core + reval
            sh = shares / (4 if step < 200 else 1)
            out.append({
                "end": end.strftime("%Y-%m-%d"), "revenue": rev, "op_income": op,
                "ebitda": op * rng.uniform(1.05, 1.4), "interest": itr, "tax": tax,
                "pretax": op - itr, "net_income": ni, "net_cont": ni,
                "unusual": ni * rng.choice([0.0, 0.0, 0.0, rng.uniform(-0.2, 0.2)]),
                "eps": ni / shares, "shares": shares,
            })
        return out

    det["annual"] = periods(4, 365)
    det["quarterly"] = periods(8, 91)
    det["bs"] = [{"end": p["end"], "assets": rev0 * 8, "prop": rev0 * 7,
                  "debt": rev0 * 3.4, "cash": rev0 * 0.3, "equity": rev0 * 4.2,
                  "pref": 0.0, "shares_out": shares} for p in det["annual"]]
    det["cf"] = [{"end": p["end"], "ocf": p["op_income"] * 0.8,
                  "capex": -p["revenue"] * 0.12, "fcf": p["op_income"] * 0.55,
                  "divs_paid": -p["op_income"] * 0.5, "acq": -p["revenue"] * 0.2,
                  "disposals": p["revenue"] * 0.1} for p in det["annual"]]

    normalise_earnings(det["annual"], fair_value)
    normalise_earnings(det["quarterly"], fair_value)
    det["mult"] = multiple_history(close, pit_trailing_eps(det["quarterly"]))
    det["q"] = quality(det)
    return det


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    snap = json.loads((DATA / "snapshot.json").read_text())
    rows = snap["rows"]
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        rows = [r for r in rows if r["t"] in want]
    if args.limit:
        rows = rows[:args.limit]

    print(f"detail for {len(rows)} names  (mock={args.mock})")
    t0, done, failed = time.time(), 0, []
    for i, r in enumerate(rows, 1):
        t = r["t"]
        try:
            det = synth_one(t, bool(r.get("fv"))) if args.mock \
                else fetch_one(t, bool(r.get("fv")))
            if det is None:
                failed.append(t); continue
            det["n"], det["ccy"], det["cc"] = r["n"], r["ccy"], r["cc"]
            (CO / f"{t.replace('/', '_')}.json").write_text(
                json.dumps(det, separators=(",", ":")))
            done += 1
        except Exception as e:
            failed.append(t)
            print(f"    {t}: {type(e).__name__} {e}", file=sys.stderr)
        if not args.mock:
            time.sleep(SLEEP)
        if i % 25 == 0:
            print(f"  …{i}/{len(rows)}  {time.time()-t0:.0f}s")

    kb = sum(f.stat().st_size for f in CO.glob("*.json")) / 1024
    print(f"wrote {done} files, {kb:,.0f} KB total, in {time.time()-t0:.0f}s")
    if failed:
        print(f"failed ({len(failed)}): {', '.join(failed[:20])}")

    # coverage summary, so short histories are visible in aggregate too
    qs = []
    for f in CO.glob("*.json"):
        d = json.loads(f.read_text())
        qs.append(d.get("q", {}))
    if qs:
        import statistics as st
        print(f"  annual periods:    median {st.median([q.get('annual_periods',0) for q in qs]):.0f}")
        print(f"  quarterly periods: median {st.median([q.get('quarterly_periods',0) for q in qs]):.0f}")
        print(f"  price weeks:       median {st.median([q.get('price_weeks',0) for q in qs]):.0f}")
        print(f"  with multiple series: {sum(1 for q in qs if q.get('mult_points'))}/{len(qs)}")


if __name__ == "__main__":
    main()
