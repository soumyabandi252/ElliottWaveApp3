# Elliott Wave NASDAQ Composite — Live Streamlit Deployment

This package turns your existing Elliott Wave screener script into a free,
live, publicly-hosted website with all scan results under a
"Nasdaq_Composite" tab.

## What's included
- app.py                -> Streamlit web app (entry point)
- requirements.txt      -> Python dependencies for the host to install
- .streamlit/config.toml-> basic theming/server config

## What YOU need to add
Copy your existing script into this same folder:
    elliott_wave_NASDAQ_FINAL_optimized.py

Then edit TWO lines inside that script (search for "SCRIPT_DIR"):

    BEFORE:
    SCRIPT_DIR = r"C:\Sanjeev\Python projects\...\NASDAQ_Composite"
    OUTPUT_FILENAME = os.path.join(SCRIPT_DIR, "Elliott_Wave_NASDAQ_Composite_Master_Workbook.xlsx")

    AFTER (relative path works on any server, Windows or Linux):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_FILENAME = os.path.join(SCRIPT_DIR, "Elliott_Wave_NASDAQ_Composite_Master_Workbook.xlsx")

There are TWO occurrences of a similar SCRIPT_DIR block in the file
(one near the top USER INPUTS section, one near the bottom EXECUTION
BLOCK). Update BOTH so the workbook always saves next to the script,
not to a hardcoded Windows path that will not exist on the server.

## Why this works
Your script already builds the full master workbook (Cheat_Sheet,
Tomorrow_Buys, LongTerm/MidTerm/ShortTerm, MonthlyEW, MultiTFAlignment,
BacktestResults, etc). app.py runs your script exactly as-is in the
background, reads every sheet from the resulting .xlsx, and renders
each sheet as its own tab inside a single outer "Nasdaq_Composite" tab
on the website. Results auto-refresh every 60 minutes (adjustable via
REFRESH_SECONDS in app.py), and there's a manual "Force refresh now"
button too.

## Deployment steps (100% free)

1. Create a free GitHub account (github.com) if you don't have one.
2. Create a new PUBLIC repository, e.g. "elliott-wave-nasdaq".
3. Upload these files to the repo root:
     - app.py
     - requirements.txt
     - .streamlit/config.toml
     - elliott_wave_NASDAQ_FINAL_optimized.py  (your edited version)
4. Go to https://share.streamlit.io and sign in with GitHub.
5. Click "Create app" -> select your repo -> branch "main" ->
   main file path: app.py -> click "Deploy".
6. Wait 2-5 minutes for the build. You'll get a live URL like:
     https://your-username-elliott-wave-nasdaq.streamlit.app
7. Share that URL with anyone — it's free, public, and auto-redeploys
   every time you push a change to GitHub.

## Notes on the free tier
- The app "sleeps" after a period of no visitors and wakes up in
  ~20-30 seconds on the next visit.
- Community Cloud gives ~1GB RAM per app — fine for this use case
  since your scan writes to disk and Streamlit only reads the result.
- First scan after a cold start / cache expiry can take several
  minutes for ~3,300 NASDAQ tickers — this is expected and matches
  your script's own runtime, not a limitation of Streamlit.
- If you want faster page loads, reduce REFRESH_SECONDS's scan
  frequency, or later move to a scheduled GitHub Action that writes
  the .xlsx on a timer, letting app.py just read a pre-built file.
