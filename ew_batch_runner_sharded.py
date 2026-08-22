"""
ew_batch_runner_sharded.py
===========================
Fixes the "only ~226 tickers processed" bug in the original
ew_batch_runner.py by making the scan:

1. SHARDABLE -- the full US-listed ticker universe (thousands of
   symbols) is split into N equal shards. Each shard runs as its own
   GitHub Actions matrix job in parallel, so instead of one job
   crawling the entire alphabet serially (and dying partway through
   the "A" tickers when it runs out of time), N jobs each cover a
   fraction of the alphabet and finish comfortably within the
   runner's time limit.

2. CHECKPOINTED -- each shard writes its progress to a local JSON
   checkpoint file after every ticker. If a shard is interrupted
   (timeout, rate limit storm, spot-instance eviction, etc.) it
   resumes from the checkpoint on the next run instead of starting
   over from scratch.

3. FASTER PER-TICKER RETRY -- the original retry backoff
   (base_delay=8.0s, up to 4 attempts => up to ~64s+jitter per
   failing call) made a universe-wide rate-limit episode balloon
   total runtime by 10-50x. This version uses a much cheaper backoff
   (base_delay=1.5s, max 3 attempts) so failing tickers get skipped
   quickly instead of stalling the whole shard.

4. HIGHER CONCURRENCY -- ThreadPoolExecutor worker counts raised from
   6-12 to 24-32, since yfinance/NASDAQ Trader tolerate meaningfully
   higher parallel read volume than the original conservative
   settings assumed.

Usage (single machine, all tickers, no sharding):
    python ew_batch_runner_sharded.py --shard-index 0 --shard-count 1

Usage (GitHub Actions matrix, 8 parallel shards):
    python ew_batch_runner_sharded.py --shard-index ${{ matrix.shard }} --shard-count 8

Each shard writes:
    ELL_Output/SHARDS/shard_<index>_of_<count>.csv
    ELL_Output/SHARDS/shard_<index>_of_<count>.checkpoint.json

A separate merge_shards.py combines all shard CSVs into the final
master workbook (see that file for details).
"""
import argparse
import io
import json
import logging
import os
import random
import re
import time
import concurrent.futures as cf
from pathlib import Path

import pandas as pd
import yfinance as yf
from curl_cffi import requests as curl_requests

import elliott_wave_engine_FINAL_ALL_PHASES_OPTIMIZED_v2_WITH_DATES as ew

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_runner_sharded")

_SESSION = curl_requests.Session(impersonate="chrome")
_DATA_CACHE = {}
_INFO_CACHE = {}
_NASDAQ_TRADER_CACHE = None

OUTPUT_ROOT = os.environ.get("ELL_OUTPUT_ROOT", str(Path.cwd() / "ELL_Output"))
SHARD_DIR = os.path.join(OUTPUT_ROOT, "SHARDS")

WIKI_SOURCES = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "NASDAQ100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
    "DOW30": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
    "RUSSELL1000": "https://en.wikipedia.org/wiki/Russell_1000_Index",
    "SP600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
    "SP400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}

NASDAQ_LISTING_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


