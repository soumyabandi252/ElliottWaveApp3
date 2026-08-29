"""
Custom-ticker runner for the Elliott Wave engine.

This replaces the old multi-index universe scanner (S&P 500, NASDAQ 100,
NASDAQ Composite, Dow 30, Russell 1000/2000, S&P 600/400, IWM,
ALL_US_LISTED) with a single, focused mode: analyze ONLY the tickers you
explicitly ask for.

Everything unrelated to index-list scraping is carried forward unchanged
from the prior "v7" index runner:
  - Shared curl_cffi browser-impersonating session for all network calls
    (yfinance + any HTTP requests) to dodge TLS-fingerprint blocking.
  - Retry-with-backoff wrapper, with a much longer specific backoff for
    detected rate-limit errors (429 / "Too Many Requests" / "Rate limited").
  - On-disk persistent cache (DiskCache) for price history and fast_info,
    with a TTL, so repeated runs don't refetch unchanged data.
  - Per-run fetch budget + an implicit rate-limit circuit breaker that
    backs off / stops early if several consecutive chunks come back empty.
  - _patched_fundamental_strength() never raises; any failure degrades to
    "UNKNOWN (data error)" written into the row instead of dropping the
    ticker entirely.

Removed entirely (no longer needed for custom-ticker execution):
  - All Wikipedia / NASDAQ Trader / iShares index-constituent scraping
    (get_sp500_tickers, get_nasdaq100_tickers, get_dow30_tickers,
    get_russell1000_tickers, get_russell2000_tickers, get_sp600_tickers,
    get_sp400_tickers, get_nasdaq_composite_tickers, get_all_us_listed_tickers,
    get_nasdaq1000_tickers, get_nasdaq2000_tickers, get_iwm_tickers).
  - resolve_index_map(), INDEX_ORDER, WIKI_SOURCES, DOW30_FALLBACK,
    ETF_PROXY, NASDAQ_LISTING_URLS, IWM_HOLDINGS_URL, and the NASDAQ
    Trader frame cache.

How to specify tickers to run:
  1. Command line:      python ew_batch_runner.py AAPL MSFT TSLA
  2. Environment var:   CUSTOM_TICKERS="AAPL,MSFT,TSLA" python ew_batch_runner.py
  3. Fallback default:  CUSTOM_TICKERS_DEFAULT below, if neither is given.
"""
import os
import re
import sys
import time
import random
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

_SESSION = curl_requests.Session(impersonate="chrome")

if os.name == "nt":
    DEFAULT_OUTPUT_ROOT = r"C:\IdentifyStockLowsHighs\ELL_Output"
else:
    DEFAULT_OUTPUT_ROOT = str(Path.cwd() / "ELL_Output")

# ---------------------------------------------------------------------------
# Cache + fetch-budget configuration (unchanged from the index runner)
# ---------------------------------------------------------------------------
PRICE_CACHE_TTL_DAYS = 1
INFO_CACHE_TTL_DAYS = 3
MAX_NEW_PRICE_FETCHES_PER_RUN = 250
MAX_NEW_INFO_FETCHES_PER_RUN = 250
CONSECUTIVE_EMPTY_CHUNKS_BEFORE_ABORT = 3

# Used only if no tickers are supplied via CLI args or CUSTOM_TICKERS env var.
CUSTOM_TICKERS_DEFAULT = ["AAPL", "MSFT", "NVDA"]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
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


def resolve_custom_tickers(cli_args=None):
    """Resolves the ticker list to run, in priority order:
    1. CLI arguments (python ew_batch_runner.py AAPL MSFT TSLA)
    2. CUSTOM_TICKERS environment variable (comma-separated)
    3. CUSTOM_TICKERS_DEFAULT fallback
    """
    args = cli_args if cli_args is not None else sys.argv[1:]
    if args:
        tickers = _clean_symbols(args)
        logger.info(f"Using {len(tickers)} ticker(s) from command-line arguments: {tickers}")
        return tickers

    env_val = os.environ.get("CUSTOM_TICKERS", "").strip()
    if env_val:
        tickers = _clean_symbols(env_val.split(","))
        logger.info(f"Using {len(tickers)} ticker(s) from CUSTOM_TICKERS env var: {tickers}")
        return tickers

    logger.info(f"No tickers supplied via CLI or CUSTOM_TICKERS; using default: {CUSTOM_TICKERS_DEFAULT}")
    return _clean_symbols(CUSTOM_TICKERS_DEFAULT)


