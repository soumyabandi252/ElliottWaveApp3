"""
Performance-optimized batch/index runner for the Elliott Wave engine.
Core Elliott logic is untouched. This layer only optimizes data acquisition,
index constituent resolution, parallel execution, and output orchestration.
"""
import requests
import io
import os
import re
import time
import math
import json
import concurrent.futures as cf
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
import requests

import elliott_wave_engine_FINAL_ALL_PHASES_OPTIMIZED_v2_WITH_DATES as ew

_ORIGINAL_YF_DOWNLOAD = yf.download
_DATA_CACHE = {}
_INFO_CACHE = {}
_BULK_INFO = {}

DEFAULT_OUTPUT_ROOT = r"C:\\IdentifyStockLowsHighs\\ELL_Output"
INDEX_ORDER = [
    "SP500", "NASDAQ100", "NASDAQ_COMPOSITE", "DOW30", "RUSSELL1000",
    "RUSSELL2000", "SP600", "SP400", "NASDAQ1000", "NASDAQ2000", "IWM"
]

WIKI_SOURCES = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "NASDAQ100": "https://en.wikipedia.org/wiki/Nasdaq-100",
    "DOW30": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
    "RUSSELL1000": "https://en.wikipedia.org/wiki/Russell_1000_Index",
    "RUSSELL2000": "https://en.wikipedia.org/wiki/Russell_2000_Index",
    "SP600": "https://en.wikipedia.org/wiki/S%26P_600",
    "SP400": "https://en.wikipedia.org/wiki/S%26P_400",
}

ETF_PROXY = {
    "IWM": ["IWM"],
}

NASDAQ_LISTING_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]


def _sanitize_symbol(sym):
    if sym is None:
        return None
    s = str(sym).strip().upper()
    if not s or s in {"NAN", "NONE"}:
        return None
    s = s.replace(".", "-")
    s = s.replace("/", "-")
    s = re.sub(r"\s+", "", s)
    return s


def _clean_symbols(symbols):
    seen = set()
    out = []
    for s in symbols:
        ss = _sanitize_symbol(s)
        if not ss:
            continue
        if any(ch in ss for ch in ['$', '^']):
            continue
        if ss not in seen:
            seen.add(ss)
            out.append(ss)
    return out


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


def _fetch_html(url):
    r = requests.get(url, headers=REQUEST_HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def _read_html_table_symbols(url, match_columns):
    # Use requests with a standard User-Agent to prevent Wikipedia from blocking the scraper
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Ensure the request was successful
    
    # Wrap the raw HTML string in StringIO for modern pandas compatibility
    html_io = io.StringIO(response.text)
    
    # Let pandas parse the tables from the StringIO object
    tables = pd.read_html(html_io)
    
    # The rest of your existing logic to find the correct table:
    for df in tables:
        if all(col in df.columns for col in match_columns):
            # Assuming you extract the first matching column as the symbols
            return df[match_columns[0]].tolist()
            
    raise ValueError(f"Could not find a table with columns {match_columns} at {url}")


def get_sp500_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP500"], ("Symbol",))


def get_nasdaq100_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["NASDAQ100"], ("Ticker", "Symbol"))


def get_dow30_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["DOW30"], ("Symbol",))


def get_russell1000_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["RUSSELL1000"], ("Ticker", "Symbol"))


def get_russell2000_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["RUSSELL2000"], ("Ticker", "Symbol"))


def get_sp600_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP600"], ("Symbol", "Ticker"))


def get_sp400_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP400"], ("Symbol", "Ticker"))


