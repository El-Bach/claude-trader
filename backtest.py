"""
backtest.py — APEX Capital AI Rule-Based Historical Backtest
=============================================================
Tests the mechanical indicator foundation of each entry agent
using real MT5 historical data. No Claude API calls — free to run.

Rules implemented:
  GOLD   : EMA stack + ADX > 25 + RSI + DI cross (H4 + H1 confirmation)
  EURUSD : EMA stack + ADX > 22 + RSI + Stochastic pullback (H4 trend + H1 entry)
  USDJPY : EMA stack + ADX > 25 + RSI + Ichimoku (H4, strict)

Output:
  - Terminal summary (win rate, R:R, drawdown, P&L sim)
  - logs/backtest_{AGENT}_{from}_{to}.csv

Usage:
    python backtest.py --agent GOLD
    python backtest.py --agent EURUSD --from 2025-01-01
    python backtest.py --agent USDJPY --from 2025-06-01 --to 2025-12-31
    python backtest.py --all
    python backtest.py --all --smc --from 2024-01-01 --csv
    python backtest.py --all --compare --from 2024-01-01 --csv
"""

import os
import sys
import csv
import argparse
from datetime import datetime, timedelta

# Windows console UTF-8 fix
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy  as np
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

os.makedirs("logs", exist_ok=True)

# ── Agent configs ─────────────────────────────────────────────────────────────
AGENTS = {
    "GOLD": {
        "symbol":   "XAUUSD",
        "pip_size": 0.01,
        "sl_mult":  1.2,    # SL = sl_mult * H4 ATR
        "sl_floor": 20.0,   # floor in points
        "tp_mult":  2.5,    # TP = tp_mult * SL
        "session_utc": (7, 19),   # active UTC hours (start inclusive, end exclusive)
    },
    "EURUSD": {
        "symbol":   "EURUSD",
        "pip_size": 0.0001,
        "sl_mult":  1.2,
        "sl_floor": 0.0020,
        "tp_mult":  2.5,
        "session_utc": (7, 19),
    },
    "USDJPY": {
        "symbol":   "USDJPY",
        "pip_size": 0.01,
        "sl_mult":  1.5,
        "sl_floor": 0.80,
        "tp_mult":  2.5,
        "session_utc": (0, 19),   # includes Tokyo
    },
    "GBPUSD": {
        "symbol":   "GBPUSD",
        "pip_size": 0.0001,
        "sl_mult":  1.5,          # wider than EURUSD (1.2x) — Cable higher volatility
        "sl_floor": 0.0030,       # 30 pip floor (vs 20 pips EURUSD)
        "tp_mult":  2.5,
        "session_utc": (7, 19),   # London + NY overlap
    },
}


# ── Indicator calculations ────────────────────────────────────────────────────

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta     = close.diff()
    gain      = delta.clip(lower=0)
    loss      = (-delta).clip(lower=0)
    alpha     = 1.0 / period
    avg_gain  = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss  = loss.ewm(alpha=alpha, adjust=False).mean()
    rs        = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up    = df['high'].diff()
    down  = -df['low'].diff()
    pdm   = up.where((up > down) & (up > 0), 0.0)
    mdm   = down.where((down > up) & (down > 0), 0.0)
    atr_  = calc_atr(df, period)
    alpha = 1.0 / period
    pdi   = 100 * pdm.ewm(alpha=alpha, adjust=False).mean() / atr_.replace(0, np.nan)
    mdi   = 100 * mdm.ewm(alpha=alpha, adjust=False).mean() / atr_.replace(0, np.nan)
    dx    = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan))
    adx   = dx.ewm(alpha=alpha, adjust=False).mean()
    return pd.DataFrame({'adx': adx, 'pdi': pdi, 'mdi': mdi})


def calc_stoch(df: pd.DataFrame, k: int = 14, d: int = 3) -> pd.DataFrame:
    low_k  = df['low'].rolling(k).min()
    high_k = df['high'].rolling(k).max()
    stoch_k = 100 * (df['close'] - low_k) / (high_k - low_k).replace(0, np.nan)
    stoch_d = stoch_k.rolling(d).mean()
    return pd.DataFrame({'stoch_k': stoch_k, 'stoch_d': stoch_d})


def calc_ichimoku(df: pd.DataFrame):
    tenkan = (df['high'].rolling(9).max()  + df['low'].rolling(9).min())  / 2
    kijun  = (df['high'].rolling(26).max() + df['low'].rolling(26).min()) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (df['high'].rolling(52).max() + df['low'].rolling(52).min()) / 2
    # Cloud at current bar = span values from 26 bars ago
    cloud_a = span_a.shift(26)
    cloud_b = span_b.shift(26)
    return pd.DataFrame({
        'tenkan':  tenkan,
        'kijun':   kijun,
        'cloud_a': cloud_a,
        'cloud_b': cloud_b,
    })


# ── SMC Detection (Fair Value Gap / Order Block / Liquidity) ──────────────────

def detect_fvg(df: pd.DataFrame, current_price: float, lookback: int = 60) -> dict:
    """
    Detect Fair Value Gaps (price imbalances) in last `lookback` bars.

    Bullish FVG : candle[i-1].high < candle[i+1].low  → gap zone above c[i-1]
    Bearish FVG : candle[i-1].low  > candle[i+1].high → gap zone below c[i-1]

    A FVG is "filled" when any subsequent bar's range overlaps the gap.
    Returns nearest active bull FVG (support) and bear FVG (resistance),
    plus booleans for whether current price is INSIDE each.
    """
    if len(df) < 3:
        return {'bull': None, 'bear': None, 'in_bull': False, 'in_bear': False}

    tail = df.tail(lookback).reset_index(drop=True)
    n    = len(tail)

    bull_fvg = None   # nearest active bullish FVG (highest high below or around price)
    bear_fvg = None   # nearest active bearish FVG (lowest low above or around price)

    for i in range(1, n - 1):
        c0h = tail['high'].iloc[i - 1]
        c0l = tail['low'].iloc[i - 1]
        c2h = tail['high'].iloc[i + 1]
        c2l = tail['low'].iloc[i + 1]

        # ── Bullish FVG ──
        if c2l > c0h:
            fvg_low  = float(c0h)
            fvg_high = float(c2l)
            # Check fill: any bar after formation entered the gap
            filled = False
            for k in range(i + 2, n):
                if tail['low'].iloc[k] <= fvg_high and tail['high'].iloc[k] >= fvg_low:
                    filled = True
                    break
            if not filled:
                # Keep only if near current price (within 2% above or price inside)
                if fvg_high >= current_price * 0.98:
                    if bull_fvg is None or fvg_high > bull_fvg['high']:
                        bull_fvg = {'high': round(fvg_high, 5),
                                    'low':  round(fvg_low,  5)}

        # ── Bearish FVG ──
        if c2h < c0l:
            fvg_high = float(c0l)
            fvg_low  = float(c2h)
            filled   = False
            for k in range(i + 2, n):
                if tail['low'].iloc[k] <= fvg_high and tail['high'].iloc[k] >= fvg_low:
                    filled = True
                    break
            if not filled:
                if fvg_low <= current_price * 1.02:
                    if bear_fvg is None or fvg_low < bear_fvg['low']:
                        bear_fvg = {'high': round(fvg_high, 5),
                                    'low':  round(fvg_low,  5)}

    in_bull = (bull_fvg is not None
               and bull_fvg['low'] <= current_price <= bull_fvg['high'])
    in_bear = (bear_fvg is not None
               and bear_fvg['low'] <= current_price <= bear_fvg['high'])

    return {'bull': bull_fvg, 'bear': bear_fvg,
            'in_bull': in_bull, 'in_bear': in_bear}


