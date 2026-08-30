"""
Performance-optimized batch/index runner for the Elliott Wave engine.
v7 -- fixes the failures seen in the v6 GitHub Actions run:

1. DOW30 / RUSSELL1000 Wikipedia scraping was failing outright
   ("Could not find a table with columns ('Symbol',)") because Wikipedia
   changed those pages' table structure. Fixed with multi-candidate
   column matching (tries several likely column names, flattens
   MultiIndex headers) AND a hardcoded static fallback list for DOW30
   (only 30 tickers, changes a few times a year -- a fallback list is
   both safe and far more reliable than scraping a page that can change
   its markup at any time).

2. RUSSELL2000 iShares CSV fetch was failing ("Could not locate holdings
   table header"). Added proper CSV Accept header; still degrades
   gracefully to empty (skipped) if iShares blocks/changes format again
   -- there's no safe hardcoded fallback for a 2000-name list that
   reconstitutes quarterly.

3. THE BIG ONE: get_nasdaq1000_tickers()/get_nasdaq2000_tickers() were
   ranking the ENTIRE NASDAQ_COMPOSITE list (thousands of tickers) by
   market cap via fresh Yahoo calls before the real prefetch even
   started. This alone burned thousands of requests and is what tripped
   Yahoo's rate limiter before your budgeted 400-ticker price prefetch
   ran -- which is why ALL 400 of those then failed silently (yfinance
   swallows per-ticker YFRateLimitError internally without raising, so
   the run never detected it was rate-limited). Fixed by:
     - Removing NASDAQ1000/NASDAQ2000 from the default INDEX_ORDER
       (they were always redundant with NASDAQ_COMPOSITE/ALL_US_LISTED
       anyway -- same underlying tickers, just re-ranked).
     - Re-implemented ranking (if you explicitly request these indexes)
       to use ONLY the on-disk fast_info cache -- zero fresh network
       calls. If the cache is cold, it falls back to an alphabetical
       slice instead of hammering Yahoo.

4. Added an implicit rate-limit circuit breaker: since yfinance does
   NOT raise a catchable exception for per-ticker YFRateLimitError
   inside yf.download(), we now measure the per-chunk success rate.
   If a chunk comes back with 0% success, that's treated as an
   implicit rate-limit signal -- back off hard, and if it happens on
   several chunks in a row, stop prefetching entirely for this run
   instead of burning through the whole budget on doomed requests.

5. Reduced default chunk sizes/workers further and silenced the
   harmless TzCache warning.

Carried forward from v6: disk cache for prices/fundamentals with TTL,
per-run fetch budget, fundamentals never raise (write "UNKNOWN (data
error)" instead of dropping the row), dropped-ticker diagnostics.
"""
import requests
import io
import os
import re
import time
import random
import math
import json
import logging
import concurrent.futures as cf
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from curl_cffi import requests as curl_requests

import elliott_wave_engine_FINAL_ALL_PHASES_OPTIMIZED_v2_WITH_DATES as ew

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_runner")

try:
    _tz_cache_dir = os.path.join(str(Path.home()), ".cache", "py-yfinance")
    os.makedirs(_tz_cache_dir, exist_ok=True)
    yf.set_tz_cache_location(_tz_cache_dir)
except Exception:
    pass

_ORIGINAL_YF_DOWNLOAD = yf.download
_DATA_CACHE = {}
_INFO_CACHE = {}
_BULK_INFO = {}

_SESSION = curl_requests.Session(impersonate="chrome")

if os.name == "nt":
    DEFAULT_OUTPUT_ROOT = r"C:\IdentifyStockLowsHighs\ELL_Output"
else:
    DEFAULT_OUTPUT_ROOT = str(Path.cwd() / "ELL_Output")

PRICE_CACHE_TTL_DAYS = 1
INFO_CACHE_TTL_DAYS = 3
MAX_NEW_PRICE_FETCHES_PER_RUN = 250
MAX_NEW_INFO_FETCHES_PER_RUN = 250
CONSECUTIVE_EMPTY_CHUNKS_BEFORE_ABORT = 3

INDEX_ORDER = [
    "SP500", "NASDAQ100", "NASDAQ_COMPOSITE", "DOW30", "RUSSELL1000",
    "RUSSELL2000", "SP600", "SP400", "IWM", "ALL_US_LISTED",
]

