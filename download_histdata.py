"""
download_histdata.py — APEX Capital AI Historical Data Downloader
=================================================================
Downloads M1 OHLCV data from HistData.com for all 4 instruments,
resamples to M15, and saves to data/ folder.

Source: https://www.histdata.com (free, no registration required)
Data:   EURUSD / GBPUSD / USDJPY / XAUUSD — back to 2020

Usage:
    python download_histdata.py                      # all pairs, 2020-2025
    python download_histdata.py --pairs EURUSD GOLD  # specific pairs
    python download_histdata.py --from 2022          # specific start year
    python download_histdata.py --to 2023            # specific end year

Output:
    data/EURUSD_M15_2020.csv  ... data/XAUUSD_M15_2025.csv
    (one file per pair per year, M15 OHLCV, semicolon-delimited)

Combined file (for backtest):
    data/EURUSD_M15_ALL.csv   (all years merged, sorted)
"""

import os
import re
import sys
import io
import time
import zipfile
import argparse
import requests
import pandas as pd
from datetime import datetime

# Windows UTF-8 fix
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

os.makedirs("data", exist_ok=True)

# ── Instrument mapping ────────────────────────────────────────────────────────
# HistData pair name → local file name
PAIRS = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
    "XAUUSD": "XAUUSD",   # HistData uses XAUUSD
}

HISTDATA_BASE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes"
HISTDATA_POST = "https://www.histdata.com/get.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Download one year ─────────────────────────────────────────────────────────

def download_year(session: requests.Session, pair: str, year: int) -> pd.DataFrame | None:
    """
    Downloads one year of M1 data for a pair from HistData.com.
    Returns a DataFrame with columns: datetime, open, high, low, close, volume
    Returns None on failure.
    """
    page_url = f"{HISTDATA_BASE}/{pair.lower()}/{year}"

    # Step 1: GET the page to scrape the tk token
    try:
        r = session.get(page_url, timeout=30, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] GET failed: {e}")
        return None

    # Extract hidden tk token
    match = re.search(r'name="tk"[^>]*value="([^"]+)"', r.text)
    if not match:
        # Try alternate pattern
        match = re.search(r'value="([a-f0-9]{32})"', r.text)
    if not match:
        print(f"    [!] Could not find tk token on page. Data may not be available for {pair} {year}.")
        return None

    tk = match.group(1)

    # Step 2: POST to get the ZIP
    post_data = {
        "tk":        tk,
        "date":      str(year),
        "datemonth": str(year),
        "platform":  "ASCII",
        "timeframe": "M1",
        "fxpair":    pair.upper(),
    }
    post_headers = {**HEADERS, "Referer": page_url,
                    "Content-Type": "application/x-www-form-urlencoded"}

    try:
        r2 = session.post(HISTDATA_POST, data=post_data, headers=post_headers, timeout=120)
        r2.raise_for_status()
    except Exception as e:
        print(f"    [!] POST failed: {e}")
        return None

    if len(r2.content) < 1000:
        print(f"    [!] Response too small ({len(r2.content)} bytes) — likely not a ZIP.")
        return None

    # Step 3: Parse ZIP
    try:
        zf = zipfile.ZipFile(io.BytesIO(r2.content))
    except zipfile.BadZipFile:
        print(f"    [!] Response is not a valid ZIP file.")
        return None

    # Find the CSV file inside
    csv_name = next((n for n in zf.namelist() if n.endswith('.csv')), None)
    if not csv_name:
        print(f"    [!] No CSV found in ZIP. Files: {zf.namelist()}")
        return None

    # Step 4: Parse M1 CSV
    # Format: YYYYMMDD HHMMSS;open;high;low;close;volume
    try:
        with zf.open(csv_name) as f:
            df = pd.read_csv(
                f,
                sep=';',
                header=None,
                names=['datetime', 'open', 'high', 'low', 'close', 'volume'],
                dtype={'open': float, 'high': float, 'low': float,
                       'close': float, 'volume': float},
            )
        # Parse datetime
        df['datetime'] = pd.to_datetime(df['datetime'], format='%Y%m%d %H%M%S', errors='coerce')
        df = df.dropna(subset=['datetime'])
        df = df.set_index('datetime')
        df = df.sort_index()
        return df
    except Exception as e:
        print(f"    [!] CSV parse error: {e}")
        return None


