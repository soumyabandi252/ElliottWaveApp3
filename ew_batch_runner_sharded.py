"""
ew_batch_runner_sharded.py  (v2 -- diagnostic + rate-limit resilient)
======================================================================
v1 fixed the "dies partway through the alphabet" bug by sharding, but
introduced a NEW failure mode: 8 shards launching near-simultaneously
from GitHub's shared runner IP pool triggered aggressive rate-limiting
from Yahoo Finance / NASDAQ Trader, so every shard silently ended up
with zero rows -- no exception, no artifact, just an empty success
that only surfaced downstream as a confusing "no shard outputs found"
error in the merge job.

v2 changes:
1. STAGGERED START -- each shard sleeps (shard_index * 25s) before
   making its first network call, so 8 jobs don't all slam the same
   endpoints in the same second.
2. LOUDER RETRY (tuned back up for real rate-limit survival) --
   base_delay=4.0s, max_retries=5 specifically for yfinance calls
   (NASDAQ Trader/Wikipedia calls keep a lighter retry since those
   are one-shot per shard, not thousands of calls).
3. FAIL LOUDLY INSTEAD OF SILENTLY -- if the ticker universe resolves
   to zero symbols, or if analysis produces zero total rows, the
   shard now (a) still writes whatever CSV it has (even if just
   headers, so the artifact exists and is inspectable) and then
   (b) raises a RuntimeError with clear diagnostics, so the GitHub
   Actions job shows a red X with a real reason instead of a quiet
   fake-success that only breaks things two jobs later in `merge`.
4. CACHE-HIT DIAGNOSTICS -- after price prefetch, logs what fraction
   of tickers actually got usable price data. A very low hit rate
   (<10%) is a strong signal of IP-level rate limiting/blocking and
   is called out explicitly in the log and in the raised error.
"""
import argparse
import io
import json
import logging
import os
import random
import re
import sys
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


def _with_retry(fn, *args, max_retries=3, base_delay=2.0, label="", **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                logger.warning(f"[{label}] failed after {max_retries} attempts: {e}")
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 2)
            time.sleep(delay)


def _with_retry_yf(fn, *args, label="", **kwargs):
    """Heavier retry specifically for Yahoo Finance calls, since those
    are the ones actually hammered thousands of times per shard and
    the ones most likely to get rate-limited under parallel load."""
    return _with_retry(fn, *args, max_retries=5, base_delay=4.0, label=label, **kwargs)


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
    if not combined:
        logger.warning("NASDAQ Trader returned zero usable symbols; trying Wikipedia fallback.")
        combined = _fallback_wiki_universe()
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
    return _with_retry_yf(_do, label=f"yf.download:{label}")


def _prefetch_prices(tickers, period="10y", interval="1d", chunk_size=100, pause_sec=2.0):
    total = len(tickers)
    hit = 0
    for i in range(0, total, chunk_size):
        chunk = tickers[i:i + chunk_size]
        logger.info(f"[PRICE PREFETCH] {i+1}-{i+len(chunk)} / {total}")
        try:
            data = _yf_download_safe(chunk, period=period, interval=interval,
                                      group_by="ticker", threads=True,
                                      progress=False, auto_adjust=True)
            if len(chunk) == 1:
                sub = data.dropna(how="all")
                if not sub.empty:
                    _DATA_CACHE[chunk[0]] = sub
                    hit += 1
            else:
                for t in chunk:
                    try:
                        sub = data[t].dropna(how="all")
                        if not sub.empty:
                            _DATA_CACHE[t] = sub
                            hit += 1
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning(f"  price chunk failed, skipping {len(chunk)} tickers: {exc}")
        time.sleep(pause_sec)

    hit_rate = (hit / total * 100.0) if total else 0.0
    logger.info(f"[PRICE PREFETCH] cache hit rate: {hit}/{total} ({hit_rate:.1f}%)")
    if total >= 20 and hit_rate < 10.0:
        logger.warning(
            "PRICE PREFETCH HIT RATE IS SUSPICIOUSLY LOW (<10%). This strongly "
            "suggests Yahoo Finance is rate-limiting/blocking this runner's IP "
            "rather than individual tickers having no data. Consider: reducing "
            "matrix parallelism, adding start-time stagger, or running on a "
            "self-hosted runner with a stable IP."
        )
    return hit, total


def _prefetch_fast_info(tickers, max_workers=16, pause_between_batches=1.0, batch_size=100):
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


def run_shard(shard_index, shard_count, max_workers=24, stagger_sec=25):
    if stagger_sec and shard_count > 1:
        delay = shard_index * stagger_sec
        logger.info(f"Staggering shard {shard_index} start by {delay}s to avoid a synchronized burst against Yahoo Finance/NASDAQ Trader.")
        time.sleep(delay)

    ew.yf.download = _patched_download
    ew.fundamental_strength = _patched_fundamental_strength

    logger.info("Resolving full US-listed ticker universe...")
    all_tickers = _clean_symbols(sorted(_all_us_listed_symbols()))
    total_universe = len(all_tickers)
    logger.info(f"Full universe size: {total_universe} tickers.")

    csv_path, checkpoint_path = _shard_paths(shard_index, shard_count)

    if total_universe == 0:
        pd.DataFrame(columns=["Symbol"]).to_csv(csv_path, index=False)
        raise RuntimeError(
            "Ticker universe resolved to ZERO symbols (both NASDAQ Trader and the "
            "Wikipedia fallback failed). This is almost certainly a network-level "
            "block on this runner's IP, not a code bug. Wrote an empty placeholder "
            "CSV so the artifact upload step doesn't error, but this shard produced "
            "no usable data."
        )

    my_tickers = [t for i, t in enumerate(all_tickers) if i % shard_count == shard_index]
    logger.info(f"Shard {shard_index}/{shard_count}: assigned {len(my_tickers)} tickers.")

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

    new_rows = []
    hit = total = 0
    if remaining:
        logger.info("Prefetching price history for remaining tickers...")
        hit, total = _prefetch_prices(remaining)
        logger.info("Prefetching fast fundamental info for remaining tickers...")
        _prefetch_fast_info(remaining)

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

    if all_rows:
        pd.DataFrame(all_rows).to_csv(csv_path, index=False)
    else:
        pd.DataFrame(columns=["Symbol"]).to_csv(csv_path, index=False)

    logger.info(f"Shard {shard_index}/{shard_count} complete: {len(all_rows)} rows written to {csv_path}")

    if len(all_rows) == 0 and len(remaining) > 0:
        raise RuntimeError(
            f"Shard {shard_index}/{shard_count} analyzed {len(remaining)} tickers but "
            f"produced ZERO valid rows (price cache hit rate was {hit}/{total}). "
            "This is almost certainly Yahoo Finance rate-limiting/blocking this "
            "runner's requests, not a code bug. A placeholder CSV was written so "
            "the artifact upload step succeeds, but this shard's data is empty. "
            "Re-run with fewer parallel shards (lower matrix parallelism) or add "
            "a longer stagger_sec, or run on a self-hosted runner with a stable IP."
        )

    return csv_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--max-workers", type=int, default=24)
    parser.add_argument("--stagger-sec", type=int, default=25)
    args = parser.parse_args()
    try:
        run_shard(args.shard_index, args.shard_count, args.max_workers, args.stagger_sec)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)