def detect_order_block(df: pd.DataFrame, current_price: float,
                        atr_val: float, lookback: int = 60,
                        min_impulse: int = 2) -> dict:
    """
    Detect Order Blocks — the last candle before a strong impulse move.

    Bullish OB : last bearish candle before min_impulse+ consecutive bull candles
    Bearish OB : last bullish candle before min_impulse+ consecutive bear candles

    OB is "mitigated" (invalidated) when price closes beyond its body.
    'at_bull' = current price within 0.5 ATR of bullish OB zone (potential long entry).
    'at_bear' = current price within 0.5 ATR of bearish OB zone (potential short entry).
    """
    if len(df) < min_impulse + 3:
        return {'bull': None, 'bear': None, 'at_bull': False, 'at_bear': False}

    tail = df.tail(lookback).reset_index(drop=True)
    n    = len(tail)
    prox = atr_val * 0.5   # proximity threshold

    bull_ob = None
    bear_ob = None

    for i in range(n - min_impulse - 1):
        close_i = tail['close'].iloc[i]
        open_i  = tail['open'].iloc[i]

        # ── Bullish OB: bearish candle before impulse up ──
        if close_i < open_i:
            bull_run = 0
            for j in range(i + 1, min(i + 1 + min_impulse + 1, n)):
                if tail['close'].iloc[j] > tail['open'].iloc[j]:
                    bull_run += 1
                else:
                    break
            if bull_run >= min_impulse:
                ob_high = float(open_i)    # bearish candle body top
                ob_low  = float(close_i)   # bearish candle body bottom
                if ob_low < ob_high < current_price:
                    # Not mitigated: no close below ob_low after formation
                    mitigated = any(
                        tail['close'].iloc[k] < ob_low
                        for k in range(i + 1, n)
                    )
                    if not mitigated:
                        if bull_ob is None or ob_high > bull_ob['high']:
                            bull_ob = {'high': round(ob_high, 5),
                                       'low':  round(ob_low,  5)}

        # ── Bearish OB: bullish candle before impulse down ──
        if close_i > open_i:
            bear_run = 0
            for j in range(i + 1, min(i + 1 + min_impulse + 1, n)):
                if tail['close'].iloc[j] < tail['open'].iloc[j]:
                    bear_run += 1
                else:
                    break
            if bear_run >= min_impulse:
                ob_high = float(close_i)   # bullish candle body top
                ob_low  = float(open_i)    # bullish candle body bottom
                if ob_low > current_price > ob_low - prox * 2:
                    mitigated = any(
                        tail['close'].iloc[k] > ob_high
                        for k in range(i + 1, n)
                    )
                    if not mitigated:
                        if bear_ob is None or ob_low < bear_ob['low']:
                            bear_ob = {'high': round(ob_high, 5),
                                       'low':  round(ob_low,  5)}

    at_bull = (bull_ob is not None
               and current_price <= bull_ob['high'] + prox
               and current_price >= bull_ob['low']  - prox)
    at_bear = (bear_ob is not None
               and current_price >= bear_ob['low']  - prox
               and current_price <= bear_ob['high'] + prox)

    return {'bull': bull_ob, 'bear': bear_ob,
            'at_bull': at_bull, 'at_bear': at_bear}


def detect_liquidity(df: pd.DataFrame, current_price: float,
                     lookback: int = 60, tol_pct: float = 0.0015) -> dict:
    """
    Detect equal highs (bear stop clusters above) and equal lows (bull stop clusters below).
    Two swing points are 'equal' if within tol_pct of each other.
    Returns the nearest liquidity target above and below current price.
    """
    if len(df) < 10:
        return {'nearest_high': None, 'nearest_low': None}

    recent = df.tail(lookback)
    highs  = recent['high'].values.astype(float)
    lows   = recent['low'].values.astype(float)

    def _clusters(values: np.ndarray, above: bool) -> list:
        used    = np.zeros(len(values), dtype=bool)
        results = []
        for i in range(len(values)):
            if used[i]:
                continue
            v = values[i]
            mask = np.abs(values - v) / (v + 1e-10) <= tol_pct
            if mask.sum() >= 2:
                avg = float(values[mask].mean())
                if (above and avg > current_price) or (not above and avg < current_price):
                    results.append(round(avg, 5))
                used[mask] = True
        return results

    high_clusters = sorted(_clusters(highs, above=True))
    low_clusters  = sorted(_clusters(lows, above=False), reverse=True)

    return {
        'nearest_high': high_clusters[0] if high_clusters else None,
        'nearest_low':  low_clusters[0]  if low_clusters  else None,
    }


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['ema20']  = calc_ema(df['close'], 20)
    df['ema50']  = calc_ema(df['close'], 50)
    df['ema200'] = calc_ema(df['close'], 200)
    df['rsi']    = calc_rsi(df['close'])
    df['atr']    = calc_atr(df)
    adx = calc_adx(df)
    df['adx']        = adx['adx']
    df['pdi']        = adx['pdi']
    df['mdi']        = adx['mdi']
    # FIX 2: ADX rising — ADX must be increasing, not just above threshold
    df['adx_rising'] = df['adx'] > df['adx'].shift(1)
    # FIX 5: ATR expanding — ATR above its own 5-bar rolling mean (trend accelerating)
    df['atr_ma5']    = df['atr'].rolling(5).mean()
    return df


# ── CSV data fetch (HistData.com local files) ─────────────────────────────────

