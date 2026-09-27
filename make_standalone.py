#!/usr/bin/env python3
"""
make_standalone.py — bundle index.html + data/snapshot.json into one file.

    python make_standalone.py

Produces rescreen-standalone.html, which opens by double-clicking. No server,
no install. Regenerate it after each data refresh to pick up new numbers.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
html = (ROOT / "index.html").read_text()
snap = json.loads((ROOT / "data" / "snapshot.json").read_text())

# Escaping < prevents a "</script>" inside any string field from ending the tag early.
blob = json.dumps(snap, separators=(",", ":")).replace("<", "\\u003c")

# Swap the network fetch for a read of the embedded blob. Everything else is untouched,
# so the two builds cannot drift apart.
OLD = """    const res=await fetch("data/snapshot.json",{cache:"no-store"});
    if(!res.ok) throw new Error("HTTP "+res.status);
    DB=await res.json();"""
NEW = """    DB=JSON.parse(document.getElementById("snap").textContent);"""
assert OLD in html, "index.html changed — update the OLD marker in this script"
html = html.replace(OLD, NEW)

html = html.replace(
    '<script>\n"use strict";',
    f'<script id="snap" type="application/json">{blob}</script>\n<script>\n"use strict";',
)

# The offline build can never refresh itself. Say so in the header rather than
# letting a stale timestamp imply otherwise.
html = html.replace(
    'feed.textContent="EOD · "+ROWS.length+" LOADED"; feed.className="chip live";',
    'feed.textContent="OFFLINE COPY"; feed.className="chip live";',
).replace(
    'feed.title="Free feeds are end-of-day to 15-minute delayed, refreshed on a schedule.";',
    'feed.title="Self-contained file. Figures are frozen at the timestamp shown and will not update.";',
)

# ── inline per-company detail, trimmed ───────────────────────────────────────
# Full detail is ~16 MB. The offline build drops the two-year daily series and the
# weekly date array (dates are implied by the weekly cadence), which lands near
# 6 MB — large for a single file but still openable. Charts fall back to weekly,
# so the 1M and 6M ranges are coarser offline than in the served build.
import glob, sys

# At 926 names the inlined detail runs to ~14 MB, which makes the file slow to
# open and awkward to email. --light drops it: the screen still works in full,
# and the company panel points at the served build for charts and financials.
LIGHT = "--light" in sys.argv

co = {}
if LIGHT:
    co = {}
for f in ([] if LIGHT else sorted(glob.glob(str(ROOT / "data" / "co" / "*.json")))):
    d = json.loads(Path(f).read_text())
    d["px"].pop("d", None)
    w = d["px"].get("w", {})
    if w.get("t"):
        d["px"]["w"] = {"t0": w["t"][0], "p": w["p"], "v": w["v"]}
    co[d["t"]] = d

detail_blob = json.dumps(co, separators=(",", ":")).replace("<", "\\u003c")
html = html.replace(
    '<script id="snap" type="application/json">',
    '<script id="detail" type="application/json">' + detail_blob + '</script>\n'
    '<script id="snap" type="application/json">',
)

FETCH_OLD = (
    '    const res=await fetch(`data/co/${encodeURIComponent(t)}.json`,{cache:"force-cache"});\n'
    '    DETAIL[t]=res.ok?await res.json():null;'
)
FETCH_NEW = """    if(!window.__CO){
      window.__CO=JSON.parse(document.getElementById("detail").textContent);
      for(const k in window.__CO){          // rebuild the dropped weekly dates
        const w=window.__CO[k].px&&window.__CO[k].px.w;
        if(w&&!w.t&&w.t0){
          const d0=new Date(w.t0+"T00:00:00Z"), out=[];
          for(let i=0;i<w.p.length;i++)
            out.push(new Date(d0.getTime()+i*7*864e5).toISOString().slice(0,10));
          w.t=out;
        }
      }
    }
    DETAIL[t]=window.__CO[t]||null;"""
assert FETCH_OLD in html, "detail fetch block changed — update make_standalone.py"
html = html.replace(FETCH_OLD, FETCH_NEW)

# no daily series offline, so every range reads from the weekly array
html = html.replace('  const daily=(rng==="1M"||rng==="6M"||rng==="1Y");',
                    '  const daily=false;   // offline build carries weekly only')

out = ROOT / ("rescreen-light.html" if LIGHT else "rescreen-standalone.html")
out.write_text(html)
print(f"wrote {out.name} — {out.stat().st_size/1024:,.0f} KB, {snap['n']} names, mock={snap['mock']}")