def _download_text(url):
    r = requests.get(url, headers=REQUEST_HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def _load_nasdaq_trader_frames():
    frames = []
    for url in NASDAQ_LISTING_URLS:
        txt = _download_text(url)
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        header = lines[0].split('|')
        rows = [ln.split('|') for ln in lines[1:] if not ln.startswith('File Creation Time')]
        df = pd.DataFrame(rows, columns=header)
        frames.append(df)
    return frames


def _all_nasdaq_exchange_symbols():
    frames = _load_nasdaq_trader_frames()
    nasdaqlisted = frames[0].copy()
    otherlisted = frames[1].copy()

    nl = nasdaqlisted.copy()
    if 'Test Issue' in nl.columns:
        nl = nl[nl['Test Issue'].astype(str).str.upper() != 'Y']
    if 'ETF' in nl.columns:
        nl = nl[nl['ETF'].astype(str).str.upper() != 'Y']
    nl_syms = nl['Symbol'].tolist() if 'Symbol' in nl.columns else []

    ol = otherlisted.copy()
    if 'Test Issue' in ol.columns:
        ol = ol[ol['Test Issue'].astype(str).str.upper() != 'Y']
    if 'Exchange' in ol.columns:
        ol = ol[ol['Exchange'].astype(str).str.upper() == 'Q']
    if 'ETF' in ol.columns:
        ol = ol[ol['ETF'].astype(str).str.upper() != 'Y']
    symcol = 'NASDAQ Symbol' if 'NASDAQ Symbol' in ol.columns else ('Symbol' if 'Symbol' in ol.columns else None)
    ol_syms = ol[symcol].tolist() if symcol else []
    return _clean_symbols(nl_syms + ol_syms)


def get_nasdaq_composite_tickers():
    return _all_nasdaq_exchange_symbols()


def _caps_from_fast_download(symbols, chunk_size=400, pause_sec=1.5):
    caps = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        data = _ORIGINAL_YF_DOWNLOAD(chunk, period='5d', interval='1d', group_by='ticker', threads=True, progress=False, auto_adjust=True)
        if len(chunk) == 1:
            t = chunk[0]
            try:
                px = float(data['Close'].dropna().iloc[-1])
                sh = yf.Ticker(t).fast_info.get('shares') or yf.Ticker(t).fast_info.get('sharesOutstanding')
                if px and sh:
                    caps[t] = float(px) * float(sh)
            except Exception:
                pass
        else:
            for t in chunk:
                try:
                    sub = data[t]
                    px = float(sub['Close'].dropna().iloc[-1])
                except Exception:
                    px = None
                try:
                    fi = yf.Ticker(t).fast_info
                    sh = fi.get('shares') or fi.get('sharesOutstanding')
                except Exception:
                    sh = None
                if px and sh:
                    caps[t] = float(px) * float(sh)
        time.sleep(pause_sec)
    return caps


def _top_n_by_market_cap(symbols, n):
    caps = _caps_from_fast_download(symbols)
    ranked = sorted(caps.items(), key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in ranked[:n]]


def get_nasdaq1000_tickers():
    syms = get_nasdaq_composite_tickers()
    return _top_n_by_market_cap(syms, 1000)


def get_nasdaq2000_tickers():
    syms = get_nasdaq_composite_tickers()
    return _top_n_by_market_cap(syms, 2000)


def get_iwm_tickers():
    return ETF_PROXY['IWM']


def resolve_index_map(target_indexes=None):
    builders = {
        'SP500': get_sp500_tickers,
        'NASDAQ100': get_nasdaq100_tickers,
        'NASDAQ_COMPOSITE': get_nasdaq_composite_tickers,
        'DOW30': get_dow30_tickers,
        'RUSSELL1000': get_russell1000_tickers,
        'RUSSELL2000': get_russell2000_tickers,
        'SP600': get_sp600_tickers,
        'SP400': get_sp400_tickers,
        'NASDAQ1000': get_nasdaq1000_tickers,
        'NASDAQ2000': get_nasdaq2000_tickers,
        'IWM': get_iwm_tickers,
    }
    requested = target_indexes or INDEX_ORDER
    idx = {}
    for name in requested:
        idx[name] = builders[name]()
    return {k: _clean_symbols(v) for k, v in idx.items()}


def _patched_download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True, **kwargs):
    if ticker in _DATA_CACHE:
        return _DATA_CACHE[ticker].copy()
    return _ORIGINAL_YF_DOWNLOAD(ticker, period=period, interval=interval, progress=progress, auto_adjust=auto_adjust, **kwargs)


def _prefetch_prices(tickers, period='10y', interval='1d', chunk_size=120, pause_sec=1.5):
    tickers = _clean_symbols(tickers)
    total = len(tickers)
    for i in range(0, total, chunk_size):
        chunk = tickers[i:i+chunk_size]
        print(f"[PRICE PREFETCH] {i+1}-{i+len(chunk)} / {total}")
        try:
            data = _ORIGINAL_YF_DOWNLOAD(chunk, period=period, interval=interval, group_by='ticker', threads=True, progress=False, auto_adjust=True)
            if len(chunk) == 1:
                _DATA_CACHE[chunk[0]] = data.dropna(how='all')
            else:
                for t in chunk:
                    try:
                        sub = data[t].dropna(how='all')
                        if not sub.empty:
                            _DATA_CACHE[t] = sub
                    except Exception:
                        continue
        except Exception as exc:
            print(f"  chunk failed: {exc}")
        time.sleep(pause_sec)


def _prefetch_fast_info(tickers, max_workers=24):
    tickers = _clean_symbols(tickers)
    def grab(t):
        try:
            fi = dict(yf.Ticker(t).fast_info)
            _INFO_CACHE[t] = fi
            return True
        except Exception:
            return False
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(grab, tickers))