WIKI_SOURCES = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "NASDAQ100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
    "DOW30": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
    "RUSSELL1000": "https://en.wikipedia.org/wiki/Russell_1000_Index",
    "RUSSELL2000": "https://en.wikipedia.org/wiki/Russell_2000_Index",
    "SP600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
    "SP400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}

DOW30_FALLBACK = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX",
    "DIS", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

ETF_PROXY = {"IWM": ["IWM"]}

NASDAQ_LISTING_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

IWM_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"
    "/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

CSV_REQUEST_HEADERS = {
    **REQUEST_HEADERS,
    "Accept": "text/csv,application/csv,text/plain,*/*;q=0.8",
}

_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "429")


def _is_rate_limit_error(exc) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _with_retry(fn, *args, max_retries=4, base_delay=8.0, label="", **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if _is_rate_limit_error(e):
                if attempt == max_retries:
                    logger.error(f"[{label}] rate-limited after {max_retries} attempts, giving up: {e}")
                    raise
                delay = 60.0 * attempt + random.uniform(0, 15)
                logger.warning(f"[{label}] RATE LIMITED (attempt {attempt}/{max_retries}); "
                                f"backing off {delay:.0f}s before retry")
                time.sleep(delay)
                continue
            if attempt == max_retries:
                logger.error(f"[{label}] failed after {max_retries} attempts: {e}")
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 4)
            logger.warning(f"[{label}] attempt {attempt}/{max_retries} failed ({e}); "
                            f"retrying in {delay:.1f}s")
            time.sleep(delay)


def _sanitize_symbol(sym):
    if sym is None:
        return None
    s = str(sym).strip().upper()
    if not s or s in {"NAN", "NONE"}:
        return None
    s = s.replace(".", "-").replace("/", "-")
    s = re.sub(r"\s+", "", s)
    return s


def _clean_symbols(symbols):
    seen, out = set(), []
    for s in symbols:
        ss = _sanitize_symbol(s)
        if not ss or any(ch in ss for ch in ['$', '^']):
            continue
        if ss not in seen:
            seen.add(ss)
            out.append(ss)
    return out


def _flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [" ".join(str(c) for c in col if str(c) != 'nan').strip()
                      for col in df.columns]
    return df


def _read_html_table_symbols(url, candidate_columns):
    def _do():
        response = _SESSION.get(url, headers=REQUEST_HEADERS, timeout=60)
        response.raise_for_status()
        html_io = io.StringIO(response.text)
        tables = pd.read_html(html_io)
        for raw_df in tables:
            df = _flatten_columns(raw_df)
            cols_lower = {str(c).strip().lower(): c for c in df.columns}
            for cand in candidate_columns:
                key = cand.strip().lower()
                if key in cols_lower:
                    return df[cols_lower[key]].tolist()
        raise ValueError(f"Could not find any of columns {candidate_columns} at {url}")
    try:
        return _with_retry(_do, label=f"wiki_table:{url}")
    except Exception as e:
        logger.warning(f"Skipping source {url} after repeated failures: {e}")
        return []


def get_sp500_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP500"], ["Symbol", "Ticker symbol", "Ticker"])


def get_nasdaq100_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["NASDAQ100"], ["Ticker", "Symbol", "Ticker symbol"])


def get_dow30_tickers():
    result = _read_html_table_symbols(
        WIKI_SOURCES["DOW30"], ["Symbol", "Ticker symbol", "Ticker", "Company Symbol"]
    )
    if not result:
        logger.warning("DOW30 live scrape failed; using hardcoded fallback list of 30 tickers.")
        return list(DOW30_FALLBACK)
    return result


def get_russell1000_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["RUSSELL1000"], ["Symbol", "Ticker symbol", "Ticker"])


