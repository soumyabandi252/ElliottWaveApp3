"""
merge_shards.py (v2 -- clearer diagnostics on partial/empty shards)
"""
import argparse
import glob
import os
import re
from pathlib import Path

import pandas as pd

import elliott_wave_engine_FINAL_ALL_PHASES_OPTIMIZED_v2_WITH_DATES as ew


def merge(output_root, shard_count):
    shard_dir = os.path.join(output_root, "SHARDS")
    frames = []
    empty_shards = []
    missing_shards = []
    for i in range(shard_count):
        path = os.path.join(shard_dir, f"shard_{i}_of_{shard_count}.csv")
        if os.path.exists(path):
            df = pd.read_csv(path)
            print(f"Shard {i}: {len(df)} rows (file present)")
            if len(df) == 0:
                empty_shards.append(i)
            else:
                frames.append(df)
        else:
            missing_shards.append(i)
            print(f"Shard {i}: FILE MISSING (artifact never uploaded)")

    if missing_shards:
        print(f"WARNING: shard artifacts entirely missing: {missing_shards} "
              "(likely the job crashed before writing any CSV, or upload-artifact "
              "step itself failed)")
    if empty_shards:
        print(f"WARNING: shards ran but produced zero rows: {empty_shards} "
              "(very likely Yahoo Finance / NASDAQ Trader rate-limited this run -- "
              "check that shard's job log for the 'PRICE PREFETCH cache hit rate' line)")

    if not frames:
        raise RuntimeError(
            "No shard produced any usable rows. All shards either failed to upload "
            "an artifact or completed with zero rows. This is a network/rate-limit "
            "issue on the runner, not a bug in this merge script -- check individual "
            "shard job logs for 'PRICE PREFETCH cache hit rate' to confirm."
        )

    combined = pd.concat(frames, ignore_index=True)
    if "Symbol" in combined.columns:
        combined = combined.drop_duplicates(subset=["Symbol"], keep="first")
    print(f"Combined total: {len(combined)} unique tickers across {len(frames)} non-empty shard(s) "
          f"(missing: {missing_shards}, empty: {empty_shards}).")

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

    ew.write_excel(combined, new_filename)
    print(f"Wrote merged master workbook: {new_filename} ({len(combined)} tickers).")
    return new_filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=8)
    parser.add_argument("--output-root", type=str, default=str(Path.cwd() / "ELL_Output"))
    args = parser.parse_args()
    merge(args.output_root, args.shard_count)
