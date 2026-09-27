@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM UPDATE PRICES.bat
REM
REM Double-click this file to pull fresh prices and dividends into the screener.
REM No typing required — everything below runs automatically.

cls
echo ============================================================
echo   RE.SCREEN - updating prices, dividends and financials
echo ============================================================
echo.
echo This window will fill with text as it works. That's normal --
echo it's showing progress, not an error. This takes 30-60 minutes.
echo You can leave it running and do something else.
echo.

REM ── check for Python ─────────────────────────────────────────────────────
where python >nul 2>nul
if errorlevel 1 (
    echo ------------------------------------------------------------
    echo   Python is not installed on this computer yet.
    echo ------------------------------------------------------------
    echo.
    echo   This is a one-time setup, about 5 minutes:
    echo.
    echo   1. Go to python.org/downloads
    echo   2. Click the big yellow "Download Python" button
    echo   3. Run the downloaded installer
    echo      IMPORTANT: on the first screen, check the box that says
    echo      "Add python.exe to PATH" before clicking Install
    echo   4. Come back here and double-click this file again
    echo.
    pause
    exit /b 1
)

echo Python found: OK
echo.
echo Installing the tools needed to fetch data (one-time, ~1 minute)...
python -m pip install --quiet yfinance pandas numpy
if errorlevel 1 (
    echo.
    echo ------------------------------------------------------------
    echo   Something went wrong installing the tools.
    echo   Take a screenshot of this window and send it for help.
    echo ------------------------------------------------------------
    pause
    exit /b 1
)

echo Tools ready: OK
echo.
echo ------------------------------------------------------------
echo   Step 1 of 2 - fetching prices, dividends, financials
echo   (this is the long one - 15 to 45 minutes for ~900 names)
echo ------------------------------------------------------------
python build_snapshot.py
if errorlevel 1 (
    echo.
    echo ------------------------------------------------------------
    echo   Step 1 hit an error. Take a screenshot of everything
    echo   above and send it for help -- nothing was corrupted,
    echo   it's safe to try again.
    echo ------------------------------------------------------------
    pause
    exit /b 1
)

echo.
echo ------------------------------------------------------------
echo   Step 2 of 2 - company charts and financial statements
echo ------------------------------------------------------------
python build_detail.py

echo.
echo ------------------------------------------------------------
echo   Building the two files you upload to the website
echo ------------------------------------------------------------
python make_standalone.py --light
python make_standalone.py

echo.
echo ============================================================
echo   DONE. Prices are now real and current.
echo ============================================================
echo.
echo   Next: open this same folder, and drag it onto
echo   app.netlify.com/drop to publish the update.
echo   (Or, if the site is already live on Netlify, drag the
echo   folder onto your existing site's page there instead --
echo   see UPDATE_WEBSITE instructions in README.md)
echo.
pause
