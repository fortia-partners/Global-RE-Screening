#!/usr/bin/env python3
"""
build_snapshot.py — fetch the real-estate universe and write data/snapshot.json

    python build_snapshot.py                 # full run
    python build_snapshot.py --limit 40      # quick smoke test
    python build_snapshot.py --mock          # offline: synthetic data, no network
    python build_snapshot.py --no-info       # skip per-ticker fundamentals (much faster)

Everything downstream reads only data/snapshot.json, so the frontend never talks
to a data vendor and never hits CORS.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CACHE = ROOT / ".cache"
DATA.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)

# ── tunables ────────────────────────────────────────────────────────────────
MIN_ADV_USD = 100_000      # drop anything thinner than this (user's floor)

# Regions to build. Names outside these stay in universe.csv but are skipped, so
# turning a region back on is a one-line edit rather than re-sourcing tickers.
# Asia-Pacific (Japan, Singapore, Hong Kong, Australia), Mexico and South Africa
# are currently off. Brazil is kept: it is the home market and overlaps SIGMA.
INCLUDE_REGIONS = {"Europe", "North America", "Latin America"}
EXCLUDE_COUNTRIES = {"MX", "ZA"}        # inside an included region but not wanted
SPECIAL_MULT = 1.75        # payment > this × baseline ⇒ treat excess as special
# A REIT explicitly winding down (selling its portfolio and returning the
# proceeds) pays real cash on purpose, every time — so no single payment looks
# like an outlier against its own history, and payment-level special detection
# above cannot catch it. Confirmed real-world cases: NLOP (spun off from W. P.
# Carey in Nov 2023 explicitly to liquidate; "will not pay a regular dividend")
# and SITC (a run of asset-sale special distributions, incl. $3.25/share in
# Aug 2025, after its Oct 2024 Curbline spin-off). Both produce a trailing
# yield of 60-160%+ that is completely real and completely non-recurring.
LIQUIDATION_YIELD = 0.22   # median annual distribution/price above this ⇒ liquidating pattern
ABS_YIELD_CEILING = 0.30   # backstop: no verified figure is ever published above this
HISTORY_YEARS = 6
BATCH = 50                 # tickers per price request
BATCH_SLEEP = 1.2          # seconds between price batches
INFO_SLEEP = 0.25          # seconds between fundamentals requests
VARIABLE_CV = 0.22         # payment CoV above this ⇒ flag distribution as variable

# Yahoo quotes these in minor units (pence / cents), not the major currency.
MINOR_UNIT = {"GBp": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Yahoo `industry` → our property-type taxonomy. Only used when universe.csv
# leaves the category blank.
INDUSTRY_MAP = {
    "reit—industrial": "Logistics", "reit - industrial": "Logistics",
    "reit—retail": "Retail", "reit - retail": "Retail",
    "reit—residential": "Multifamily", "reit - residential": "Multifamily",
    "reit—office": "Office", "reit - office": "Office",
    "reit—healthcare facilities": "Healthcare", "reit - healthcare facilities": "Healthcare",
    "reit—hotel & motel": "Hotels", "reit - hotel & motel": "Hotels",
    "reit—diversified": "Diversified", "reit - diversified": "Diversified",
    "reit—specialty": "Diversified", "reit - specialty": "Diversified",
    "reit—mortgage": "Mortgage", "reit - mortgage": "Mortgage",
    "real estate—development": "Developer", "real estate - development": "Developer",
    "real estate—diversified": "Diversified", "real estate - diversified": "Diversified",
    "real estate services": "Services",
    "residential construction": "Developer",
    "engineering & construction": "Developer",
}
# Name keywords are the last resort — they catch European names Yahoo mislabels.
KEYWORD_MAP = [
    ("logisti", "Logistics"), ("industrial", "Logistics"), ("warehouse", "Logistics"),
    ("shurgard", "SelfStorage"), ("storage", "SelfStorage"), ("self stor", "SelfStorage"),
    ("student", "Student"), ("residential", "Multifamily"), ("apartment", "Multifamily"),
    ("wohnen", "Multifamily"), ("immobilien", "Diversified"),
    ("shopping", "Malls"), ("mall", "Malls"), ("centre", "Retail"), ("center", "Retail"),
    ("retail", "Retail"), ("supermarket", "Retail"),
    ("office", "Office"), ("health", "Healthcare"), ("medical", "Healthcare"),
    ("care", "Senior"), ("senior", "Senior"), ("hotel", "Hotels"), ("hospitality", "Hotels"),
    ("data cent", "DataCenter"), ("tower", "Towers"), ("timber", "Timber"),
    ("farmland", "Farmland"), ("gaming", "Gaming"), ("mortgage", "Mortgage"),
    ("homes", "Developer"), ("construction", "Developer"), ("development", "Developer"),
]

CATEGORY_ORDER = [
    "Logistics", "Malls", "Retail", "NetLease", "Multifamily", "SingleFamily",
    "Student", "Senior", "Office", "Healthcare", "DataCenter", "Towers",
    "SelfStorage", "Hotels", "HotelOp", "Timber", "Farmland", "Gaming", "Diversified",
    "Developer", "Services", "Mortgage",
]

COUNTRY_NAME = {
    "US": "United States", "GB": "United Kingdom", "FR": "France", "DE": "Germany",
    "NL": "Netherlands", "BE": "Belgium", "ES": "Spain", "PT": "Portugal", "IT": "Italy",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "DK": "Denmark", "CH": "Switzerland",
    "AT": "Austria", "PL": "Poland", "IE": "Ireland", "GR": "Greece", "CA": "Canada",
    "IS": "Iceland", "LU": "Luxembourg",
    "AU": "Australia", "JP": "Japan", "SG": "Singapore", "HK": "Hong Kong",
    "BR": "Brazil", "MX": "Mexico", "ZA": "South Africa",
}
EUROPE = {"GB", "FR", "DE", "NL", "BE", "ES", "PT", "IT", "SE", "NO", "FI", "DK",
          "CH", "AT", "PL", "IE", "GR", "IS", "LU"}


def region_of(cc: str) -> str:
    if cc in EUROPE:
        return "Europe"
    if cc in {"US", "CA"}:
        return "North America"
    if cc in {"JP", "SG", "HK", "AU", "NZ", "CN", "KR"}:
        return "Asia-Pacific"
    if cc in {"BR", "MX", "CL", "AR"}:
        return "Latin America"
    return "Other"


# ── universe ────────────────────────────────────────────────────────────────
def load_universe(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("ticker")]


def classify(row: dict, industry: str | None) -> str:
    if row.get("category"):
        return row["category"]
    if industry:
        hit = INDUSTRY_MAP.get(industry.strip().lower())
        if hit:
            return hit
    blob = f"{row.get('name','')}".lower()
    for kw, cat in KEYWORD_MAP:
        if kw in blob:
            return cat
    return {"MORTGAGE": "Mortgage", "DEVELOPER": "Developer",
            "SERVICES": "Services"}.get(row.get("vehicle", ""), "Diversified")


# ── dividend analytics ──────────────────────────────────────────────────────
def analyse_dividends(divs: pd.Series, asof: pd.Timestamp) -> dict:
    """
    Split a dividend history into its recurring component and its one-off
    component, and characterise the trend.

    Returns per-share annual figures in the listing currency.
    """
    out = dict(freq=0, ttm_total=0.0, ttm_recurring=0.0, indicated=0.0,
               special_ttm=0.0, variable=False, cut=False, streak=0, cagr3=None,
               last_pay=None, n_pay_5y=0)
    if divs is None or len(divs) == 0:
        return out

    d = divs[divs > 0].sort_index()
    if isinstance(d.index, pd.DatetimeIndex) and d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    d = d[d.index >= asof - pd.Timedelta(days=365 * HISTORY_YEARS)]
    if len(d) == 0:
        return out

    out["n_pay_5y"] = int(len(d))
    out["last_pay"] = d.index[-1].strftime("%Y-%m-%d")

    d3 = d[d.index >= asof - pd.Timedelta(days=365 * 3)]
    ttm = d[d.index >= asof - pd.Timedelta(days=365)]

    # payment frequency, from spacing rather than from a vendor field
    ref = d3 if len(d3) >= 3 else d
    if len(ref) >= 3:
        gap = float(np.median(np.diff(ref.index.values).astype("timedelta64[D]").astype(int)))
        freq = min({1: 1, 2: 2, 4: 4, 12: 12}.items(),
                   key=lambda kv: abs(kv[0] - (365.0 / gap)) if gap > 0 else 1e9)[1]
    else:
        freq = max(len(ttm), 1)
    out["freq"] = int(freq)

    # baseline payment: median over 3y is robust to both specials and cuts
    base = float(np.median(d3.values)) if len(d3) >= 2 else float(np.median(d.values))
    if base <= 0:
        return out

    out["ttm_total"] = float(ttm.sum())

    # a payment above SPECIAL_MULT × baseline is a special; only the *excess*
    # over baseline is treated as non-recurring
    is_special = ttm.values > SPECIAL_MULT * base
    out["special_ttm"] = float(np.clip(ttm.values[is_special] - base, 0, None).sum())
    out["ttm_recurring"] = max(out["ttm_total"] - out["special_ttm"], 0.0)

    regular = ttm.values[~is_special]
    if len(regular) == 0:
        regular = np.array([base])

    # indicated (forward) rate: most recent regular payment, annualised
    out["indicated"] = float(regular[-1]) * freq

    # variable distributions make the indicated rate unreliable — say so
    reg3 = d3.values[d3.values <= SPECIAL_MULT * base]
    if len(reg3) >= 3:
        mu = float(np.mean(reg3))
        if mu > 0 and float(np.std(reg3)) / mu > VARIABLE_CV:
            out["variable"] = True

    # calendar-year totals drive streak / cut / growth
    yearly = d.groupby(d.index.year).sum()
    complete = yearly[yearly.index < asof.year]
    if len(complete) >= 2:
        vals = complete.values
        streak = 0
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] >= vals[i - 1] * 0.995:
                streak += 1
            else:
                break
        out["streak"] = int(streak)
        if len(complete) >= 4 and complete.values[-4] > 0:
            n = 3
            out["cagr3"] = float((complete.values[-1] / complete.values[-4]) ** (1 / n) - 1)
        peak3 = float(np.max(complete.values[-3:]))
        if peak3 > 0 and out["ttm_recurring"] < 0.90 * peak3:
            out["cut"] = True
    return out


YEARS = [2022, 2023, 2024, 2025]        # complete calendar years to report


def year_stats(divs: pd.Series, close: pd.Series, asof: pd.Timestamp) -> dict:
    """
    DPS and dividend yield for each complete calendar year.

    The yield is DPS for that year over that year's closing price — i.e. the
    yield an owner actually earned, not today's yield applied to old payments.
    """
    out = {"dps": {}, "yld": {}}
    if divs is None or len(divs) == 0:
        return out
    d = divs[divs > 0]
    if isinstance(d.index, pd.DatetimeIndex) and d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    c = close.copy()
    if isinstance(c.index, pd.DatetimeIndex) and c.index.tz is not None:
        c.index = c.index.tz_localize(None)

    per_year = d.groupby(d.index.year).sum()
    for y in YEARS:
        dps = float(per_year.get(y, 0.0))
        out["dps"][str(y)] = round(dps, 6)
        ye = c[c.index <= pd.Timestamp(year=y, month=12, day=31)]
        px = float(ye.iloc[-1]) if len(ye) else None
        out["yld"][str(y)] = round(dps / px, 5) if px and px > 0 and dps > 0 else 0.0
    return out


def audit_recurrence(ys: dict, ttm_recurring: float, price: float) -> dict:
    """
    Decide what part of the payout history is actually repeatable.

    A trailing-twelve-month yield cannot distinguish a REIT that pays 8% every
    year from one that paid 4% three times and 20% once. This compares the
    calendar years against each other and returns the figure that survives.

    Verdicts
      STABLE   every year within ±18% of the median      → median
      RAMP     rising through the period                 → latest year
      FADE     falling through the period                → latest year
      SPIKE    one year well above the rest              → median of the others
      ERRATIC  no pattern, high dispersion               → median, unverified
      SHORT    fewer than three years of payments        → TTM recurring
      LIQUIDATING  distributions imply an unsustainable yield on their own
                   → unverifiable; no recurring rate is claimed
    """
    vals = [ys["dps"].get(str(y), 0.0) for y in YEARS]
    paid = [(y, v) for y, v in zip(YEARS, vals) if v > 0]
    res = {"verdict": "SHORT", "vdps": ttm_recurring, "spike_yr": None,
           "cov": None, "verified": False}

    # Checked first, before anything else, and regardless of how many years of
    # history exist: a company selling off its portfolio and returning the
    # proceeds pays distributions that are each individually unremarkable in
    # size relative to each other — every one is large — so nothing above ever
    # flags a "special", and with under three years of history (exactly the
    # case for a REIT spun off for this purpose) it would otherwise fall
    # straight into SHORT and have its raw TTM rubber-stamped as verified.
    #
    # Each year's yield is struck on that year's own price (from ys["yld"],
    # computed in year_stats), not today's — dividing an old year's payout by
    # today's price would conflate "the price has moved since then" with "this
    # was a liquidating payout," which is a different thing and a false trigger.
    paid_yields = [ys["yld"].get(str(y), 0.0) for y, v_ in paid if v_ > 0]
    paid_yields = [yy for yy in paid_yields if yy > 0]
    if paid_yields and float(np.median(paid_yields)) > LIQUIDATION_YIELD:
        res.update(verdict="LIQUIDATING", vdps=0.0, verified=False)
        return res

    if len(paid) < 3:
        res["vdps"] = ttm_recurring
        return res

    v = np.array([p[1] for p in paid], dtype=float)
    core = float(np.median(v))
    if core <= 0:
        return res
    cov = float(np.std(v) / np.mean(v))
    res["cov"] = round(cov, 3)
    ratios = v / core

    rising = bool(np.all(np.diff(v) >= -1e-9)) and v[-1] > v[0] * 1.05
    falling = bool(np.all(np.diff(v) <= 1e-9)) and v[-1] < v[0] * 0.95
    spikes = [i for i, r in enumerate(ratios) if r > 1.5]

    # Trend is tested before spikes, and the order matters: a steadily falling
    # payout always makes its earliest year look like a spike against the median,
    # so checking spikes first would label decliners SPIKE and then verify them
    # on the average of their better years — exactly the error this is meant to catch.
    if rising:
        res.update(verdict="RAMP", vdps=float(v[-1]), verified=True)
    elif falling:
        res.update(verdict="FADE", vdps=float(v[-1]), verified=True)
    elif len(spikes) == 1:
        i = spikes[0]
        others = np.delete(v, i)
        res.update(verdict="SPIKE", spike_yr=paid[i][0],
                   vdps=float(np.median(others)), verified=True)
    elif cov <= 0.18:
        res.update(verdict="STABLE", vdps=core, verified=True)
    else:
        res.update(verdict="ERRATIC", vdps=core, verified=False)

    # The audit only ever cuts. Calendar-year DPS is historical, but the yield is
    # struck on today's price, so a company that paid more in 2023 than it does now
    # would otherwise be credited with a yield it no longer offers. Capping at the
    # current recurring run-rate keeps every verified figure achievable today.
    if ttm_recurring > 0:
        res["vdps"] = min(res["vdps"], ttm_recurring)

    # Backstop, independent of everything above: whatever path got here, a
    # verified figure this high on a going-concern REIT has no precedent in
    # free financial data that turned out to be sustainable — every case
    # checked was this same liquidation pattern. Treat it the same way rather
    # than publish an unverifiable outlier under a different verdict's name.
    if price > 0 and res["vdps"] / price > ABS_YIELD_CEILING:
        res.update(verdict="LIQUIDATING", vdps=0.0, verified=False)
    return res


def project_2027(vdps: float, g_own, g_cat: float, flags: dict) -> dict:
    """
    2027 DPS, extrapolated. This is arithmetic, not a forecast — no free source
    publishes consensus DPS, so there is nothing to borrow.

    Own growth is shrunk 60/40 toward the property-type median, because a single
    company's three-year DPS CAGR is a noisy estimator of its next year, and
    shrinking toward the peer group beats trusting it outright.
    """
    if vdps <= 0:
        return {"d27": 0.0, "g": None, "conf": "none"}
    g = 0.60 * g_own + 0.40 * g_cat if g_own is not None else g_cat
    g = float(np.clip(g, -0.12, 0.15))

    # a cut or a payout above cash flow caps the upside at flat
    if flags.get("cut") or (flags.get("po") or 0) > 1.0:
        g = min(g, 0.0)

    d27 = vdps * (1 + g) ** 1.5      # ~18 months from the current run-rate

    bad = sum([bool(flags.get("cut")), bool(flags.get("var")),
               not flags.get("verified"), (flags.get("po") or 0) > 1.1,
               flags.get("verdict") in ("FADE", "ERRATIC")])
    good = sum([(flags.get("stk") or 0) >= 3, flags.get("verdict") in ("STABLE", "RAMP"),
                0 < (flags.get("po") or 0) < 0.9])
    conf = "low" if bad >= 2 else ("high" if good >= 2 and bad == 0 else "med")
    return {"d27": round(d27, 6), "g": round(g, 4), "conf": conf}


# ── balance sheet: the debt bridge ──────────────────────────────────────────
# Yahoo's `totalDebt` is borrowings plus capital leases and nothing else. A credit
# view needs the debt-like claims that rank ahead of common equity: preferred stock,
# perpetual hybrids, and minority interests in consolidated vehicles.
BS_ROWS = {
    "total_debt":  ["total debt"],
    "lt_debt":     ["long term debt and capital lease obligation", "long term debt"],
    "st_debt":     ["current debt and capital lease obligation", "current debt"],
    "leases":      ["capital lease obligations", "long term capital lease obligation"],
    "pref":        ["preferred stock"],
    "hybrid":      ["preferred securities outside stock equity"],
    "minority":    ["minority interest"],
    "equity":      ["stockholders equity", "total equity gross minority interest"],
    "cash":        ["cash cash equivalents and short term investments",
                    "cash and cash equivalents"],
    "sti":         ["other short term investments"],
    "assets":      ["total assets"],
}


def _bs_row(df, names):
    """Pull a normalised balance-sheet line by fuzzy label match, latest period."""
    if df is None or df.empty:
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for want in names:
        for lab, orig in idx.items():
            if lab == want:
                s = df.loc[orig].dropna()
                if len(s):
                    return float(s.iloc[0])
    for want in names:                      # loosen to substring
        for lab, orig in idx.items():
            if want in lab:
                s = df.loc[orig].dropna()
                if len(s):
                    return float(s.iloc[0])
    return None


def fetch_balance(tickers: list[str], mock: bool) -> dict[str, dict]:
    cache_path = CACHE / "balance.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if mock:
        return {}
    import yfinance as yf
    fresh = time.time() - 30 * 86400          # balance sheets move quarterly at most
    todo = [t for t in tickers if cache.get(t, {}).get("_ts", 0) < fresh]
    print(f"  balance sheets: {len(todo)} to refresh, {len(tickers)-len(todo)} cached")
    for i, t in enumerate(todo, 1):
        rec = {"_ts": time.time()}
        try:
            tk = yf.Ticker(t)
            df = tk.quarterly_balance_sheet
            if df is None or df.empty:
                df = tk.balance_sheet
            for k, names in BS_ROWS.items():
                rec[k] = _bs_row(df, names)
        except Exception as e:
            print(f"    {t}: balance sheet failed — {type(e).__name__}", file=sys.stderr)
        cache[t] = rec
        if i % 25 == 0:
            print(f"    …{i}/{len(todo)}")
            cache_path.write_text(json.dumps(cache))
        time.sleep(INFO_SLEEP)
    cache_path.write_text(json.dumps(cache))
    return cache


def debt_bridge(bs: dict, nfo: dict, mcap, to_usd: float) -> dict:
    """
    Build net debt the way a credit committee would, then gear it against common equity.

    Preferred stock sits inside Yahoo's `stockholdersEquity`, so adding it to debt
    without removing it from equity would double-count it — the gearing denominator
    uses common equity only.

    Deferred consideration on acquisitions is deliberately absent: normalised free
    feeds fold it into other current liabilities and never tag it, so there is no
    honest way to isolate it. Where it matters, it has to come off the filings.
    """
    o = dict(gdebt=None, leases=None, pref=None, hyb=None, mi=None, cashx=None,
             and_=None, gear=None, eqc=None, nd_pref=None, tcap=None, bs=False)
    g = lambda k: (float(bs[k]) if bs.get(k) not in (None, "") else None)

    gross = g("total_debt")
    if gross is None:
        lt, st = g("lt_debt") or 0.0, g("st_debt") or 0.0
        gross = (lt + st) or None
    if gross is None:
        gross = float(nfo["totalDebt"]) if nfo.get("totalDebt") else None
    if gross is None:
        return o

    o["bs"] = True
    leases = g("leases")                      # already inside Yahoo's total debt
    pref = g("pref") or 0.0
    hyb = g("hybrid") or 0.0
    mi = g("minority") or 0.0
    cash = g("cash")
    if cash is None:
        cash = float(nfo.get("totalCash") or 0.0)
    cash_total = cash

    # debt-like claims ranking ahead of common equity, net of cash
    nd_core = gross - cash_total
    nd_pref = nd_core + pref + hyb

    eq_total = g("equity")
    eq_common = (eq_total - pref) if (eq_total is not None) else None

    o.update(
        gdebt=round(gross * to_usd / 1e6, 1),
        leases=round(leases * to_usd / 1e6, 1) if leases else None,
        pref=round(pref * to_usd / 1e6, 1) if pref else None,
        hyb=round(hyb * to_usd / 1e6, 1) if hyb else None,
        mi=round(mi * to_usd / 1e6, 1) if mi else None,
        cashx=round(cash_total * to_usd / 1e6, 1),
        and_=round(nd_pref * to_usd / 1e6, 1),
        eqc=round(eq_common * to_usd / 1e6, 1) if eq_common is not None else None,
    )
    # the ratio asked for: net debt ÷ (net debt + shareholders' equity)
    if eq_common is not None and (nd_pref + eq_common) > 0:
        o["gear"] = round(nd_pref / (nd_pref + eq_common), 4)
    # total capitalisation at market, including the claims above common
    if mcap:
        tc = nd_pref + mi + mcap
        if tc > 0:
            o["tcap"] = round(tc * to_usd / 1e6, 1)
            o["nd_pref"] = round(nd_pref / tc, 4)
    return o


# ── fx ──────────────────────────────────────────────────────────────────────
def fetch_fx(currencies: set[str], mock: bool) -> dict[str, float]:
    fx = {"USD": 1.0}
    need = sorted(c for c in currencies if c and c != "USD")
    if mock:
        demo = {"EUR": 1.09, "GBP": 1.27, "CHF": 1.12, "SEK": 0.095, "NOK": 0.093,
                "DKK": 0.146, "PLN": 0.25, "CAD": 0.73, "AUD": 0.66, "JPY": 0.0064,
                "SGD": 0.74, "HKD": 0.128, "BRL": 0.18, "MXN": 0.052, "ZAR": 0.055}
        fx.update({c: demo.get(c, 1.0) for c in need})
        return fx
    import yfinance as yf
    for c in need:
        try:
            h = yf.Ticker(f"{c}USD=X").history(period="5d")
            if len(h):
                fx[c] = float(h["Close"].iloc[-1])
            else:
                print(f"  fx: no quote for {c}USD=X", file=sys.stderr)
        except Exception as e:
            print(f"  fx: {c} failed — {e}", file=sys.stderr)
        time.sleep(0.2)
    return fx


# ── fundamentals (cached) ───────────────────────────────────────────────────
INFO_FIELDS = (
    "longName", "shortName", "currency", "industry", "sector", "marketCap",
    "sharesOutstanding", "operatingCashflow", "quoteType", "exchange",
    # balance sheet / NAV
    "bookValue", "priceToBook", "totalDebt", "totalCash", "enterpriseValue",
    # earnings & cash
    "ebitda", "trailingEps", "forwardEps", "trailingPE", "forwardPE",
    "freeCashflow", "totalRevenue", "returnOnEquity", "revenueGrowth",
    # market structure
    "floatShares", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    # sell-side consensus — Yahoo aggregates this and it is free
    "targetMeanPrice", "recommendationMean", "numberOfAnalystOpinions",
)

# Property carried at fair value (IFRS and equivalents) versus depreciated
# historical cost. This decides whether book value per share is usable as a
# stand-in for NAV — under US GAAP it is not, and comparing P/NAV across the
# two regimes without adjustment is a category error.
FAIR_VALUE_GAAP = {"GB", "FR", "DE", "NL", "BE", "ES", "PT", "IT", "SE", "NO", "FI",
                   "DK", "CH", "AT", "PL", "IE", "GR", "CA", "AU", "SG", "HK",
                   "BR", "MX", "ZA"}


def compute_comp(nfo: dict, r: dict, price: float, to_usd: float, rec_dps: float) -> dict:
    """
    The valuation and leverage block of a peer comp sheet.

    Every figure here is derived from what a free feed actually publishes, so
    several are proxies for the metric a bank would quote. Each is named for what
    it is rather than what it approximates — `pnav` is price over book, not EPRA
    NTA, and `ltv` is built from book equity, not from an appraised property value.
    """
    out = dict(pnav=None, disc=None, pe=None, pocf=None, evebitda=None, cap=None,
               nd=None, ev=None, ltv=None, ndeb=None, dcap=None, cover=None,
               roe=None, flt=None, beta=None, upside=None, rec=None, nan=None,
               fv=r.get("country") in FAIR_VALUE_GAAP)

    g = lambda k: (float(nfo[k]) if nfo.get(k) not in (None, 0, "") else None)
    shares = g("sharesOutstanding")
    mcap = g("marketCap")
    bvps = g("bookValue")
    debt, cash, ebitda = g("totalDebt"), g("totalCash") or 0.0, g("ebitda")
    ocf = g("operatingCashflow")

    # NAV proxy — book value per share, same currency as price
    if bvps and bvps > 0:
        out["pnav"] = round(price / bvps, 3)
        out["disc"] = round(price / bvps - 1, 4)

    # earnings multiples
    fe = g("forwardEps") or g("trailingEps")
    if fe and fe > 0:
        out["pe"] = round(price / fe, 2)

    # P/FFO stand-in: operating cash flow per share
    ocfps = (ocf / shares) if (ocf and shares) else None
    if ocfps and ocfps > 0:
        out["pocf"] = round(price / ocfps, 2)
        if rec_dps > 0:
            out["cover"] = round(ocfps / rec_dps, 2)      # dividend cover, ×

    # net debt, enterprise value, leverage
    if debt is not None:
        nd = debt - cash
        out["nd"] = round(nd * to_usd / 1e6, 1)
        if mcap:
            ev = mcap + nd
            out["ev"] = round(ev * to_usd / 1e6, 1)
            out["dcap"] = round(nd / (nd + mcap), 4) if (nd + mcap) > 0 else None
            if ebitda and ebitda > 0:
                out["evebitda"] = round(ev / ebitda, 1)
                out["cap"] = round(ebitda / ev, 4)        # EBITDA yield on EV
        if bvps and shares:
            eq = bvps * shares
            gross = nd + eq
            if gross > 0:
                out["ltv"] = round(nd / gross, 4)
        if ebitda and ebitda > 0:
            out["ndeb"] = round(nd / ebitda, 1)

    if g("returnOnEquity") is not None:
        out["roe"] = round(g("returnOnEquity"), 4)
    if shares and g("floatShares"):
        out["flt"] = round(min(g("floatShares") / shares, 1.0), 3)
    if g("beta") is not None:
        out["beta"] = round(g("beta"), 2)

    # consensus — free from Yahoo, unlike forward DPS
    tgt = g("targetMeanPrice")
    if tgt and price > 0:
        out["upside"] = round(tgt / price - 1, 4)
    if g("recommendationMean"):
        out["rec"] = round(g("recommendationMean"), 2)
    if g("numberOfAnalystOpinions"):
        out["nan"] = int(g("numberOfAnalystOpinions"))
    return out


def period_returns(close: pd.Series, nfo: dict, price: float) -> dict:
    """Multi-period price returns, realised volatility, and position in the 52w band."""
    out = dict(d1=None, w1=None, r1m=None, r3m=None, r6m=None, rytd=None,
               vol=None, p52=None)
    c = close.copy()
    if isinstance(c.index, pd.DatetimeIndex) and c.index.tz is not None:
        c.index = c.index.tz_localize(None)
    last = c.index[-1]
    # one session, not one calendar day — a Monday quote compares to Friday
    if len(c) >= 2 and float(c.iloc[-2]) > 0:
        out["d1"] = round(float(c.iloc[-1] / c.iloc[-2] - 1), 4)
    for key, days in (("w1", 7), ("r1m", 30), ("r3m", 91), ("r6m", 182)):
        w = c[c.index >= last - pd.Timedelta(days=days)]
        if len(w) > 3 and float(w.iloc[0]) > 0:
            out[key] = round(float(w.iloc[-1] / w.iloc[0] - 1), 4)
    ytd = c[c.index >= pd.Timestamp(year=last.year, month=1, day=1)]
    if len(ytd) > 3 and float(ytd.iloc[0]) > 0:
        out["rytd"] = round(float(ytd.iloc[-1] / ytd.iloc[0] - 1), 4)

    w = c[c.index >= last - pd.Timedelta(days=120)]
    if len(w) > 30:
        ret = np.diff(np.log(w.values.astype(float)))
        out["vol"] = round(float(np.std(ret) * math.sqrt(252)), 4)

    hi = nfo.get("fiftyTwoWeekHigh")
    lo = nfo.get("fiftyTwoWeekLow")
    if not (hi and lo):
        y = c[c.index >= last - pd.Timedelta(days=365)]
        if len(y) > 20:
            hi, lo = float(y.max()), float(y.min())
    if hi and lo and float(hi) > float(lo):
        out["p52"] = round((price - float(lo)) / (float(hi) - float(lo)), 3)
    return out


def synth_info(ticker: str, price: float, ccy: str) -> dict:
    """Plausible fundamentals for offline mode, so the comp columns render."""
    rng = random.Random(ticker + "|info")
    shares = rng.uniform(4e7, 9e8)
    mcap = price * shares
    # book value per share sits either side of price — real REITs trade at both
    # premiums and discounts to NAV, and the spread is the interesting part
    bvps = price / rng.uniform(0.55, 1.35)
    debt = mcap * rng.uniform(0.35, 1.5)
    ebitda = mcap * rng.uniform(0.055, 0.13)
    ocf = ebitda * rng.uniform(0.55, 0.85)
    eps = price / rng.uniform(9, 26)
    return {
        "shortName": ticker, "currency": ccy, "industry": None, "sector": "Real Estate",
        "marketCap": mcap, "sharesOutstanding": shares, "operatingCashflow": ocf,
        "bookValue": bvps, "totalDebt": debt, "totalCash": debt * rng.uniform(0.02, 0.14),
        "ebitda": ebitda, "trailingEps": eps, "forwardEps": eps * rng.uniform(0.95, 1.12),
        "freeCashflow": ocf * rng.uniform(0.4, 0.9), "totalRevenue": ebitda / rng.uniform(0.4, 0.75),
        "returnOnEquity": rng.uniform(-0.04, 0.16), "revenueGrowth": rng.uniform(-0.08, 0.15),
        "floatShares": shares * rng.uniform(0.28, 0.99), "beta": rng.uniform(0.45, 1.55),
        "targetMeanPrice": price * rng.uniform(0.82, 1.35),
        "recommendationMean": rng.uniform(1.6, 4.1),
        "numberOfAnalystOpinions": rng.randint(0, 22),
        "_ts": time.time(),
    }


def synth_balance(ticker: str, nfo: dict) -> dict:
    rng = random.Random(ticker + "|bs")
    mcap = nfo["marketCap"]
    debt = nfo["totalDebt"]
    eq = nfo["bookValue"] * nfo["sharesOutstanding"]
    pref = mcap * rng.choice([0.0, 0.0, 0.0, rng.uniform(0.02, 0.14)])
    return {
        "total_debt": debt, "leases": debt * rng.uniform(0.0, 0.06),
        "pref": pref, "hybrid": mcap * rng.choice([0.0, 0.0, 0.0, rng.uniform(0.01, 0.09)]),
        "minority": mcap * rng.choice([0.0, 0.0, rng.uniform(0.01, 0.18)]),
        "equity": eq + pref, "cash": debt * rng.uniform(0.02, 0.16),
        "assets": eq + debt * rng.uniform(1.0, 1.3), "_ts": time.time(),
    }


def fetch_info(tickers: list[str], mock: bool) -> dict[str, dict]:
    cache_path = CACHE / "info.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if mock:
        return {}          # filled per-ticker in build(), where price is known
    import yfinance as yf
    fresh_cutoff = time.time() - 7 * 86400
    todo = [t for t in tickers if cache.get(t, {}).get("_ts", 0) < fresh_cutoff]
    print(f"  fundamentals: {len(todo)} to refresh, {len(tickers)-len(todo)} cached")
    for i, t in enumerate(todo, 1):
        try:
            raw = yf.Ticker(t).get_info() or {}
            cache[t] = {k: raw.get(k) for k in INFO_FIELDS} | {"_ts": time.time()}
        except Exception as e:
            cache.setdefault(t, {})["_ts"] = time.time()
            print(f"    {t}: info failed — {type(e).__name__}", file=sys.stderr)
        if i % 25 == 0:
            print(f"    …{i}/{len(todo)}")
            cache_path.write_text(json.dumps(cache))
        time.sleep(INFO_SLEEP)
    cache_path.write_text(json.dumps(cache))
    return cache


# ── prices ──────────────────────────────────────────────────────────────────
def fetch_prices(tickers: list[str], mock: bool) -> dict[str, pd.DataFrame]:
    if mock:
        return {t: synth_history(t) for t in tickers}
    import yfinance as yf
    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        print(f"  prices: batch {i//BATCH+1}/{math.ceil(len(tickers)/BATCH)} ({len(chunk)})")
        try:
            raw = yf.download(chunk, period=f"{HISTORY_YEARS}y", interval="1d",
                              actions=True, auto_adjust=False, group_by="ticker",
                              threads=True, progress=False)
        except Exception as e:
            print(f"    batch failed — {e}", file=sys.stderr)
            time.sleep(5)
            continue
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                if df is not None and len(df.dropna(how="all")):
                    frames[t] = df
            except Exception:
                pass
        time.sleep(BATCH_SLEEP)
    return frames


def synth_history(ticker: str) -> pd.DataFrame:
    """Deterministic fake series so the frontend can be built/tested offline."""
    rng = random.Random(ticker)
    n = 252 * HISTORY_YEARS
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    n = len(idx)          # pandas may return periods-1 depending on version; trust the index
    px0 = rng.uniform(4, 180)
    drift = rng.uniform(-0.03, 0.06) / 252
    vol = rng.uniform(0.009, 0.022)
    steps = [px0]
    for _ in range(n - 1):
        steps.append(max(0.4, steps[-1] * (1 + drift + rng.gauss(0, vol))))
    close = pd.Series(steps, index=idx)
    vol_sh = pd.Series([rng.uniform(2e4, 6e6) for _ in range(n)], index=idx)

    divs = pd.Series(0.0, index=idx)
    freq = rng.choice([4, 4, 4, 4, 2, 2, 12, 1, 0])
    if freq:
        yld = rng.uniform(0.008, 0.095)               # target running yield
        grow = rng.uniform(-0.02, 0.06)               # annual DPS growth
        pay_idx = idx[:: max(1, n // (freq * HISTORY_YEARS))]
        for k, dt in enumerate(pay_idx):
            yrs_ago = (len(pay_idx) - 1 - k) / freq
            # peg the payment to the price on the day, so the yield stays sane
            amt = close.loc[dt] * yld / freq * (1 + grow) ** (-yrs_ago) * (1 + rng.gauss(0, 0.03))
            if rng.random() < 0.05:
                amt *= rng.uniform(2.2, 3.8)          # occasional special
            divs.loc[dt] = max(amt, 0)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Adj Close": close, "Volume": vol_sh,
                         "Dividends": divs})


def sector_ok(nfo: dict, name: str) -> tuple[bool, str]:
    """
    Confirm a symbol actually resolved to a property company.

    A ticker that does not exist is harmless — it returns no price and the row is
    dropped. The dangerous case is a symbol that resolves to a *different, real*
    company: it returns a full set of plausible figures under the name we expected,
    and nothing downstream can tell. This is the check for that, and it runs on
    every build rather than only when the universe changes.

    Returns (ok, reason). `ok=False` rows are dropped and listed at the end of the
    run so a bad symbol is visible rather than silent.
    """
    sec = (nfo.get("sector") or "").strip().lower()
    ind = (nfo.get("industry") or "").strip().lower()
    got = (nfo.get("longName") or nfo.get("shortName") or "").lower()

    if not sec and not ind:
        return True, "no sector data"          # nothing to judge on; keep it

    if "real estate" in sec or "reit" in ind or "real estate" in ind:
        return True, ""
    # construction and homebuilding sit outside Yahoo's real-estate sector but
    # belong in this universe
    if any(k in ind for k in ("construction", "engineering", "building products",
                              "homebuilding", "residential")):
        return True, ""
    # a property name in a non-property sector is the signature of a mis-resolved
    # symbol — Yahoo returning a bank or an industrial where a REIT was expected
    if any(w in got for w in ("propert", "immobil", "fastighet", "real estate",
                              "reit", "estates", "realty", "eiendom", "vastgoed",
                              "foncier", "inmobiliaria")):
        return True, ""
    return False, f"resolved to {sec or '?'} / {ind or '?'} as '{got[:34]}'"


# ── assembly ────────────────────────────────────────────────────────────────
def build(args) -> dict:
    uni = load_universe(ROOT / "universe.csv")
    if args.limit:
        uni = uni[:args.limit]
    total = len(uni)
    uni = [r for r in uni
           if region_of(r["country"]) in INCLUDE_REGIONS
           and r["country"] not in EXCLUDE_COUNTRIES]
    tickers = [r["ticker"] for r in uni]
    print(f"universe: {len(tickers)} of {total} names "
          f"({total - len(tickers)} outside {sorted(INCLUDE_REGIONS)})")

    print("fetching prices…")
    px = fetch_prices(tickers, args.mock)
    print(f"  got history for {len(px)}/{len(tickers)}")

    info = {} if args.no_info else fetch_info(tickers, args.mock)
    bal = {} if (args.no_info or args.no_bs) else fetch_balance(tickers, args.mock)

    currencies = {(info.get(t, {}) or {}).get("currency") or guess_ccy(t) for t in tickers}
    resolved = set()
    for c in currencies:
        resolved.add(MINOR_UNIT.get(c, (c, 1))[0] if c else None)
    print("fetching fx…")
    fx = fetch_fx({c for c in resolved if c}, args.mock)

    asof = pd.Timestamp.today().normalize()
    rows, dropped = [], {"no_price": 0, "thin": 0, "no_ccy": 0, "wrong_sector": 0}
    misresolved = []

    for r in uni:
        t = r["ticker"]
        df = px.get(t)
        if df is None or "Close" not in df or df["Close"].dropna().empty:
            dropped["no_price"] += 1
            continue
        close = df["Close"].dropna()
        price = float(close.iloc[-1])
        nfo = info.get(t, {}) or {}
        if args.mock:
            nfo = synth_info(t, price, guess_ccy(t))
        if price <= 0:
            dropped["no_price"] += 1
            continue

        ok, why = sector_ok(nfo, r.get("name", ""))
        if not ok:
            dropped["wrong_sector"] += 1
            misresolved.append(f"{t} ({r.get('name','?')}) — {why}")
            continue

        ccy_raw = nfo.get("currency") or guess_ccy(t)
        ccy, unit = MINOR_UNIT.get(ccy_raw, (ccy_raw, 1.0))
        rate = fx.get(ccy)
        if not rate:
            dropped["no_ccy"] += 1
            continue
        to_usd = rate / unit

        # liquidity: median dollar volume over the last 30 sessions
        tail = df.tail(30)
        dv = (tail["Close"] * tail["Volume"]).dropna()
        adv_usd = float(np.median(dv.values)) * to_usd if len(dv) else 0.0
        if adv_usd < MIN_ADV_USD:
            dropped["thin"] += 1
            continue

        divs = df["Dividends"] if "Dividends" in df else pd.Series(dtype=float)
        dv_stats = analyse_dividends(divs, asof)
        ys = year_stats(divs, close, asof)
        aud = audit_recurrence(ys, dv_stats["ttm_recurring"], price)

        rec_y = dv_stats["ttm_recurring"] / price if price else 0.0
        ind_y = dv_stats["indicated"] / price if price else 0.0
        ttm_y = dv_stats["ttm_total"] / price if price else 0.0

        mcap = nfo.get("marketCap")
        mcap_usd = float(mcap) * to_usd if mcap else None

        # payout on operating cash flow — a rough stand-in for an FFO payout,
        # since FFO is not available in any free feed
        payout = None
        ocf, sh = nfo.get("operatingCashflow"), nfo.get("sharesOutstanding")
        if ocf and sh and ocf > 0:
            ocf_ps = float(ocf) / float(sh)
            if ocf_ps > 0:
                payout = dv_stats["ttm_recurring"] / ocf_ps

        # 12-month total return and a 60-point sparkline
        r1y = None
        yr = close[close.index >= close.index[-1] - pd.Timedelta(days=365)]
        if len(yr) > 20 and float(yr.iloc[0]) > 0:
            r1y = float(yr.iloc[-1] / yr.iloc[0] - 1)
        spark = close.tail(252)
        if len(spark) > 60:
            spark = spark.iloc[:: max(1, len(spark) // 60)]
        lo, hi = float(spark.min()), float(spark.max())
        sp = [round((v - lo) / (hi - lo), 3) for v in spark.values] if hi > lo else [0.5] * len(spark)

        bsr = bal.get(t, {}) or {}
        if args.mock:
            bsr = synth_balance(t, nfo)
        comp = compute_comp(nfo, r, price, to_usd, dv_stats["ttm_recurring"])
        brg = debt_bridge(bsr, nfo, nfo.get("marketCap"), to_usd)
        rets = period_returns(close, nfo, price)

        rows.append({
            "t": t,
            "n": (r.get("name") or nfo.get("shortName") or nfo.get("longName") or t)[:42],
            "cc": r["country"],
            "rg": region_of(r["country"]),
            "cat": classify(r, nfo.get("industry")),
            "veh": r.get("vehicle") or "REIT",
            "px": round(price, 4),
            "ccy": ccy_raw or ccy,
            "mcap": round(mcap_usd / 1e6, 1) if mcap_usd else None,
            "adv": round(adv_usd / 1e3, 1),
            "ry": round(rec_y, 5),          # recurring yield  ← primary sort
            "iy": round(ind_y, 5),          # indicated forward yield
            "ty": round(ttm_y, 5),          # trailing 12m incl. specials
            "spc": round(dv_stats["special_ttm"] / max(dv_stats["ttm_total"], 1e-9), 3),
            "frq": dv_stats["freq"],
            "var": dv_stats["variable"],
            "cut": dv_stats["cut"],
            "stk": dv_stats["streak"],
            "g3": round(dv_stats["cagr3"], 4) if dv_stats["cagr3"] is not None else None,
            "po": round(payout, 3) if payout is not None else None,
            "r1y": round(r1y, 4) if r1y is not None else None,
            "lp": dv_stats["last_pay"],
            "sp": sp,
            # per-year history — DPS in listing currency, yield on that year's close
            "dps": ys["dps"],
            "hy": ys["yld"],
            # audit
            "aud": aud["verdict"],
            "vdps": round(aud["vdps"], 6),
            "vy": round(aud["vdps"] / price, 5) if price else 0.0,
            "spy": aud["spike_yr"],
            "cov": aud["cov"],
            "ver": aud["verified"],
            **comp, **rets, **brg,
        })

    print(f"kept {len(rows)}  |  dropped: {dropped}")
    if misresolved:
        print(f"\n  {len(misresolved)} symbol(s) resolved to something that is not a "
              f"property company — remove or correct these in universe.csv:")
        for m in misresolved:
            print(f"    {m}")

    # group medians, so a yield can be read against its own peer set
    peer = {}
    for row in rows:
        k = f"{row['cc']}|{row['cat']}"
        peer.setdefault(k, []).append(row["ry"])
    peer_med = {k: float(np.median(v)) for k, v in peer.items()}
    cat_all = {}
    for row in rows:
        cat_all.setdefault(row["cat"], []).append(row["ry"])
    cat_med = {k: float(np.median(v)) for k, v in cat_all.items()}
    for row in rows:
        row["pm"] = round(peer_med[f"{row['cc']}|{row['cat']}"], 5)

    # ── second pass: 2027E needs the property-type growth median first ──────
    gcat = {}
    for row in rows:
        if row["g3"] is not None:
            gcat.setdefault(row["cat"], []).append(row["g3"])
    gmed = {k: float(np.median(v)) for k, v in gcat.items()}
    g_all = float(np.median([g for v in gcat.values() for g in v])) if gcat else 0.0

    for row in rows:
        pj = project_2027(
            row["vdps"], row["g3"], gmed.get(row["cat"], g_all),
            {"cut": row["cut"], "var": row["var"], "po": row["po"],
             "stk": row["stk"], "verdict": row["aud"], "verified": row["ver"]},
        )
        row["d27"] = pj["d27"]
        row["y27"] = round(pj["d27"] / row["px"], 5) if row["px"] else 0.0
        row["g27"] = pj["g"]
        row["c27"] = pj["conf"]

    # how much the audit removed versus the naive trailing figure
    for row in rows:
        row["hc"] = round(row["ty"] - row["vy"], 5)

    # ── plausibility scan ─────────────────────────────────────────────────
    # The three checks above catch one specific, now-confirmed failure mode
    # (a liquidating REIT). This catches everything else the same way a human
    # reviewer would: by noticing a number sits far outside its own peers.
    # It doesn't know *why* a value is wrong — a bad currency conversion, a
    # stale price, a mismatched statement line, a real anomaly that deserves
    # a look — it only knows that it doesn't resemble its neighbours, which is
    # exactly the state a silent data error leaves behind.
    #
    # Robust statistics on purpose: a plain mean and standard deviation are
    # themselves dragged around by the very outlier being hunted for. The
    # median and MAD (median absolute deviation) are not — a peer group can
    # contain one wildly wrong number and its median barely moves.
    OUTLIER_METRICS = [
        ("ry", "recurring yield", 0.10), ("ty", "TTM yield", 0.10),
        ("vy", "verified yield", 0.10), ("pnav", "P/NAV", None),
        ("pe", "P/E", None), ("ltv", "LTV proxy", None), ("cover", "dividend cover", None),
    ]
    Z_THRESH = 8.0        # deliberately high — this flags "not remotely like its peers",
                          # not "a bit rich", so it stays rare enough to be worth reading
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(f"{row['cc']}|{row['cat']}", []).append(row)

    outlier_rows = []
    for members in groups.values():
        if len(members) < 4:              # too few peers for a median to mean anything
            continue
        for key, label, floor in OUTLIER_METRICS:
            vals = [(m, m[key]) for m in members if m.get(key) is not None]
            if len(vals) < 4:
                continue
            arr = np.array([v for _, v in vals])
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med)))
            sigma = mad * 1.4826 if mad > 0 else max(abs(med) * 0.15, 1e-6)
            for m, v in vals:
                if floor is not None and abs(v) < floor:
                    continue                        # trivial in absolute terms either way
                z = abs(v - med) / sigma
                if z > Z_THRESH:
                    m.setdefault("outliers", []).append({
                        "metric": key, "label": label, "value": round(v, 4),
                        "peer_med": round(med, 4),
                        "mult": round(v / med, 2) if med else None,
                    })
    for row in rows:
        if row.get("outliers"):
            row["flag_outlier"] = True
            outlier_rows.append(row)

    if outlier_rows:
        print(f"\nplausibility scan: {len(outlier_rows)} name(s) sit far outside their "
              f"(country, sector) peers — verify before relying on these:")
        for r in outlier_rows[:20]:
            for o in r["outliers"]:
                mult = f"{o['mult']}×" if o["mult"] is not None else "—"
                print(f"   {r['t']:<12} {o['label']:<16} {o['value']:>10}  "
                      f"peer median {o['peer_med']:>10}  ({mult} peer median)")
        if len(outlier_rows) > 20:
            print(f"   … and {len(outlier_rows) - 20} more")

    liq = [r for r in rows if r["aud"] == "LIQUIDATING"]
    if liq:
        print(f"\n{len(liq)} name(s) classified LIQUIDATING — distributions imply an "
              f"unsustainable yield on their own basis, so no recurring rate is claimed:")
        for r in liq[:20]:
            print(f"   {r['t']:<12} {r['n'][:30]:<30} ttm {r['ty']*100:6.2f}%")

    n_hy = sum(1 for r in rows if r["ry"] >= 0.06)
    n_flag = sum(1 for r in rows if r["ry"] >= 0.06 and r["aud"] in ("SPIKE", "FADE", "ERRATIC"))
    print(f"audit: {n_hy} names at or above 6% recurring — {n_flag} failed verification")
    for r in sorted((x for x in rows if x["ry"] >= 0.06), key=lambda x: -x["ry"])[:12]:
        print(f"   {r['t']:<12} {r['aud']:<8} ttm {r['ty']*100:5.2f}%  "
              f"verified {r['vy']*100:5.2f}%  2027E {r['y27']*100:5.2f}% ({r['c27']})")

    return {
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(rows),
        "min_adv_usd": MIN_ADV_USD,
        "special_mult": SPECIAL_MULT,
        "mock": bool(args.mock),
        "fx": {k: round(v, 6) for k, v in fx.items()},
        "countries": {c: COUNTRY_NAME.get(c, c) for c in sorted({r["cc"] for r in rows})},
        "cat_order": [c for c in CATEGORY_ORDER if c in cat_med],
        "cat_median": {k: round(v, 5) for k, v in cat_med.items()},
        "rows": rows,
    }


def guess_ccy(ticker: str) -> str:
    sfx = ticker.rsplit(".", 1)[-1].upper() if "." in ticker else ""
    return {"L": "GBp", "PA": "EUR", "AS": "EUR", "BR": "EUR", "DE": "EUR", "MC": "EUR",
            "MI": "EUR", "LS": "EUR", "HE": "EUR", "IR": "EUR", "AT": "EUR", "VI": "EUR",
            "ST": "SEK", "OL": "NOK", "CO": "DKK", "SW": "CHF", "WA": "PLN",
            "TO": "CAD", "AX": "AUD", "T": "JPY", "SI": "SGD", "HK": "HKD",
            "IC": "ISK", "LU": "EUR",
            "SA": "BRL", "MX": "MXN", "JO": "ZAc"}.get(sfx, "USD")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--no-info", action="store_true")
    p.add_argument("--no-bs", action="store_true",
                   help="skip balance sheets (halves fetch time, drops the debt bridge)")
    p.add_argument("--out", default=str(DATA / "snapshot.json"))
    args = p.parse_args()

    t0 = time.time()
    snap = build(args)
    Path(args.out).write_text(json.dumps(snap, separators=(",", ":")))
    kb = Path(args.out).stat().st_size / 1024
    print(f"wrote {args.out}  {kb:,.0f} KB  in {time.time()-t0:,.0f}s")


if __name__ == "__main__":
    main()
