# ══════════════════════════════════════════════════════════════════
# ELLIOTT WAVE MASTER WORKBOOK SCREENER — U.S. INDEX AUTO-FETCH
#
# OUTPUT:
#   Elliott_Wave_<INDEX>_Master_Workbook.xlsx
#   ├── Cheat_Sheet
#   ├── Tomorrow_Buys
#   ├── LongTerm_Buys
#   ├── MidTerm_Buys
#   ├── ShortTerm_Buys
#   ├── LongTerm
#   ├── MidTerm
#   ├── ShortTerm
#   └── All_3_Bullish
#
# INSTALL:
#   pip install requests yfinance pandas numpy openpyxl lxml html5lib beautifulsoup4
#
# RUN:
#   python elliott_wave_index_master_workbook.py
#
# CHANGE ONLY THESE INPUTS:
#   INDEX_CHOICE = "SP500"
#   OUTPUT_FILENAME = None
# ══════════════════════════════════════════════════════════════════

import requests
import yfinance as yf
import pandas as pd
import numpy as np
from io import StringIO
from datetime import datetime, timedelta
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import warnings
warnings.filterwarnings("ignore")

END_DATE = datetime.today()

# ────────────────────────────────────────────────────────────────
# USER INPUTS — CHANGE ONLY THESE
# ────────────────────────────────────────────────────────────────
INDEX_CHOICE = "NASDAQ_COMPOSITE"   # SP500, NASDAQ100, DOW30, RUSSELL1000, RUSSELL2000, IWM, SP400, SP600, NASDAQ_COMPOSITE, NASDAQ1000, TOTAL_US
OUTPUT_FILENAME = None   # None = auto name based on selected universe

# Pivot settings
PIVOT_LEFT_BARS = 5
PIVOT_RIGHT_BARS = 5
PIVOT_NEAR_BARS = 2   # confirmed pivot in last 2 bars or nearby

HEADERS = {
    "User-Agent": "ElliottWaveIndexScreener/1.0 (contact: local-script; educational use)"
}


# ────────────────────────────────────────────────────────────────
# UNIVERSE FETCH HELPERS
# ────────────────────────────────────────────────────────────────
def normalize_ticker_series(s):
    return s.astype(str).str.replace(".", "-", regex=False).str.strip()