# Symbol → HistData filename prefix
HISTDATA_SYMBOLS = {
    "XAUUSD": "XAUUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}

_csv_cache: dict = {}   # (symbol, tf_name) -> full DataFrame, loaded once


def fetch_rates_csv(symbol: str, tf_name: str,
                    date_from: datetime, date_to: datetime) -> pd.DataFrame:
    """
    Reads local CSV files from data/ (downloaded by download_histdata.py).
    M15 data is read directly from data/{SYMBOL}_M15_ALL.csv.
    H4 and H1 are resampled from the same M15 source (enough for indicators).
    Results are cached in memory to avoid re-reading on each call.
    """
    cache_key = (symbol, tf_name)
    if cache_key not in _csv_cache:
        hist_sym = HISTDATA_SYMBOLS.get(symbol, symbol)
        path = f"data/{hist_sym}_M15_ALL.csv"
        if not os.path.exists(path):
            print(f"[CSV] ERROR: {path} not found. Run: python download_histdata.py")
            _csv_cache[cache_key] = pd.DataFrame()
            return pd.DataFrame()

        # Load M15 base
        base = pd.read_csv(path, index_col=0, parse_dates=True)
        base.index = pd.to_datetime(base.index, utc=True)
        base = base.sort_index()

        if tf_name == 'M15':
            _csv_cache[cache_key] = base
        elif tf_name == 'H1':
            _csv_cache[cache_key] = base.resample('1h').agg(
                {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
            ).dropna(subset=['open'])
        elif tf_name == 'H4':
            _csv_cache[cache_key] = base.resample('4h').agg(
                {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
            ).dropna(subset=['open'])
        else:
            _csv_cache[cache_key] = base

    df = _csv_cache[cache_key]
    if df.empty:
        return df

    # Filter to requested window
    tz_from = pd.Timestamp(date_from, tz='UTC')
    tz_to   = pd.Timestamp(date_to,   tz='UTC')
    return df[(df.index >= tz_from) & (df.index <= tz_to)].copy()


# ── MT5 data fetch ────────────────────────────────────────────────────────────

def _tf(name: str):
    return {
        'M15': mt5.TIMEFRAME_M15,
        'H1':  mt5.TIMEFRAME_H1,
        'H4':  mt5.TIMEFRAME_H4,
    }[name]


def fetch_rates(symbol: str, tf_name: str, date_from: datetime, date_to: datetime,
                max_bars: int = 0) -> pd.DataFrame:
    """
    If max_bars > 0: use copy_rates_from_pos to get last N bars (ignores date_from).
    Otherwise: use copy_rates_range. Falls back to copy_rates_from_pos(10000) if range returns empty.
    """
    if max_bars > 0:
        rates = mt5.copy_rates_from_pos(symbol, _tf(tf_name), 0, max_bars)
    else:
        rates = mt5.copy_rates_range(symbol, _tf(tf_name), date_from, date_to)
        if rates is None or len(rates) == 0:
            # Fallback: pull all available data and filter by date
            rates = mt5.copy_rates_from_pos(symbol, _tf(tf_name), 0, 10000)

    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.set_index('time', inplace=True)
    df.rename(columns={'tick_volume': 'volume'}, inplace=True)
    df = df[['open', 'high', 'low', 'close', 'volume']]
    # Filter to requested window when using date range
    if max_bars == 0 and date_to is not None:
        df = df[df.index <= pd.Timestamp(date_to, tz='UTC')]
    return df


# ── Signal logic per agent ────────────────────────────────────────────────────

def signal_gold(h4: pd.Series, h1: pd.Series) -> str:
    """
    BUY:  H4 full bull stack (price > EMA20 > EMA50 > EMA200) — EMA200 = macro filter
          ADX > 25 AND rising (FIX 2) | PDI > MDI | RSI > 58 (FIX 4)
          ATR >= ATR_MA5 — trend accelerating, not exhausting (FIX 5)
          H1: price > EMA20 AND RSI > 55
    SELL: mirror
    """
    adx_ok   = h4['adx'] > 25 and bool(h4.get('adx_rising', False))           # FIX 2
    atr_ok   = h4['atr'] >= h4.get('atr_ma5', h4['atr'])                       # FIX 5

    # H4 bull — EMA200 = macro gate (FIX 3)
    if (h4['close'] > h4['ema20'] > h4['ema50'] > h4['ema200']                 # FIX 3
            and adx_ok
            and h4['pdi'] > h4['mdi']
            and h4['rsi'] > 58                                                  # FIX 4
            and atr_ok
            and h1['close'] > h1['ema20']
            and h1['rsi'] > 55):
        return "BUY"
    # H4 bear
    if (h4['close'] < h4['ema20'] < h4['ema50'] < h4['ema200']                 # FIX 3
            and adx_ok
            and h4['mdi'] > h4['pdi']
            and h4['rsi'] < 42                                                  # FIX 4
            and atr_ok
            and h1['close'] < h1['ema20']
            and h1['rsi'] < 45):
        return "SELL"
    return "NO_TRADE"


def signal_eurusd(h4: pd.Series, h1: pd.Series) -> str:
    """
    BUY:  H4 bull stack (price > EMA20 > EMA50 > EMA200) — EMA200 = macro gate (FIX 3)
          ADX > 22 AND rising (FIX 2) | RSI > 58 (FIX 4)
          H1: stoch_k < 25 oversold pullback ONLY — loose fallback removed (FIX 4)
    SELL: mirror
    """
    adx_ok = h4['adx'] > 22 and bool(h4.get('adx_rising', False))              # FIX 2

    # H4 bull — EMA200 macro gate (FIX 3)
    h4_bull = (h4['close'] > h4['ema20'] > h4['ema50']
               and h4['close'] > h4['ema200']                                   # FIX 3
               and adx_ok
               and h4['rsi'] > 58)                                              # FIX 4
    # H4 bear
    h4_bear = (h4['close'] < h4['ema20'] < h4['ema50']
               and h4['close'] < h4['ema200']                                   # FIX 3
               and adx_ok
               and h4['rsi'] < 42)                                              # FIX 4

    # H1 entry — Stochastic pullback ONLY (loose fallback removed) (FIX 4)
    h1_long  = h1.get('stoch_k', 50) < 25 and h1['close'] > h1['ema50']
    h1_short = h1.get('stoch_k', 50) > 75 and h1['close'] < h1['ema50']

    if h4_bull and h1_long:
        return "BUY"
    if h4_bear and h1_short:
        return "SELL"
    return "NO_TRADE"


def signal_gbpusd(h4: pd.Series, h1: pd.Series) -> str:
    """
    BUY:  H4 bull stack (price > EMA20 > EMA50 > EMA200) — EMA200 = macro gate (FIX 3)
          ADX > 28 AND rising (TUNED — raises bar, filters choppy Cable)
          H1: stoch_k < 25 pullback ONLY
    SELL: mirror (RSI < 45, stoch_k > 75)
    """
    adx_ok = h4['adx'] > 28 and bool(h4.get('adx_rising', False))              # TUNED 22→28

    # H4 bull — EMA200 macro gate (FIX 3)
    h4_bull = (h4['close'] > h4['ema20'] > h4['ema50']
               and h4['close'] > h4['ema200']                                   # FIX 3
               and adx_ok
               and h4['pdi'] > h4['mdi']
               and h4['rsi'] > 55)                                              # TUNED (was 58)
    # H4 bear
    h4_bear = (h4['close'] < h4['ema20'] < h4['ema50']
               and h4['close'] < h4['ema200']                                   # FIX 3
               and adx_ok
               and h4['mdi'] > h4['pdi']
               and h4['rsi'] < 45)                                              # TUNED (was 42)

    # H1 entry — Stochastic pullback, loosened for Cable's deep swings
    h1_long  = h1.get('stoch_k', 50) < 25 and h1['close'] > h1['ema50']       # TUNED (was 20)
    h1_short = h1.get('stoch_k', 50) > 75 and h1['close'] < h1['ema50']       # TUNED (was 80)

    if h4_bull and h1_long:
        return "BUY"
    if h4_bear and h1_short:
        return "SELL"
    return "NO_TRADE"


def signal_usdjpy(h4: pd.Series) -> str:
    """
    BUY:  Full H4 bull stack (EMA200 gate)
          ADX > 30 AND rising (TUNED — only strong trends)
          + Ichimoku: tenkan > kijun AND price clearly above cloud (0.25x ATR clearance)
          NOT above 148.00 (tightened BoJ block — was 149)
    SELL: mirror
    """
    adx_ok       = h4['adx'] > 30 and bool(h4.get('adx_rising', False))        # TUNED 25→30
    cloud_top    = max(h4.get('cloud_a', 0), h4.get('cloud_b', 0))
    cloud_bottom = min(h4.get('cloud_a', 0), h4.get('cloud_b', 0))
    atr          = float(h4['atr'])
    cloud_clear  = atr * 0.25                                                   # TUNED: confirmed breakout

    if (h4['close'] > h4['ema20'] > h4['ema50'] > h4['ema200']
            and adx_ok
            and h4['pdi'] > h4['mdi']
            and h4['rsi'] > 60
            and h4.get('tenkan', 0) > h4.get('kijun', 0)
            and h4['close'] > cloud_top + cloud_clear                           # TUNED: clearance
            and h4['close'] < 148.00):                                          # TUNED: 149→148
        return "BUY"

    if (h4['close'] < h4['ema20'] < h4['ema50'] < h4['ema200']
            and adx_ok
            and h4['mdi'] > h4['pdi']
            and h4['rsi'] < 40
            and h4.get('tenkan', 0) < h4.get('kijun', 0)
            and h4['close'] < cloud_bottom - cloud_clear):                      # TUNED: clearance
        return "SELL"

    return "NO_TRADE"


# ── SMC-Enhanced Signal Functions ────────────────────────────────────────────
# These extend the baseline signals with FVG / Order Block / Liquidity context.
# Key difference: RSI threshold relaxed from >58 to >50 when FVG or OB
# confluence is present — because FVG/OB entries ARE pullback entries where
# RSI naturally cools. Structure justifies entry when momentum has paused.


def signal_gold_smc(h4: pd.Series, h1: pd.Series, h1_df: pd.DataFrame) -> tuple:
    """
    Returns (direction, sl_override, tp_override)
    sl_override / tp_override = None means use ATR default
    """
    adx_ok = h4['adx'] > 25 and bool(h4.get('adx_rising', False))
    atr_ok = h4['atr'] >= h4.get('atr_ma5', h4['atr'])
    price  = float(h1['close'])
    atr_h1 = float(h1['atr']) if not pd.isna(h1.get('atr', np.nan)) else h4['atr']

    fvg = detect_fvg(h1_df, price)
    ob  = detect_order_block(h1_df, price, atr_h1)
    liq = detect_liquidity(h1_df, price)

    smc_bull = fvg['in_bull'] or ob['at_bull']
    smc_bear = fvg['in_bear'] or ob['at_bear']

    h4_bull = (h4['close'] > h4['ema20'] > h4['ema50'] > h4['ema200']
               and adx_ok and h4['pdi'] > h4['mdi'] and atr_ok)
    h4_bear = (h4['close'] < h4['ema20'] < h4['ema50'] < h4['ema200']
               and adx_ok and h4['mdi'] > h4['pdi'] and atr_ok)

    # RSI: standard >58 OR relaxed >50 with SMC confluence
    rsi_bull = h4['rsi'] > 58 or (smc_bull and h4['rsi'] > 50)
    rsi_bear = h4['rsi'] < 42 or (smc_bear and h4['rsi'] < 50)

    # H1 entry: standard OR SMC confluence
    h1_long  = (h1['close'] > h1['ema20'] and h1['rsi'] > 55) or smc_bull
    h1_short = (h1['close'] < h1['ema20'] and h1['rsi'] < 45) or smc_bear

    sl_ov = tp_ov = None

    if h4_bull and rsi_bull and h1_long:
        # Tighter SL: below OB.low or FVG.low if available
        if ob['bull'] is not None and ob['at_bull']:
            sl_ov = max(price - ob['bull']['low'], h4['atr'] * 0.8)
        elif fvg['bull'] is not None and fvg['in_bull']:
            sl_ov = max(price - fvg['bull']['low'], h4['atr'] * 0.8)
        # Smarter TP: liquidity target if >= 2x SL away
        if liq['nearest_high'] and sl_ov:
            dist = liq['nearest_high'] - price
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "BUY", sl_ov, tp_ov

    if h4_bear and rsi_bear and h1_short:
        if ob['bear'] is not None and ob['at_bear']:
            sl_ov = max(ob['bear']['high'] - price, h4['atr'] * 0.8)
        elif fvg['bear'] is not None and fvg['in_bear']:
            sl_ov = max(fvg['bear']['high'] - price, h4['atr'] * 0.8)
        if liq['nearest_low'] and sl_ov:
            dist = price - liq['nearest_low']
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "SELL", sl_ov, tp_ov

    return "NO_TRADE", None, None


def signal_eurusd_smc(h4: pd.Series, h1: pd.Series, h1_df: pd.DataFrame) -> tuple:
    adx_ok = h4['adx'] > 22 and bool(h4.get('adx_rising', False))
    price  = float(h1['close'])
    atr_h1 = float(h1['atr']) if not pd.isna(h1.get('atr', np.nan)) else h4['atr']

    fvg = detect_fvg(h1_df, price)
    ob  = detect_order_block(h1_df, price, atr_h1)
    liq = detect_liquidity(h1_df, price)

    smc_bull = fvg['in_bull'] or ob['at_bull']
    smc_bear = fvg['in_bear'] or ob['at_bear']

    h4_bull = (h4['close'] > h4['ema20'] > h4['ema50']
               and h4['close'] > h4['ema200'] and adx_ok)
    h4_bear = (h4['close'] < h4['ema20'] < h4['ema50']
               and h4['close'] < h4['ema200'] and adx_ok)

    rsi_bull = h4['rsi'] > 58 or (smc_bull and h4['rsi'] > 50)
    rsi_bear = h4['rsi'] < 42 or (smc_bear and h4['rsi'] < 50)

    # H1 entry: stoch pullback OR SMC confluence
    stoch_long  = h1.get('stoch_k', 50) < 25 and h1['close'] > h1['ema50']
    stoch_short = h1.get('stoch_k', 50) > 75 and h1['close'] < h1['ema50']
    h1_long  = stoch_long  or smc_bull
    h1_short = stoch_short or smc_bear

    sl_ov = tp_ov = None

    if h4_bull and rsi_bull and h1_long:
        if ob['bull'] is not None and ob['at_bull']:
            sl_ov = max(price - ob['bull']['low'], h4['atr'] * 0.8)
        elif fvg['bull'] is not None and fvg['in_bull']:
            sl_ov = max(price - fvg['bull']['low'], h4['atr'] * 0.8)
        if liq['nearest_high'] and sl_ov:
            dist = liq['nearest_high'] - price
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "BUY", sl_ov, tp_ov

    if h4_bear and rsi_bear and h1_short:
        if ob['bear'] is not None and ob['at_bear']:
            sl_ov = max(ob['bear']['high'] - price, h4['atr'] * 0.8)
        elif fvg['bear'] is not None and fvg['in_bear']:
            sl_ov = max(fvg['bear']['high'] - price, h4['atr'] * 0.8)
        if liq['nearest_low'] and sl_ov:
            dist = price - liq['nearest_low']
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "SELL", sl_ov, tp_ov

    return "NO_TRADE", None, None


def signal_gbpusd_smc(h4: pd.Series, h1: pd.Series, h1_df: pd.DataFrame) -> tuple:
    adx_ok = h4['adx'] > 28 and bool(h4.get('adx_rising', False))              # TUNED 22→28
    price  = float(h1['close'])
    atr_h1 = float(h1['atr']) if not pd.isna(h1.get('atr', np.nan)) else h4['atr']

    fvg = detect_fvg(h1_df, price)
    ob  = detect_order_block(h1_df, price, atr_h1)
    liq = detect_liquidity(h1_df, price)

    smc_bull = fvg['in_bull'] or ob['at_bull']
    smc_bear = fvg['in_bear'] or ob['at_bear']

    h4_bull = (h4['close'] > h4['ema20'] > h4['ema50']
               and h4['close'] > h4['ema200'] and adx_ok
               and h4['pdi'] > h4['mdi'])
    h4_bear = (h4['close'] < h4['ema20'] < h4['ema50']
               and h4['close'] < h4['ema200'] and adx_ok
               and h4['mdi'] > h4['pdi'])

    rsi_bull = h4['rsi'] > 55 or (smc_bull and h4['rsi'] > 48)
    rsi_bear = h4['rsi'] < 45 or (smc_bear and h4['rsi'] < 52)

    stoch_long  = h1.get('stoch_k', 50) < 25 and h1['close'] > h1['ema50']
    stoch_short = h1.get('stoch_k', 50) > 75 and h1['close'] < h1['ema50']
    # stoch OR smc — double confirmation preferred but either valid with ADX>28 gate
    h1_long  = stoch_long  or smc_bull
    h1_short = stoch_short or smc_bear

    sl_ov = tp_ov = None

    if h4_bull and rsi_bull and h1_long:
        if ob['bull'] is not None and ob['at_bull']:
            sl_ov = max(price - ob['bull']['low'], h4['atr'] * 0.8)
        elif fvg['bull'] is not None and fvg['in_bull']:
            sl_ov = max(price - fvg['bull']['low'], h4['atr'] * 0.8)
        if liq['nearest_high'] and sl_ov:
            dist = liq['nearest_high'] - price
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "BUY", sl_ov, tp_ov

    if h4_bear and rsi_bear and h1_short:
        if ob['bear'] is not None and ob['at_bear']:
            sl_ov = max(ob['bear']['high'] - price, h4['atr'] * 0.8)
        elif fvg['bear'] is not None and fvg['in_bear']:
            sl_ov = max(fvg['bear']['high'] - price, h4['atr'] * 0.8)
        if liq['nearest_low'] and sl_ov:
            dist = price - liq['nearest_low']
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "SELL", sl_ov, tp_ov

    return "NO_TRADE", None, None


def signal_usdjpy_smc(h4: pd.Series, h4_df: pd.DataFrame) -> tuple:
    adx_ok       = h4['adx'] > 30 and bool(h4.get('adx_rising', False))        # TUNED 25→30
    cloud_top    = max(h4.get('cloud_a', 0), h4.get('cloud_b', 0))
    cloud_bottom = min(h4.get('cloud_a', 0), h4.get('cloud_b', 0))
    price        = float(h4['close'])
    atr_val      = float(h4['atr'])
    cloud_clear  = atr_val * 0.25                                               # TUNED: confirmed breakout

    fvg = detect_fvg(h4_df, price, lookback=40)
    ob  = detect_order_block(h4_df, price, atr_val, lookback=40)
    liq = detect_liquidity(h4_df, price, lookback=40)

    smc_bull = fvg['in_bull'] or ob['at_bull']
    smc_bear = fvg['in_bear'] or ob['at_bear']

    rsi_bull = h4['rsi'] > 60 or (smc_bull and h4['rsi'] > 52)
    rsi_bear = h4['rsi'] < 40 or (smc_bear and h4['rsi'] < 48)

    sl_ov = tp_ov = None

    if (h4['close'] > h4['ema20'] > h4['ema50'] > h4['ema200']
            and adx_ok and h4['pdi'] > h4['mdi'] and rsi_bull
            and h4.get('tenkan', 0) > h4.get('kijun', 0)
            and h4['close'] > cloud_top + cloud_clear                           # TUNED: clearance
            and h4['close'] < 148.00):                                          # TUNED: 149→148
        if ob['bull'] is not None and ob['at_bull']:
            sl_ov = max(price - ob['bull']['low'], atr_val * 0.8)
        elif fvg['bull'] is not None and fvg['in_bull']:
            sl_ov = max(price - fvg['bull']['low'], atr_val * 0.8)
        if liq['nearest_high'] and sl_ov:
            dist = liq['nearest_high'] - price
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "BUY", sl_ov, tp_ov

    if (h4['close'] < h4['ema20'] < h4['ema50'] < h4['ema200']
            and adx_ok and h4['mdi'] > h4['pdi'] and rsi_bear
            and h4.get('tenkan', 0) < h4.get('kijun', 0)
            and h4['close'] < cloud_bottom - cloud_clear):                      # TUNED: clearance
        if ob['bear'] is not None and ob['at_bear']:
            sl_ov = max(ob['bear']['high'] - price, atr_val * 0.8)
        elif fvg['bear'] is not None and fvg['in_bear']:
            sl_ov = max(fvg['bear']['high'] - price, atr_val * 0.8)
        if liq['nearest_low'] and sl_ov:
            dist = price - liq['nearest_low']
            if dist >= sl_ov * 2.0:
                tp_ov = dist
        return "SELL", sl_ov, tp_ov

    return "NO_TRADE", None, None


# ── Trade simulation ──────────────────────────────────────────────────────────

def simulate_trade(df_m15: pd.DataFrame, entry_idx: int,
                   direction: str, sl: float, tp: float) -> tuple:
    """
    Returns (outcome, pnl_pts, bars_held, exit_time)
      outcome: 'WIN' | 'LOSS' | 'TIME_EXIT'

    FIX 1: Time-based exit at 60 bars (~15 hours on M15).
    TIME_EXIT closes at the bar-60 close price, returning actual P&L.
    This frees capital from stuck trades and gives a realistic cost.
    """
    if entry_idx + 1 >= len(df_m15):
        return 'TIME_EXIT', 0.0, 0, None

    entry_price = df_m15['open'].iloc[entry_idx + 1]
    if direction == 'BUY':
        sl_price = entry_price - sl
        tp_price = entry_price + tp
    else:
        sl_price = entry_price + sl
        tp_price = entry_price - tp

    max_bars = 60   # FIX 1: ~15 hours on M15 (was 200 = ~50 hours)
    end_idx  = min(entry_idx + 1 + max_bars, len(df_m15))

    for i in range(entry_idx + 1, end_idx):
        high = df_m15['high'].iloc[i]
        low  = df_m15['low'].iloc[i]
        t    = df_m15.index[i]

        if direction == 'BUY':
            if low <= sl_price:
                return 'LOSS', sl_price - entry_price, i - entry_idx, t
            if high >= tp_price:
                return 'WIN',  tp_price - entry_price, i - entry_idx, t
        else:  # SELL
            if high >= sl_price:
                return 'LOSS', sl_price - entry_price, i - entry_idx, t
            if low <= tp_price:
                return 'WIN',  entry_price - tp_price, i - entry_idx, t

    # FIX 1: Time exit — close at bar-60 close price, calculate actual P&L
    exit_bar   = min(entry_idx + max_bars, len(df_m15) - 1)
    exit_price = df_m15['close'].iloc[exit_bar]
    exit_time  = df_m15.index[exit_bar]
    if direction == 'BUY':
        pnl = exit_price - entry_price
    else:
        pnl = entry_price - exit_price
    return 'TIME_EXIT', pnl, max_bars, exit_time


# ── Core backtest runner ──────────────────────────────────────────────────────

def run_backtest(agent_name: str, date_from: datetime, date_to: datetime,
                 use_csv: bool = False, use_smc: bool = False) -> list:
    cfg    = AGENTS[agent_name]
    symbol = cfg['symbol']

    print(f"\n[BACKTEST] Fetching {symbol} historical data{'  [CSV mode]' if use_csv else ''}...")

    if use_csv:
        df_h4  = fetch_rates_csv(symbol, 'H4',  date_from, date_to)
        df_h1  = fetch_rates_csv(symbol, 'H1',  date_from, date_to)
        df_m15 = fetch_rates_csv(symbol, 'M15', date_from, date_to)
    else:
        # H4/H1: pull all available bars (EMA200 needs warmup; copy_rates_from_pos avoids
        # date boundary errors when start date is near the edge of MT5 history)
        df_h4  = fetch_rates(symbol, 'H4',  date_from, date_to, max_bars=10000)
        df_h1  = fetch_rates(symbol, 'H1',  date_from, date_to, max_bars=15000)
        # M15: use exact date range (can be very large — use copy_rates_range directly)
        df_m15 = fetch_rates(symbol, 'M15', date_from, date_to)

    if df_h4.empty or df_m15.empty:
        print(f"[BACKTEST] ERROR: No data returned for {symbol}. Check MT5 connection.")
        return []

    print(f"[BACKTEST] Data: H4={len(df_h4)} | H1={len(df_h1)} | M15={len(df_m15)} candles")

    # ── Build indicators ─────────────────────────────────────────────
    h4_ind = build_indicators(df_h4)
    h1_ind = build_indicators(df_h1)

    # EURUSD / GBPUSD: add Stochastic on H1
    if agent_name in ('EURUSD', 'GBPUSD'):
        stoch = calc_stoch(df_h1)
        h1_ind = pd.concat([h1_ind, stoch], axis=1)

    # USDJPY: add Ichimoku on H4
    if agent_name == 'USDJPY':
        ichi = calc_ichimoku(df_h4)
        h4_ind = pd.concat([h4_ind, ichi], axis=1)

    # ── Signal loop ──────────────────────────────────────────────────
    session_start, session_end = cfg['session_utc']
    trades  = []
    in_trade_until = None   # index: don't re-enter until this bar closes

    print(f"[BACKTEST] Running signals on {len(df_m15)} M15 candles...")

    for idx in range(len(df_m15)):
        bar_time = df_m15.index[idx]

        # Session filter
        if not (session_start <= bar_time.hour < session_end):
            continue

        # Cooldown: wait for current trade to finish
        if in_trade_until is not None and idx < in_trade_until:
            continue

        # Get latest H4 / H1 values at this M15 bar
        h4_row = h4_ind[h4_ind.index <= bar_time]
        h1_row = h1_ind[h1_ind.index <= bar_time]

        if h4_row.empty or h1_row.empty:
            continue
        h4 = h4_row.iloc[-1]
        h1 = h1_row.iloc[-1]

        # Skip if indicators are NaN (warm-up period)
        if pd.isna(h4['ema200']) or pd.isna(h4['adx']) or pd.isna(h4['rsi']):
            continue

        # ── Signal ───────────────────────────────────────────────────
        sl_override = tp_override = None

        if use_smc:
            if agent_name == 'GOLD':
                if pd.isna(h1['ema20']) or pd.isna(h1['rsi']):
                    continue
                direction, sl_override, tp_override = signal_gold_smc(h4, h1, h1_row)
            elif agent_name == 'EURUSD':
                if pd.isna(h1['ema20']):
                    continue
                direction, sl_override, tp_override = signal_eurusd_smc(h4, h1, h1_row)
            elif agent_name == 'GBPUSD':
                if pd.isna(h1['ema20']):
                    continue
                direction, sl_override, tp_override = signal_gbpusd_smc(h4, h1, h1_row)
            else:  # USDJPY
                if pd.isna(h4.get('tenkan', np.nan)):
                    continue
                direction, sl_override, tp_override = signal_usdjpy_smc(h4, h4_row)
        else:
            if agent_name == 'GOLD':
                if pd.isna(h1['ema20']) or pd.isna(h1['rsi']):
                    continue
                direction = signal_gold(h4, h1)
            elif agent_name == 'EURUSD':
                if pd.isna(h1['ema20']):
                    continue
                direction = signal_eurusd(h4, h1)
            elif agent_name == 'GBPUSD':
                if pd.isna(h1['ema20']):
                    continue
                direction = signal_gbpusd(h4, h1)
            else:  # USDJPY
                if pd.isna(h4.get('tenkan', np.nan)):
                    continue
                direction = signal_usdjpy(h4)

        if direction == 'NO_TRADE':
            continue

        # ── SL / TP ───────────────────────────────────────────────────
        atr_val = h4['atr']
        sl_pts  = sl_override if sl_override else max(cfg['sl_mult'] * atr_val, cfg['sl_floor'])
        sl_pts  = max(sl_pts, cfg['sl_floor'])   # always respect floor
        tp_pts  = tp_override if tp_override else cfg['tp_mult'] * sl_pts

        # ── Simulate ──────────────────────────────────────────────────
        outcome, pnl_pts, bars_held, exit_time = simulate_trade(
            df_m15, idx, direction, sl_pts, tp_pts
        )

        entry_price = df_m15['open'].iloc[idx + 1] if idx + 1 < len(df_m15) else df_m15['close'].iloc[idx]

        trades.append({
            "signal_time":  bar_time.strftime('%Y-%m-%d %H:%M'),
            "exit_time":    exit_time.strftime('%Y-%m-%d %H:%M') if exit_time else '',
            "direction":    direction,
            "entry_price":  round(entry_price, 5),
            "sl_pts":       round(sl_pts, 5),
            "tp_pts":       round(tp_pts, 5),
            "outcome":      outcome,
            "pnl_pts":      round(pnl_pts, 5),
            "bars_held":    bars_held,
            "h4_adx":       round(h4['adx'], 1),
            "h4_rsi":       round(h4['rsi'], 1),
            "h4_atr":       round(atr_val, 5),
        })

        # Advance cooldown to after this trade closes
        in_trade_until = idx + 1 + bars_held

    return trades


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(agent_name: str, trades: list, date_from: datetime, date_to: datetime,
                 start_balance: float = 12200.0, risk_pct: float = 0.01):

    cfg = AGENTS[agent_name]

    print(f"\n{'='*58}")
    print(f"  APEX Capital AI — Backtest Results")
    print(f"{'='*58}")
    print(f"  Agent  : {agent_name} ({cfg['symbol']})")
    print(f"  Period : {date_from.strftime('%Y-%m-%d')} → {date_to.strftime('%Y-%m-%d')}")
    print(f"{'='*58}")

    if not trades:
        print("  No trades generated. Check date range and data.")
        return

    wins      = [t for t in trades if t['outcome'] == 'WIN']
    losses    = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts  = [t for t in trades if t['outcome'] in ('TIMEOUT', 'TIME_EXIT')]
    decided   = wins + losses  # exclude time exits from win rate

    total    = len(trades)
    n_wins   = len(wins)
    n_loss   = len(losses)
    n_to     = len(timeouts)
    win_rate = n_wins / len(decided) * 100 if decided else 0
    rr       = cfg['tp_mult'] / 1.0        # fixed R:R based on multipliers
    ev       = (win_rate / 100 * rr) - ((1 - win_rate / 100) * 1.0)  # per 1R

    print(f"\n  SIGNALS & OUTCOMES")
    print(f"  {'─'*50}")
    print(f"  Total trades   : {total}")
    print(f"  Wins           : {n_wins}  ({n_wins/total*100:.1f}%)")
    print(f"  Losses         : {n_loss}  ({n_loss/total*100:.1f}%)")
    print(f"  Time Exit(15h) : {n_to}   ({n_to/total*100:.1f}%)")
    print(f"  Win rate       : {win_rate:.1f}%  (excl. time exit)")
    print(f"  Avg R:R        : {rr:.1f}x (fixed by SL/TP multipliers)")
    print(f"  Expected value : {ev:+.2f}R per trade")

    # Max consecutive losses
    max_consec_loss = 0
    consec = 0
    for t in trades:
        if t['outcome'] in ('LOSS', 'TIME_EXIT'):
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        elif t['outcome'] == 'WIN':
            consec = 0

    print(f"  Max consec. L  : {max_consec_loss}")

    # P&L simulation (risk-based)
    # TIME_EXIT uses actual pnl_pts scaled by sl_pts to get R-value
    risk_per_trade = start_balance * risk_pct
    balance        = start_balance
    peak           = start_balance
    max_dd         = 0.0
    pnl_curve      = []

    for t in trades:
        if t['outcome'] == 'WIN':
            balance += risk_per_trade * rr
        elif t['outcome'] == 'LOSS':
            balance -= risk_per_trade
        elif t['outcome'] == 'TIME_EXIT':
            # Actual P&L scaled to risk: (pnl_pts / sl_pts) * risk_per_trade
            sl = t.get('sl_pts', 0)
            if sl and sl > 0:
                balance += (t['pnl_pts'] / sl) * risk_per_trade
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100
        if dd > max_dd:
            max_dd = dd
        pnl_curve.append(balance)

    total_pnl = balance - start_balance
    total_pct = total_pnl / start_balance * 100

    print(f"\n  P&L SIMULATION ({risk_pct*100:.0f}% risk, ${start_balance:,.0f} start)")
    print(f"  {'─'*50}")
    print(f"  Start balance : ${start_balance:,.2f}")
    print(f"  End balance   : ${balance:,.2f}")
    print(f"  Total P&L     : ${total_pnl:+,.2f}  ({total_pct:+.1f}%)")
    print(f"  Max drawdown  : {max_dd:.1f}%")

    # Profit factor — include TIME_EXIT contributions
    te_pnl     = sum((t['pnl_pts'] / t['sl_pts']) * risk_per_trade
                     for t in timeouts
                     if t['outcome'] == 'TIME_EXIT' and t.get('sl_pts', 0) > 0)
    gross_win  = n_wins * risk_per_trade * rr + max(te_pnl, 0)
    gross_loss = n_loss * risk_per_trade + abs(min(te_pnl, 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    print(f"  Profit factor : {pf:.2f}")
    print(f"{'='*58}")


def save_csv(agent_name: str, trades: list, date_from: datetime, date_to: datetime):
    if not trades:
        return
    fname = (f"logs/backtest_{agent_name}_"
             f"{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.csv")
    fields = list(trades[0].keys())
    with open(fname, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    print(f"\n  Results saved → {fname}")


def print_comparison(agent_name: str, baseline: list, smc: list,
                     date_from: datetime, date_to: datetime,
                     start_balance: float = 12200.0, risk_pct: float = 0.01):
    """Print side-by-side comparison of baseline vs SMC-enhanced results."""
    cfg = AGENTS[agent_name]
    rr  = cfg['tp_mult']

    def _stats(trades):
        if not trades:
            return {}
        wins     = [t for t in trades if t['outcome'] == 'WIN']
        losses   = [t for t in trades if t['outcome'] == 'LOSS']
        timeouts = [t for t in trades if t['outcome'] == 'TIME_EXIT']
        decided  = wins + losses
        wr       = len(wins) / len(decided) * 100 if decided else 0
        ev       = (wr / 100 * rr) - ((1 - wr / 100) * 1.0)

        risk_pt  = start_balance * risk_pct
        balance  = start_balance
        peak     = start_balance
        max_dd   = 0.0
        for t in trades:
            if t['outcome'] == 'WIN':
                balance += risk_pt * rr
            elif t['outcome'] == 'LOSS':
                balance -= risk_pt
            elif t['outcome'] == 'TIME_EXIT':
                sl = t.get('sl_pts', 0)
                if sl > 0:
                    balance += (t['pnl_pts'] / sl) * risk_pt
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100
            if dd > max_dd:
                max_dd = dd

        te_pnl     = sum((t['pnl_pts'] / t['sl_pts']) * risk_pt
                         for t in timeouts if t.get('sl_pts', 0) > 0)
        gross_win  = len(wins) * risk_pt * rr + max(te_pnl, 0)
        gross_loss = len(losses) * risk_pt + abs(min(te_pnl, 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

        return {
            'total':   len(trades),
            'wins':    len(wins),
            'losses':  len(losses),
            'te':      len(timeouts),
            'wr':      wr,
            'ev':      ev,
            'pnl':     balance - start_balance,
            'pnl_pct': (balance - start_balance) / start_balance * 100,
            'max_dd':  max_dd,
            'pf':      pf,
        }

    b = _stats(baseline)
    s = _stats(smc)

    if not b or not s:
        print("  Insufficient data for comparison.")
        return

    def _chg(new, old, pct=False):
        if old == 0:
            return "N/A"
        diff = new - old
        if pct:
            return f"{diff:+.1f}pp"
        rel = diff / abs(old) * 100
        return f"{rel:+.0f}%"

    print(f"\n{'='*62}")
    print(f"  SMC vs Baseline — {agent_name} ({cfg['symbol']})")
    print(f"  Period: {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')}")
    print(f"{'='*62}")
    print(f"  {'Metric':<22} {'BASELINE':>10} {'SMC':>10} {'CHANGE':>10}")
    print(f"  {'─'*58}")
    print(f"  {'Trades':<22} {b['total']:>10} {s['total']:>10} {_chg(s['total'],b['total']):>10}")
    print(f"  {'Win rate (excl TE)':<22} {b['wr']:>9.1f}% {s['wr']:>9.1f}% {_chg(s['wr'],b['wr'],True):>10}")
    print(f"  {'Profit factor':<22} {b['pf']:>10.2f} {s['pf']:>10.2f} {_chg(s['pf'],b['pf']):>10}")
    print(f"  {'Total P&L':<22} ${b['pnl']:>+9,.0f} ${s['pnl']:>+9,.0f} {_chg(s['pnl'],b['pnl']):>10}")
    print(f"  {'P&L %':<22} {b['pnl_pct']:>+9.1f}% {s['pnl_pct']:>+9.1f}% {_chg(s['pnl_pct'],b['pnl_pct'],True):>10}")
    print(f"  {'Max drawdown':<22} {b['max_dd']:>9.1f}% {s['max_dd']:>9.1f}% {_chg(s['max_dd'],b['max_dd'],True):>10}")
    print(f"  {'Expected value':<22} {b['ev']:>+9.2f}R {s['ev']:>+9.2f}R {_chg(s['ev'],b['ev']):>10}")
    print(f"  {'─'*58}")

    # Verdict
    score = 0
    if s['wr']      > b['wr']:      score += 1
    if s['pf']      > b['pf']:      score += 1
    if s['pnl']     > b['pnl']:     score += 1
    if s['max_dd']  < b['max_dd']:  score += 1
    if s['ev']      > b['ev']:      score += 1

    if score >= 4:
        verdict = "SMC BETTER — recommend adding to live agents"
    elif score == 3:
        verdict = "SMC MARGINAL — run longer period before deciding"
    else:
        verdict = "BASELINE BETTER — keep current system"

    print(f"  Score: {score}/5 metrics improved")
    print(f"  VERDICT: {verdict}")
    print(f"{'='*62}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="APEX Capital AI Rule-Based Backtest")
    parser.add_argument('--agent',  choices=['GOLD', 'EURUSD', 'GBPUSD', 'USDJPY', 'all'],
                        default='all', help='Agent to backtest (default: all)')
    parser.add_argument('--all',    action='store_true', help='Run all three agents')
    parser.add_argument('--from',   dest='date_from', default='2025-01-01',
                        help='Start date YYYY-MM-DD (default: 2025-01-01)')
    parser.add_argument('--to',     dest='date_to',
                        default=datetime.utcnow().strftime('%Y-%m-%d'),
                        help='End date YYYY-MM-DD (default: today)')
    parser.add_argument('--balance', type=float, default=12200.0,
                        help='Starting balance for P&L sim (default: 12200)')
    parser.add_argument('--risk',    type=float, default=0.01,
                        help='Risk per trade as decimal (default: 0.01 = 1%%)')
    parser.add_argument('--csv',     action='store_true',
                        help='Use local CSV files from data/ instead of MT5 '
                             '(run download_histdata.py first)')
    parser.add_argument('--smc',     action='store_true',
                        help='Use SMC-enhanced signals (FVG + Order Block + Liquidity)')
    parser.add_argument('--compare', action='store_true',
                        help='Run both baseline and SMC, print side-by-side comparison')
    args = parser.parse_args()

    date_from = datetime.strptime(args.date_from, '%Y-%m-%d')
    date_to   = datetime.strptime(args.date_to,   '%Y-%m-%d') + timedelta(days=1)

    agents_to_run = (list(AGENTS.keys())
                     if args.agent == 'all' or args.all
                     else [args.agent])

    print(f"\n{'='*58}")
    print(f"  APEX Capital AI — Rule-Based Backtest")
    print(f"{'='*58}")
    print(f"  Period  : {args.date_from} -> {args.date_to}")
    print(f"  Agents  : {', '.join(agents_to_run)}")
    print(f"  Balance : ${args.balance:,.0f}  |  Risk: {args.risk*100:.1f}% per trade")
    mode = 'SMC-Enhanced' if args.smc else ('Comparison' if args.compare else 'Baseline')
    print(f"  Source  : {'Local CSV (data/)' if args.csv else 'MT5 live connection'}")
    print(f"  Mode    : {mode}")
    print(f"{'='*58}")

    if args.csv:
        # CSV mode — no MT5 needed
        missing = []
        for agent in agents_to_run:
            sym = HISTDATA_SYMBOLS.get(AGENTS[agent]['symbol'], AGENTS[agent]['symbol'])
            path = f"data/{sym}_M15_ALL.csv"
            if not os.path.exists(path):
                missing.append(path)
        if missing:
            print(f"\n[CSV] Missing data files:")
            for m in missing:
                print(f"  {m}")
            print(f"\n  Run first:  python download_histdata.py")
            sys.exit(1)

        for agent in agents_to_run:
            if args.compare:
                baseline = run_backtest(agent, date_from, date_to,
                                        use_csv=True, use_smc=False)
                smc_res  = run_backtest(agent, date_from, date_to,
                                        use_csv=True, use_smc=True)
                print_report(agent, baseline, date_from, date_to, args.balance, args.risk)
                save_csv(agent, baseline, date_from, date_to)
                save_csv(agent, smc_res,  date_from, date_to)
                print_comparison(agent, baseline, smc_res,
                                 date_from, date_to, args.balance, args.risk)
            else:
                trades = run_backtest(agent, date_from, date_to,
                                      use_csv=True, use_smc=args.smc)
                print_report(agent, trades, date_from, date_to, args.balance, args.risk)
                save_csv(agent, trades, date_from, date_to)
    else:
        # MT5 mode
        login    = int(os.getenv("MT5_LOGIN",    0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER",   "")

        print(f"\n[MT5] Connecting...")
        if not mt5.initialize(login=login, password=password, server=server):
            print(f"[MT5] ERROR: Could not connect — {mt5.last_error()}")
            print("      Make sure MT5 terminal is open and logged in.")
            sys.exit(1)
        print(f"[MT5] Connected.")

        try:
            for agent in agents_to_run:
                if args.compare:
                    baseline = run_backtest(agent, date_from, date_to,
                                            use_csv=False, use_smc=False)
                    smc_res  = run_backtest(agent, date_from, date_to,
                                            use_csv=False, use_smc=True)
                    print_report(agent, baseline, date_from, date_to, args.balance, args.risk)
                    save_csv(agent, baseline, date_from, date_to)
                    save_csv(agent, smc_res,  date_from, date_to)
                    print_comparison(agent, baseline, smc_res,
                                     date_from, date_to, args.balance, args.risk)
                else:
                    trades = run_backtest(agent, date_from, date_to,
                                          use_csv=False, use_smc=args.smc)
                    print_report(agent, trades, date_from, date_to, args.balance, args.risk)
                    save_csv(agent, trades, date_from, date_to)
        finally:
            mt5.shutdown()

    print(f"\n[BACKTEST] Done.\n")


if __name__ == "__main__":
    main()
