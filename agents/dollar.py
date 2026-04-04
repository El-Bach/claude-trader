"""
DOLLAR — US Dollar Index Specialist
APEX Capital AI

Role: Runs FIRST every cycle. Analyses DXY direction using 4 macro pillars
and broadcasts a signal that all other agents must consume before trading.
Also trades EURUSD when the setup aligns.

Macro Pillars:
  1. DXY Basket     — weighted USD momentum from MT5 (EUR/JPY/GBP/CAD)
  2. Rate Diff      — US 10Y vs EU 10Y yield spread (FRED + ECB APIs)
  3. Fed Rhetoric   — hawkish/dovish keyword count from Fed RSS
  4. Technicals     — EMA stack, RSI, MACD, ATR, ADX on EURUSD H4+H1

Model: claude-sonnet-4-5
"""

import os
import io
import json
import requests
import anthropic
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()


class DollarAgent:
    NAME = "DOLLAR"
    INSTRUMENT = "DXY / US Dollar"

    # MT5 symbols to try for DXY (broker-dependent)
    DXY_SYMBOLS  = ["USDX", "DXY", "US.DOLLAR", "DOLLAR"]
    PROXY_SYMBOL = "EURUSD"  # Inverse proxy if DXY not available

    # DXY basket composition (main 4 components = 92.4% of DXY)
    BASKET = {
        "EURUSD": {"weight": 0.576, "direction": "inverse"},
        "USDJPY": {"weight": 0.136, "direction": "direct"},
        "GBPUSD": {"weight": 0.119, "direction": "inverse"},
        "USDCAD": {"weight": 0.091, "direction": "direct"},
    }

    def __init__(self):
        self.client        = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.broadcast     = None
        self.active_symbol = None

    # ------------------------------------------------------------------ #
    #  MT5 CONNECTION
    # ------------------------------------------------------------------ #

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

    def _find_active_symbol(self) -> str | None:
        for sym in self.DXY_SYMBOLS:
            info = mt5.symbol_info(sym)
            if info and info.visible:
                return sym
        info = mt5.symbol_info(self.PROXY_SYMBOL)
        if info:
            print(f"[{self.NAME}] DXY not found, using {self.PROXY_SYMBOL} as inverse proxy")
            return self.PROXY_SYMBOL
        return None

    # ------------------------------------------------------------------ #
    #  DATA FETCHING (MT5)
    # ------------------------------------------------------------------ #

    def _get_ohlcv(self, symbol: str, timeframe, bars: int = 200) -> pd.DataFrame | None:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    # ------------------------------------------------------------------ #
    #  TECHNICAL INDICATORS
    # ------------------------------------------------------------------ #

    def _calc_ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _calc_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        """Wilder RSI matching other agents' pattern."""
        alpha = 1.0 / period
        delta = series.diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta.clip(upper=0))
        avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
        avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _calc_macd(self, series: pd.Series):
        ema12      = self._calc_ema(series, 12)
        ema26      = self._calc_ema(series, 26)
        macd_line  = ema12 - ema26
        signal     = self._calc_ema(macd_line, 9)
        histogram  = macd_line - signal
        return macd_line, signal, histogram

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Wilder ewm ATR — matches gold.py/eurusd.py pattern exactly."""
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def _calc_adx(self, df: pd.DataFrame, period: int = 14):
        """Wilder ADX — matches gold.py/eurusd.py pattern exactly."""
        aa    = 1.0 / period
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        tr    = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        pdm = high.diff()
        mdm = -low.diff()
        pdm = pdm.where((pdm > mdm) & (pdm > 0), 0.0)
        mdm = mdm.where((mdm > pdm) & (mdm > 0), 0.0)
        atr_w = tr.ewm(alpha=aa, adjust=False).mean()
        pdi   = 100 * (pdm.ewm(alpha=aa, adjust=False).mean() / atr_w)
        mdi   = 100 * (mdm.ewm(alpha=aa, adjust=False).mean() / atr_w)
        dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1)
        adx   = dx.ewm(alpha=aa, adjust=False).mean()
        return adx, pdi, mdi

    def _analyse_technicals(self, df: pd.DataFrame, is_inverse: bool = False) -> dict:
        """Calculate all indicators and return a clean snapshot dict."""
        close = df["close"]

        ema20  = self._calc_ema(close, 20)
        ema50  = self._calc_ema(close, 50)
        ema200 = self._calc_ema(close, 200)
        rsi    = self._calc_rsi(close, 14)
        macd_line, signal_line, histogram = self._calc_macd(close)
        atr    = self._calc_atr(df, 14)
        adx, pdi, mdi = self._calc_adx(df, 14)

        last_close = close.iloc[-1]
        last_atr   = atr.iloc[-1]
        last_adx   = adx.iloc[-1]
        last_pdi   = pdi.iloc[-1]
        last_mdi   = mdi.iloc[-1]
        last_rsi   = rsi.iloc[-1]
        last_macd  = macd_line.iloc[-1]
        last_sig   = signal_line.iloc[-1]
        last_hist  = histogram.iloc[-1]
        prev_hist  = histogram.iloc[-2]

        # If using EURUSD as proxy, invert the logic
        price_above_ema20  = (last_close > ema20.iloc[-1])  if not is_inverse else (last_close < ema20.iloc[-1])
        price_above_ema50  = (last_close > ema50.iloc[-1])  if not is_inverse else (last_close < ema50.iloc[-1])
        price_above_ema200 = (last_close > ema200.iloc[-1]) if not is_inverse else (last_close < ema200.iloc[-1])
        rsi_bullish        = (last_rsi > 50)                if not is_inverse else (last_rsi < 50)
        macd_bullish       = (last_macd > last_sig)         if not is_inverse else (last_macd < last_sig)
        hist_expanding     = abs(last_hist) > abs(prev_hist)

        return {
            "last_close":        round(float(last_close), 5),
            "ema20":             round(float(ema20.iloc[-1]), 5),
            "ema50":             round(float(ema50.iloc[-1]), 5),
            "ema200":            round(float(ema200.iloc[-1]), 5),
            "rsi":               round(float(last_rsi), 2),
            "macd":              round(float(last_macd), 5),
            "signal":            round(float(last_sig), 5),
            "histogram":         round(float(last_hist), 5),
            "atr":               round(float(last_atr), 5),
            "adx":               round(float(last_adx), 2),
            "plus_di":           round(float(last_pdi), 2),
            "minus_di":          round(float(last_mdi), 2),
            "price_above_ema20":  bool(price_above_ema20),
            "price_above_ema50":  bool(price_above_ema50),
            "price_above_ema200": bool(price_above_ema200),
            "rsi_bullish_usd":    bool(rsi_bullish),
            "macd_bullish_usd":   bool(macd_bullish),
            "momentum_expanding": bool(hist_expanding),
        }

    # ------------------------------------------------------------------ #
    #  MACRO PILLAR 1 — DXY BASKET (MT5, called before shutdown)
    # ------------------------------------------------------------------ #

    def _get_dxy_basket(self) -> dict:
        """
        Compute weighted USD momentum from DXY basket components.
        Requires active MT5 connection. Call before mt5.shutdown().
        """
        pair_results = {}
        total_weight = 0.0

        for symbol, cfg in self.BASKET.items():
            weight    = cfg["weight"]
            direction = cfg["direction"]
            try:
                df = self._get_ohlcv(symbol, mt5.TIMEFRAME_H4, 60)
                if df is None or len(df) < 55:
                    continue

                close  = df["close"]
                ema20  = self._calc_ema(close, 20).iloc[-1]
                ema50  = self._calc_ema(close, 50).iloc[-1]
                price  = close.iloc[-1]

                # Determine USD signal from this pair
                if direction == "direct":
                    # e.g. USDJPY: price rising = USD bullish
                    if price > ema20 and ema20 > ema50:
                        usd_signal = 1.0   # USD bullish
                    elif price < ema20 and ema20 < ema50:
                        usd_signal = -1.0  # USD bearish
                    else:
                        usd_signal = 0.0   # neutral
                else:
                    # e.g. EURUSD: price falling = USD bullish
                    if price < ema20 and ema20 < ema50:
                        usd_signal = 1.0   # USD bullish
                    elif price > ema20 and ema20 > ema50:
                        usd_signal = -1.0  # USD bearish
                    else:
                        usd_signal = 0.0   # neutral

                pair_results[symbol] = {
                    "usd_signal": usd_signal,
                    "weight":     weight,
                    "price":      round(float(price), 5),
                }
                total_weight += weight

            except Exception as e:
                print(f"[{self.NAME}] Basket: {symbol} error — {e}")

        if not pair_results or total_weight == 0:
            return {
                "weighted_usd_score": 0.0,
                "basket_trend":       "FLAT",
                "pairs_available":    0,
                "pair_breakdown":     {},
            }

        # Renormalize weights if some pairs were unavailable
        weighted_score = sum(
            p["usd_signal"] * (p["weight"] / total_weight)
            for p in pair_results.values()
        )

        if weighted_score > 0.3:
            basket_trend = "RISING"
        elif weighted_score < -0.3:
            basket_trend = "FALLING"
        else:
            basket_trend = "FLAT"

        print(f"[{self.NAME}] Basket score: {weighted_score:+.3f} → {basket_trend} "
              f"({len(pair_results)}/4 pairs)")

        return {
            "weighted_usd_score": round(weighted_score, 4),
            "basket_trend":       basket_trend,
            "pairs_available":    len(pair_results),
            "pair_breakdown":     pair_results,
        }

    # ------------------------------------------------------------------ #
    #  MACRO PILLAR 2 — RATE DIFFERENTIAL (HTTP, after MT5 shutdown)
    # ------------------------------------------------------------------ #

    def _get_rate_differential(self) -> dict:
        """
        Fetch US 10Y (FRED) and EU 10Y German Bund (ECB) yields.
        Compute spread and interpret USD implications.
        Falls back gracefully on any network error.
        """
        fallback = {
            "us_10y":         None,
            "eu_10y":         None,
            "spread":         None,
            "interpretation": "NEUTRAL",
            "data_available": False,
        }

        # ── US 10Y (FRED CSV, no auth required) ──────────────────────
        us_10y = None
        try:
            r = requests.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
                timeout=10,
                headers={"User-Agent": "APEX-Capital-AI/1.0"},
            )
            r.raise_for_status()
            lines = r.text.strip().split("\n")
            # Header is DATE,DGS10. Values use "." for missing (weekends)
            for line in reversed(lines[1:]):
                parts = line.split(",")
                if len(parts) == 2 and parts[1].strip() not in (".", ""):
                    us_10y = float(parts[1].strip())
                    break
        except Exception as e:
            print(f"[{self.NAME}] FRED fetch failed: {e}")

        # ── EU 10Y — German Bund (ECB API, no auth required) ─────────
        eu_10y = None
        try:
            start = (date.today() - timedelta(days=30)).isoformat()
            url   = (
                f"https://data-api.ecb.europa.eu/service/data/"
                f"IRS/D.DE.L.L40.CI.0.EUR.N.Z"
                f"?startPeriod={start}&format=csvdata"
            )
            r = requests.get(url, timeout=10,
                             headers={"User-Agent": "APEX-Capital-AI/1.0"})
            r.raise_for_status()
            if r.text.strip():
                df = pd.read_csv(io.StringIO(r.text))
                col = next((c for c in df.columns
                            if "OBS_VALUE" in c.upper()), None)
                if col:
                    vals = pd.to_numeric(df[col], errors="coerce").dropna()
                    if not vals.empty:
                        eu_10y = float(vals.iloc[-1])
        except Exception as e:
            print(f"[{self.NAME}] ECB fetch failed: {e}")

        if us_10y is None and eu_10y is None:
            print(f"[{self.NAME}] Rate diff: both APIs unavailable — using NEUTRAL")
            return fallback

        # ── Spread and interpretation ─────────────────────────────────
        spread = None
        if us_10y is not None and eu_10y is not None:
            spread = round(us_10y - eu_10y, 4)
            if spread > 1.5:
                interp = "BULLISH_USD"
            elif spread < 0.5:
                interp = "BEARISH_USD"
            else:
                interp = "NEUTRAL"
        else:
            interp = "NEUTRAL"

        print(f"[{self.NAME}] Rate diff: US={us_10y}% EU={eu_10y}% "
              f"Spread={spread}% → {interp}")

        return {
            "us_10y":         us_10y,
            "eu_10y":         eu_10y,
            "spread":         spread,
            "interpretation": interp,
            "data_available": True,
        }

    # ------------------------------------------------------------------ #
    #  MACRO PILLAR 3 — FED RHETORIC (HTTP + optional news context)
    # ------------------------------------------------------------------ #

    def _get_fed_sentiment(self, news_broadcast: dict | None = None) -> dict:
        """
        Count hawkish/dovish signals from Fed RSS headlines.
        Optionally enriches with NEWS agent's usd_bias.
        Falls back gracefully on any network error.
        """
        HAWKISH_KW = ["rate hike", "tightening", "inflation", "higher for longer",
                      "restrict", "above target", "aggressive", "overheat"]
        DOVISH_KW  = ["rate cut", "easing", "pause", "unemployment", "softer",
                      "pivot", "below target", "cooling", "slowdown", "recession"]

        hawkish_count  = 0
        dovish_count   = 0
        latest_headline = ""
        data_available  = False

        try:
            r = requests.get(
                "https://www.federalreserve.gov/feeds/press_all.xml",
                timeout=10,
                headers={"User-Agent": "APEX-Capital-AI/1.0"},
            )
            r.raise_for_status()
            xml = r.text.lower()

            # Extract <title> tags from <item> blocks (avoid channel title)
            in_item = False
            for line in xml.split("\n"):
                if "<item>" in line:
                    in_item = True
                if in_item and "<title>" in line:
                    start = line.find("<title>") + 7
                    end   = line.find("</title>")
                    if end > start:
                        title = line[start:end].strip()
                        if not latest_headline:
                            latest_headline = title
                        for kw in HAWKISH_KW:
                            if kw in title:
                                hawkish_count += 1
                        for kw in DOVISH_KW:
                            if kw in title:
                                dovish_count += 1
                if "</item>" in line:
                    in_item = False

            data_available = True

        except Exception as e:
            print(f"[{self.NAME}] Fed RSS fetch failed: {e}")

        # Also incorporate NEWS agent's USD bias if available
        news_usd_bias = ""
        if news_broadcast:
            news_usd_bias = news_broadcast.get("usd_bias", "")
            if news_usd_bias == "BULLISH":
                hawkish_count += 1
            elif news_usd_bias == "BEARISH":
                dovish_count += 1

        if hawkish_count > dovish_count:
            fed_stance = "HAWKISH"
        elif dovish_count > hawkish_count:
            fed_stance = "DOVISH"
        else:
            fed_stance = "NEUTRAL"

        print(f"[{self.NAME}] Fed sentiment: {fed_stance} "
              f"(hawkish={hawkish_count} dovish={dovish_count})")

        return {
            "fed_stance":      fed_stance,
            "hawkish_count":   hawkish_count,
            "dovish_count":    dovish_count,
            "latest_headline": latest_headline[:120] if latest_headline else "N/A",
            "news_usd_bias":   news_usd_bias,
            "data_available":  data_available,
        }

    # ------------------------------------------------------------------ #
    #  CLAUDE AI ANALYSIS (all 4 pillars)
    # ------------------------------------------------------------------ #

    def _ask_claude(self, h4_data: dict, h1_data: dict,
                    basket: dict, rate_diff: dict,
                    fed_sentiment: dict, symbol: str) -> dict:

        is_inverse = (symbol == self.PROXY_SYMBOL)
        proxy_note = (f"\nNOTE: {symbol} is an INVERSE proxy — "
                      f"{symbol} down = USD up, {symbol} up = USD down.\n"
                      if is_inverse else "")

        # Format basket breakdown
        pair_lines = "\n".join(
            f"  {sym}: signal={v['usd_signal']:+.0f} weight={v['weight']:.1%} price={v['price']}"
            for sym, v in basket.get("pair_breakdown", {}).items()
        ) or "  N/A"

        prompt = f"""You are DOLLAR, the US Dollar Index specialist at APEX Capital AI.