def fetch_wikipedia_api_tables(page_title):
    api_url = (
        "https://en.wikipedia.org/w/api.php"
        f"?action=parse&page={page_title}&prop=text&formatversion=2&format=json"
    )
    r = requests.get(api_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    html = data.get("parse", {}).get("text", "")
    if not html:
        raise RuntimeError(f"Wikimedia API returned no parse text for {page_title}.")
    return pd.read_html(StringIO(html))

def fetch_html_tables(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_html(StringIO(r.text))


# ────────────────────────────────────────────────────────────────
# S&P 500 AUTO-FETCH
# ────────────────────────────────────────────────────────────────
def get_sp500_constituents():
    api_url = (
        "https://en.wikipedia.org/w/api.php"
        "?action=parse"
        "&page=List_of_S%26P_500_companies"
        "&prop=text"
        "&formatversion=2"
        "&format=json"
    )

    direct_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

    last_error = None

    try:
        r = requests.get(api_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()

        html = data.get("parse", {}).get("text", "")
        if html:
            tables = pd.read_html(StringIO(html))
            for df in tables:
                if "Symbol" in df.columns:
                    sp500 = df.copy()
                    break
            else:
                raise RuntimeError("API HTML returned, but no table with 'Symbol' column found.")
        else:
            raise RuntimeError("Wikimedia API returned no parse text.")

        sp500 = sp500.rename(columns={
            "Symbol": "Ticker",
            "Security": "Company",
            "GICS Sector": "Sector",
            "GICS Sub-Industry": "SubIndustry"
        })

        sp500["Ticker"] = normalize_ticker_series(sp500["Ticker"])

        keep_cols = [c for c in ["Ticker", "Company", "Sector", "SubIndustry"] if c in sp500.columns]
        sp500 = sp500[keep_cols].dropna(subset=["Ticker"]).reset_index(drop=True)

        if len(sp500) < 400:
            raise RuntimeError(f"Parsed too few constituents from API source: {len(sp500)}")

        return sp500

    except Exception as e:
        last_error = f"API method failed: {e}"

    try:
        r = requests.get(direct_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))

        sp500 = None
        for df in tables:
            if "Symbol" in df.columns:
                sp500 = df.copy()
                break

        if sp500 is None:
            raise RuntimeError("Direct page returned, but no table with 'Symbol' column found.")

        sp500 = sp500.rename(columns={
            "Symbol": "Ticker",
            "Security": "Company",
            "GICS Sector": "Sector",
            "GICS Sub-Industry": "SubIndustry"
        })

        sp500["Ticker"] = normalize_ticker_series(sp500["Ticker"])

        keep_cols = [c for c in ["Ticker", "Company", "Sector", "SubIndustry"] if c in sp500.columns]
        sp500 = sp500[keep_cols].dropna(subset=["Ticker"]).reset_index(drop=True)

        if len(sp500) < 400:
            raise RuntimeError(f"Parsed too few constituents from direct source: {len(sp500)}")

        return sp500

    except Exception as e:
        last_error = f"{last_error} | Direct method failed: {e}"

    raise RuntimeError(f"Unable to fetch S&P 500 constituents. {last_error}")


# ────────────────────────────────────────────────────────────────
# NASDAQ-100
# ────────────────────────────────────────────────────────────────
def get_nasdaq100_constituents():
    tables = fetch_html_tables("https://en.wikipedia.org/wiki/Nasdaq-100")

    for df in tables:
        cols = [str(c) for c in df.columns]
        if "Ticker" in cols and ("Company" in cols or "Name" in cols):
            out = df.copy()
            if "Name" in out.columns and "Company" not in out.columns:
                out = out.rename(columns={"Name": "Company"})
            if "Sector" not in out.columns:
                out["Sector"] = out["GICS Sector"] if "GICS Sector" in out.columns else ""
            if "SubIndustry" not in out.columns:
                out["SubIndustry"] = out["GICS Sub-Industry"] if "GICS Sub-Industry" in out.columns else ""
            out["Ticker"] = normalize_ticker_series(out["Ticker"])
            return out[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)

    raise RuntimeError("Nasdaq-100 table not found.")


# ────────────────────────────────────────────────────────────────
# DOW 30
# ────────────────────────────────────────────────────────────────
def get_dow30_constituents():
    tables = fetch_html_tables("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")

    for df in tables:
        cols = [str(c) for c in df.columns]
        if "Symbol" in cols and "Company" in cols:
            out = df.rename(columns={"Symbol": "Ticker"}).copy()
            if "Sector" not in out.columns:
                out["Sector"] = ""
            if "SubIndustry" not in out.columns:
                out["SubIndustry"] = ""
            out["Ticker"] = normalize_ticker_series(out["Ticker"])
            return out[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)

    raise RuntimeError("Dow 30 table not found.")


# ────────────────────────────────────────────────────────────────
# RUSSELL 1000
# ────────────────────────────────────────────────────────────────
def get_russell1000_constituents():
    tables = fetch_html_tables("https://en.wikipedia.org/wiki/Russell_1000_Index")

    for df in tables:
        cols = [str(c) for c in df.columns]
        if "Company" in cols and "Ticker" in cols:
            out = df.copy()
            if "Sector" not in out.columns:
                out["Sector"] = ""
            if "SubIndustry" not in out.columns:
                out["SubIndustry"] = ""
            out["Ticker"] = normalize_ticker_series(out["Ticker"])
            return out[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)

    raise RuntimeError("Russell 1000 table not found.")


# ────────────────────────────────────────────────────────────────
# S&P 400
# ────────────────────────────────────────────────────────────────
def get_sp400_constituents():
    tables = fetch_html_tables("https://en.wikipedia.org/wiki/S%26P_400")

    for df in tables:
        cols = [str(c) for c in df.columns]
        if "Symbol" in cols and "Security" in cols:
            out = df.rename(columns={
                "Symbol": "Ticker",
                "Security": "Company",
                "GICS Sector": "Sector",
                "GICS Sub-Industry": "SubIndustry"
            }).copy()
            out["Ticker"] = normalize_ticker_series(out["Ticker"])
            return out[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)

    raise RuntimeError("S&P 400 table not found.")


# ────────────────────────────────────────────────────────────────
# S&P 600
# ────────────────────────────────────────────────────────────────
def get_sp600_constituents():
    tables = fetch_html_tables("https://en.wikipedia.org/wiki/S%26P_600")

    for df in tables:
        cols = [str(c) for c in df.columns]
        if "Symbol" in cols and ("Company" in cols or "Security" in cols):
            out = df.copy()
            if "Security" in out.columns and "Company" not in out.columns:
                out = out.rename(columns={"Security": "Company"})
            out = out.rename(columns={"Symbol": "Ticker"})
            if "Sector" not in out.columns:
                out["Sector"] = out["GICS Sector"] if "GICS Sector" in out.columns else ""
            if "SubIndustry" not in out.columns:
                out["SubIndustry"] = out["GICS Sub-Industry"] if "GICS Sub-Industry" in out.columns else ""
            out["Ticker"] = normalize_ticker_series(out["Ticker"])
            return out[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)

    raise RuntimeError("S&P 600 table not found.")


# ────────────────────────────────────────────────────────────────
# NASDAQ BROAD LIST
# ────────────────────────────────────────────────────────────────
def get_nasdaq_listed_common():
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    lines = r.text.splitlines()
    rows = [line.split("|") for line in lines if "|" in line]
    df = pd.DataFrame(rows[1:], columns=rows[0])

    df = df[df["Symbol"] != "File Creation Time"].copy()
    df = df[df["ETF"] == "N"].copy()
    df["Ticker"] = normalize_ticker_series(df["Symbol"])
    df["Company"] = df["Security Name"]
    df["Sector"] = ""
    df["SubIndustry"] = ""

    return df[["Ticker", "Company", "Sector", "SubIndustry"]].dropna(subset=["Ticker"]).reset_index(drop=True)


def get_nasdaq_composite_proxy():
    return get_nasdaq_listed_common()


def get_nasdaq1000_proxy():
    base = get_nasdaq_listed_common()
    caps = []

    for i, ticker in enumerate(base["Ticker"].tolist(), start=1):
        try:
            info = yf.Ticker(ticker).fast_info
            caps.append((ticker, info.get("marketCap", None)))
        except Exception:
            caps.append((ticker, None))

        if i % 250 == 0:
            print(f"Ranking Nasdaq universe by market cap: {i}/{len(base)}", end="\r")

    cap_df = pd.DataFrame(caps, columns=["Ticker", "MarketCap"]).dropna()
    top = cap_df.sort_values("MarketCap", ascending=False).head(1000)[["Ticker"]]
    out = base.merge(top, on="Ticker", how="inner").reset_index(drop=True)
    return out


# ────────────────────────────────────────────────────────────────
# RUSSELL 2000 / IWM PROXY
# ────────────────────────────────────────────────────────────────
def get_russell2000_proxy():
    return get_sp600_constituents()

def get_iwm_proxy():
    return get_russell2000_proxy()


# ────────────────────────────────────────────────────────────────
# TOTAL U.S. PROXY
# ────────────────────────────────────────────────────────────────
def get_total_us_proxy():
    sp500 = get_sp500_constituents()
    sp400 = get_sp400_constituents()
    sp600 = get_sp600_constituents()
    nasdaq = get_nasdaq_listed_common()

    out = pd.concat([sp500, sp400, sp600, nasdaq], ignore_index=True)
    out = out.drop_duplicates(subset=["Ticker"]).reset_index(drop=True)
    return out


# ────────────────────────────────────────────────────────────────
# UNIVERSE ROUTER
# ────────────────────────────────────────────────────────────────
def get_universe(choice):
    choice = str(choice).upper().strip()

    if choice == "SP500":
        return "S&P 500", "SP500", get_sp500_constituents()
    elif choice == "NASDAQ100":
        return "Nasdaq-100", "NASDAQ100", get_nasdaq100_constituents()
    elif choice == "DOW30":
        return "Dow 30", "DOW30", get_dow30_constituents()
    elif choice == "RUSSELL1000":
        return "Russell 1000", "RUSSELL1000", get_russell1000_constituents()
    elif choice == "RUSSELL2000":
        return "Russell 2000 Proxy", "RUSSELL2000", get_russell2000_proxy()
    elif choice == "IWM":
        return "IWM / Russell 2000 Proxy", "IWM", get_iwm_proxy()
    elif choice == "SP400":
        return "S&P 400", "SP400", get_sp400_constituents()
    elif choice == "SP600":
        return "S&P 600", "SP600", get_sp600_constituents()
    elif choice == "NASDAQ_COMPOSITE":
        return "Nasdaq Composite Proxy", "NASDAQ_COMPOSITE", get_nasdaq_composite_proxy()
    elif choice == "NASDAQ1000":
        return "Nasdaq-1000 Proxy", "NASDAQ1000", get_nasdaq1000_proxy()
    elif choice == "TOTAL_US":
        return "Total U.S. Proxy", "TOTAL_US", get_total_us_proxy()
    else:
        raise ValueError(
            "Unsupported INDEX_CHOICE. Use one of: "
            "SP500, NASDAQ100, DOW30, RUSSELL1000, RUSSELL2000, IWM, "
            "SP400, SP600, NASDAQ_COMPOSITE, NASDAQ1000, TOTAL_US"
        )


# ────────────────────────────────────────────────────────────────
# DATA DOWNLOAD
# ────────────────────────────────────────────────────────────────
def download_data(ticker, days, interval="1d"):
    start = END_DATE - timedelta(days=days)
    try:
        df = yf.download(
            ticker,
            start=start,
            end=END_DATE,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False
        )

        if df.empty or len(df) < 30:
            return None

        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        needed = ["Open", "High", "Low", "Close", "Volume"]

        if not all(col in df.columns for col in needed):
            return None

        return df[needed].dropna()

    except Exception:
        return None


# ────────────────────────────────────────────────────────────────
# INDICATORS
# ────────────────────────────────────────────────────────────────
def ema(arr, period):
    return pd.Series(arr).ewm(span=period, adjust=False).mean().values

def rsi_calc(arr, period=14):
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gains).ewm(span=period, adjust=False).mean().values
    avg_loss = pd.Series(losses).ewm(span=period, adjust=False).mean().values
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - 100 / (1 + rs)

def macd_calc(arr, fast=12, slow=26, signal=9):
    fast_ema = pd.Series(arr).ewm(span=fast, adjust=False).mean()
    slow_ema = pd.Series(arr).ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line.values, signal_line.values, hist.values

def obv_calc(close, volume):
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return obv

def fmt_price(x):
    try:
        return round(float(x), 2)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────
# PIVOT DETECTION (Pine-style confirmed pivots)
# ────────────────────────────────────────────────────────────────
def detect_confirmed_pivots(df, left_bars=5, right_bars=5):
    if df is None or len(df) < left_bars + right_bars + 3:
        return pd.DataFrame(columns=[
            "PivotType", "PivotBar", "ConfirmBar",
            "PivotDate", "ConfirmDate", "PivotPrice"
        ])

    highs = df["High"].values
    lows = df["Low"].values
    idx = df.index
    rows = []

    for i in range(left_bars, len(df) - right_bars):
        hi_window = highs[i - left_bars:i + right_bars + 1]
        lo_window = lows[i - left_bars:i + right_bars + 1]

        is_pivot_high = highs[i] == np.max(hi_window) and np.sum(hi_window == highs[i]) == 1
        is_pivot_low = lows[i] == np.min(lo_window) and np.sum(lo_window == lows[i]) == 1

        confirm_i = i + right_bars

        if is_pivot_high:
            rows.append({
                "PivotType": "HIGH",
                "PivotBar": i,
                "ConfirmBar": confirm_i,
                "PivotDate": idx[i],
                "ConfirmDate": idx[confirm_i],
                "PivotPrice": float(highs[i]),
            })

        if is_pivot_low:
            rows.append({
                "PivotType": "LOW",
                "PivotBar": i,
                "ConfirmBar": confirm_i,
                "PivotDate": idx[i],
                "ConfirmDate": idx[confirm_i],
                "PivotPrice": float(lows[i]),
            })

    return pd.DataFrame(rows)


def recent_pivot_summary(df, left_bars=5, right_bars=5, near_bars=2):
    out = {
        "Pivot_Low": "",
        "Pivot_Low_Date": "",
        "Pivot_Low_Price": "",
        "Pivot_Low_Bars_Ago": "",
        "Pivot_High": "",
        "Pivot_High_Date": "",
        "Pivot_High_Price": "",
        "Pivot_High_Bars_Ago": "",
    }

    piv = detect_confirmed_pivots(df, left_bars=left_bars, right_bars=right_bars)
    if piv.empty:
        return out

    last_bar_num = len(df) - 1

    lows = piv[piv["PivotType"] == "LOW"].copy()
    highs = piv[piv["PivotType"] == "HIGH"].copy()

    if not lows.empty:
        lows["BarsAgoFromConfirmation"] = last_bar_num - lows["ConfirmBar"]
        recent_lows = lows[lows["BarsAgoFromConfirmation"] <= near_bars].sort_values("ConfirmBar", ascending=False)
        if not recent_lows.empty:
            r = recent_lows.iloc[0]
            out["Pivot_Low"] = "YES"
            out["Pivot_Low_Date"] = str(pd.to_datetime(r["PivotDate"]).date())
            out["Pivot_Low_Price"] = round(float(r["PivotPrice"]), 2)
            out["Pivot_Low_Bars_Ago"] = int(r["BarsAgoFromConfirmation"])

    if not highs.empty:
        highs["BarsAgoFromConfirmation"] = last_bar_num - highs["ConfirmBar"]
        recent_highs = highs[highs["BarsAgoFromConfirmation"] <= near_bars].sort_values("ConfirmBar", ascending=False)
        if not recent_highs.empty:
            r = recent_highs.iloc[0]
            out["Pivot_High"] = "YES"
            out["Pivot_High_Date"] = str(pd.to_datetime(r["PivotDate"]).date())
            out["Pivot_High_Price"] = round(float(r["PivotPrice"]), 2)
            out["Pivot_High_Bars_Ago"] = int(r["BarsAgoFromConfirmation"])

    return out


# ────────────────────────────────────────────────────────────────
# GUIDANCE
# ────────────────────────────────────────────────────────────────
def wave_guidance(state, close, f382, f500, f618, f786, f100, f161, hi52, lo52, m20, m50, m200):
    s = str(state).upper()

    current_target = None
    ideal_entry = None
    pullback_zone = None
    stop_loss = None
    extension_target = None
    invalidation = None
    if_extended_then = ""
    beginner_note = ""
    trade_quality = ""

    if "WAVE 2" in s:
        current_target = hi52
        ideal_entry = f618
        pullback_zone = f"{fmt_price(f618)} - {fmt_price(f786)}"
        stop_loss = f100
        invalidation = f100
        extension_target = hi52 + (hi52 - lo52) * 0.272
        if_extended_then = (
            f"If price rebounds strongly from this correction, first target is the old high near {fmt_price(hi52)}. "
            f"If momentum expands further, the move can extend toward {fmt_price(extension_target)}."
        )
        beginner_note = (
            "This is one of the best beginner-style entries because you are buying a pullback, not chasing a rally. "
            "Enter near the zone and keep the stop below invalidation."
        )
        trade_quality = "HIGH QUALITY DIP BUY"

    elif "ACCUMULATION" in s or "CAPITULATION" in s:
        current_target = hi52
        ideal_entry = f500 if "ACCUMULATION" in s else close
        pullback_zone = f"{fmt_price(f500)} - {fmt_price(f618)}"
        stop_loss = lo52
        invalidation = lo52
        extension_target = hi52 + (hi52 - lo52) * 0.618
        if_extended_then = (
            f"If the stock truly starts a new cycle, it can reclaim the old high and later extend toward {fmt_price(extension_target)}."
        )
        beginner_note = (
            "This is an early-cycle opportunity. It can be rewarding, but it may not move immediately. "
            "Start small and only add after price confirms strength."
        )
        trade_quality = "EARLY OPPORTUNITY"

    elif "WAVE 3" in s or "3RD OF 3RD" in s or "BREAKOUT" in s:
        current_target = f161 if f161 is not None else hi52
        ideal_entry = m20 if close > m20 else close
        pullback_zone = f"{fmt_price(m20)} - {fmt_price(m50)}"
        stop_loss = m50
        invalidation = m50
        extension_target = hi52 + (hi52 - lo52) * 0.618
        if_extended_then = (
            f"If this Wave 3 becomes extended, price can run beyond the normal target and push toward {fmt_price(extension_target)}."
        )
        beginner_note = (
            "This is usually the strongest trend phase. Beginners should avoid buying after a giant green candle. "
            "Wait for a pullback toward the 20 EMA or 50 EMA."
        )
        trade_quality = "TREND FOLLOWING — VERY GOOD"

    elif "TRIANGLE" in s:
        current_target = hi52
        ideal_entry = close
        pullback_zone = f"{fmt_price(f382)} - {fmt_price(f500)}"
        stop_loss = f618
        invalidation = f618
        extension_target = f161
        if_extended_then = (
            f"If the triangle resolves upward with volume, the thrust can first target {fmt_price(hi52)} and then possibly extend toward {fmt_price(f161)}."
        )
        beginner_note = "Do not force a trade inside a tight range. Wait for breakout confirmation."
        trade_quality = "WAIT FOR CONFIRMATION"

    elif "WAVE 5" in s or "LATE BULL" in s:
        current_target = hi52
        ideal_entry = ""
        pullback_zone = f"{fmt_price(m20)} - {fmt_price(m50)}"
        stop_loss = m50
        invalidation = m50
        extension_target = hi52 + (hi52 - lo52) * 0.272
        if_extended_then = (
            f"Wave 5 can still extend toward {fmt_price(extension_target)}, but the risk/reward is worse and reversals can happen quickly."
        )
        beginner_note = "This is not the safest fresh entry for a beginner. Better to protect profits or wait for the next correction."
        trade_quality = "LOWER QUALITY — DO NOT CHASE"

    elif "BLOWOFF" in s or "TRAP" in s or "BEAR" in s or "EXIT" in s:
        current_target = m50
        ideal_entry = ""
        pullback_zone = ""
        stop_loss = ""
        invalidation = ""
        extension_target = m200
        if_extended_then = "This is not a healthy bullish extension setup. If weakness continues, price may retrace deeper toward major support."
        beginner_note = "Avoid fresh buying here. Let the chart reset and wait for a cleaner setup."
        trade_quality = "AVOID NEW ENTRY"

    else:
        current_target = hi52
        ideal_entry = f500
        pullback_zone = f"{fmt_price(f500)} - {fmt_price(f618)}"
        stop_loss = f786
        invalidation = f100
        extension_target = f161
        if_extended_then = f"If bullish momentum improves, price may move toward {fmt_price(f161)}. If it breaks invalidation, wait for a better setup."
        beginner_note = "This setup is not fully mature yet. Wait for either a clearer pullback or a confirmed breakout."
        trade_quality = "MEDIUM / WAIT"

    return {
        "Current_Wave_Target": fmt_price(current_target) if current_target != "" else "",
        "Ideal_Entry_Price": fmt_price(ideal_entry) if ideal_entry not in ["", None] else "",
        "Pullback_Buy_Zone": pullback_zone,
        "Stop_Loss": fmt_price(stop_loss) if stop_loss not in ["", None] else "",
        "Invalidation_Level": fmt_price(invalidation) if invalidation not in ["", None] else "",
        "Extension_Target": fmt_price(extension_target) if extension_target not in ["", None] else "",
        "If_Extended_Then": if_extended_then,
        "Beginner_Note": beginner_note,
        "Trade_Quality": trade_quality
    }


# ────────────────────────────────────────────────────────────────
# SCREENING ENGINE
# ────────────────────────────────────────────────────────────────
def ew_screen(df, horizon="long"):
    if df is None or len(df) < 50:
        return None

    c = df["Close"].values
    h = df["High"].values
    l = df["Low"].values
    v = df["Volume"].values
    n = len(c)

    p200 = min(200, max(20, n // 2))
    p50 = min(50, max(10, n // 3))
    p20 = 20 if n >= 20 else max(5, n // 4)

    m200 = ema(c, p200)
    m50 = ema(c, p50)
    m20 = ema(c, p20)

    above_200 = c[-1] > m200[-1]
    above_50 = c[-1] > m50[-1]
    above_20 = c[-1] > m20[-1]

    bull_ma_score = int(above_200) + int(above_50) + int(above_20)
    ma_bull_align = m200[-1] < m50[-1] < m20[-1] < c[-1]
    ma_bear_align = m200[-1] > m50[-1] > m20[-1] > c[-1]

    rsi_arr = rsi_calc(c)
    if len(rsi_arr) == 0:
        return None

    rsi_cur = rsi_arr[-1]
    rsi_prev = rsi_arr[-5] if len(rsi_arr) > 5 else rsi_cur
    rsi_rising = rsi_cur > rsi_prev

    macd_line, macd_signal, macd_hist = macd_calc(c)
    macd_bull = macd_line[-1] > macd_signal[-1] and macd_line[-1] > 0
    macd_bear = macd_line[-1] < macd_signal[-1] and macd_line[-1] < 0
    macd_expanding = len(macd_hist) > 3 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]
    macd_shrinking = len(macd_hist) > 3 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]
    macd_div_bull = macd_line[-1] > macd_signal[-1] and macd_line[-1] < 0
    macd_div_bear = macd_line[-1] < macd_signal[-1] and macd_line[-1] > 0
    macd_cross_up = len(macd_line) > 2 and macd_line[-1] > macd_signal[-1] and macd_line[-2] <= macd_signal[-2]

    vol_ma20 = pd.Series(v).rolling(20).mean().values
    vol_ratio = v[-1] / (vol_ma20[-1] + 1e-6) if len(vol_ma20) else 0
    vol_rising = np.mean(v[-5:]) > np.mean(v[-20:]) if len(v) >= 20 else False
    vol_dry = vol_ratio < 0.75
    vol_surge = vol_ratio > 1.5
    vol_climax = vol_ratio > 2.5

    obv = obv_calc(c, v)
    obv_ma = pd.Series(obv).rolling(20).mean().values
    obv_rising = obv[-1] > obv_ma[-1] and obv[-1] > obv[-5] if len(obv) > 20 and len(obv_ma) > 0 else False
    obv_bull_div = c[-1] < c[-10] and obv[-1] > obv[-10] if len(c) > 10 else False
    obv_bear_div = c[-1] > c[-10] and obv[-1] < obv[-10] if len(c) > 10 else False

    lb = min(100, n)
    swg_hi = np.max(h[-lb:])
    swg_lo = np.min(l[-lb:])
    fib_range = swg_hi - swg_lo if swg_hi > swg_lo else 0.0001

    f382 = swg_hi - fib_range * 0.382
    f500 = swg_hi - fib_range * 0.500
    f618 = swg_hi - fib_range * 0.618
    f786 = swg_hi - fib_range * 0.786
    f100 = swg_lo
    f161 = swg_hi + fib_range * 0.618

    in_w2_zone = (f618 * 0.97 <= c[-1] <= f618 * 1.03) and above_200
    near_bottom = c[-1] <= f100 * 1.05 and not above_50

    hi52 = np.max(h[-252:]) if n >= 252 else np.max(h)
    lo52 = np.min(l[-252:]) if n >= 252 else np.min(l)
    pct_from_hi = (hi52 - c[-1]) / hi52 * 100 if hi52 != 0 else 0
    pct_from_lo = (c[-1] - lo52) / lo52 * 100 if lo52 != 0 else 0

    range10 = (np.max(h[-10:]) - np.min(l[-10:])) if n >= 10 else 0
    range20 = (np.max(h[-20:]) - np.min(l[-20:])) if n >= 20 else 0
    range40 = (np.max(h[-40:]) - np.min(l[-40:])) if n >= 40 else 0
    range_contracting = range20 > 0 and range40 > 0 and range10 < range20 * 0.7 and range20 < range40 * 0.7

    selling_climax = vol_climax and not above_50 and rsi_cur < 30
    blowoff = vol_climax and above_200 and rsi_cur > 75
    wave3_active = vol_surge and macd_bull and macd_expanding and bull_ma_score == 3
    wave5_warn = (
        c[-1] >= np.max(c[-20:]) * 0.995 and
        vol_dry and
        (rsi_cur > 65 or obv_bear_div) and
        macd_div_bear and
        above_200
    )
    b_trap = (not above_200) and c[-1] > c[-5] and macd_shrinking and vol_ratio < 1.1 if len(c) > 5 else False
    accumulation = above_50 and not above_200 and 35 < rsi_cur < 58 and obv_rising

    rule1_ok = c[-1] > f100
    w4_overlap_risk = above_200 and c[-1] < f786 and 35 < rsi_cur < 50

    pivot_info = recent_pivot_summary(
        df,
        left_bars=PIVOT_LEFT_BARS,
        right_bars=PIVOT_RIGHT_BARS,
        near_bars=PIVOT_NEAR_BARS
    )

    score = 0
    signals = []

    if horizon == "long":
        if selling_climax:
            score += 20; signals.append("Selling Climax")
        if near_bottom:
            score += 15; signals.append("Near Cycle Bottom")
        if obv_bull_div:
            score += 15; signals.append("OBV Bullish Divergence")
        if macd_div_bull:
            score += 10; signals.append("MACD Below-Zero Bull Cross")
        if ma_bull_align:
            score += 10; signals.append("All MAs Bull Aligned")
        if bull_ma_score >= 2:
            score += 10; signals.append(f"Dow Score {bull_ma_score}/3")
        if rsi_cur < 40 and rsi_rising:
            score += 10; signals.append("RSI Recovering from Oversold")
        if pct_from_lo < 25:
            score += 5; signals.append("Near 52-Week Low")
        if above_200:
            score += 5; signals.append("Above 200 EMA")

        if selling_climax and obv_bull_div:
            state = "CAPITULATION — Bull Cycle Starting"
        elif accumulation or (near_bottom and obv_rising):
            state = "ACCUMULATION — Dow Phase 1"
        elif ma_bull_align and macd_bull and macd_expanding:
            state = "WAVE 3 — Strongest Wave (Ride It)"
        elif bull_ma_score >= 2 and (macd_div_bull or in_w2_zone):
            state = "WAVE 2 Buy Zone — Best Long Entry"
        elif wave5_warn:
            state = "WAVE 5 — Late Bull, Start Trimming"
        elif blowoff:
            state = "BLOWOFF TOP — Exit All Longs"
        elif ma_bear_align:
            state = "BEAR MARKET — Avoid / Cash"
        elif b_trap:
            state = "B-WAVE BULL TRAP — Fake Rally"
        else:
            state = "TRANSITIONING / Sideways"

        action = (
            "BUY / BUILD POSITION" if any(x in state for x in ["CAPITULATION", "ACCUMULATION", "WAVE 2"])
            else "HOLD / ADD DIPS" if "WAVE 3" in state
            else "TRIM / EXIT" if any(x in state for x in ["WAVE 5", "BLOWOFF", "BEAR", "TRAP"])
            else "WATCH"
        )
        hold_period = "1-5 Years"
        risk = f"Stop: 200 EMA ({fmt_price(m200[-1])}) | Wave2 invalidation: {fmt_price(f100)}"

    elif horizon == "mid":
        if in_w2_zone:
            score += 20; signals.append("In Wave 2 61.8% Zone")
        if obv_bull_div:
            score += 15; signals.append("OBV Bullish Divergence")
        if macd_div_bull or macd_cross_up:
            score += 15; signals.append("MACD Bullish Cross")
        if bull_ma_score >= 2:
            score += 10; signals.append(f"Dow Score {bull_ma_score}/3")
        if 40 <= rsi_cur <= 60 and rsi_rising:
            score += 10; signals.append("RSI Neutral Rising")
        if vol_rising:
            score += 10; signals.append("Volume Expanding")
        if above_50 and not above_200:
            score += 10; signals.append("Reclaiming 50 EMA")
        if range_contracting:
            score += 5; signals.append("Triangle Compression")
        if pct_from_hi < 30 and above_200:
            score += 5; signals.append("Near High in Bull Trend")

        if in_w2_zone and obv_bull_div:
            state = "WAVE 2 Buy Zone (Best Risk:Reward)"
        elif macd_cross_up and above_50 and vol_rising:
            state = "WAVE 3 Starting — Enter Now"
        elif wave3_active:
            state = "WAVE 3 Active — Hold / Trail"
        elif range_contracting and above_50:
            state = "TRIANGLE — Wait for Thrust"
        elif macd_bull and ma_bull_align:
            state = "WAVE 3 Active"
        elif wave5_warn:
            state = "WAVE 5 — Reduce Risk"
        elif b_trap:
            state = "B-WAVE TRAP — Stand Aside"
        elif accumulation:
            state = "ACCUMULATION — Build Slowly"
        else:
            state = "WATCH LIST — Setup Forming"

        action = (
            "BUY HERE" if "WAVE 2" in state
            else "ENTER LONG" if "Starting" in state
            else "HOLD" if "WAVE 3 Active" in state or "Hold" in state
            else "BUY ON BREAKOUT" if "TRIANGLE" in state
            else "REDUCE / EXIT" if any(x in state for x in ["WAVE 5", "TRAP"])
            else "WATCH"
        )
        hold_period = "2 Weeks - 4 Months"
        risk = f"Stop: 61.8% Fib ({fmt_price(f618)}) | 50 EMA ({fmt_price(m50[-1])})"

    else:
        if macd_bull and macd_expanding:
            score += 25; signals.append("MACD Bull + Expanding")
        if bull_ma_score == 3:
            score += 20; signals.append("All 3 MAs Bullish")
        if vol_ratio > 1.5:
            score += 15; signals.append(f"Volume Surge {round(vol_ratio,1)}x")
        if 55 <= rsi_cur <= 75:
            score += 10; signals.append("RSI Bullish Momentum")
        if obv_rising:
            score += 10; signals.append("OBV Rising")
        if macd_cross_up:
            score += 10; signals.append("Fresh MACD Bull Cross")
        if above_20 and above_50:
            score += 5; signals.append("Price Above 20 & 50 EMA")
        if vol_ratio > 2.0:
            score += 5; signals.append("Volume Climax Confirmation")

        if macd_bull and macd_expanding and bull_ma_score == 3 and vol_surge:
            state = "3rd of 3rd Wave — DO NOT SHORT"
        elif macd_cross_up and vol_ratio > 1.3 and above_50:
            state = "WAVE 3 Breakout — Enter Now"
        elif range_contracting and macd_cross_up:
            state = "TRIANGLE THRUST — Buy Breakout"
        elif macd_bull and vol_rising and bull_ma_score >= 2:
            state = "WAVE 3 Active — Ride It"
        elif wave5_warn:
            state = "WAVE 5 TOP — Start Exiting"
        elif blowoff:
            state = "BLOWOFF — Exit Immediately"
        else:
            state = "SETUP FORMING — Wait for Signal"

        action = (
            "BUY NOW / LONG" if any(x in state for x in ["3rd of 3rd", "Breakout", "THRUST"])
            else "HOLD LONG" if "WAVE 3 Active" in state or "Ride It" in state
            else "EXIT / SHORT" if any(x in state for x in ["WAVE 5", "BLOWOFF"])
            else "WAIT"
        )
        hold_period = "Hours - 2 Weeks"
        risk = f"Trail: 20 EMA ({fmt_price(m20[-1])}) | Stop: 50 EMA ({fmt_price(m50[-1])})"

    explain = wave_guidance(
        state=state,
        close=c[-1],
        f382=f382,
        f500=f500,
        f618=f618,
        f786=f786,
        f100=f100,
        f161=f161,
        hi52=hi52,
        lo52=lo52,
        m20=m20[-1],
        m50=m50[-1],
        m200=m200[-1]
    )

    return {
        "EW_Score": min(score, 100),
        "Trade_Quality": explain["Trade_Quality"],
        "Elliott_State": state,
        "Current_Price": fmt_price(c[-1]),
        "Current_Wave_Target": explain["Current_Wave_Target"],
        "Ideal_Entry_Price": explain["Ideal_Entry_Price"],
        "Pullback_Buy_Zone": explain["Pullback_Buy_Zone"],
        "Stop_Loss": explain["Stop_Loss"],
        "Invalidation_Level": explain["Invalidation_Level"],
        "Extension_Target": explain["Extension_Target"],
        "If_Extended_Then": explain["If_Extended_Then"],
        "Beginner_Note": explain["Beginner_Note"],
        "RSI": round(float(rsi_cur), 1),
        "Vol_Ratio": round(float(vol_ratio), 2),
        "Dow_MA_Score": f"{bull_ma_score}/3",
        "OBV_Divergence": "YES" if obv_bull_div else "No",
        "MACD_Signal": "BULL" if macd_bull else "CROSS↑" if macd_cross_up else "BEAR" if macd_bear else "NEUTRAL",
        "Pct_from_52w_High": round(float(pct_from_hi), 1),
        "Pct_from_52w_Low": round(float(pct_from_lo), 1),
        "Key_Signals": ", ".join(signals) if signals else "None",
        "52w_High": fmt_price(hi52),
        "52w_Low": fmt_price(lo52),
        "Fib_61.8%": fmt_price(f618),
        "Fib_38.2%": fmt_price(f382),
        "Fib_161.8%_Target": fmt_price(f161),
        "Recommended_Action": action,
        "Hold_Period": hold_period,
        "Risk_Management": risk,
        "Rule1_Wave2_OK": "OK" if rule1_ok else "VIOLATED",
        "W4_Overlap_Risk": "YES" if w4_overlap_risk else "No",
        "Pivot_Low": pivot_info["Pivot_Low"],
        "Pivot_Low_Date": pivot_info["Pivot_Low_Date"],
        "Pivot_Low_Price": pivot_info["Pivot_Low_Price"],
        "Pivot_Low_Bars_Ago": pivot_info["Pivot_Low_Bars_Ago"],
        "Pivot_High": pivot_info["Pivot_High"],
        "Pivot_High_Date": pivot_info["Pivot_High_Date"],
        "Pivot_High_Price": pivot_info["Pivot_High_Price"],
        "Pivot_High_Bars_Ago": pivot_info["Pivot_High_Bars_Ago"]
    }


# ────────────────────────────────────────────────────────────────
# GET UNIVERSE
# ────────────────────────────────────────────────────────────────
UNIVERSE_NAME, UNIVERSE_CODE, universe_meta = get_universe(INDEX_CHOICE)
TICKERS = universe_meta["Ticker"].tolist()

if OUTPUT_FILENAME is None:
    OUTPUT_FILENAME = f"Elliott_Wave_{UNIVERSE_CODE}_Master_Workbook.xlsx"

print("=" * 72)
print(f"ELLIOTT WAVE {UNIVERSE_NAME.upper()} MASTER WORKBOOK SCREENER")
print(f"Scan time: {datetime.today().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{UNIVERSE_NAME} tickers fetched: {len(TICKERS)}")
print("=" * 72)


# ────────────────────────────────────────────────────────────────
# RUN SCAN
# ────────────────────────────────────────────────────────────────
results_lt, results_mt, results_st = [], [], []

for i, ticker in enumerate(TICKERS, start=1):
    print(f"[{i:03d}/{len(TICKERS)}] Scanning {ticker}...", end="\r")

    meta_match = universe_meta.loc[universe_meta["Ticker"] == ticker]
    if meta_match.empty:
        company = ""
        sector = ""
        subindustry = ""
    else:
        meta_row = meta_match.iloc[0]
        company = meta_row.get("Company", "")
        sector = meta_row.get("Sector", "")
        subindustry = meta_row.get("SubIndustry", "")

    try:
        df_l = download_data(ticker, 365 * 5, interval="1wk")
        r_l = ew_screen(df_l, "long")
        if r_l:
            r_l["Ticker"] = ticker
            r_l["Company"] = company
            r_l["Sector"] = sector
            r_l["SubIndustry"] = subindustry
            results_lt.append(r_l)

        df_m = download_data(ticker, 365, interval="1d")
        r_m = ew_screen(df_m, "mid")
        if r_m:
            r_m["Ticker"] = ticker
            r_m["Company"] = company
            r_m["Sector"] = sector
            r_m["SubIndustry"] = subindustry
            results_mt.append(r_m)

        df_s = download_data(ticker, 90, interval="1d")
        r_s = ew_screen(df_s, "short")
        if r_s:
            r_s["Ticker"] = ticker
            r_s["Company"] = company
            r_s["Sector"] = sector
            r_s["SubIndustry"] = subindustry
            results_st.append(r_s)

    except Exception as e:
        print(f"\nSkip {ticker}: {e}")

print("\nScan complete.")


# ────────────────────────────────────────────────────────────────
# BUILD DATAFRAMES
# ────────────────────────────────────────────────────────────────
col_order = [
    "Ticker","Company","Sector","SubIndustry",
    "EW_Score","Trade_Quality","Elliott_State","Current_Price",
    "Current_Wave_Target","Ideal_Entry_Price","Pullback_Buy_Zone",
    "Stop_Loss","Invalidation_Level","Extension_Target","If_Extended_Then",
    "Beginner_Note","RSI","Vol_Ratio","Dow_MA_Score","OBV_Divergence","MACD_Signal",
    "Pct_from_52w_High","Pct_from_52w_Low","Key_Signals",
    "52w_High","52w_Low","Fib_61.8%","Fib_38.2%","Fib_161.8%_Target",
    "Recommended_Action","Hold_Period","Risk_Management",
    "Rule1_Wave2_OK","W4_Overlap_Risk",
    "Pivot_Low","Pivot_Low_Date","Pivot_Low_Price","Pivot_Low_Bars_Ago",
    "Pivot_High","Pivot_High_Date","Pivot_High_Price","Pivot_High_Bars_Ago"
]

df_long = pd.DataFrame(results_lt)
df_mid = pd.DataFrame(results_mt)
df_short = pd.DataFrame(results_st)

if not df_long.empty:
    df_long = df_long[col_order].sort_values("EW_Score", ascending=False).reset_index(drop=True)
if not df_mid.empty:
    df_mid = df_mid[col_order].sort_values("EW_Score", ascending=False).reset_index(drop=True)
if not df_short.empty:
    df_short = df_short[col_order].sort_values("EW_Score", ascending=False).reset_index(drop=True)


# ────────────────────────────────────────────────────────────────
# ALL 3 TIMEFRAMES BULLISH
# ────────────────────────────────────────────────────────────────
def is_bullish_state(state_text):
    if pd.isna(state_text):
        return False

    s = str(state_text).upper()

    bullish_keywords = [
        "CAPITULATION","ACCUMULATION","WAVE 2 BUY ZONE","WAVE 3",
        "3RD OF 3RD","BREAKOUT","RIDE IT","ENTER NOW","BUY BREAKOUT",
        "BUY HERE","STARTING","ACTIVE"
    ]
    bearish_keywords = [
        "BEAR","BLOWOFF","EXIT","TRAP","REDUCE","LATE BULL",
        "SIDEWAYS","TRANSITIONING","WATCH LIST","SETUP FORMING",
        "WAIT FOR SIGNAL","STAND ASIDE"
    ]

    if any(k in s for k in bearish_keywords):
        return False
    return any(k in s for k in bullish_keywords)

if not df_long.empty and not df_mid.empty and not df_short.empty:
    lt_cols = [
        "Ticker","Company","Sector","SubIndustry",
        "EW_Score","Trade_Quality","Elliott_State","Current_Price",
        "Current_Wave_Target","Ideal_Entry_Price","Stop_Loss","Extension_Target",
        "Recommended_Action","Pivot_Low","Pivot_Low_Date","Pivot_Low_Price","Pivot_Low_Bars_Ago",
        "Pivot_High","Pivot_High_Date","Pivot_High_Price","Pivot_High_Bars_Ago"
    ]
    mt_cols = lt_cols.copy()
    st_cols = lt_cols.copy()

    df_lt_m = df_long[lt_cols].copy().rename(columns={
        "EW_Score": "LT_EW_Score",
        "Trade_Quality": "LT_Trade_Quality",
        "Elliott_State": "LT_State",
        "Current_Price": "LT_Price",
        "Current_Wave_Target": "LT_Target",
        "Ideal_Entry_Price": "LT_Entry",
        "Stop_Loss": "LT_Stop",
        "Extension_Target": "LT_Extension",
        "Recommended_Action": "LT_Action",
        "Pivot_Low": "LT_Pivot_Low",
        "Pivot_Low_Date": "LT_Pivot_Low_Date",
        "Pivot_Low_Price": "LT_Pivot_Low_Price",
        "Pivot_Low_Bars_Ago": "LT_Pivot_Low_Bars_Ago",
        "Pivot_High": "LT_Pivot_High",
        "Pivot_High_Date": "LT_Pivot_High_Date",
        "Pivot_High_Price": "LT_Pivot_High_Price",
        "Pivot_High_Bars_Ago": "LT_Pivot_High_Bars_Ago"
    })

    df_mt_m = df_mid[mt_cols].copy().rename(columns={
        "EW_Score": "MT_EW_Score",
        "Trade_Quality": "MT_Trade_Quality",
        "Elliott_State": "MT_State",
        "Current_Price": "MT_Price",
        "Current_Wave_Target": "MT_Target",
        "Ideal_Entry_Price": "MT_Entry",
        "Stop_Loss": "MT_Stop",
        "Extension_Target": "MT_Extension",
        "Recommended_Action": "MT_Action",
        "Pivot_Low": "MT_Pivot_Low",
        "Pivot_Low_Date": "MT_Pivot_Low_Date",
        "Pivot_Low_Price": "MT_Pivot_Low_Price",
        "Pivot_Low_Bars_Ago": "MT_Pivot_Low_Bars_Ago",
        "Pivot_High": "MT_Pivot_High",
        "Pivot_High_Date": "MT_Pivot_High_Date",
        "Pivot_High_Price": "MT_Pivot_High_Price",
        "Pivot_High_Bars_Ago": "MT_Pivot_High_Bars_Ago"
    })

    df_st_m = df_short[st_cols].copy().rename(columns={
        "EW_Score": "ST_EW_Score",
        "Trade_Quality": "ST_Trade_Quality",
        "Elliott_State": "ST_State",
        "Current_Price": "ST_Price",
        "Current_Wave_Target": "ST_Target",
        "Ideal_Entry_Price": "ST_Entry",
        "Stop_Loss": "ST_Stop",
        "Extension_Target": "ST_Extension",
        "Recommended_Action": "ST_Action",
        "Pivot_Low": "ST_Pivot_Low",
        "Pivot_Low_Date": "ST_Pivot_Low_Date",
        "Pivot_Low_Price": "ST_Pivot_Low_Price",
        "Pivot_Low_Bars_Ago": "ST_Pivot_Low_Bars_Ago",
        "Pivot_High": "ST_Pivot_High",
        "Pivot_High_Date": "ST_Pivot_High_Date",
        "Pivot_High_Price": "ST_Pivot_High_Price",
        "Pivot_High_Bars_Ago": "ST_Pivot_High_Bars_Ago"
    })

    df_all = (
        df_lt_m.merge(df_mt_m, on=["Ticker","Company","Sector","SubIndustry"], how="inner")
               .merge(df_st_m, on=["Ticker","Company","Sector","SubIndustry"], how="inner")
    )

    df_all["LT_Bull"] = df_all["LT_State"].apply(is_bullish_state)
    df_all["MT_Bull"] = df_all["MT_State"].apply(is_bullish_state)
    df_all["ST_Bull"] = df_all["ST_State"].apply(is_bullish_state)

    df_all_bull = df_all[(df_all["LT_Bull"]) & (df_all["MT_Bull"]) & (df_all["ST_Bull"])].copy()

    df_all_bull["Triple_Bull_Score"] = (
        df_all_bull["LT_EW_Score"] * 0.40 +
        df_all_bull["MT_EW_Score"] * 0.35 +
        df_all_bull["ST_EW_Score"] * 0.25
    ).round(2)

    def alignment_label(row):
        lt = str(row["LT_State"]).upper()
        mt = str(row["MT_State"]).upper()
        st = str(row["ST_State"]).upper()

        if "WAVE 3" in lt and "WAVE 3" in mt and ("3RD OF 3RD" in st or "BREAKOUT" in st or "WAVE 3" in st):
            return "PERFECT BULL ALIGNMENT"
        if (("ACCUMULATION" in lt) or ("WAVE 2" in lt)) and (("WAVE 3" in mt) or ("STARTING" in mt)) and (("BREAKOUT" in st) or ("WAVE 3" in st)):
            return "EARLY BULL ALIGNMENT"
        return "BULL ALIGNMENT"

    def master_action(row):
        if row["Triple_Bull_Score"] >= 88:
            return "STRONG BUY"
        elif row["Triple_Bull_Score"] >= 78:
            return "BUY / ADD"
        else:
            return "WATCH FOR ENTRY"

    def professor_note(row):
        return (
            f"Long-term trend: {row['LT_State']}. "
            f"Mid-term structure: {row['MT_State']}. "
            f"Short-term timing: {row['ST_State']}. "
            f"For a beginner, use the short-term or mid-term pullback entry instead of chasing strength."
        )

    df_all_bull["Alignment_Type"] = df_all_bull.apply(alignment_label, axis=1)
    df_all_bull["Master_Action"] = df_all_bull.apply(master_action, axis=1)
    df_all_bull["Professor_Note"] = df_all_bull.apply(professor_note, axis=1)

    df_all_bull = df_all_bull[
        [
            "Ticker","Company","Sector","SubIndustry","Triple_Bull_Score","Alignment_Type","Master_Action","Professor_Note",

            "LT_EW_Score","LT_Trade_Quality","LT_State","LT_Price","LT_Target","LT_Entry","LT_Stop","LT_Extension","LT_Action",
            "LT_Pivot_Low","LT_Pivot_Low_Date","LT_Pivot_Low_Price","LT_Pivot_Low_Bars_Ago",
            "LT_Pivot_High","LT_Pivot_High_Date","LT_Pivot_High_Price","LT_Pivot_High_Bars_Ago",

            "MT_EW_Score","MT_Trade_Quality","MT_State","MT_Price","MT_Target","MT_Entry","MT_Stop","MT_Extension","MT_Action",
            "MT_Pivot_Low","MT_Pivot_Low_Date","MT_Pivot_Low_Price","MT_Pivot_Low_Bars_Ago",
            "MT_Pivot_High","MT_Pivot_High_Date","MT_Pivot_High_Price","MT_Pivot_High_Bars_Ago",

            "ST_EW_Score","ST_Trade_Quality","ST_State","ST_Price","ST_Target","ST_Entry","ST_Stop","ST_Extension","ST_Action",
            "ST_Pivot_Low","ST_Pivot_Low_Date","ST_Pivot_Low_Price","ST_Pivot_Low_Bars_Ago",
            "ST_Pivot_High","ST_Pivot_High_Date","ST_Pivot_High_Price","ST_Pivot_High_Bars_Ago"
        ]
    ].sort_values(["Triple_Bull_Score","Ticker"], ascending=[False, True]).reset_index(drop=True)
else:
    df_all_bull = pd.DataFrame()


# ────────────────────────────────────────────────────────────────
# BUY LIST BUILDERS
# ────────────────────────────────────────────────────────────────
def safe_num(x):
    try:
        return float(x)
    except Exception:
        return np.nan

def build_buy_list(df, timeframe_label):
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    out["Current_Price_num"] = out["Current_Price"].apply(safe_num)
    out["Ideal_Entry_Price_num"] = out["Ideal_Entry_Price"].apply(safe_num)
    out["Stop_Loss_num"] = out["Stop_Loss"].apply(safe_num)
    out["Current_Wave_Target_num"] = out["Current_Wave_Target"].apply(safe_num)
    out["Extension_Target_num"] = out["Extension_Target"].apply(safe_num)

    out["Pct_to_Ideal_Entry"] = np.where(
        out["Ideal_Entry_Price_num"].notna() & (out["Ideal_Entry_Price_num"] > 0),
        ((out["Current_Price_num"] - out["Ideal_Entry_Price_num"]) / out["Ideal_Entry_Price_num"] * 100).round(2),
        np.nan
    )

    out["Pct_to_Stop"] = np.where(
        out["Stop_Loss_num"].notna() & (out["Current_Price_num"] > 0),
        ((out["Current_Price_num"] - out["Stop_Loss_num"]) / out["Current_Price_num"] * 100).round(2),
        np.nan
    )

    out["Pct_to_Target"] = np.where(
        out["Current_Wave_Target_num"].notna() & (out["Current_Price_num"] > 0),
        ((out["Current_Wave_Target_num"] - out["Current_Price_num"]) / out["Current_Price_num"] * 100).round(2),
        np.nan
    )

    out["Entry_Closeness_Score"] = np.where(
        out["Pct_to_Ideal_Entry"].notna(),
        100 - np.abs(out["Pct_to_Ideal_Entry"]),
        0
    )

    buy_states = [
        "WAVE 2", "WAVE 3", "ACCUMULATION", "CAPITULATION", "BREAKOUT", "3RD OF 3RD"
    ]
    buy_actions = [
        "BUY", "ENTER LONG", "BUY HERE", "BUY NOW / LONG", "BUY / BUILD POSITION", "HOLD / ADD DIPS"
    ]
    avoid_words = [
        "EXIT", "TRAP", "BLOWOFF", "BEAR", "REDUCE", "AVOID"
    ]

    def is_buy_candidate(row):
        state = str(row.get("Elliott_State", "")).upper()
        action = str(row.get("Recommended_Action", "")).upper()
        quality = str(row.get("Trade_Quality", "")).upper()

        bullish = any(x in state for x in buy_states) or any(x in action for x in buy_actions)
        bad = any(x in state for x in avoid_words) or any(x in action for x in avoid_words)
        decent_quality = any(x in quality for x in ["HIGH QUALITY", "VERY GOOD", "EARLY", "MEDIUM"])

        return bullish and (not bad) and decent_quality

    out = out[out.apply(is_buy_candidate, axis=1)].copy()

    out["Tomorrow_Buy_Rank"] = (
        out["EW_Score"].fillna(0) * 0.45 +
        out["Entry_Closeness_Score"].fillna(0) * 0.30 +
        out["Pct_to_Target"].clip(lower=0).fillna(0) * 0.15 +
        out["Pct_to_Stop"].clip(lower=0).fillna(0) * 0.10
    ).round(2)

    out["Timeframe"] = timeframe_label

    preferred_cols = [
        "Ticker","Company","Sector","SubIndustry","Timeframe",
        "Tomorrow_Buy_Rank","EW_Score","Trade_Quality","Elliott_State",
        "Current_Price","Ideal_Entry_Price","Pct_to_Ideal_Entry",
        "Pullback_Buy_Zone","Stop_Loss","Pct_to_Stop",
        "Current_Wave_Target","Pct_to_Target","Extension_Target",
        "Recommended_Action","Beginner_Note","Key_Signals"
    ]

    preferred_cols = [c for c in preferred_cols if c in out.columns]
    out = out[preferred_cols].sort_values(
        ["Tomorrow_Buy_Rank","EW_Score","Ticker"],
        ascending=[False, False, True]
    ).reset_index(drop=True)

    return out

df_long_buys = build_buy_list(df_long, "LongTerm")
df_mid_buys = build_buy_list(df_mid, "MidTerm")
df_short_buys = build_buy_list(df_short, "ShortTerm")

df_tomorrow_buys = pd.concat(
    [df_short_buys, df_mid_buys, df_long_buys],
    ignore_index=True
).sort_values(
    ["Tomorrow_Buy_Rank","EW_Score","Ticker"],
    ascending=[False, False, True]
).reset_index(drop=True)


# ────────────────────────────────────────────────────────────────
# EXCEL HELPERS
# ────────────────────────────────────────────────────────────────
def thin_border():
    side = Side(style="thin", color="BDBDBD")
    return Border(left=side, right=side, top=side, bottom=side)

def score_fill(score):
    if score >= 88:
        return PatternFill("solid", fgColor="00C853")
    if score >= 78:
        return PatternFill("solid", fgColor="64DD17")
    if score >= 68:
        return PatternFill("solid", fgColor="FFD600")
    return PatternFill("solid", fgColor="FF6D00")

def state_fill(state):
    s = str(state).upper()
    if "3RD OF 3RD" in s or "EXPLOSIVE" in s:
        return PatternFill("solid", fgColor="00E5FF")
    if "WAVE 3" in s or "BREAKOUT" in s:
        return PatternFill("solid", fgColor="00C853")
    if "WAVE 2" in s or "BUY ZONE" in s:
        return PatternFill("solid", fgColor="FF9800")
    if "ACCUMULATION" in s or "CAPITULATION" in s:
        return PatternFill("solid", fgColor="B9F6CA")
    if "TRIANGLE" in s:
        return PatternFill("solid", fgColor="B0BEC5")
    if "BEAR" in s or "EXIT" in s or "TRAP" in s or "BLOWOFF" in s:
        return PatternFill("solid", fgColor="EF5350")
    return PatternFill("solid", fgColor="ECEFF1")

def action_fill(action_text):
    s = str(action_text).upper()
    if any(x in s for x in ["BUY","LONG","HOLD","ADD","BUILD"]):
        return PatternFill("solid", fgColor="00C853"), Font(bold=True, size=9, color="FFFFFF")
    if any(x in s for x in ["EXIT","AVOID","SELL","TRIM","SHORT","REDUCE"]):
        return PatternFill("solid", fgColor="EF5350"), Font(bold=True, size=9, color="FFFFFF")
    return PatternFill("solid", fgColor="FFD600"), Font(bold=True, size=9, color="000000")

def quality_fill(q):
    s = str(q).upper()
    if "VERY GOOD" in s or "HIGH QUALITY" in s:
        return PatternFill("solid", fgColor="00C853"), Font(bold=True, size=9, color="FFFFFF")
    if "EARLY" in s or "MEDIUM" in s:
        return PatternFill("solid", fgColor="FFD600"), Font(bold=True, size=9, color="000000")
    if "LOWER" in s or "AVOID" in s or "WAIT" in s:
        return PatternFill("solid", fgColor="FF6D00"), Font(bold=True, size=9, color="FFFFFF")
    return PatternFill("solid", fgColor="ECEFF1"), Font(bold=True, size=9, color="000000")


# ────────────────────────────────────────────────────────────────
# CHEAT SHEET TAB
# ────────────────────────────────────────────────────────────────
def add_cheat_sheet_tab(wb):
    ws = wb.create_sheet(title="Cheat_Sheet", index=0)
    ws.sheet_view.showGridLines = False

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 62

    ws.merge_cells("A1:C1")
    ws["A1"] = "ELLIOTT WAVE CHEAT SHEET — HOW TO READ THIS WORKBOOK"
    ws["A1"].font = Font(bold=True, size=15, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="263238")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:C2")
    ws["A2"] = "Read in order: State → Quality → Entry → Stop → Target → Note"
    ws["A2"].font = Font(italic=True, size=10, color="37474F")
    ws["A2"].fill = PatternFill("solid", fgColor="ECEFF1")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 22

    def section(row, title, color="1565C0"):
        ws.merge_cells(f"A{row}:C{row}")
        cell = ws[f"A{row}"]
        cell.value = title
        cell.font = Font(bold=True, size=12, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=color)
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[row].height = 22

    def headers(row):
        for col, text in zip(["A", "B", "C"], ["What you see", "What it means", "What you should do"]):
            c = ws[f"{col}{row}"]
            c.value = text
            c.font = Font(bold=True, size=10, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="455A64")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = thin_border()
        ws.row_dimensions[row].height = 22

    def add_row(row, a, b, c, fill="FFFFFF"):
        for col, val in zip(["A", "B", "C"], [a, b, c]):
            cell = ws[f"{col}{row}"]
            cell.value = val
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.font = Font(size=10, color="000000")
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            cell.border = thin_border()
        ws.row_dimensions[row].height = 48

    section(4, "1) BEST ORDER TO READ A STOCK")
    headers(5)
    add_row(6, "Elliott_State", "Current phase of the stock cycle", "This is the first thing to read.")
    add_row(7, "Trade_Quality", "How clean or risky the setup is", "Prefer HIGH QUALITY or VERY GOOD first.")
    add_row(8, "Ideal_Entry_Price / Pullback_Buy_Zone", "Best entry area", "Do not chase far above these levels.")
    add_row(9, "Stop_Loss / Invalidation_Level", "Where the idea is wrong", "If price breaks this, exit and reassess.")
    add_row(10, "Current_Wave_Target / Extension_Target", "Likely target and stronger scenario", "Use as target zones, not guaranteed exact prices.")

    section(12, "2) HOW TO INTERPRET MAIN STATES", color="2E7D32")
    headers(13)
    add_row(14, "ACCUMULATION", "Early base building after weakness", "Good watchlist candidate; enter small or wait for more confirmation.", "F1F8E9")
    add_row(15, "CAPITULATION", "Panic may be ending", "High opportunity but high risk. Buy small, never all-in.", "F1F8E9")
    add_row(16, "WAVE 2 Buy Zone", "Pullback inside bullish structure", "Best beginner setup. Buy near entry zone, not far above it.", "FFF8E1")
    add_row(17, "WAVE 3 Starting", "Trend is beginning to accelerate", "Good setup. Enter on confirmation or a small pullback.", "E8F5E9")
    add_row(18, "WAVE 3 Active", "Strong trend already underway", "Great if already in. If new, wait for a pullback instead of chasing.", "E8F5E9")
    add_row(19, "3rd of 3rd Wave", "Explosive momentum", "Good for holders, risky for late buyers. Wait for pause/pullback.", "E0F7FA")
    add_row(20, "TRIANGLE", "Compression before possible thrust", "Wait for breakout confirmation; do not guess.", "ECEFF1")
    add_row(21, "WAVE 5", "Late-stage uptrend", "Avoid chasing. Better to protect profits or wait for correction.", "FFF3E0")
    add_row(22, "BLOWOFF / TRAP / BEAR", "Exhaustion or failed bullish structure", "Avoid fresh long entries.", "FFEBEE")

    section(24, "3) SIMPLE ACTION RULES", color="6A1B9A")
    headers(25)
    add_row(26, "BUY / Consider Entry", "State is bullish + quality is good + price near entry zone", "Best case: Wave 2 Buy Zone or Wave 3 Starting.")
    add_row(27, "WAIT", "Price is too far above ideal entry or setup is not confirmed", "Patience is a position too.")
    add_row(28, "AVOID", "Late wave, trap, blowoff, or bearish condition", "Preserve cash and move on.")
    add_row(29, "BEST TAB TO START", "All_3_Bullish", "Use this first because long, mid, and short timeframes are aligned.")

    section(31, "4) GOLDEN BEGINNER RULES", color="BF360C")
    headers(32)
    add_row(33, "Rule 1", "Buy pullbacks, not excitement", "If price is already far above entry, wait.")
    add_row(34, "Rule 2", "Respect invalidation, not hope", "If stop/invalidation breaks, exit.")
    add_row(35, "Rule 3", "Use targets as zones, not promises", "Take them seriously, but not literally.")
    add_row(36, "Rule 4", "Higher timeframe wins", "If short-term is bullish but long-term is weak, be cautious.")
    add_row(37, "Rule 5", "Start small", "Especially if you are new or the setup is early/uncertain.")

    ws.merge_cells("A39:C39")
    ws["A39"] = "Memory aid: Buy pullbacks, not excitement; respect invalidation, not hope."
    ws["A39"].font = Font(bold=True, italic=True, size=11, color="FFFFFF")
    ws["A39"].fill = PatternFill("solid", fgColor="37474F")
    ws["A39"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[39].height = 24

    ws.freeze_panes = "A5"


# ────────────────────────────────────────────────────────────────
# DATA SHEET WRITER
# ────────────────────────────────────────────────────────────────
def add_sheet_to_workbook(wb, df, sheet_name, theme_color, title_text):
    ws = wb.create_sheet(title=sheet_name)
    ws.sheet_view.showGridLines = False

    if df is None or df.empty:
        df = pd.DataFrame({"Message": [f"No rows available for {sheet_name}"]})

    max_col_letter = get_column_letter(len(df.columns) if len(df.columns) > 0 else 1)

    ws.merge_cells(f"A1:{max_col_letter}1")
    ws["A1"] = title_text
    ws["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=theme_color)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    ws.merge_cells(f"A2:{max_col_letter}2")
    ws["A2"] = "Elliott Wave + Fibonacci projection + beginner-friendly interpretation | Generated: " + datetime.today().strftime("%Y-%m-%d")
    ws["A2"].font = Font(italic=True, size=9, color="455A64")
    ws["A2"].fill = PatternFill("solid", fgColor="ECEFF1")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18

    for ci, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=3, column=ci, value=col.replace("_", " "))
        cell.font = Font(bold=True, size=9, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=theme_color)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border()
    ws.row_dimensions[3].height = 30

    for ri, row in enumerate(df.itertuples(index=False), start=4):
        alt_fill = "F7F7F7" if ri % 2 == 0 else "FFFFFF"

        for ci, val in enumerate(row, start=1):
            col_name = df.columns[ci - 1]
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill = PatternFill("solid", fgColor=alt_fill)
            cell.font = Font(size=9, color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border()

            if col_name == "Ticker":
                cell.font = Font(bold=True, size=11, color="0D47A1")
            elif col_name in ["Company", "Sector", "SubIndustry"]:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            elif col_name in ["EW_Score", "Triple_Bull_Score", "LT_EW_Score", "MT_EW_Score", "ST_EW_Score", "Tomorrow_Buy_Rank"]:
                try:
                    cell.fill = score_fill(float(val))
                    cell.font = Font(bold=True, size=10, color="FFFFFF")
                except Exception:
                    pass
            elif "State" in col_name:
                cell.fill = state_fill(str(val))
                cell.font = Font(bold=True, size=9, color="000000")
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            elif col_name in ["Trade_Quality", "LT_Trade_Quality", "MT_Trade_Quality", "ST_Trade_Quality", "Alignment_Type"]:
                fill, font = quality_fill(str(val))
                cell.fill = fill
                cell.font = font
            elif "Action" in col_name:
                fill, font = action_fill(str(val))
                cell.fill = fill
                cell.font = font
            elif col_name in ["Beginner_Note", "If_Extended_Then", "Professor_Note", "Key_Signals"]:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                cell.font = Font(size=8, italic=True, color="263238")
            elif "RSI" in col_name:
                try:
                    rv = float(val)
                    color = "EF5350" if rv > 70 else "00C853" if rv < 40 else "FF9800"
                    cell.font = Font(bold=True, size=9, color=color)
                except Exception:
                    pass
            elif "Vol_Ratio" in col_name:
                try:
                    vv = float(val)
                    color = "00C853" if vv > 1.5 else "FF6D00" if vv < 0.75 else "455A64"
                    cell.font = Font(bold=True, size=9, color=color)
                except Exception:
                    pass

        ws.row_dimensions[ri].height = 42

    default_widths = {
        "Ticker": 10,
        "Company": 26,
        "Sector": 18,
        "SubIndustry": 26,
        "Timeframe": 12,

        "EW_Score": 10,
        "Tomorrow_Buy_Rank": 14,
        "Trade_Quality": 20,
        "Elliott_State": 30,
        "Current_Price": 11,
        "Current_Wave_Target": 14,
        "Ideal_Entry_Price": 14,
        "Pct_to_Ideal_Entry": 14,
        "Pullback_Buy_Zone": 16,
        "Stop_Loss": 11,
        "Pct_to_Stop": 12,
        "Invalidation_Level": 14,
        "Extension_Target": 13,
        "Pct_to_Target": 12,
        "If_Extended_Then": 42,
        "Beginner_Note": 52,
        "RSI": 8,
        "Vol_Ratio": 10,
        "Dow_MA_Score": 12,
        "OBV_Divergence": 12,
        "MACD_Signal": 12,
        "Pct_from_52w_High": 14,
        "Pct_from_52w_Low": 14,
        "Key_Signals": 40,
        "52w_High": 10,
        "52w_Low": 10,
        "Fib_61.8%": 10,
        "Fib_38.2%": 10,
        "Fib_161.8%_Target": 14,
        "Recommended_Action": 20,
        "Hold_Period": 16,
        "Risk_Management": 36,
        "Rule1_Wave2_OK": 14,
        "W4_Overlap_Risk": 14,

        "Pivot_Low": 10,
        "Pivot_Low_Date": 14,
        "Pivot_Low_Price": 14,
        "Pivot_Low_Bars_Ago": 16,
        "Pivot_High": 10,
        "Pivot_High_Date": 14,
        "Pivot_High_Price": 14,
        "Pivot_High_Bars_Ago": 16,

        "Triple_Bull_Score": 14,
        "Alignment_Type": 22,
        "Master_Action": 18,
        "Professor_Note": 60,

        "LT_EW_Score": 11, "LT_Trade_Quality": 18, "LT_State": 24, "LT_Price": 10, "LT_Target": 11, "LT_Entry": 11, "LT_Stop": 10, "LT_Extension": 12, "LT_Action": 16,
        "LT_Pivot_Low": 10, "LT_Pivot_Low_Date": 14, "LT_Pivot_Low_Price": 14, "LT_Pivot_Low_Bars_Ago": 16,
        "LT_Pivot_High": 10, "LT_Pivot_High_Date": 14, "LT_Pivot_High_Price": 14, "LT_Pivot_High_Bars_Ago": 16,

        "MT_EW_Score": 11, "MT_Trade_Quality": 18, "MT_State": 24, "MT_Price": 10, "MT_Target": 11, "MT_Entry": 11, "MT_Stop": 10, "MT_Extension": 12, "MT_Action": 16,
        "MT_Pivot_Low": 10, "MT_Pivot_Low_Date": 14, "MT_Pivot_Low_Price": 14, "MT_Pivot_Low_Bars_Ago": 16,
        "MT_Pivot_High": 10, "MT_Pivot_High_Date": 14, "MT_Pivot_High_Price": 14, "MT_Pivot_High_Bars_Ago": 16,

        "ST_EW_Score": 11, "ST_Trade_Quality": 18, "ST_State": 24, "ST_Price": 10, "ST_Target": 11, "ST_Entry": 11, "ST_Stop": 10, "ST_Extension": 12, "ST_Action": 16,
        "ST_Pivot_Low": 10, "ST_Pivot_Low_Date": 14, "ST_Pivot_Low_Price": 14, "ST_Pivot_Low_Bars_Ago": 16,
        "ST_Pivot_High": 10, "ST_Pivot_High_Date": 14, "ST_Pivot_High_Price": 14, "ST_Pivot_High_Bars_Ago": 16
    }

    for ci, col in enumerate(df.columns, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = default_widths.get(col, 15)

    ws.freeze_panes = "E4"


# ────────────────────────────────────────────────────────────────
# SAVE WORKBOOK
# ────────────────────────────────────────────────────────────────
def save_master_workbook(
    df_long, df_mid, df_short, df_all_bull,
    df_long_buys, df_mid_buys, df_short_buys, df_tomorrow_buys,
    filename="Elliott_Wave_SP500_Master_Workbook.xlsx"
):
    wb = openpyxl.Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)

    add_cheat_sheet_tab(wb)

    add_sheet_to_workbook(
        wb, df_tomorrow_buys, "Tomorrow_Buys", "BF360C",
        f"BEST BUY CANDIDATES FOR TOMORROW | {UNIVERSE_NAME}"
    )
    add_sheet_to_workbook(
        wb, df_long_buys, "LongTerm_Buys", "1B5E20",
        f"LONG-TERM BUY CANDIDATES | {UNIVERSE_NAME}"
    )
    add_sheet_to_workbook(
        wb, df_mid_buys, "MidTerm_Buys", "0D47A1",
        f"MID-TERM BUY CANDIDATES | {UNIVERSE_NAME}"
    )
    add_sheet_to_workbook(
        wb, df_short_buys, "ShortTerm_Buys", "4A148C",
        f"SHORT-TERM BUY CANDIDATES | {UNIVERSE_NAME}"
    )

    add_sheet_to_workbook(
        wb, df_long, "LongTerm", "2E7D32",
        f"ELLIOTT WAVE — LONG-TERM SUPER CYCLE SCREENER | {UNIVERSE_NAME} | Weekly 5y"
    )
    add_sheet_to_workbook(
        wb, df_mid, "MidTerm", "1565C0",
        f"ELLIOTT WAVE — MID-TERM SWING TRADE SCREENER | {UNIVERSE_NAME} | Daily 1y"
    )
    add_sheet_to_workbook(
        wb, df_short, "ShortTerm", "6A1B9A",
        f"ELLIOTT WAVE — SHORT-TERM MOMENTUM SCREENER | {UNIVERSE_NAME} | Daily 90d"
    )
    add_sheet_to_workbook(
        wb, df_all_bull, "All_3_Bullish", "004D40",
        f"ELLIOTT WAVE — ALL 3 TIMEFRAMES BULLISH | {UNIVERSE_NAME}"
    )

    wb.save(filename)
    print(f"\nSaved workbook: {filename}")


# ────────────────────────────────────────────────────────────────
# FINAL SAVE
# ────────────────────────────────────────────────────────────────
save_master_workbook(
    df_long=df_long,
    df_mid=df_mid,
    df_short=df_short,
    df_all_bull=df_all_bull,
    df_long_buys=df_long_buys,
    df_mid_buys=df_mid_buys,
    df_short_buys=df_short_buys,
    df_tomorrow_buys=df_tomorrow_buys,
    filename=OUTPUT_FILENAME
)

print("\nDone.")
print(f"Workbook created: {OUTPUT_FILENAME}")
print("Tabs: Cheat_Sheet | Tomorrow_Buys | LongTerm_Buys | MidTerm_Buys | ShortTerm_Buys | LongTerm | MidTerm | ShortTerm | All_3_Bullish")
