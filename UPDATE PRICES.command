#!/bin/bash
# UPDATE PRICES.command
#
# Double-click this file to pull fresh prices and dividends into the screener.
# No typing required — everything below runs automatically.
#
# The first time you double-click, macOS may show a warning because this file
# wasn't downloaded from the App Store. That's normal for a script like this,
# not a sign anything is wrong. To allow it:
#   Right-click (or Control-click) this file → Open → click "Open" again
#   on the popup. You only need to do that once.

cd "$(dirname "$0")"

clear
echo "════════════════════════════════════════════════════════"
echo "  RE.SCREEN — updating prices, dividends and financials"
echo "════════════════════════════════════════════════════════"
echo ""
echo "This window will fill with text as it works. That's normal —"
echo "it's showing progress, not an error. This takes 30-60 minutes."
echo "You can leave it running and do something else."
echo ""

# ── check for Python ─────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
    echo "──────────────────────────────────────────────────────────"
    echo "  Python is not installed on this Mac yet."
    echo "──────────────────────────────────────────────────────────"
    echo ""
    echo "  This is a one-time setup, about 5 minutes:"
    echo ""
    echo "  1. Go to python.org/downloads"
    echo "  2. Click the big yellow 'Download Python' button"
    echo "  3. Open the downloaded file and click Continue through the"
    echo "     installer (the default options are fine)"
    echo "  4. Come back here and double-click this file again"
    echo ""
    read -p "Press Enter to close this window..."
    exit 1
fi

echo "✓ Python found"
echo ""
echo "Installing the tools needed to fetch data (one-time, ~1 minute)..."
python3 -m pip install --quiet --break-system-packages yfinance pandas numpy 2>/dev/null \
    || python3 -m pip install --quiet yfinance pandas numpy

if [ $? -ne 0 ]; then
    echo ""
    echo "──────────────────────────────────────────────────────────"
    echo "  Something went wrong installing the tools."
    echo "  Take a screenshot of this window and send it for help."
    echo "──────────────────────────────────────────────────────────"
    read -p "Press Enter to close this window..."
    exit 1
fi

echo "✓ Tools ready"
echo ""
echo "──────────────────────────────────────────────────────────"
echo "  Step 1 of 2 — fetching prices, dividends, financials"
echo "  (this is the long one — 15 to 45 minutes for ~900 names)"
echo "──────────────────────────────────────────────────────────"
python3 build_snapshot.py

if [ $? -ne 0 ]; then
    echo ""
    echo "──────────────────────────────────────────────────────────"
    echo "  Step 1 hit an error. Take a screenshot of everything"
    echo "  above and send it for help — nothing was corrupted,"
    echo "  it's safe to try again."
    echo "──────────────────────────────────────────────────────────"
    read -p "Press Enter to close this window..."
    exit 1
fi

echo ""
echo "──────────────────────────────────────────────────────────"
echo "  Step 2 of 2 — company charts and financial statements"
echo "──────────────────────────────────────────────────────────"
python3 build_detail.py

echo ""
echo "──────────────────────────────────────────────────────────"
echo "  Building the two files you upload to the website"
echo "──────────────────────────────────────────────────────────"
python3 make_standalone.py --light
python3 make_standalone.py

echo ""
echo "════════════════════════════════════════════════════════"
echo "  DONE. Prices are now real and current."
echo "════════════════════════════════════════════════════════"
echo ""
echo "  Next: open this same folder, and drag it onto"
echo "  app.netlify.com/drop to publish the update."
echo "  (Or, if the site is already live on Netlify, drag the"
echo "  folder onto your existing site's page there instead —"
echo "  see UPDATE_WEBSITE.command)"
echo ""
read -p "Press Enter to close this window..."