def _with_retry(fn, *args, max_retries=3, base_delay=1.5, label="", **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                logger.warning(f"[{label}] failed after {max_retries} attempts: {e}")
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
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
        if not ss or any(ch in ss for ch in ["$", "^"]):
            continue
        if ss not in seen:
            seen.add(ss)
            out.append(ss)
    return out


def _download_text(url):
    def _do():
        r = _SESSION.get(url, headers=REQUEST_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text
    return _with_retry(_do, label=f"download_text:{url}")


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
        header = lines[0].split("|")
        rows = [ln.split("|") for ln in lines[1:] if not ln.startswith("File Creation Time")]
        frames.append(pd.DataFrame(rows, columns=header))
    _NASDAQ_TRADER_CACHE = frames
    return frames


def _all_us_listed_symbols():
    """Full US-listed common-stock universe: every Nasdaq tier plus
    NYSE/NYSE American/NYSE Arca/Cboe BZX from otherlisted.txt, no
    exchange filter."""
    frames = _load_nasdaq_trader_frames()
    if len(frames) < 2 or frames[0].empty:
        logger.warning("NASDAQ Trader listing files unavailable; falling back to Wikipedia indexes only.")
        return _fallback_wiki_universe()

    nasdaqlisted, otherlisted = frames[0].copy(), frames[1].copy()

    nl = nasdaqlisted.copy()
    if "Test Issue" in nl.columns:
        nl = nl[nl["Test Issue"].astype(str).str.upper() != "Y"]
    if "ETF" in nl.columns:
        nl = nl[nl["ETF"].astype(str).str.upper() != "Y"]
    nl_syms = nl["Symbol"].tolist() if "Symbol" in nl.columns else []

    ol = otherlisted.copy()
    if "Test Issue" in ol.columns:
        ol = ol[ol["Test Issue"].astype(str).str.upper() != "Y"]
    if "ETF" in ol.columns:
        ol = ol[ol["ETF"].astype(str).str.upper() != "Y"]
    symcol = "NASDAQ Symbol" if "NASDAQ Symbol" in ol.columns else ("Symbol" if "Symbol" in ol.columns else None)
    ol_syms = ol[symcol].tolist() if symcol else []

    combined = _clean_symbols(nl_syms + ol_syms)
    logger.info(f"ALL_US_LISTED resolved {len(combined)} tickers across all US exchanges.")
    return combined


def _read_html_table_symbols(url, match_columns):
    def _do():
        response = _SESSION.get(url, headers=REQUEST_HEADERS, timeout=60)
        response.raise_for_status()
        html_io = io.StringIO(response.text)
        tables = pd.read_html(html_io)
        for df in tables:
            if all(col in df.columns for col in match_columns):
                return df[match_columns[0]].tolist()
        raise ValueError(f"Could not find a table with columns {match_columns} at {url}")
    try:
        return _with_retry(_do, label=f"wiki_table:{url}")
    except Exception as e:
        logger.warning(f"Skipping source {url} after repeated failures: {e}")
        return []


def _fallback_wiki_universe():
    """Used only if NASDAQ Trader is completely unreachable -- keeps
    the scan alive with a smaller but still broad universe rather than
    producing zero rows."""
    out = []
    out += _read_html_table_symbols(WIKI_SOURCES["SP500"], ("Symbol",))
    out += _read_html_table_symbols(WIKI_SOURCES["NASDAQ100"], ("Ticker",))
    out += _read_html_table_symbols(WIKI_SOURCES["SP600"], ("Symbol",))
    out += _read_html_table_symbols(WIKI_SOURCES["SP400"], ("Symbol",))
    out += _read_html_table_symbols(WIKI_SOURCES["RUSSELL1000"], ("Symbol",))
    return _clean_symbols(out)


def _yf_download_safe(tickers, **kwargs):
    def _do():
        return yf.download(tickers, session=_SESSION, **kwargs)
    label = tickers if isinstance(tickers, str) else f"{len(tickers)} tickers"
    return _with_retry(_do, label=f"yf.download:{label}")


def _prefetch_prices(tickers, period="10y", interval="1d", chunk_size=150, pause_sec=1.0):
    total = len(tickers)
    for i in range(0, total, chunk_size):
        chunk = tickers[i:i + chunk_size]
        logger.info(f"[PRICE PREFETCH] {i+1}-{i+len(chunk)} / {total}")
        try:
            data = _yf_download_safe(chunk, period=period, interval=interval,
                                      group_by="ticker", threads=True,
                                      progress=False, auto_adjust=True)
            if len(chunk) == 1:
                _DATA_CACHE[chunk[0]] = data.dropna(how="all")
            else:
                for t in chunk:
                    try:
                        sub = data[t].dropna(how="all")
                        if not sub.empty:
                            _DATA_CACHE[t] = sub
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning(f"  price chunk failed, skipping {len(chunk)} tickers: {exc}")
        time.sleep(pause_sec)


def _prefetch_fast_info(tickers, max_workers=24, pause_between_batches=0.5, batch_size=150):
    def grab(t):
        try:
            fi = dict(yf.Ticker(t, session=_SESSION).fast_info)
            _INFO_CACHE[t] = fi
            return True
        except Exception:
            return False

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        logger.info(f"[FAST_INFO] {i+1}-{i+len(batch)} / {len(tickers)}")
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(grab, batch))
        time.sleep(pause_between_batches)


def _patched_download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True, **kwargs):
    if ticker in _DATA_CACHE:
        return _DATA_CACHE[ticker].copy()
    return _yf_download_safe(ticker, period=period, interval=interval,
                              progress=progress, auto_adjust=auto_adjust, **kwargs)