def get_russell2000_tickers():
    def _do():
        r = _SESSION.get(IWM_HOLDINGS_URL, headers=CSV_REQUEST_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text

    try:
        text = _with_retry(_do, label="ishares_iwm_holdings")
    except Exception as e:
        logger.warning(f"iShares IWM holdings fetch failed: {e}")
        return []

    lines = text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Ticker,") or ln.strip().lower().startswith("ticker,"):
            header_idx = i
            break

    if header_idx is None:
        logger.warning("Could not locate holdings table header in iShares CSV response "
                        "(format/blocking likely changed on their end); skipping RUSSELL2000 this run.")
        return []

    try:
        csv_body = "\n".join(lines[header_idx:])
        df = pd.read_csv(io.StringIO(csv_body))
    except Exception as e:
        logger.warning(f"Failed to parse iShares IWM holdings CSV: {e}")
        return []

    if "Ticker" not in df.columns:
        return []

    tickers = df["Ticker"].dropna().astype(str).tolist()
    tickers = [t for t in tickers if t.strip() and t.strip().upper() not in
               {"CASH", "USD CASH", "--", "N/A"}]

    logger.info(f"Fetched {len(tickers)} Russell 2000 constituents from iShares IWM holdings.")
    return tickers


def get_sp600_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP600"], ["Symbol", "Ticker symbol", "Ticker"])


def get_sp400_tickers():
    return _read_html_table_symbols(WIKI_SOURCES["SP400"], ["Symbol", "Ticker symbol", "Ticker"])


def _download_text(url):
    def _do():
        r = _SESSION.get(url, headers=REQUEST_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text
    return _with_retry(_do, label=f"download_text:{url}")


_NASDAQ_TRADER_CACHE = None


def _load_nasdaq_trader_frames():
    global _NASDAQ_TRADER_CACHE
    if _NASDAQ_TRADER_CACHE is not None:
        return _NASDAQ_TRADER_CACHE

    frames = []
    for url in NASDAQ_LISTING_URLS:
        try:
            txt = _download_text(url)
        except Exception as e:
            logger.warning(f"NASDAQ Trader listing failed for {url}: {e}")
            frames.append(pd.DataFrame())
            continue
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if not lines:
            frames.append(pd.DataFrame())
            continue
        header = lines[0].split('|')
        rows = [ln.split('|') for ln in lines[1:] if not ln.startswith('File Creation Time')]
        frames.append(pd.DataFrame(rows, columns=header))

    _NASDAQ_TRADER_CACHE = frames
    return frames


def _all_nasdaq_exchange_symbols():
    frames = _load_nasdaq_trader_frames()
    if len(frames) < 2 or frames[0].empty:
        logger.warning("NASDAQ exchange symbol lists unavailable; returning empty list.")
        return []
    nasdaqlisted, otherlisted = frames[0].copy(), frames[1].copy()

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


def _all_us_listed_symbols():
    frames = _load_nasdaq_trader_frames()
    if len(frames) < 2 or frames[0].empty:
        logger.warning("NASDAQ Trader listing files unavailable; ALL_US_LISTED empty.")
        return []
    nasdaqlisted, otherlisted = frames[0].copy(), frames[1].copy()

    nl = nasdaqlisted.copy()
    if 'Test Issue' in nl.columns:
        nl = nl[nl['Test Issue'].astype(str).str.upper() != 'Y']
    if 'ETF' in nl.columns:
        nl = nl[nl['ETF'].astype(str).str.upper() != 'Y']
    nl_syms = nl['Symbol'].tolist() if 'Symbol' in nl.columns else []

    ol = otherlisted.copy()
    if 'Test Issue' in ol.columns:
        ol = ol[ol['Test Issue'].astype(str).str.upper() != 'Y']
    if 'ETF' in ol.columns:
        ol = ol[ol['ETF'].astype(str).str.upper() != 'Y']
    symcol = 'NASDAQ Symbol' if 'NASDAQ Symbol' in ol.columns else ('Symbol' if 'Symbol' in ol.columns else None)
    ol_syms = ol[symcol].tolist() if symcol else []

    combined = _clean_symbols(nl_syms + ol_syms)
    logger.info(f"ALL_US_LISTED resolved {len(combined)} tickers across all US exchanges.")
    return combined


def get_all_us_listed_tickers():
    return _all_us_listed_symbols()


def _yf_download_safe(tickers, **kwargs):
    def _do():
        return _ORIGINAL_YF_DOWNLOAD(tickers, session=_SESSION, **kwargs)
    label = tickers if isinstance(tickers, str) else f"{len(tickers)} tickers"
    return _with_retry(_do, label=f"yf.download:{label}")


def get_nasdaq1000_tickers(cache=None):
    universe = get_nasdaq_composite_tickers()
    return _rank_from_cache_or_fallback(universe, 1000, cache)


def get_nasdaq2000_tickers(cache=None):
    universe = get_nasdaq_composite_tickers()
    return _rank_from_cache_or_fallback(universe, 2000, cache)


def _rank_from_cache_or_fallback(universe, n, cache):
    if cache is None:
        logger.info(f"No disk cache available for market-cap ranking; returning first {n} alphabetically.")
        return sorted(universe)[:n]
    caps = {}
    for t in universe:
        info = cache.load_info(t)
        if info:
            mc = info.get('marketCap')
            if mc:
                caps[t] = mc
    if not caps:
        logger.info(f"No cached market-cap data yet for ranking; returning first {n} alphabetically "
                    f"(will improve as the fast_info cache warms up across future runs).")
        return sorted(universe)[:n]
    ranked = sorted(caps.items(), key=lambda kv: kv[1], reverse=True)
    top = [k for k, _ in ranked[:n]]
    if len(top) < n:
        remaining = [t for t in sorted(universe) if t not in top]
        top += remaining[:n - len(top)]
    return top


def get_iwm_tickers():
    return ETF_PROXY['IWM']


def resolve_index_map(target_indexes=None, cache=None):
    builders = {
        'SP500': get_sp500_tickers,
        'NASDAQ100': get_nasdaq100_tickers,
        'NASDAQ_COMPOSITE': get_nasdaq_composite_tickers,
        'DOW30': get_dow30_tickers,
        'RUSSELL1000': get_russell1000_tickers,
        'RUSSELL2000': get_russell2000_tickers,
        'SP600': get_sp600_tickers,
        'SP400': get_sp400_tickers,
        'NASDAQ1000': lambda: get_nasdaq1000_tickers(cache),
        'NASDAQ2000': lambda: get_nasdaq2000_tickers(cache),
        'IWM': get_iwm_tickers,
        'ALL_US_LISTED': get_all_us_listed_tickers,
    }

    requested = target_indexes or INDEX_ORDER
    idx = {}
    for name in requested:
        try:
            idx[name] = builders[name]()
        except Exception as e:
            logger.error(f"Index source {name} failed entirely, skipping: {e}")
            idx[name] = []
    return {k: _clean_symbols(v) for k, v in idx.items()}


class DiskCache:
    def __init__(self, root):
        self.root = root
        self.price_dir = os.path.join(root, "_cache", "prices")
        self.info_path = os.path.join(root, "_cache", "fast_info.json")
        os.makedirs(self.price_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.info_path), exist_ok=True)
        self._info_store = self._load_info_store()

    def _load_info_store(self):
        if os.path.exists(self.info_path):
            try:
                with open(self.info_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not read fast_info cache, starting fresh: {e}")
        return {}

    def save_info_store(self):
        try:
            with open(self.info_path, "w", encoding="utf-8") as f:
                json.dump(self._info_store, f)
        except Exception as e:
            logger.warning(f"Could not write fast_info cache: {e}")

    def price_path(self, ticker):
        return os.path.join(self.price_dir, f"{ticker}.csv")

    def price_is_fresh(self, ticker, ttl_days=PRICE_CACHE_TTL_DAYS):
        p = self.price_path(ticker)
        if not os.path.exists(p):
            return False
        age = time.time() - os.path.getmtime(p)
        return age < ttl_days * 86400

    def load_price(self, ticker):
        p = self.price_path(ticker)
        try:
            return pd.read_csv(p, index_col=0, parse_dates=True)
        except Exception:
            return None

    def save_price(self, ticker, df):
        try:
            df.to_csv(self.price_path(ticker))
        except Exception as e:
            logger.warning(f"Could not cache price data for {ticker}: {e}")

    def info_is_fresh(self, ticker, ttl_days=INFO_CACHE_TTL_DAYS):
        entry = self._info_store.get(ticker)
        if not entry:
            return False
        ts = entry.get("_cached_at", 0)
        return (time.time() - ts) < ttl_days * 86400

    def load_info(self, ticker):
        return self._info_store.get(ticker)

    def save_info(self, ticker, info_dict):
        info_dict = dict(info_dict)
        info_dict["_cached_at"] = time.time()
        self._info_store[ticker] = info_dict


def _safe_dict_from_fast_info(fi):
    wanted = [
        'trailingPE', 'forwardPE', 'returnOnEquity', 'revenueGrowth',
        'debtToEquity', 'profitMargins', 'shares', 'sharesOutstanding',
        'lastPrice', 'currency', 'marketCap',
    ]
    out = {}
    for name in wanted:
        try:
            val = fi.get(name) if hasattr(fi, "get") else getattr(fi, name, None)
            out[name] = val
        except Exception:
            out[name] = None
    return out


def _patched_download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True, **kwargs):
    if ticker in _DATA_CACHE:
        return _DATA_CACHE[ticker].copy()
    return _yf_download_safe(ticker, period=period, interval=interval,
                              progress=progress, auto_adjust=auto_adjust, **kwargs)


def _prefetch_prices(tickers, cache: DiskCache, period='10y', interval='1d',
                      chunk_size=25, pause_sec=8.0,
                      max_new_fetches=MAX_NEW_PRICE_FETCHES_PER_RUN):
    tickers = _clean_symbols(tickers)

    cached_hits, needs_fetch = [], []
    for t in tickers:
        if cache.price_is_fresh(t):
            df = cache.load_price(t)
            if df is not None and not df.empty:
                _DATA_CACHE[t] = df
                cached_hits.append(t)
                continue
        needs_fetch.append(t)

    logger.info(f"[PRICE CACHE] {len(cached_hits)}/{len(tickers)} tickers served from disk cache "
                f"(no network call); {len(needs_fetch)} need fetching.")

    to_fetch = needs_fetch[:max_new_fetches]
    if len(needs_fetch) > max_new_fetches:
        logger.warning(f"[PRICE PREFETCH] Capping this run to {max_new_fetches} new/stale tickers "
                        f"out of {len(needs_fetch)}. Remainder picked up on a future run.")

    total = len(to_fetch)
    consecutive_empty = 0
    for i in range(0, total, chunk_size):
        chunk = to_fetch[i:i + chunk_size]
        logger.info(f"[PRICE PREFETCH] {i+1}-{i+len(chunk)} / {total}")
        saved_this_chunk = 0
        try:
            data = _yf_download_safe(chunk, period=period, interval=interval,
                                      group_by='ticker', threads=True,
                                      progress=False, auto_adjust=True)
            if len(chunk) == 1:
                df = data.dropna(how='all')
                if not df.empty:
                    _DATA_CACHE[chunk[0]] = df
                    cache.save_price(chunk[0], df)
                    saved_this_chunk += 1
            else:
                for t in chunk:
                    try:
                        sub = data[t].dropna(how='all')
                        if not sub.empty:
                            _DATA_CACHE[t] = sub
                            cache.save_price(t, sub)
                            saved_this_chunk += 1
                    except Exception:
                        continue
        except Exception as exc:
            if _is_rate_limit_error(exc):
                logger.error(f"  RATE LIMITED (raised) mid-run; stopping further price fetches "
                              f"({len(to_fetch) - i} tickers left for next run). {exc}")
                break
            logger.warning(f"  chunk failed, skipping {len(chunk)} tickers: {exc}")

        if saved_this_chunk == 0 and len(chunk) >= 5:
            consecutive_empty += 1
            logger.warning(f"  0/{len(chunk)} tickers in this chunk returned usable data -- "
                            f"likely silent rate-limiting ({consecutive_empty}/"
                            f"{CONSECUTIVE_EMPTY_CHUNKS_BEFORE_ABORT} consecutive empty chunks).")
            if consecutive_empty >= CONSECUTIVE_EMPTY_CHUNKS_BEFORE_ABORT:
                logger.error(f"  {consecutive_empty} consecutive empty chunks -- treating as a hard "
                              f"rate-limit wall. Stopping price prefetch for this run to preserve "
                              f"budget; remaining {len(to_fetch) - i - len(chunk)} tickers will be "
                              f"retried on a future run.")
                break
            time.sleep(90 + random.uniform(0, 20))
        else:
            consecutive_empty = 0
            time.sleep(pause_sec)


def _prefetch_fast_info(tickers, cache: DiskCache, max_workers=2, pause_between_batches=8.0,
                         batch_size=25, max_new_fetches=MAX_NEW_INFO_FETCHES_PER_RUN):
    tickers = _clean_symbols(tickers)

    cached_hits, needs_fetch = [], []
    for t in tickers:
        if cache.info_is_fresh(t):
            _INFO_CACHE[t] = cache.load_info(t)
            cached_hits.append(t)
        else:
            needs_fetch.append(t)

    logger.info(f"[FAST_INFO CACHE] {len(cached_hits)}/{len(tickers)} tickers served from disk cache; "
                f"{len(needs_fetch)} need fetching.")

    to_fetch = needs_fetch[:max_new_fetches]
    if len(needs_fetch) > max_new_fetches:
        logger.warning(f"[FAST_INFO PREFETCH] Capping this run to {max_new_fetches} new/stale tickers "
                        f"out of {len(needs_fetch)}. Remainder picked up on a future run.")

    rate_limited = {"hit": False}

    def grab(t):
        if rate_limited["hit"]:
            return False
        try:
            fi = yf.Ticker(t, session=_SESSION).fast_info
            info = _safe_dict_from_fast_info(fi)
            _INFO_CACHE[t] = info
            cache.save_info(t, info)
            return True
        except Exception as e:
            if _is_rate_limit_error(e):
                rate_limited["hit"] = True
            return False

    consecutive_empty = 0
    for i in range(0, len(to_fetch), batch_size):
        if rate_limited["hit"]:
            logger.error(f"  RATE LIMITED mid-run; stopping further fast_info fetches "
                         f"({len(to_fetch) - i} tickers left for next run).")
            break
        batch = to_fetch[i:i + batch_size]
        logger.info(f"[FAST_INFO] {i+1}-{i+len(batch)} / {len(to_fetch)}")
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(grab, batch))
        successes = sum(1 for r in results if r)
        if successes == 0 and len(batch) >= 5:
            consecutive_empty += 1
            if consecutive_empty >= CONSECUTIVE_EMPTY_CHUNKS_BEFORE_ABORT:
                logger.error(f"  {consecutive_empty} consecutive empty fast_info batches -- "
                              f"stopping this run's fetches early.")
                break
            time.sleep(90 + random.uniform(0, 20))
        else:
            consecutive_empty = 0
            time.sleep(pause_between_batches)

    cache.save_info_store()