# ── Resample M1 → M15 ────────────────────────────────────────────────────────

def resample_to_m15(df_m1: pd.DataFrame) -> pd.DataFrame:
    """Resample M1 OHLCV DataFrame to M15."""
    df = df_m1.resample('15min').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna(subset=['open', 'close'])
    return df


# ── Save / load ───────────────────────────────────────────────────────────────

def save_year_csv(df_m15: pd.DataFrame, pair: str, year: int):
    path = f"data/{pair}_M15_{year}.csv"
    df_m15.to_csv(path)
    print(f"    Saved {len(df_m15):,} M15 bars -> {path}")


def merge_all_years(pair: str, years: list) -> pd.DataFrame:
    """Load and merge all yearly CSVs for a pair."""
    frames = []
    for year in years:
        path = f"data/{pair}_M15_{year}.csv"
        if os.path.exists(path):
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep='last')]
    return combined


def save_combined(pair: str, years: list):
    """Save merged file for backtest use."""
    df = merge_all_years(pair, years)
    if df.empty:
        print(f"  [!] No data to merge for {pair}")
        return
    path = f"data/{pair}_M15_ALL.csv"
    df.to_csv(path)
    earliest = df.index.min().strftime('%Y-%m-%d')
    latest   = df.index.max().strftime('%Y-%m-%d')
    print(f"  Combined: {len(df):,} M15 bars | {earliest} to {latest} -> {path}")


# ── Progress helpers ──────────────────────────────────────────────────────────

def bar(current, total, width=30):
    filled = int(width * current / total)
    return f"[{'#' * filled}{'.' * (width - filled)}] {current}/{total}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download HistData.com M15 forex data")
    parser.add_argument('--pairs', nargs='+',
                        choices=list(PAIRS.keys()),
                        default=list(PAIRS.keys()),
                        help='Pairs to download (default: all)')
    parser.add_argument('--from', dest='year_from', type=int, default=2020,
                        help='Start year (default: 2020)')
    parser.add_argument('--to', dest='year_to', type=int,
                        default=datetime.utcnow().year - 1,
                        help=f'End year (default: {datetime.utcnow().year - 1})')
    parser.add_argument('--skip-existing', action='store_true', default=True,
                        help='Skip already downloaded years (default: True)')
    parser.add_argument('--no-skip', dest='skip_existing', action='store_false',
                        help='Re-download even if file exists')
    args = parser.parse_args()

    years = list(range(args.year_from, args.year_to + 1))

    print(f"\n{'='*58}")
    print(f"  APEX Capital AI — HistData.com Downloader")
    print(f"{'='*58}")
    print(f"  Pairs  : {', '.join(args.pairs)}")
    print(f"  Years  : {args.year_from} - {args.year_to}  ({len(years)} years)")
    print(f"  Output : data/  (M15 CSV per year + combined ALL file)")
    print(f"{'='*58}\n")

    session = requests.Session()
    total_jobs = len(args.pairs) * len(years)
    done = 0

    for pair in args.pairs:
        print(f"\n[{pair}] Downloading {len(years)} years...")
        pair_success = 0

        for year in years:
            done += 1
            out_path = f"data/{pair}_M15_{year}.csv"

            if args.skip_existing and os.path.exists(out_path):
                size = os.path.getsize(out_path)
                if size > 1000:
                    print(f"  {bar(done, total_jobs)}  {pair} {year} — SKIP (exists, {size//1024}KB)")
                    pair_success += 1
                    continue

            print(f"  {bar(done, total_jobs)}  {pair} {year} — downloading...", end='', flush=True)

            df_m1 = download_year(session, pair, year)
            if df_m1 is None or df_m1.empty:
                print(f" FAILED")
                time.sleep(2)
                continue

            df_m15 = resample_to_m15(df_m1)
            save_year_csv(df_m15, pair, year)
            pair_success += 1

            # Polite delay between requests
            time.sleep(1.5)

        # Merge all successful years
        if pair_success > 0:
            print(f"\n  Merging {pair_success} year(s) for {pair}...")
            save_combined(pair, years)

    print(f"\n{'='*58}")
    print(f"  Download complete.")
    print(f"  Files saved in: data/")
    print(f"  Run backtest:   python backtest.py --all --from 2020-01-01 --csv")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    main()