Synthesise the following 4 macro pillars into a definitive DXY broadcast.
{proxy_note}
=== PILLAR 1: TECHNICAL ANALYSIS ({symbol}) ===
H4 Snapshot:
  Price: {h4_data['last_close']} | EMA20: {h4_data['ema20']} | EMA50: {h4_data['ema50']} | EMA200: {h4_data['ema200']}
  RSI: {h4_data['rsi']} | MACD hist: {h4_data['histogram']} | ATR: {h4_data['atr']} | ADX: {h4_data['adx']}
  Price>EMA20: {h4_data['price_above_ema20']} | Price>EMA50: {h4_data['price_above_ema50']} | Price>EMA200: {h4_data['price_above_ema200']}
  RSI bullish USD: {h4_data['rsi_bullish_usd']} | MACD bullish USD: {h4_data['macd_bullish_usd']}

H1 Snapshot:
  Price: {h1_data['last_close']} | ATR: {h1_data['atr']} | ADX: {h1_data['adx']}
  RSI: {h1_data['rsi']} | MACD hist: {h1_data['histogram']}
  RSI bullish USD: {h1_data['rsi_bullish_usd']} | MACD bullish USD: {h1_data['macd_bullish_usd']}

=== PILLAR 2: DXY BASKET ANALYSIS ===
Weighted USD score: {basket['weighted_usd_score']:+.4f}  (-1.0=max bearish → +1.0=max bullish)
Basket trend: {basket['basket_trend']} | Pairs available: {basket['pairs_available']}/4
Per-pair breakdown:
{pair_lines}