def _patched_fundamental_strength(ticker):
    try:
        if not ew.YF_AVAILABLE:
            return "UNKNOWN (yfinance not installed)", "run: pip install yfinance"

        info = _INFO_CACHE.get(ticker)
        if not info:
            try:
                fi = yf.Ticker(ticker, session=_SESSION).fast_info
                info = _safe_dict_from_fast_info(fi)
                _INFO_CACHE[ticker] = info
            except Exception as exc:
                return "UNKNOWN (data error)", f"fetch failed: {exc}"

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

        label = ("FUNDAMENTALLY STRONG" if score >= 4 else
                  ("MODERATE" if score >= 2 else "FUNDAMENTALLY WEAK"))
        return label, (" | ".join(detail) if detail else "No data")

    except Exception as exc:
        return "UNKNOWN (data error)", f"scoring failed: {exc}"


def _diagnose_dropped_tickers(all_unique, unique_rows):
    dropped = [t for t in all_unique if t not in unique_rows]
    if dropped:
        logger.warning(
            f"[DIAGNOSTIC] {len(dropped)}/{len(all_unique)} tickers produced NO "
            f"row (analyze_ticker failed/returned falsy, or no cached/fetched price "
            f"data was available this run). NOT fundamentals-related. "
            f"Sample: {dropped[:25]}"
        )
    else:
        logger.info("[DIAGNOSTIC] Every analyzed ticker produced a row -- no drops.")
    return dropped