# ---------------------------------------------------------------------------
# On-disk persistent cache (unchanged from the index runner)
# ---------------------------------------------------------------------------
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


def _yf_download_safe(tickers, **kwargs):
    def _do():
        return _ORIGINAL_YF_DOWNLOAD(tickers, session=_SESSION, **kwargs)
    label = tickers if isinstance(tickers, str) else f"{len(tickers)} tickers"
    return _with_retry(_do, label=f"yf.download:{label}")


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


def _diagnose_dropped_tickers(all_tickers, unique_rows):
    dropped = [t for t in all_tickers if t not in unique_rows]
    if dropped:
        logger.warning(
            f"[DIAGNOSTIC] {len(dropped)}/{len(all_tickers)} tickers produced NO row "
            f"(no cached/fetched price data was available this run, or analyze_ticker "
            f"failed). NOT fundamentals-related. Sample: {dropped[:25]}"
        )
    else:
        logger.info("[DIAGNOSTIC] Every analyzed ticker produced a row -- no drops.")
    return dropped


def run_custom_tickers(tickers=None, output_root=DEFAULT_OUTPUT_ROOT, max_workers=6):
    """Runs the Elliott Wave engine on ONLY the tickers you supply --
    no index scraping, no universe scanning. This is the sole execution
    path now; there is no 'all indexes' mode."""
    ew.yf.download = _patched_download
    ew.fundamental_strength = _patched_fundamental_strength

    tickers = _clean_symbols(tickers if tickers is not None else resolve_custom_tickers())
    if not tickers:
        raise ValueError("No custom tickers were provided. Pass them as CLI arguments, "
                          "set the CUSTOM_TICKERS environment variable, or pass a list "
                          "to run_custom_tickers(tickers=[...]).")

    cache = DiskCache(output_root)
    logger.info(f"Running custom-ticker analysis for {len(tickers)} ticker(s): {tickers}")

    logger.info('Prefetching price history (disk cache + capped fresh fetches, with circuit breaker)...')
    _prefetch_prices(tickers, cache)

    logger.info('Prefetching fast fundamental info (disk cache + capped fresh fetches, with circuit breaker)...')
    _prefetch_fast_info(tickers, cache)

    analyzable = [t for t in tickers if t in _DATA_CACHE]
    logger.info(f"{len(analyzable)}/{len(tickers)} tickers have usable price data this run "
                f"({len(tickers) - len(analyzable)} awaiting a future run's fetch budget).")

    logger.info(f"Analyzing {len(analyzable)} ticker(s)...")
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
                    row['Source_Index'] = 'CUSTOM'
                    unique_rows[t] = row
            except Exception as exc:
                logger.warning(f"  {t} failed: {exc}")
            logger.info(f"  Custom analysis: {done}/{len(analyzable)} complete")

    _diagnose_dropped_tickers(tickers, unique_rows)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(output_root, "CUSTOM_TICKERS")
    os.makedirs(out_dir, exist_ok=True)

    combined_xlsx = None
    combined_csv = None
    combined_df = pd.DataFrame()

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

        combined_csv = os.path.join(out_dir, f'CUSTOM_TICKERS_{ts}.csv')
        combined_df.to_csv(combined_csv, index=False)
        combined_xlsx = new_filename
        ew.write_excel(combined_df, combined_xlsx)
        logger.info(f"Successfully saved custom-ticker workbook: {combined_xlsx} "
                    f"({len(combined_df)} ticker(s)).")
    else:
        logger.error("No data collected for any requested ticker -- check network/rate-limit "
                     "warnings above, and confirm the ticker symbols are valid.")
        raise RuntimeError("No data produced for any requested ticker; refusing to proceed "
                            "without a valid workbook.")

    summary = {
        'output_root': output_root,
        'requested_tickers': tickers,
        'combined_csv': combined_csv,
        'combined_xlsx': combined_xlsx,
        'total_tickers_analyzed': len(unique_rows),
    }
    summary_json = os.path.join(out_dir, f'RUN_SUMMARY_{ts}.json')
    with open(summary_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


if __name__ == '__main__':
    run_custom_tickers()