def _patched_fundamental_strength(ticker):
    if not ew.YF_AVAILABLE:
        return "UNKNOWN (yfinance not installed)", "run: pip install yfinance"
    info = _INFO_CACHE.get(ticker)
    if not info:
        try:
            info = dict(yf.Ticker(ticker).fast_info)
            _INFO_CACHE[ticker] = info
        except Exception as exc:
            return "UNKNOWN (data error)", str(exc)
    pe = info.get('trailingPE') or info.get('forwardPE')
    roe = info.get('returnOnEquity')
    rev_growth = info.get('revenueGrowth')
    de = info.get('debtToEquity')
    profit_margin = info.get('profitMargins')
    score, detail = 0, []
    if pe and 0 < pe < 35:
        score += 1; detail.append(f"PE={round(pe,1)} [healthy]")
    elif pe:
        detail.append(f"PE={round(pe,1)} [elevated]")
    if roe and roe > 0.10:
        score += 1; detail.append(f"ROE={round(roe*100,1)}% [>10%]")
    elif roe:
        detail.append(f"ROE={round(roe*100,1)}% [low]")
    if rev_growth and rev_growth > 0:
        score += 1; detail.append(f"RevGrowth={round(rev_growth*100,1)}% [positive]")
    elif rev_growth:
        detail.append(f"RevGrowth={round(rev_growth*100,1)}% [negative]")
    if de and de < 150:
        score += 1; detail.append(f"D/E={round(de,1)} [manageable]")
    elif de:
        detail.append(f"D/E={round(de,1)} [elevated]")
    if profit_margin and profit_margin > 0:
        score += 1; detail.append(f"Margin={round(profit_margin*100,1)}% [profitable]")
    elif profit_margin:
        detail.append(f"Margin={round(profit_margin*100,1)}% [loss]")
    label = ("FUNDAMENTALLY STRONG" if score >= 4 else ("MODERATE" if score >= 2 else "FUNDAMENTALLY WEAK"))
    return label, " | ".join(detail) if detail else "No data"


def run_one_index(index_name, tickers, output_root=DEFAULT_OUTPUT_ROOT, max_workers=12):
    tickers = _clean_symbols(tickers)
    out_dir = os.path.join(output_root, index_name)
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    print(f"\n=== {index_name}: {len(tickers)} tickers ===")
    with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(ew.analyze_ticker, t): t for t in tickers}
        done = 0
        for future in cf.as_completed(future_map):
            t = future_map[future]
            done += 1
            try:
                row = future.result()
                if row:
                    row = dict(row)
                    row['Source_Index'] = index_name
                    rows.append(row)
                if done % 25 == 0 or done == len(tickers):
                    print(f"  {index_name}: {done}/{len(tickers)} complete")
            except Exception as exc:
                print(f"  {index_name}: {t} failed: {exc}")
    if not rows:
        return pd.DataFrame(), None, None
    df = pd.DataFrame(rows)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(out_dir, f'{index_name}_Elliott_Wave_Signals_{ts}.csv')
    xlsx_path = os.path.join(out_dir, f'{index_name}_Elliott_Wave_Signals_{ts}.xlsx')
    df.to_csv(csv_path, index=False)
    ew.write_excel(df, xlsx_path)
    return df, csv_path, xlsx_path


def run_all_indexes(output_root=DEFAULT_OUTPUT_ROOT, max_workers=12):
    ew.yf.download = _patched_download
    ew.fundamental_strength = _patched_fundamental_strength
    index_map = resolve_index_map()
    all_unique = _clean_symbols(sorted({s for vals in index_map.values() for s in vals}))
    print(f'Total unique tickers across requested indexes: {len(all_unique)}')
    print('Prefetching price history...')
    _prefetch_prices(all_unique)
    print('Prefetching fast fundamental info...')
    _prefetch_fast_info(all_unique)

    results = {}
    combined = []
    manifest_rows = []
    for name in INDEX_ORDER:
        tickers = index_map.get(name, [])
        if not tickers:
            continue
        df, csv_path, xlsx_path = run_one_index(name, tickers, output_root=output_root, max_workers=max_workers)
        results[name] = {'rows': len(df), 'csv': csv_path, 'xlsx': xlsx_path, 'tickers': len(tickers)}
        manifest_rows.append({'Index': name, 'Input_Tickers': len(tickers), 'Output_Rows': len(df), 'CSV_Path': csv_path, 'XLSX_Path': xlsx_path})
        if not df.empty:
            combined.append(df)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    combined_dir = os.path.join(output_root, 'COMBINED_ALL_INDEXES')
    os.makedirs(combined_dir, exist_ok=True)
    manifest_path = os.path.join(combined_dir, f'INDEX_RUN_MANIFEST_{ts}.csv')
    pd.DataFrame(manifest_rows).to_csv(manifest_path, index=False)

    if combined:
        combined_df = pd.concat(combined, ignore_index=True)
        combined_csv = os.path.join(combined_dir, f'ALL_INDEXES_COMBINED_{ts}.csv')
        combined_xlsx = os.path.join(combined_dir, f'ALL_INDEXES_COMBINED_{ts}.xlsx')
        combined_df.to_csv(combined_csv, index=False)
        ew.write_excel(combined_df, combined_xlsx)
    else:
        combined_df = pd.DataFrame()
        combined_csv = None
        combined_xlsx = None

    summary = {
        'output_root': output_root,
        'combined_csv': combined_csv,
        'combined_xlsx': combined_xlsx,
        'manifest_csv': manifest_path,
        'indexes': results,
    }
    summary_json = os.path.join(combined_dir, f'RUN_SUMMARY_{ts}.json')
    with open(summary_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


if __name__ == '__main__':
    run_all_indexes()