=== PILLAR 3: RATE DIFFERENTIAL (US 10Y vs EU 10Y) ===
US 10Y yield:  {rate_diff['us_10y']}%
EU 10Y yield:  {rate_diff['eu_10y']}%  (German Bund)
Spread (US-EU): {rate_diff['spread']}%
Signal: {rate_diff['interpretation']}
Guide: spread >1.5% = strong USD carry support | <0.5% = USD disadvantage
Data available: {rate_diff['data_available']}

=== PILLAR 4: FED RHETORIC ===
Fed stance: {fed_sentiment['fed_stance']}
Hawkish signals: {fed_sentiment['hawkish_count']} | Dovish signals: {fed_sentiment['dovish_count']}
Latest Fed headline: {fed_sentiment['latest_headline']}
NEWS agent USD bias: {fed_sentiment['news_usd_bias'] or 'not provided'}
Data available: {fed_sentiment['data_available']}

=== YOUR TASK ===
Weigh all 4 pillars and produce the DXY broadcast. When pillars conflict,
technical + basket confirmation outweigh individual macro readings.
Rate differential is a slow-moving anchor; Fed rhetoric amplifies short-term moves.

Respond with ONLY this JSON object:
{{
  "dxy_trend":            "RISING" | "FALLING" | "FLAT",
  "usd_bias":             "BULLISH_USD" | "BEARISH_USD" | "NEUTRAL",
  "strength":             "STRONG" | "MODERATE" | "WEAK",
  "key_level_note":       "one sentence on key technical level",
  "gold_implication":     "BEARISH" | "BULLISH" | "NEUTRAL",
  "eurusd_implication":   "HEADWIND" | "TAILWIND" | "NEUTRAL",
  "usdjpy_implication":   "HEADWIND" | "TAILWIND" | "NEUTRAL",
  "risk_regime":          "RISK_ON" | "RISK_OFF" | "MIXED",
  "confidence":           0-100,
  "yield_spread_signal":  "BULLISH_USD" | "BEARISH_USD" | "NEUTRAL",
  "fed_stance_confirmed": "HAWKISH" | "DOVISH" | "NEUTRAL",
  "basket_confirmation":  "STRONG" | "MODERATE" | "WEAK" | "CONFLICTING",
  "reasoning":            "2-3 sentences synthesising all 4 pillars"
}}"""

        response = self.client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        if not raw:
            raise ValueError("Claude returned empty response")
        return json.loads(raw)

    # ------------------------------------------------------------------ #
    #  TRADE PROPOSAL (ATR-based SL/TP)
    # ------------------------------------------------------------------ #

    def build_proposal(self, h4_data: dict, h1_data: dict,
                        broadcast: dict) -> dict | None:
        """
        Build a EURUSD trade proposal when Dollar sees a strong setup.
        SL = 1.2x H1 ATR (min 20 pips floor)
        TP = 2.5x SL (~2.1 R:R)
        """
        bullish_score = sum([
            h4_data["price_above_ema20"],
            h4_data["price_above_ema50"],
            h4_data["rsi_bullish_usd"],
            h4_data["macd_bullish_usd"],
        ])
        bearish_score = sum([
            not h4_data["price_above_ema20"],
            not h4_data["price_above_ema50"],
            not h4_data["rsi_bullish_usd"],
            not h4_data["macd_bullish_usd"],
        ])

        if bullish_score < 3 and bearish_score < 3:
            return None

        direction  = "LONG_USD" if bullish_score >= 3 else "SHORT_USD"
        trade_dir  = "SHORT" if direction == "LONG_USD" else "LONG"
        confidence = broadcast.get("confidence", 0)

        if confidence < 70:
            return None

        current_price = h1_data.get("last_close", 0)
        if current_price <= 0:
            return None

        # ── ATR-based SL/TP ───────────────────────────────────────────
        pip    = 0.0001
        h1_atr = h1_data.get("atr", 0.0)

        # Minimum 20 pip floor; use ATR if it gives a wider SL
        sl_pts = max(1.2 * h1_atr, 20 * pip)
        tp_pts = 2.5 * sl_pts

        if trade_dir == "SHORT":
            sl_price = round(current_price + sl_pts, 5)
            tp_price = round(current_price - tp_pts, 5)
        else:
            sl_price = round(current_price - sl_pts, 5)
            tp_price = round(current_price + tp_pts, 5)

        lot = 0.01 if confidence < 75 else (0.02 if confidence < 85 else 0.03)

        return {
            "agent":             self.NAME,
            "instrument":        "EURUSD",
            "direction":         trade_dir,
            "confidence":        confidence,
            "lot_size_request":  lot,
            "sl_points":         round(sl_pts, 5),
            "tp_points":         round(tp_pts, 5),
            "stop_loss_price":   sl_price,
            "take_profit_price": tp_price,
            "entry_price":       current_price,
            "atr":               round(h1_atr, 5),
            "h4_trend":          broadcast["dxy_trend"],
            "rsi":               h4_data["rsi"],
            "adx":               h4_data["adx"],
            "macd_bullish":      h4_data["macd_bullish_usd"],
            "reasoning":         (
                f"USD {direction} — {bullish_score if direction == 'LONG_USD' else bearish_score}/4 technicals. "
                f"Basket: {broadcast.get('basket_confirmation','N/A')} | "
                f"Yield spread: {broadcast.get('yield_spread_signal','N/A')} | "
                f"Fed: {broadcast.get('fed_stance_confirmed','N/A')}. "
                f"SL: {round(sl_pts/pip):.0f}pips | TP: {round(tp_pts/pip):.0f}pips. "
                f"{broadcast['reasoning']}"
            ),
            "risk_regime":       broadcast["risk_regime"],
            "timestamp":         datetime.utcnow().isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  MAIN ENTRY POINT
    # ------------------------------------------------------------------ #

    def analyse(self, news_broadcast: dict | None = None) -> dict:
        """
        Run the full DOLLAR analysis cycle (4 macro pillars).
        Accepts optional news_broadcast from the NEWS agent for Fed sentiment enrichment.
        Returns the broadcast dict that ALL other agents must consume.
        """
        print(f"\n[{self.NAME}] Starting macro analysis (4 pillars)...")

        if not self._connect_mt5():
            return self._fallback_broadcast("MT5 connection failed")

        symbol = self._find_active_symbol()
        if not symbol:
            mt5.shutdown()
            return self._fallback_broadcast("No DXY symbol available")

        self.active_symbol = symbol
        is_inverse = (symbol == self.PROXY_SYMBOL)

        df_h4 = self._get_ohlcv(symbol, mt5.TIMEFRAME_H4)
        df_h1 = self._get_ohlcv(symbol, mt5.TIMEFRAME_H1)

        if df_h4 is None or df_h1 is None:
            mt5.shutdown()
            return self._fallback_broadcast("Could not fetch price data")

        h4_data = self._analyse_technicals(df_h4, is_inverse)
        h1_data = self._analyse_technicals(df_h1, is_inverse)

        # Pillar 1 (technicals) done above.
        # Pillar 2: DXY basket — while MT5 still connected
        basket = self._get_dxy_basket()

        mt5.shutdown()

        # Pillars 3 & 4 — external HTTP (MT5 not needed)
        rate_diff     = self._get_rate_differential()
        fed_sentiment = self._get_fed_sentiment(news_broadcast)

        try:
            broadcast = self._ask_claude(
                h4_data, h1_data, basket, rate_diff, fed_sentiment, symbol
            )
        except Exception as e:
            return self._fallback_broadcast(f"Claude API error: {e}")

        # Attach metadata
        broadcast["symbol_used"]       = symbol
        broadcast["is_inverse_proxy"]  = is_inverse
        broadcast["timestamp"]         = datetime.utcnow().isoformat()
        broadcast["agent"]             = self.NAME
        broadcast["dxy_basket"]        = basket
        broadcast["rate_differential"] = rate_diff
        broadcast["fed_sentiment"]     = fed_sentiment

        # Build trade proposal
        proposal = self.build_proposal(h4_data, h1_data, broadcast)
        broadcast["trade_proposal"] = proposal

        self.broadcast = broadcast

        print(f"[{self.NAME}] Broadcast ready: {broadcast['usd_bias']} | "
              f"Basket: {basket['basket_trend']} ({basket['weighted_usd_score']:+.3f}) | "
              f"Spread: {rate_diff.get('spread')}% | "
              f"Fed: {fed_sentiment['fed_stance']} | "
              f"Regime: {broadcast['risk_regime']} | "
              f"Confidence: {broadcast['confidence']}%")

        return broadcast

    def on_atlas_decision(self, decision: dict):
        """Receive MANAGER approval/rejection for trade proposal."""
        status = decision.get("status", "UNKNOWN")
        reason = decision.get("reason", "")
        print(f"[{self.NAME}] MANAGER decision: {status} — {reason}")

    def receive_dollar_broadcast(self, broadcast: dict):
        """No-op — DOLLAR is the source, not a consumer."""
        pass

    def _fallback_broadcast(self, reason: str) -> dict:
        """Safe fallback when analysis fails."""
        print(f"[{self.NAME}] WARNING — Using fallback broadcast. Reason: {reason}")
        return {
            "agent":                self.NAME,
            "dxy_trend":            "FLAT",
            "usd_bias":             "NEUTRAL",
            "strength":             "WEAK",
            "key_level_note":       "Analysis unavailable",
            "gold_implication":     "NEUTRAL",
            "eurusd_implication":   "NEUTRAL",
            "usdjpy_implication":   "NEUTRAL",
            "risk_regime":          "MIXED",
            "confidence":           0,
            "yield_spread_signal":  "NEUTRAL",
            "fed_stance_confirmed": "NEUTRAL",
            "basket_confirmation":  "WEAK",
            "reasoning":            f"Fallback: {reason}",
            "trade_proposal":       None,
            "dxy_basket":           {"weighted_usd_score": 0.0, "basket_trend": "FLAT",
                                     "pairs_available": 0, "pair_breakdown": {}},
            "rate_differential":    {"data_available": False, "interpretation": "NEUTRAL"},
            "fed_sentiment":        {"fed_stance": "NEUTRAL", "data_available": False},
            "timestamp":            datetime.utcnow().isoformat(),
            "error":                reason,
        }
