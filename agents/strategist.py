"""
STRATEGIST — Daily Top-Down Technical Analyst
APEX Capital AI

Role: Professional chart analyst who runs once per day (07:00 UTC) to create
execution plans for each instrument. Mirrors a real trading desk workflow:

  Analysis  → Chart markup → Market structure → Price action → Execution plan

Uses:
  D1 candles  — weekly structure, key S/R, macro trend
  H4 candles  — session structure, trend direction, entry zones
  H1 candles  — current leg, near-term levels, entry timing context

Calls Claude Opus once per instrument (4 calls/day).
Distributes plans to entry agents via agent.receive_strategy_plan(plan).

Output per instrument:
  - Daily bias (BULLISH / BEARISH / NEUTRAL / WAIT)
  - Market structure notes (HH/HL or LH/LL, key swing points)
  - Key support & resistance levels
  - Entry zone (price area to watch)
  - Invalidation level (where the idea is wrong)
  - Trade idea (brief thesis)
  - TP target
  - SL suggestion
  - Session notes

Usage:
    strategist.run_daily()                   # runs all 4 instruments
    strategist.run_daily(["GOLD", "EURUSD"]) # specific instruments
    plan = strategist.get_plan("GOLD")       # retrieve stored plan
"""

import os
import glob
import json
import time
import anthropic
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests
import feedparser

load_dotenv()

INSTRUMENTS = {
    "GOLD": {
        "symbol":     "XAUUSD",
        "pip_size":   1.0,
        "round_levels": [1900, 1950, 2000, 2050, 2100, 2150, 2200,
                         2250, 2300, 2350, 2400, 2450, 2500, 2600,
                         2700, 2800, 2900, 3000, 3100, 3200, 3300],
        "description": "Gold (XAU/USD) — safe haven / USD inverse / inflation hedge",
    },
    "EURUSD": {
        "symbol":     "EURUSD",
        "pip_size":   0.0001,
        "round_levels": [1.0200, 1.0400, 1.0500, 1.0600, 1.0800,
                         1.1000, 1.1200, 1.1400, 1.1500],
        "description": "Euro/USD — primary DXY inverse, ECB vs Fed driven",
    },
    "GBPUSD": {
        "symbol":     "GBPUSD",
        "pip_size":   0.0001,
        "round_levels": [1.2000, 1.2500, 1.3000, 1.3500, 1.4000],
        "description": "Cable (GBP/USD) — BoE vs Fed driven, high volatility",
    },
    "USDJPY": {
        "symbol":     "USDJPY",
        "pip_size":   0.01,
        "round_levels": [140.0, 142.0, 145.0, 147.0, 148.0,
                         150.0, 151.0, 152.0, 155.0, 158.0],
        "description": "USD/JPY — USD strength + risk sentiment, BoJ intervention above 150",
    },
}

D1_BARS  = 60   # ~60 days of daily candles
H4_BARS  = 120  # ~20 days of H4
H1_BARS  = 96   # ~4 days of H1

MEMORY_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "logs", "strategist_memory.json")
MAX_MEMORY_ITEMS = 10    # max stored entries per category per instrument
WEB_CACHE_HOURS  = 6     # hours before re-fetching web content

# RSS feeds: (source_name, url, [relevant_instruments])
WEB_SOURCES = [
    ("Fed",       "https://www.federalreserve.gov/feeds/press_monetary.xml",
                  ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]),
    ("ForexLive", "https://www.forexlive.com/feed/",
                  ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]),
    ("ECB",       "https://www.ecb.europa.eu/rss/press.rss",
                  ["EURUSD", "GBPUSD"]),
    ("BoE",       "https://www.bankofengland.co.uk/rss/publications",
                  ["GBPUSD"]),
    ("FXStreet",  "https://www.fxstreet.com/rss/news",
                  ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]),
]

INSTRUMENT_KEYWORDS = {
    "GOLD":   ["gold", "xau", "bullion", "safe haven", "inflation",
               "commodity", "fed", "rate cut", "rate hike", "dollar"],
    "EURUSD": ["euro", "eur/usd", "eurusd", "ecb", "european",
               "lagarde", "eurozone", "dollar", "dxy"],
    "GBPUSD": ["pound", "gbp", "sterling", "boe", "bank of england",
               "bailey", "gbpusd", "uk gdp", "british"],
    "USDJPY": ["yen", "jpy", "usdjpy", "boj", "bank of japan",
               "ueda", "japan", "intervention", "carry trade"],
}


