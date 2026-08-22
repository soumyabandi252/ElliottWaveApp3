"""
merge_shards.py
================
Combines all per-shard CSV outputs from ew_batch_runner_sharded.py into
a single deduplicated master workbook covering the FULL US ticker
universe, instead of the old single-job run that silently died partway
through the alphabet.

Usage:
    python merge_shards.py --shard-count 8 --output-root ELL_Output
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
    missing = []
    for i in range(shard_count):
        path = os.path.join(shard_dir, f"shard_{i}_of_{shard_count}.csv")
        if os.path.exists(path):
            df = pd.read_csv(path)
            frames.append(df)
            print(f"Shard {i}: {len(df)} rows")
        else:
            missing.append(i)
    if missing:
        print(f"WARNING: shards missing (will be absent from output): {missing}")
    if not frames:
        raise RuntimeError("No shard outputs found -- nothing to merge.")

    combined = pd.concat(frames, ignore_index=True)
    if "Symbol" in combined.columns:
        combined = combined.drop_duplicates(subset=["Symbol"], keep="first")
    print(f"Combined total: {len(combined)} unique tickers across {len(frames)} shard(s).")

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