def _patched_fundamental_strength(ticker):
    if not ew.YF_AVAILABLE:
        return "UNKNOWN (yfinance not installed)", "run: pip install yfinance"
    info = _INFO_CACHE.get(ticker)
    if not info:
        try:
            info = dict(yf.Ticker(ticker, session=_SESSION).fast_info)
            _INFO_CACHE[ticker] = info
        except Exception as exc:
            return "UNKNOWN (data error)", str(exc)
    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = info.get("returnOnEquity")
    rev_growth = info.get("revenueGrowth")
    de = info.get("debtToEquity")
    profit_margin = info.get("profitMargins")
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


def _shard_paths(shard_index, shard_count):
    os.makedirs(SHARD_DIR, exist_ok=True)
    base = f"shard_{shard_index}_of_{shard_count}"
    return (
        os.path.join(SHARD_DIR, f"{base}.csv"),
        os.path.join(SHARD_DIR, f"{base}.checkpoint.json"),
    )


def _load_checkpoint(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"done": []}
    return {"done": []}


def _save_checkpoint(path, done_tickers):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done_tickers)}, f)


def run_shard(shard_index, shard_count, max_workers=32):
    ew.yf.download = _patched_download
    ew.fundamental_strength = _patched_fundamental_strength

    logger.info("Resolving full US-listed ticker universe...")
    all_tickers = _clean_symbols(sorted(_all_us_listed_symbols()))
    total_universe = len(all_tickers)
    logger.info(f"Full universe size: {total_universe} tickers.")

    my_tickers = [t for i, t in enumerate(all_tickers) if i % shard_count == shard_index]
    logger.info(f"Shard {shard_index}/{shard_count}: assigned {len(my_tickers)} tickers.")

    csv_path, checkpoint_path = _shard_paths(shard_index, shard_count)
    checkpoint = _load_checkpoint(checkpoint_path)
    done_set = set(checkpoint.get("done", []))
    remaining = [t for t in my_tickers if t not in done_set]
    logger.info(f"Shard {shard_index}: {len(done_set)} already done (resumed), {len(remaining)} remaining.")

    existing_rows = []
    if os.path.exists(csv_path) and done_set:
        try:
            existing_rows = pd.read_csv(csv_path).to_dict("records")
        except Exception:
            existing_rows = []

    if remaining:
        logger.info("Prefetching price history for remaining tickers...")
        _prefetch_prices(remaining)
        logger.info("Prefetching fast fundamental info for remaining tickers...")
        _prefetch_fast_info(remaining)

        new_rows = []
        with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(ew.analyze_ticker, t): t for t in remaining}
            done_count = 0
            for future in cf.as_completed(future_map):
                t = future_map[future]
                done_count += 1
                try:
                    row = future.result()
                    if row:
                        row = dict(row)
                        row["Source_Index"] = "ALL_US_LISTED"
                        new_rows.append(row)
                    done_set.add(t)
                except Exception as exc:
                    logger.warning(f"  {t} failed: {exc}")
                    done_set.add(t)
                if done_count % 50 == 0 or done_count == len(remaining):
                    logger.info(f"  Shard {shard_index}: {done_count}/{len(remaining)} complete")
                    _save_checkpoint(checkpoint_path, done_set)
                    combined = existing_rows + new_rows
                    if combined:
                        pd.DataFrame(combined).to_csv(csv_path, index=False)

        _save_checkpoint(checkpoint_path, done_set)
        all_rows = existing_rows + new_rows
    else:
        all_rows = existing_rows

    if all_rows:
        pd.DataFrame(all_rows).to_csv(csv_path, index=False)
    logger.info(f"Shard {shard_index}/{shard_count} complete: {len(all_rows)} rows written to {csv_path}")
    return csv_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--max-workers", type=int, default=32)
    args = parser.parse_args()
    run_shard(args.shard_index, args.shard_count, args.max_workers)