class StrategistAgent:
    NAME  = "STRATEGIST"
    MODEL = "claude-opus-4-5"

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.plans  = {}   # instrument_name -> plan dict
        self.last_run_date = None

    # ── MT5 helpers ───────────────────────────────────────────────────

    def _connect_mt5(self) -> bool:
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")
        if not mt5.initialize(login=login, password=password, server=server):
            print(f"[{self.NAME}] MT5 init failed: {mt5.last_error()}")
            return False
        if mt5.account_info() is None:
            print(f"[{self.NAME}] MT5 login failed")
            mt5.shutdown()
            return False
        return True

    def _get_rates(self, symbol: str, timeframe, n_bars: int) -> pd.DataFrame | None:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_bars)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        return df

    # ── Indicator calculations ─────────────────────────────────────────

    @staticmethod
    def _calc_ema(series, period):
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _calc_rsi(close, period=14):
        delta  = close.diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        alpha  = 1.0 / period
        avg_g  = gain.ewm(alpha=alpha, adjust=False).mean()
        avg_l  = loss.ewm(alpha=alpha, adjust=False).mean()
        rs     = avg_g / avg_l.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calc_atr(df, period=14):
        high  = df['high']
        low   = df['low']
        close = df['close']
        tr    = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _calc_adx(df, period=14):
        high  = df['high']
        low   = df['low']
        close = df['close']
        alpha = 1.0 / period
        up    = high.diff()
        down  = -low.diff()
        pdi_raw = np.where((up > down) & (up > 0), up, 0.0)
        mdi_raw = np.where((down > up) & (down > 0), down, 0.0)
        tr  = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=alpha, adjust=False).mean()
        pdi = pd.Series(pdi_raw, index=high.index).ewm(alpha=alpha, adjust=False).mean() / atr * 100
        mdi = pd.Series(mdi_raw, index=high.index).ewm(alpha=alpha, adjust=False).mean() / atr * 100
        dx  = ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)) * 100
        adx = dx.ewm(alpha=alpha, adjust=False).mean()
        return adx, pdi, mdi

    def _build_snapshot(self, df: pd.DataFrame, label: str, pip_size: float) -> dict:
        """Build indicator snapshot from a OHLCV DataFrame."""
        df['ema20']  = self._calc_ema(df['close'], 20)
        df['ema50']  = self._calc_ema(df['close'], 50)
        df['ema200'] = self._calc_ema(df['close'], 200)
        df['rsi']    = self._calc_rsi(df['close'])
        df['atr']    = self._calc_atr(df)
        df['adx'], df['pdi'], df['mdi'] = self._calc_adx(df)

        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        price = last['close']

        # Market structure — last 20 bars
        recent = df.tail(20)
        swing_high = recent['high'].max()
        swing_low  = recent['low'].min()
        trend_dir  = ("BULLISH" if last['ema20'] > last['ema50'] > last['ema200']
                      else "BEARISH" if last['ema20'] < last['ema50'] < last['ema200']
                      else "MIXED")

        # Candles tail (last 5)
        candles = []
        for _, row in df.tail(5).iterrows():
            candles.append({
                "time":  str(row.name),
                "open":  round(row['open'], 5),
                "high":  round(row['high'], 5),
                "low":   round(row['low'], 5),
                "close": round(row['close'], 5),
            })

        return {
            "label":       label,
            "price":       round(price, 5),
            "ema20":       round(last['ema20'], 5),
            "ema50":       round(last['ema50'], 5),
            "ema200":      round(last['ema200'], 5),
            "ema20_prev":  round(prev['ema20'], 5),
            "rsi":         round(last['rsi'], 2),
            "rsi_prev":    round(prev['rsi'], 2),
            "atr":         round(last['atr'], 5),
            "adx":         round(last['adx'], 2),
            "adx_prev":    round(prev['adx'], 2),
            "pdi":         round(last['pdi'], 2),
            "mdi":         round(last['mdi'], 2),
            "trend_dir":   trend_dir,
            "swing_high":  round(swing_high, 5),
            "swing_low":   round(swing_low, 5),
            "vs_ema200":   ("ABOVE" if price > last['ema200'] else "BELOW"),
            "candles_tail": candles,
        }

    def _find_key_levels(self, df_d1: pd.DataFrame, df_h4: pd.DataFrame,
                         round_levels: list, pip_size: float) -> dict:
        """Find key S/R levels from price history and round numbers."""
        price = df_d1['close'].iloc[-1]
        atr   = self._calc_atr(df_d1).iloc[-1]

        # Previous day H/L
        pdh = df_d1['high'].iloc[-2]
        pdl = df_d1['low'].iloc[-2]

        # Previous week H/L (last 5 trading days)
        week = df_d1.tail(6).iloc[:-1]
        pwh  = week['high'].max()
        pwl  = week['low'].min()

        # Monthly open (first candle of current month)
        monthly_open = df_d1[df_d1.index.month == df_d1.index[-1].month]['open'].iloc[0]

        # Nearest round numbers (above and below)
        above_rounds = [r for r in round_levels if r > price]
        below_rounds = [r for r in round_levels if r <= price]
        nearest_above = min(above_rounds) if above_rounds else None
        nearest_below = max(below_rounds) if below_rounds else None

        return {
            "pdh":           round(pdh, 5),
            "pdl":           round(pdl, 5),
            "pwh":           round(pwh, 5),
            "pwl":           round(pwl, 5),
            "monthly_open":  round(monthly_open, 5),
            "nearest_above": nearest_above,
            "nearest_below": nearest_below,
            "atr_d1":        round(atr, 5),
        }

    # ── Memory system ─────────────────────────────────────────────────

    def _load_memory(self) -> dict:
        """Load persistent memory from disk. Creates fresh structure if missing."""
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[{self.NAME}] Memory load failed: {e} — starting fresh")
        return {
            "version": 1,
            "instruments": {
                name: {"insights": [], "level_notes": [], "regime_notes": []}
                for name in INSTRUMENTS
            },
            "global_regime": [],
            "performance": {
                name: {"wins_30d": 0, "losses_30d": 0, "notes": ""}
                for name in INSTRUMENTS
            },
            "web_cache": {"last_fetch": None, "headlines": []},
        }

    def _save_memory(self, memory: dict):
        """Save memory to disk."""
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        memory["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=2, ensure_ascii=False)
            print(f"[{self.NAME}] Memory saved to {MEMORY_FILE}")
        except Exception as e:
            print(f"[{self.NAME}] Memory save failed: {e}")

    def _trim_list(self, lst: list, max_items: int = MAX_MEMORY_ITEMS) -> list:
        """Keep only the last N items in a memory list."""
        return lst[-max_items:] if len(lst) > max_items else lst

    def _update_memory(self, instrument: str, plan: dict, memory: dict):
        """Apply memory updates from Claude's plan response."""
        update = plan.get("memory_update", {})
        if not update:
            return
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        inst_mem = memory["instruments"].setdefault(instrument, {
            "insights": [], "level_notes": [], "regime_notes": []
        })
        if update.get("insight"):
            inst_mem.setdefault("insights", []).append(
                {"date": today, "text": update["insight"]}
            )
            inst_mem["insights"] = self._trim_list(inst_mem["insights"])
        if update.get("level_note"):
            inst_mem.setdefault("level_notes", []).append(
                {"date": today, "text": update["level_note"]}
            )
            inst_mem["level_notes"] = self._trim_list(inst_mem["level_notes"])
        if update.get("regime_note"):
            inst_mem.setdefault("regime_notes", []).append(
                {"date": today, "text": update["regime_note"]}
            )
            inst_mem["regime_notes"] = self._trim_list(inst_mem["regime_notes"])
        if update.get("global_regime"):
            memory.setdefault("global_regime", []).append(
                {"date": today, "text": update["global_regime"]}
            )
            memory["global_regime"] = self._trim_list(
                memory["global_regime"], max_items=5
            )
        print(f"[{self.NAME}] Memory updated for {instrument}")

    def _load_performance_feedback(self, memory: dict):
        """
        Read logs/executions.json to update 30-day win/loss counts per instrument.
        Updates memory['performance'] in-place.
        """
        logs_dir  = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
        )
        exec_file = os.path.join(logs_dir, "executions.json")
        cutoff    = datetime.now(timezone.utc) - timedelta(days=30)

        if not os.path.exists(exec_file):
            return
        try:
            with open(exec_file, "r", encoding="utf-8") as f:
                executions = json.load(f)
        except Exception:
            return

        counts = {name: {"wins": 0, "losses": 0} for name in INSTRUMENTS}
        for ex in executions:
            agent  = ex.get("agent", "").upper()
            result = ex.get("result", "")
            ts_str = ex.get("timestamp", "")
            if not agent or not result or not ts_str:
                continue
            inst = next((n for n in INSTRUMENTS if n in agent), None)
            if not inst:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                continue
            if result.upper() in ("WIN", "TP", "TAKE_PROFIT"):
                counts[inst]["wins"] += 1
            elif result.upper() in ("LOSS", "SL", "STOP_LOSS"):
                counts[inst]["losses"] += 1

        for inst, c in counts.items():
            total = c["wins"] + c["losses"]
            memory["performance"][inst]["wins_30d"]  = c["wins"]
            memory["performance"][inst]["losses_30d"] = c["losses"]
            if total > 0:
                memory["performance"][inst]["notes"] = (
                    f"{c['wins']}W / {c['losses']}L last 30 days "
                    f"({round(c['wins'] / total * 100)}% WR)"
                )

    def _memory_text(self, instrument: str, memory: dict) -> str:
        """Format stored memory for one instrument as readable text for Claude."""
        lines = []
        inst_mem = memory.get("instruments", {}).get(instrument, {})

        insights = inst_mem.get("insights", [])
        if insights:
            lines.append("LEARNED INSIGHTS:")
            for item in insights[-5:]:
                lines.append(f"  [{item['date']}] {item['text']}")

        level_notes = inst_mem.get("level_notes", [])
        if level_notes:
            lines.append("LEVEL OBSERVATIONS:")
            for item in level_notes[-5:]:
                lines.append(f"  [{item['date']}] {item['text']}")

        regime_notes = inst_mem.get("regime_notes", [])
        if regime_notes:
            lines.append("REGIME NOTES:")
            for item in regime_notes[-3:]:
                lines.append(f"  [{item['date']}] {item['text']}")

        global_regime = memory.get("global_regime", [])
        if global_regime:
            lines.append("GLOBAL REGIME:")
            for item in global_regime[-3:]:
                lines.append(f"  [{item['date']}] {item['text']}")

        perf = memory.get("performance", {}).get(instrument, {})
        if perf.get("notes"):
            lines.append(f"LIVE PERFORMANCE (30 days): {perf['notes']}")

        return "\n".join(lines) if lines else "No memory entries yet — this is the first run."

    # ── Web intelligence ───────────────────────────────────────────────

    def _fetch_web_insights(self, memory: dict) -> dict:
        """
        Fetch latest macro/instrument headlines from RSS feeds.
        Returns {instrument: [headline_strings]}
        Uses a cache — only re-fetches if cache is older than WEB_CACHE_HOURS.
        """
        cache      = memory.get("web_cache", {})
        last_fetch = cache.get("last_fetch")
        if last_fetch:
            try:
                last_dt   = datetime.fromisoformat(last_fetch)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours < WEB_CACHE_HOURS:
                    print(f"[{self.NAME}] Web cache fresh ({age_hours:.1f}h old) — using cache")
                    return self._web_cache_to_dict(cache.get("headlines", []))
            except Exception:
                pass

        print(f"[{self.NAME}] Fetching web intelligence from {len(WEB_SOURCES)} sources...")
        all_headlines = []

        for source_name, url, instruments in WEB_SOURCES:
            try:
                feed  = feedparser.parse(url)
                count = 0
                for entry in feed.entries[:20]:
                    title   = getattr(entry, "title", "").strip()
                    summary = getattr(entry, "summary", "")[:300].strip()
                    date    = getattr(entry, "published", "")[:16]
                    if not title:
                        continue
                    text_lower = (title + " " + summary).lower()
                    relevant = [
                        inst for inst in instruments
                        if any(kw in text_lower
                               for kw in INSTRUMENT_KEYWORDS.get(inst, []))
                    ]
                    if relevant:
                        all_headlines.append({
                            "source":      source_name,
                            "title":       title,
                            "date":        date,
                            "instruments": relevant,
                        })
                        count += 1
                    if count >= 6:
                        break
                print(f"[{self.NAME}]   {source_name}: {count} relevant headlines")
            except Exception as e:
                print(f"[{self.NAME}]   {source_name}: fetch failed — {e}")

        memory["web_cache"] = {
            "last_fetch": datetime.now(timezone.utc).isoformat(),
            "headlines":  all_headlines[-40:],
        }
        return self._web_cache_to_dict(all_headlines)

    def _web_cache_to_dict(self, headlines: list) -> dict:
        """Convert flat headlines list to {instrument: [text_lines]} dict."""
        result = {name: [] for name in INSTRUMENTS}
        for h in headlines:
            for inst in h.get("instruments", []):
                if inst in result:
                    result[inst].append(
                        f"[{h['source']} {h['date']}] {h['title']}"
                    )
        return result

    def _web_text(self, instrument: str, web_insights: dict) -> str:
        """Format web headlines for one instrument as readable text."""
        headlines = web_insights.get(instrument, [])
        if not headlines:
            return "No relevant headlines found at this time."
        return "\n".join(f"  {h}" for h in headlines[:8])

    # ── Backtest Intelligence ──────────────────────────────────────────

    def _load_backtest_stats(self, instrument: str) -> dict:
        """
        Load all backtest CSV files for the instrument, deduplicate,
        and compute performance statistics by ADX bucket, RSI bucket,
        direction, and time-exit quality.
        """
        logs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        pattern  = os.path.join(logs_dir, f"backtest_{instrument}_*.csv")
        files    = sorted(glob.glob(pattern))

        if not files:
            return {"available": False, "reason": "No backtest CSV files found"}

        frames = []
        for f in files:
            try:
                df = pd.read_csv(f, parse_dates=["signal_time", "exit_time"])
                frames.append(df)
            except Exception as e:
                print(f"[{self.NAME}] Could not load {f}: {e}")

        if not frames:
            return {"available": False, "reason": "All CSV files failed to load"}

        df = (pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset="signal_time")
                .sort_values("signal_time")
                .reset_index(drop=True))

        total      = len(df)
        wins       = int((df["outcome"] == "WIN").sum())
        losses     = int((df["outcome"] == "LOSS").sum())
        time_exits = int((df["outcome"] == "TIME_EXIT").sum())
        decisive   = wins + losses
        win_rate   = round(wins / decisive * 100, 1) if decisive > 0 else 0.0

        # Time-exit quality
        te_df          = df[df["outcome"] == "TIME_EXIT"]
        te_avg_pnl     = round(float(te_df["pnl_pts"].mean()), 6) if len(te_df) > 0 else 0.0
        te_positive_pct= round(float((te_df["pnl_pts"] > 0).sum() / len(te_df) * 100), 1) if len(te_df) > 0 else 0.0

        # Helper: win-rate stats for a sub-dataframe
        def _wr(sub):
            w = int((sub["outcome"] == "WIN").sum())
            l = int((sub["outcome"] == "LOSS").sum())
            d = w + l
            return {"trades": len(sub), "wins": w, "losses": l,
                    "win_rate": round(w / d * 100, 1) if d > 0 else 0.0}

        # ADX buckets
        adx_buckets = [
            ("<20",   0,  20),
            ("20-25", 20, 25),
            ("25-30", 25, 30),
            ("30-35", 30, 35),
            (">35",   35, 999),
        ]
        adx_stats = {}
        for label, lo, hi in adx_buckets:
            sub = df[(df["h4_adx"] >= lo) & (df["h4_adx"] < hi)]
            if len(sub) > 0:
                adx_stats[label] = _wr(sub)

        # RSI buckets
        rsi_buckets = [
            ("<42",   0,  42),
            ("42-58", 42, 58),
            ("58-65", 58, 65),
            ("65-70", 65, 70),
            (">70",   70, 100),
        ]
        rsi_stats = {}
        for label, lo, hi in rsi_buckets:
            sub = df[(df["h4_rsi"] >= lo) & (df["h4_rsi"] < hi)]
            if len(sub) > 0:
                rsi_stats[label] = _wr(sub)

        # Direction stats
        dir_stats = {}
        for direction in ["BUY", "SELL"]:
            sub = df[df["direction"] == direction]
            if len(sub) > 0:
                dir_stats[direction] = _wr(sub)

        # ATR vs price (relative size) split: low / medium / high ATR
        atr_med  = df["h4_atr"].median()
        atr_stats = {
            "low":  _wr(df[df["h4_atr"] <  atr_med * 0.75]),
            "med":  _wr(df[(df["h4_atr"] >= atr_med * 0.75) & (df["h4_atr"] < atr_med * 1.5)]),
            "high": _wr(df[df["h4_atr"] >= atr_med * 1.5]),
        }

        # Best ADX zone (highest win-rate among zones with >=5 decisive trades)
        best_adx = max(
            ((k, v) for k, v in adx_stats.items() if (v["wins"] + v["losses"]) >= 5),
            key=lambda x: x[1]["win_rate"],
            default=(None, None),
        )

        # Best RSI zone
        best_rsi = max(
            ((k, v) for k, v in rsi_stats.items() if (v["wins"] + v["losses"]) >= 3),
            key=lambda x: x[1]["win_rate"],
            default=(None, None),
        )

        # Preferred direction
        pref_dir = None
        if dir_stats:
            pref_dir = max(dir_stats.items(), key=lambda x: x[1]["win_rate"])[0]

        # Date range
        date_from = str(df["signal_time"].min())[:10] if total > 0 else "N/A"
        date_to   = str(df["signal_time"].max())[:10] if total > 0 else "N/A"

        return {
            "available":       True,
            "total":           total,
            "wins":            wins,
            "losses":          losses,
            "time_exits":      time_exits,
            "win_rate":        win_rate,
            "date_from":       date_from,
            "date_to":         date_to,
            "te_avg_pnl":      te_avg_pnl,
            "te_positive_pct": te_positive_pct,
            "adx_stats":       adx_stats,
            "rsi_stats":       rsi_stats,
            "dir_stats":       dir_stats,
            "atr_stats":       atr_stats,
            "atr_median":      round(float(atr_med), 6),
            "best_adx_zone":   best_adx[0],
            "best_rsi_zone":   best_rsi[0],
            "preferred_dir":   pref_dir,
        }

    def _backtest_text(self, stats: dict) -> str:
        """Format backtest stats as a readable block for Claude's prompt."""
        if not stats.get("available"):
            return f"No backtest data available ({stats.get('reason', 'unknown')})."

        lines = [
            f"Data: {stats['total']} trades | {stats['date_from']} to {stats['date_to']}",
            f"Outcomes: {stats['wins']}W / {stats['losses']}L / {stats['time_exits']} TIME_EXIT",
            f"Win rate (decisive W+L only): {stats['win_rate']}%",
            f"Time exits: {stats['te_positive_pct']}% positive | avg P&L: {stats['te_avg_pnl']}",
            "",
            "ADX PERFORMANCE (decisive outcomes only):",
        ]
        for bucket, s in stats["adx_stats"].items():
            tag = " <-- BEST ZONE" if bucket == stats["best_adx_zone"] else ""
            d   = s["wins"] + s["losses"]
            lines.append(
                f"  ADX {bucket:6s}: {s['trades']:3d} trades | "
                f"{s['wins']}W / {s['losses']}L"
                + (f" | WR: {s['win_rate']}%{tag}" if d > 0 else " | no decisive outcomes")
            )

        lines += ["", "RSI AT ENTRY (decisive outcomes only):"]
        for bucket, s in stats["rsi_stats"].items():
            tag = " <-- BEST ZONE" if bucket == stats["best_rsi_zone"] else ""
            d   = s["wins"] + s["losses"]
            lines.append(
                f"  RSI {bucket:6s}: {s['trades']:3d} trades | "
                f"{s['wins']}W / {s['losses']}L"
                + (f" | WR: {s['win_rate']}%{tag}" if d > 0 else " | no decisive outcomes")
            )

        lines += ["", "DIRECTION BIAS:"]
        for direction, s in stats["dir_stats"].items():
            tag = " <-- PREFERRED" if direction == stats["preferred_dir"] else ""
            d   = s["wins"] + s["losses"]
            lines.append(
                f"  {direction:4s}: {s['trades']:3d} trades | "
                f"{s['wins']}W / {s['losses']}L"
                + (f" | WR: {s['win_rate']}%{tag}" if d > 0 else " | no decisive outcomes")
            )

        lines += ["", "ATR REGIME AT ENTRY:"]
        for label, s in stats["atr_stats"].items():
            if s["trades"] > 0:
                d   = s["wins"] + s["losses"]
                lines.append(
                    f"  ATR {label:3s}: {s['trades']:3d} trades | "
                    f"{s['wins']}W / {s['losses']}L"
                    + (f" | WR: {s['win_rate']}%" if d > 0 else " | no decisive outcomes")
                )

        lines += [
            "",
            "KEY TAKEAWAYS FOR TODAY'S PLAN:",
            f"  Best ADX zone      : {stats['best_adx_zone'] or 'insufficient data'}",
            f"  Best RSI zone      : {stats['best_rsi_zone'] or 'insufficient data'}",
            f"  Preferred direction: {stats['preferred_dir'] or 'no clear edge'}",
            f"  ATR median         : {stats['atr_median']}  (use as volatility baseline)",
        ]
        return "\n".join(lines)

    # ── Claude AI ─────────────────────────────────────────────────────

    def _ask_claude(self, instrument: str, config: dict,
                    d1: dict, h4: dict, h1: dict, levels: dict,
                    bt_stats: dict, mem_text: str = "", web_text: str = "") -> dict:
        """Ask Claude Opus to create a daily execution plan for one instrument."""

        system_prompt = f"""You are STRATEGIST, the senior technical analyst at APEX Capital AI.
Your role is to perform a complete top-down technical analysis and create a precise daily execution plan.

You analyse {instrument} ({config['description']}) using:
- D1 chart: weekly/monthly structure, macro trend, key institutional levels
- H4 chart: session trend, current leg, intermediate S/R
- H1 chart: near-term structure, entry zone refinement
- BACKTEST INTELLIGENCE: historical performance data showing exactly which conditions win and lose

Your output is the execution plan that all entry agents will use as their daily guide.
You think like a professional prop trader who is also data-driven: structure first, then
confirm the conditions match the historically profitable setup profile.

You also have MEMORY — observations stored from previous daily runs — and WEB INTELLIGENCE
— latest macro headlines. Use both to enrich your analysis and update your memory at the
end of each plan with new observations.

=== ANALYSIS WORKFLOW ===
1. MARKET STRUCTURE (D1): Identify the dominant trend — higher highs/lows or lower highs/lows?
   Where are the last major swing points? Is price in discovery or range?

2. SESSION STRUCTURE (H4): What is the current leg? Where is price relative to recent H4 structure?
   Is H4 aligned with D1 or showing divergence?

3. KEY LEVELS: Identify the 3-4 most important levels between current price and likely targets.
   Focus on: previous day H/L, previous week H/L, round numbers, EMA200 on H4.

4. BACKTEST FIT: Check the current ADX and RSI values against the BACKTEST INTELLIGENCE section.
   - Is current ADX in the best historical zone? If yes — higher confidence.
   - Is the likely direction matching the historically preferred direction? If yes — favor it.
   - If current conditions are in a historically weak zone — lower confidence or WAIT.
   Use this to calibrate your confluence_score: backtest alignment adds 10-20 points.

5. ENTRY ZONE: Define a specific price zone where the setup would be highest quality.
   Target the historically best ADX+RSI zone. Not a single price — a zone (e.g., "1.0820-1.0840").

6. TRADE IDEA: Write a clear, brief thesis (1-2 sentences). Reference the backtest edge if applicable.

7. INVALIDATION: One specific price level. If price closes ABOVE/BELOW this on H4, the idea is wrong.

8. TP TARGET: Based on structure (swing high/low, key level) — realistic, not greedy.

9. BIAS: Overall daily bias for entry agents:
   - BULLISH: structure supports longs — agents should focus on long setups only
   - BEARISH: structure supports shorts — agents should focus on short setups only
   - NEUTRAL: structure mixed — agents may trade both directions with extra confirmation
   - WAIT: current conditions in historically weak zone or no clear structure — be patient

10. MEMORY UPDATE: At the end, write 1-sentence entries for memory_update:
    - insight: What is the most important thing I learned about this instrument today?
    - level_note: Which specific price level proved significant or was broken today?
    - regime_note: What is the current macro/regime context for this instrument?
    - global_regime: Only fill if something significant changed in the overall market today.
    These will be stored and shown to you on future days — be specific and useful.

Respond ONLY with valid JSON (no markdown, no backticks):
{{
  "instrument": "{instrument}",
  "date": "YYYY-MM-DD",
  "bias": "BULLISH" or "BEARISH" or "NEUTRAL" or "WAIT",
  "structure": "1-2 sentences describing D1+H4 market structure",
  "key_levels": "comma-separated list of key S/R price levels",
  "entry_zone": "price range to watch (e.g. 1.0820-1.0840)",
  "invalidation": "single price level that breaks the idea",
  "trade_idea": "1-2 sentence thesis of the trade",
  "tp_target": "primary TP price target",
  "sl_suggestion": "suggested SL price (below support for longs, above resistance for shorts)",
  "session_notes": "which sessions to focus on, any timing considerations",
  "backtest_fit": "brief note on how today's conditions compare to historical best setups",
  "confluence_score": 0-100,
  "notes": "any special considerations (news, BoJ zone, etc.)",
  "memory_update": {{
    "insight": "1-sentence key observation about this instrument's current behaviour — for future reference",
    "level_note": "1-sentence observation about a specific price level that proved significant or was broken",
    "regime_note": "1-sentence note about the current macro/regime context for this instrument",
    "global_regime": "1-sentence note about the overall market regime today (only if something significant changed)"
  }}
}}"""

        def _snap(s: dict) -> str:
            return (
                f"Price : {s['price']} | EMA20: {s['ema20']} | EMA50: {s['ema50']} | EMA200: {s['ema200']}\n"
                f"RSI   : {s['rsi']} (prev {s['rsi_prev']}) | ADX: {s['adx']} (prev {s['adx_prev']}) "
                f"| +DI: {s['pdi']} | -DI: {s['mdi']}\n"
                f"ATR   : {s['atr']} | Trend: {s['trend_dir']} | vs EMA200: {s['vs_ema200']}\n"
                f"Swing High: {s['swing_high']} | Swing Low: {s['swing_low']}\n"
                f"Last 5 candles: {json.dumps(s['candles_tail'])}"
            )

        user_prompt = f"""Create today's execution plan for {instrument}.

=== STRATEGIST MEMORY (What I Have Learned From Previous Days) ===
{mem_text}

=== WEB INTELLIGENCE (Latest Macro & Instrument News) ===
{web_text}

=== BACKTEST INTELLIGENCE (Historical Performance — Use This to Calibrate Confidence) ===
{self._backtest_text(bt_stats)}

=== D1 CHART (Macro Structure — {d1['label']}) ===
{_snap(d1)}

=== H4 CHART (Session Structure — {h4['label']}) ===
{_snap(h4)}

=== H1 CHART (Near-Term Context — {h1['label']}) ===
{_snap(h1)}

=== KEY INSTITUTIONAL LEVELS ===
Previous Day High   : {levels['pdh']}
Previous Day Low    : {levels['pdl']}
Previous Week High  : {levels['pwh']}
Previous Week Low   : {levels['pwl']}
Monthly Open        : {levels['monthly_open']}
Nearest Round Above : {levels['nearest_above']}
Nearest Round Below : {levels['nearest_below']}
D1 ATR              : {levels['atr_d1']}

Today's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC
Current UTC time: {datetime.now(timezone.utc).strftime('%H:%M')}

Check the current H4 ADX ({h4['adx']}) and H4 RSI ({h4['rsi']}) against the backtest data above.
Then analyse the full chart picture and create the evidence-based execution plan."""

        try:
            response = self.client.messages.create(
                model=self.MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            raw = response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            plan = json.loads(raw)
            plan["created_at"] = datetime.now(timezone.utc).isoformat()
            return plan
        except Exception as e:
            print(f"[{self.NAME}] Claude error for {instrument}: {e}")
            return self._fallback_plan(instrument)

    def _fallback_plan(self, instrument: str) -> dict:
        return {
            "instrument":      instrument,
            "date":            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "bias":            "NEUTRAL",
            "structure":       "Analysis unavailable — fallback plan.",
            "key_levels":      "N/A",
            "entry_zone":      "N/A",
            "invalidation":    "N/A",
            "trade_idea":      "No plan today — use own analysis.",
            "tp_target":       "N/A",
            "sl_suggestion":   "N/A",
            "session_notes":   "N/A",
            "backtest_fit":    "N/A",
            "confluence_score": 0,
            "notes":           "Strategist API call failed.",
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }

    # ── Analyse one instrument ─────────────────────────────────────────

    def _analyse_instrument(self, name: str, config: dict,
                            memory: dict | None = None,
                            web_insights: dict | None = None) -> dict:
        symbol  = config["symbol"]
        memory  = memory or {}
        web_insights = web_insights or {}
        print(f"[{self.NAME}] Analysing {name} ({symbol})...")

        if not self._connect_mt5():
            print(f"[{self.NAME}] MT5 unavailable — fallback plan for {name}")
            return self._fallback_plan(name)

        try:
            df_d1 = self._get_rates(symbol, mt5.TIMEFRAME_D1, D1_BARS)
            df_h4 = self._get_rates(symbol, mt5.TIMEFRAME_H4, H4_BARS)
            df_h1 = self._get_rates(symbol, mt5.TIMEFRAME_H1, H1_BARS)
        finally:
            mt5.shutdown()

        if df_d1 is None or df_h4 is None or df_h1 is None:
            print(f"[{self.NAME}] Missing data for {name} — fallback plan")
            return self._fallback_plan(name)

        if len(df_d1) < 30 or len(df_h4) < 50 or len(df_h1) < 50:
            print(f"[{self.NAME}] Insufficient data for {name} — fallback plan")
            return self._fallback_plan(name)

        pip = config["pip_size"]
        d1_snap  = self._build_snapshot(df_d1, "D1 Daily", pip)
        h4_snap  = self._build_snapshot(df_h4, "H4 Four-Hour", pip)
        h1_snap  = self._build_snapshot(df_h1, "H1 One-Hour", pip)
        levels   = self._find_key_levels(df_d1, df_h4, config["round_levels"], pip)

        # Load and print backtest intelligence
        bt_stats = self._load_backtest_stats(name)
        if bt_stats["available"]:
            print(f"[{self.NAME}] {name} backtest: {bt_stats['total']} trades | "
                  f"WR {bt_stats['win_rate']}% | "
                  f"best ADX zone: {bt_stats['best_adx_zone']} | "
                  f"preferred dir: {bt_stats['preferred_dir']}")
        else:
            print(f"[{self.NAME}] {name} backtest: {bt_stats.get('reason', 'unavailable')}")

        mem_text = self._memory_text(name, memory)
        web_text = self._web_text(name, web_insights)
        plan = self._ask_claude(name, config, d1_snap, h4_snap, h1_snap, levels,
                                bt_stats, mem_text, web_text)
        # Apply memory updates from Claude's response
        self._update_memory(name, plan, memory)

        bias  = plan.get("bias", "NEUTRAL")
        conf  = plan.get("confluence_score", 0)
        idea  = plan.get("trade_idea", "N/A")
        zone  = plan.get("entry_zone", "N/A")
        inv   = plan.get("invalidation", "N/A")
        fit   = plan.get("backtest_fit", "N/A")
        print(f"[{self.NAME}] {name}: bias={bias} ({conf}%) | zone={zone} | inv={inv}")
        print(f"[{self.NAME}] {name}: idea={idea}")
        print(f"[{self.NAME}] {name}: backtest fit={fit}")

        return plan

    # ── Public API ────────────────────────────────────────────────────

    def run_daily(self, instruments: list | None = None) -> dict:
        """
        Run full daily analysis for all (or selected) instruments.
        Returns {instrument_name: plan_dict, ...}
        """
        targets = instruments or list(INSTRUMENTS.keys())

        print(f"\n[{self.NAME}] === Daily Analysis Run — "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")

        # Load persistent memory and web intelligence once for the whole run
        memory      = self._load_memory()
        self._load_performance_feedback(memory)
        web_insights = self._fetch_web_insights(memory)

        results = {}
        for name in targets:
            if name not in INSTRUMENTS:
                print(f"[{self.NAME}] Unknown instrument: {name}")
                continue
            config = INSTRUMENTS[name]
            plan   = self._analyse_instrument(name, config,
                                              memory=memory,
                                              web_insights=web_insights)
            self.plans[name] = plan
            results[name]    = plan
            # Polite pause between MT5 + Claude calls
            if name != targets[-1]:
                time.sleep(2)

        # Save updated memory (includes all memory_update entries from this run)
        self._save_memory(memory)

        self.last_run_date = datetime.now(timezone.utc).date()
        print(f"[{self.NAME}] Daily run complete. Plans ready for: {list(results.keys())}")
        return results

    def get_plan(self, instrument: str) -> dict | None:
        """Get the stored plan for one instrument."""
        return self.plans.get(instrument)

    def distribute_plans(self, agents: dict):
        """
        Push plans to all entry agents.
        agents = {"GOLD": gold_agent, "EURUSD": eurusd_agent, ...}
        """
        for name, agent in agents.items():
            plan = self.plans.get(name)
            if plan and hasattr(agent, "receive_strategy_plan"):
                agent.receive_strategy_plan(plan)
                print(f"[{self.NAME}] Plan distributed to {name} agent: "
                      f"bias={plan.get('bias', 'N/A')}")
            elif plan is None:
                print(f"[{self.NAME}] No plan for {name} — agent will use own analysis")

    def needs_daily_run(self) -> bool:
        """Returns True if we haven't run today yet (UTC date)."""
        today = datetime.now(timezone.utc).date()
        return self.last_run_date != today

    def send_telegram_summary(self, telegram_token: str, chat_id: str):
        """Send a brief daily strategy summary to Telegram."""
        if not self.plans:
            return
        try:
            import requests as req
            lines = ["*APEX Capital AI — Daily Strategy Plan*\n"]
            for name, plan in self.plans.items():
                bias  = plan.get("bias", "?")
                zone  = plan.get("entry_zone", "?")
                idea  = plan.get("trade_idea", "?")
                conf  = plan.get("confluence_score", 0)
                emoji = {"BULLISH": "📈", "BEARISH": "📉",
                         "NEUTRAL": "➡️", "WAIT": "⏸️"}.get(bias, "❓")
                lines.append(f"{emoji} *{name}* ({bias}, {conf}%)")
                lines.append(f"   Zone: {zone}")
                lines.append(f"   {idea}\n")
            text = "\n".join(lines)
            req.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "parse_mode": "Markdown"},
                timeout=10,
            )
            print(f"[{self.NAME}] Telegram summary sent.")
        except Exception as e:
            print(f"[{self.NAME}] Telegram send failed: {e}")