def run_all_indexes(output_root=DEFAULT_OUTPUT_ROOT, max_workers=6):
    ew.yf.download = _patched_download
    ew.fundamental_strength = _patched_fundamental_strength

    cache = DiskCache(output_root)

    index_map = resolve_index_map(cache=cache)
    all_unique = _clean_symbols(sorted({s for vals in index_map.values() for s in vals}))
    logger.info(f'Total unique tickers across all indexes (deduplicated): {len(all_unique)}')

    logger.info('Prefetching price history (disk cache + capped fresh fetches, with circuit breaker)...')
    _prefetch_prices(all_unique, cache)

    logger.info('Prefetching fast fundamental info (disk cache + capped fresh fetches, with circuit breaker)...')
    _prefetch_fast_info(all_unique, cache)

    analyzable = [t for t in all_unique if t in _DATA_CACHE]
    logger.info(f"{len(analyzable)}/{len(all_unique)} tickers have usable price data this run "
                f"({len(all_unique) - len(analyzable)} awaiting a future run's fetch budget).")

    ticker_to_indexes = {}
    for name, tickers in index_map.items():
        for t in tickers:
            ticker_to_indexes.setdefault(t, set()).add(name)

    logger.info(f"Analyzing {len(analyzable)} tickers (single pass, zero duplication)...")
    unique_rows = {}
    with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(ew.analyze_ticker, t): t for t in analyzable}
        done = 0
        for future in cf.as_completed(future_map):
            t = future_map[future]
            done += 1
            try:
                row = future.result()
                if row:
                    row = dict(row)
                    row['Source_Index'] = ','.join(sorted(ticker_to_indexes.get(t, set())))
                    unique_rows[t] = row
            except Exception as exc:
                logger.warning(f"  {t} failed: {exc}")
            if done % 100 == 0 or done == len(analyzable):
                logger.info(f"  Global analysis: {done}/{len(analyzable)} complete")

    _diagnose_dropped_tickers(all_unique, unique_rows)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    results = {}
    manifest_rows = []

    for name in INDEX_ORDER:
        tickers = index_map.get(name, [])
        if not tickers:
            logger.warning(f"No tickers resolved for {name}; skipping.")
            continue
        out_dir = os.path.join(output_root, name)
        os.makedirs(out_dir, exist_ok=True)
        rows = [unique_rows[t] for t in tickers if t in unique_rows]
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        csv_path = xlsx_path = None
        if not df.empty:
            csv_path = os.path.join(out_dir, f'{name}_Elliott_Wave_Signals_{ts}.csv')
            xlsx_path = os.path.join(out_dir, f'{name}_Elliott_Wave_Signals_{ts}.xlsx')
            df.to_csv(csv_path, index=False)
            ew.write_excel(df, xlsx_path)
        results[name] = {'rows': len(df), 'csv': csv_path, 'xlsx': xlsx_path, 'tickers': len(tickers)}
        manifest_rows.append({'Index': name, 'Input_Tickers': len(tickers), 'Output_Rows': len(df),
                               'CSV_Path': csv_path, 'XLSX_Path': xlsx_path})

    combined_dir = os.path.join(output_root, 'COMBINED_ALL_INDEXES')
    os.makedirs(combined_dir, exist_ok=True)
    manifest_path = os.path.join(combined_dir, f'INDEX_RUN_MANIFEST_{ts}.csv')
    pd.DataFrame(manifest_rows).to_csv(manifest_path, index=False)

    combined_xlsx = None
    combined_csv = None

    if unique_rows:
        combined_df = pd.DataFrame(list(unique_rows.values()))
        import glob
        base_filename = "Elliott_Wave_NASDAQ_Composite_Master_Workbook"
        extension = ".xlsx"
        existing_files = glob.glob(f"{base_filename}*{extension}")

        if not existing_files:
            new_filename = f"{base_filename}{extension}"
        else:
            max_version = 0
            for f in existing_files:
                filename_only = os.path.basename(f)
                match = re.search(r"_v(\d+)\.xlsx$", filename_only)
                if match:
                    max_version = max(max_version, int(match.group(1)))
                elif filename_only == f"{base_filename}{extension}":
                    max_version = max(max_version, 0)
            new_filename = f"{base_filename}_v{max_version + 1}{extension}"

        combined_csv = os.path.join(combined_dir, f'ALL_INDEXES_COMBINED_{ts}.csv')
        combined_df.to_csv(combined_csv, index=False)
        combined_xlsx = new_filename
        ew.write_excel(combined_df, combined_xlsx)
        logger.info(f"Successfully saved versioned master file: {combined_xlsx} "
                    f"({len(combined_df)} unique tickers, zero duplicates).")
    else:
        combined_df = pd.DataFrame()
        logger.error("No data collected \u2014 check network/rate-limit warnings above.")

    if combined_xlsx is None:
        logger.error("No workbook was generated this run \u2014 every index came back empty. "
                     "Aborting so CI does not commit a deletion of the last good workbook.")
        raise RuntimeError("No data produced by any index; refusing to proceed without a valid workbook.")

    summary = {
        'output_root': output_root,
        'combined_csv': combined_csv,
        'combined_xlsx': combined_xlsx,
        'manifest_csv': manifest_path,
        'indexes': results,
        'total_unique_tickers': len(unique_rows),
        'total_universe_tickers': len(all_unique),
        'tickers_awaiting_future_fetch': len(all_unique) - len(analyzable),
    }

    summary_json = os.path.join(combined_dir, f'RUN_SUMMARY_{ts}.json')
    with open(summary_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


if __name__ == '__main__':
    run_all_indexes()
