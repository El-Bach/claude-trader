"""
USDJPY — US Dollar/Japanese Yen Specialist
APEX Capital AI

Strategy: Trend Following (stronger trends than EURUSD)
- H4 EMA stack + ADX (ADX > 25 required — USDJPY trends hard)
- Ichimoku Cloud on H4 for institutional bias
- Risk sentiment filter: RISK_OFF = Yen strengthens = USDJPY bearish
- US10Y yield proxy via DXY strength
- BoJ intervention warning above 150.00
- Session: Tokyo + London + NY overlap
- Round number awareness: 140, 145, 148, 150, 152, 155

Indicator engine: same battle-tested pattern as gold.py
"""

import os
import json
import time
import anthropic
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from agents.cot import get_cot_data, cot_text
import traceback

load_dotenv()

BEIRUT_TZ    = "Asia/Beirut"
SYMBOL       = "USDJPY"
EMA_FAST     = 20
EMA_SLOW     = 50
EMA_200      = 200
RSI_PERIOD   = 14
ATR_PERIOD   = 14
ADX_PERIOD   = 14
CANDLES_BACK = 250
MAX_SPREAD   = 0.030    # 3.0 pips max for USDJPY
MAGIC        = 20250401

# BoJ intervention danger zone
BOJ_INTERVENTION_LEVEL = 150.00

# Key psychological levels
ROUND_LEVELS = [135.00, 138.00, 140.00, 142.00, 144.00,
                145.00, 146.00, 147.00, 148.00, 149.00,
                150.00, 151.00, 152.00, 153.00, 155.00, 158.00, 160.00]


class USDJPYAgent:
    NAME   = "USDJPY"
    SYMBOL = "USDJPY"

    def __init__(self):
        self.client           = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.dollar_broadcast = None
        self.intraday_pct     = 0.0
        self.strategy_plan    = None

    # ── Dollar Broadcast ──────────────────────────────────────────────
    def receive_dollar_broadcast(self, broadcast: dict):
        self.dollar_broadcast = broadcast
        usd    = broadcast.get("usd_bias", "NEUTRAL")
        regime = broadcast.get("risk_regime", "MIXED")
        # USDJPY is DIRECT with USD (USD up = USDJPY up)
        # BUT also inverse with risk (RISK_OFF = Yen safe haven = USDJPY down)
        impl = "BULLISH" if usd == "BULLISH_USD" else (
               "BEARISH" if usd == "BEARISH_USD" else "NEUTRAL")
        # Risk-off overrides USD strength for USDJPY
        if regime == "RISK_OFF":
            impl = "BEARISH"   # Yen strengthens in risk-off regardless of USD
        print(f"[{self.NAME}] Dollar signal: USD={usd} | Regime={regime} → USDJPY bias={impl}")

    # ── Strategy Plan (from STRATEGIST agent) ─────────────────────────
    def receive_strategy_plan(self, plan: dict):
        self.strategy_plan = plan
        bias = plan.get("bias", "NEUTRAL")
        print(f"[{self.NAME}] Strategy plan received: bias={bias}")

    def _strategy_text(self) -> str:
        if not self.strategy_plan:
            return "No daily strategy plan available."
        p = self.strategy_plan
        lines = [
            f"Daily Bias    : {p.get('bias', 'N/A')}",
            f"Structure     : {p.get('structure', 'N/A')}",
            f"Key S/R       : {p.get('key_levels', 'N/A')}",
            f"Entry Zone    : {p.get('entry_zone', 'N/A')}",
            f"Invalidation  : {p.get('invalidation', 'N/A')}",
            f"Trade Idea    : {p.get('trade_idea', 'N/A')}",
            f"TP Target     : {p.get('tp_target', 'N/A')}",
            f"SL Suggestion : {p.get('sl_suggestion', 'N/A')}",
            f"Notes         : {p.get('notes', 'N/A')}",
        ]
        return "\n".join(lines)

    # ── MT5 Connection ────────────────────────────────────────────────
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

    def _ensure_connected(self) -> bool:
        if mt5.account_info() is not None:
            return True
        for attempt in range(1, 4):
            mt5.shutdown()
            time.sleep(5)
            if self._connect_mt5() and mt5.account_info() is not None:
                return True
        return False

    # ── Spread Check ──────────────────────────────────────────────────
    def _check_spread(self) -> tuple[bool, float]:
        tick = mt5.symbol_info_tick(self.SYMBOL)
        if tick is None:
            return False, 0.0
        spread = tick.ask - tick.bid
        return spread <= MAX_SPREAD, round(spread, 3)

    # ── BoJ Intervention Warning ──────────────────────────────────────
    def _boj_warning(self, price: float) -> dict:
        danger    = price >= BOJ_INTERVENTION_LEVEL
        distance  = round(abs(price - BOJ_INTERVENTION_LEVEL), 3)
        extreme   = price >= 152.00
        return {
            "in_danger_zone":   danger,
            "distance_to_150":  distance,
            "extreme_risk":     extreme,
            "warning":          "⚠️ BOJ INTERVENTION RISK" if danger else "Safe"
        }

    # ── Nearest Round Level ───────────────────────────────────────────
    def _nearest_round_level(self, price: float) -> dict:
        distances = [(abs(price - lvl), lvl) for lvl in ROUND_LEVELS]
        distances.sort()
        nearest  = distances[0][1]
        distance = distances[0][0]
        return {
            "nearest_level":    round(nearest, 2),
            "distance_pips":    round(distance * 100, 1),
            "price_near_round": distance < 0.30,
            "above_level":      price > nearest,
        }

    # ── Ichimoku Cloud ────────────────────────────────────────────────
    def _calc_ichimoku(self, df: pd.DataFrame) -> dict:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        # Tenkan-sen (9 period)
        tenkan  = (high.rolling(9).max() + low.rolling(9).min()) / 2
        # Kijun-sen (26 period)
        kijun   = (high.rolling(26).max() + low.rolling(26).min()) / 2
        # Senkou Span A
        span_a  = ((tenkan + kijun) / 2).shift(26)
        # Senkou Span B (52 period)
        span_b  = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
        # Chikou Span
        chikou  = close.shift(-26)

        lc        = close.iloc[-1]
        sa        = float(span_a.iloc[-1]) if not pd.isna(span_a.iloc[-1]) else 0
        sb        = float(span_b.iloc[-1]) if not pd.isna(span_b.iloc[-1]) else 0
        cloud_top = max(sa, sb)
        cloud_bot = min(sa, sb)

        above_cloud = bool(lc > cloud_top)
        below_cloud = bool(lc < cloud_bot)
        in_cloud    = bool(cloud_bot <= lc <= cloud_top)

        return {
            "tenkan":      round(float(tenkan.iloc[-1]), 3),
            "kijun":       round(float(kijun.iloc[-1]), 3),
            "span_a":      round(sa, 3),
            "span_b":      round(sb, 3),
            "cloud_top":   round(cloud_top, 3),
            "cloud_bot":   round(cloud_bot, 3),
            "above_cloud": above_cloud,
            "below_cloud": below_cloud,
            "in_cloud":    in_cloud,
            "bullish_cloud": sa > sb,   # Bullish cloud = span A above span B
        }

    # ── Indicators ────────────────────────────────────────────────────
    def _get_indicators(self, timeframe) -> dict | None:
        rates = mt5.copy_rates_from_pos(self.SYMBOL, timeframe, 0, CANDLES_BACK)
        if rates is None or len(rates) < 210:
            return None
        df    = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # EMAs
        ef  = close.ewm(span=EMA_FAST, adjust=False).mean()
        es  = close.ewm(span=EMA_SLOW, adjust=False).mean()
        e2  = close.ewm(span=EMA_200,  adjust=False).mean()

        # Wilder RSI
        alpha = 1.0 / RSI_PERIOD
        d     = close.diff()
        gain  = d.where(d > 0, 0.0).ewm(alpha=alpha, adjust=False).mean()
        loss  = (-d.where(d < 0, 0.0)).ewm(alpha=alpha, adjust=False).mean()
        rsi   = 100 - (100 / (1 + gain / loss))

        # ATR (ewm Wilder)
        tr  = pd.concat([high - low,
                         (high - close.shift()).abs(),
                         (low  - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

        # ADX (Wilder)
        aa      = 1.0 / ADX_PERIOD
        pdm     = high.diff()
        mdm     = -low.diff()
        pdm     = pdm.where((pdm > mdm) & (pdm > 0), 0.0)
        mdm     = mdm.where((mdm > pdm) & (mdm > 0), 0.0)
        atr_adx = tr.ewm(alpha=aa, adjust=False).mean()
        pdi     = 100 * (pdm.ewm(alpha=aa, adjust=False).mean() / atr_adx)
        mdi     = 100 * (mdm.ewm(alpha=aa, adjust=False).mean() / atr_adx)
        dx      = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1)
        adx     = dx.ewm(alpha=aa, adjust=False).mean()

        # Last 3 candles
        tail = df.tail(3)[["open","high","low","close"]].copy()
        tail["body"]       = (tail["close"] - tail["open"]).abs()
        tail["upper_wick"] = tail["high"] - tail[["open","close"]].max(axis=1)
        tail["lower_wick"] = tail[["open","close"]].min(axis=1) - tail["low"]

        return {
            "price":          float(close.iloc[-1]),
            "price_prev":     float(close.iloc[-2]),
            "ema_fast":       float(ef.iloc[-1]),
            "ema_fast_prev":  float(ef.iloc[-2]),
            "ema_slow":       float(es.iloc[-1]),
            "ema_slow_prev":  float(es.iloc[-2]),
            "ema_200":        float(e2.iloc[-1]),
            "ema_200_prev":   float(e2.iloc[-2]),
            "rsi":            float(rsi.iloc[-1]),
            "rsi_prev":       float(rsi.iloc[-2]),
            "atr":            float(atr.iloc[-1]),
            "adx":            float(adx.iloc[-1]),
            "adx_prev":       float(adx.iloc[-2]),
            "plus_di":        float(pdi.iloc[-1]),
            "minus_di":       float(mdi.iloc[-1]),
            "candles_tail":   tail[["open","high","low","close",
                                    "body","upper_wick","lower_wick"]
                                   ].round(3).reset_index(drop=True).to_dict(),
        }

    # ── Session VWAP ──────────────────────────────────────────────────
    def _get_key_levels(self, current_price: float, h4_atr: float) -> dict:
        """PDH/PDL/PWH/PWL/Monthly Open — institutional price clusters."""
        try:
            d1_rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_D1, 0, 35)
            w1_rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_W1, 0,  5)
            if d1_rates is None or len(d1_rates) < 2:
                return {"available": False}

            df_d1 = pd.DataFrame(d1_rates)
            df_d1["time"] = pd.to_datetime(df_d1["time"], unit="s")

            prev         = df_d1.iloc[-2]
            pdh          = round(float(prev["high"]),  3)
            pdl          = round(float(prev["low"]),   3)
            pdc          = round(float(prev["close"]), 3)

            now_month    = pd.Timestamp.now().replace(day=1).normalize()
            month_bars   = df_d1[df_d1["time"].dt.normalize() >= now_month]
            monthly_open = round(float(month_bars.iloc[0]["open"]), 3) if len(month_bars) > 0 else None

            pwh = pwl = None
            if w1_rates is not None and len(w1_rates) >= 2:
                prev_w = pd.DataFrame(w1_rates).iloc[-2]
                pwh = round(float(prev_w["high"]), 3)
                pwl = round(float(prev_w["low"]),  3)

            proximity = h4_atr * 0.5
            all_levels = {"PDH": pdh, "PDL": pdl, "PDC": pdc,
                          "PWH": pwh, "PWL": pwl, "Monthly_Open": monthly_open}

            nearby = []
            for name, lvl in all_levels.items():
                if lvl is None:
                    continue
                dist = abs(current_price - lvl)
                if dist <= proximity:
                    nearby.append({
                        "level":    name,
                        "price":    lvl,
                        "distance": round(dist, 3),
                        "position": "ABOVE" if current_price > lvl else "BELOW",
                    })

            return {
                "available":      True,
                "pdh":            pdh,  "pdl":  pdl,  "pdc":  pdc,
                "pwh":            pwh,  "pwl":  pwl,
                "monthly_open":   monthly_open,
                "nearby":         nearby,
                "near_key_level": len(nearby) > 0,
            }
        except Exception:
            return {"available": False}

    def _levels_text(self, levels: dict) -> str:
        if not levels.get("available"):
            return "Key levels unavailable"
        lines = [
            f"PDH: {levels['pdh']} | PDL: {levels['pdl']} | PDC: {levels['pdc']}",
            f"PWH: {levels['pwh']} | PWL: {levels['pwl']}",
            f"Monthly Open: {levels['monthly_open']}",
        ]
        if levels["nearby"]:
            near = " | ".join(
                f"{n['level']} @ {n['price']} ({n['distance']} away, price {n['position']})"
                for n in levels["nearby"]
            )
            lines.append(f"⚡ NEARBY (within 0.5x H4 ATR): {near}")
        else:
            lines.append("No key levels within 0.5x H4 ATR — price in open space")
        return "\n".join(lines)

    def _get_htf_bias(self) -> dict:
        """D1 + W1 trend bias — master timeframe filter. Cached once per day."""
        now = datetime.utcnow()
        cached_at = getattr(self, "_htf_cache_time", None)
        if cached_at and (now - cached_at).total_seconds() < 23 * 3600:
            return self._htf_cache

        # D1: 250 bars (~1 yr) — enough for EMA 200 with clean data after warmup
        # W1: 100 bars (~2 yr) — EMA 50 used as long-term anchor (EMA 200 needs 200 bars,
        #                        meaningless on weekly for an intraday system)
        result = {}
        for label, tf, bars, use_ema200, min_bars in [
            ("d1", mt5.TIMEFRAME_D1, 250, True,  210),
            ("w1", mt5.TIMEFRAME_W1, 100, False,  55),
        ]:
            try:
                rates = mt5.copy_rates_from_pos(self.SYMBOL, tf, 0, bars)
                if rates is None or len(rates) < min_bars:
                    result[label] = {"available": False}
                    continue
                df    = pd.DataFrame(rates)
                close = df["close"]
                high  = df["high"]
                low   = df["low"]

                ema20 = close.ewm(span=20, adjust=False).mean()
                ema50 = close.ewm(span=50, adjust=False).mean()

                alpha = 1.0 / 14
                d     = close.diff()
                gain  = d.where(d > 0, 0.0).ewm(alpha=alpha, adjust=False).mean()
                loss  = (-d.where(d < 0, 0.0)).ewm(alpha=alpha, adjust=False).mean()
                rsi   = float((100 - 100 / (1 + gain / loss)).iloc[-1])

                tr    = pd.concat([high - low, (high - close.shift()).abs(),
                                   (low  - close.shift()).abs()], axis=1).max(axis=1)
                aa    = 1.0 / 14
                pdm   = high.diff().where((high.diff() > -low.diff()) & (high.diff() > 0), 0.0)
                mdm   = (-low.diff()).where((-low.diff() > high.diff()) & (-low.diff() > 0), 0.0)
                atr_w = tr.ewm(alpha=aa, adjust=False).mean()
                pdi   = 100 * (pdm.ewm(alpha=aa, adjust=False).mean() / atr_w)
                mdi   = 100 * (mdm.ewm(alpha=aa, adjust=False).mean() / atr_w)
                dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1)
                adx   = float(dx.ewm(alpha=aa, adjust=False).mean().iloc[-1])

                p   = float(close.iloc[-1])
                e20 = float(ema20.iloc[-1])
                e50 = float(ema50.iloc[-1])

                if use_ema200:
                    ema200 = close.ewm(span=200, adjust=False).mean()
                    e200   = float(ema200.iloc[-1])
                    bull_stack = p > e20 > e50 > e200
                    bear_stack = p < e20 < e50 < e200
                    if bull_stack:   bias = "BULLISH"
                    elif bear_stack: bias = "BEARISH"
                    elif p > e200:   bias = "BULLISH_WEAK"
                    else:            bias = "BEARISH_WEAK"
                    anchor = round(e200, 3)
                    anchor_label = "EMA200"
                else:
                    e200 = None
                    bull_stack = p > e20 > e50
                    bear_stack = p < e20 < e50
                    if bull_stack:   bias = "BULLISH"
                    elif bear_stack: bias = "BEARISH"
                    elif p > e50:    bias = "BULLISH_WEAK"
                    else:            bias = "BEARISH_WEAK"
                    anchor = round(e50, 3)
                    anchor_label = "EMA50"

                result[label] = {
                    "available":    True,
                    "bias":         bias,
                    "price":        round(p, 3),
                    "ema20":        round(e20, 3),
                    "ema50":        round(e50, 3),
                    "ema200":       round(e200, 3) if e200 is not None else None,
                    "anchor":       anchor,
                    "anchor_label": anchor_label,
                    "rsi":          round(rsi, 1),
                    "adx":          round(adx, 1),
                    "above_anchor": p > anchor,
                }
            except Exception:
                result[label] = {"available": False}

        self._htf_cache      = result
        self._htf_cache_time = now
        print(f"[{self.NAME}] HTF bias cached: "
              f"W1={result.get('w1', {}).get('bias', 'N/A')} | "
              f"D1={result.get('d1', {}).get('bias', 'N/A')}")
        return result

    def _htf_text(self, htf: dict) -> str:
        lines = []
        for label in ("w1", "d1"):
            d = htf.get(label, {})
            if not d.get("available"):
                lines.append(f"{label.upper()}: unavailable")
                continue
            ema200_str = f" / EMA200 {d['ema200']}" if d.get("ema200") is not None else ""
            lines.append(
                f"{label.upper()}: {d['bias']} | "
                f"Price {d['price']} vs EMA20 {d['ema20']} / EMA50 {d['ema50']}{ema200_str} | "
                f"Anchor: {d['anchor_label']} {d['anchor']} ({'above' if d['above_anchor'] else 'below'}) | "
                f"RSI {d['rsi']} | ADX {d['adx']}"
            )
        return "\n".join(lines)

    def _get_vwap(self, session: str, h1_atr: float) -> dict:
        """Session VWAP — primary institutional intraday reference."""
        SESSION_START_BEIRUT = {
            "TOKYO": 3, "LONDON": 10, "OVERLAP_LONDON_NY": 13, "NEW_YORK": 20
        }
        start_hour = SESSION_START_BEIRUT.get(session, 10)
        try:
            beirut  = ZoneInfo(BEIRUT_TZ)
            utc_tz  = ZoneInfo("UTC")
            now_b   = datetime.now(beirut)
            start_b = now_b.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            start_u = start_b.astimezone(utc_tz)

            rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H1, 0, 24)
            if rates is None or len(rates) < 1:
                return {"available": False}

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df[df["time"] >= start_u]
            if len(df) < 1:
                return {"available": False}

            df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
            total_vol = df["tick_volume"].sum()
            if total_vol == 0:
                return {"available": False}

            vwap  = (df["typical"] * df["tick_volume"]).sum() / total_vol
            price = float(df["close"].iloc[-1])
            dist  = abs(price - vwap)
            return {
                "available":       True,
                "vwap":            round(float(vwap), 3),
                "price_vs_vwap":   "ABOVE" if price > vwap else "BELOW",
                "distance":        round(dist, 3),
                "distance_in_atr": round(dist / h1_atr, 2) if h1_atr > 0 else 0.0,
                "at_retest":       dist < h1_atr * 0.3,
                "session_candles": len(df),
            }
        except Exception:
            return {"available": False}

    # ── Session ───────────────────────────────────────────────────────
    def _get_session(self) -> str:
        h = datetime.now(ZoneInfo(BEIRUT_TZ)).hour
        # USDJPY: also trade Tokyo session
        if 3 <= h < 9:    return "TOKYO"
        elif h < 10:      return "CLOSED"
        elif h < 13:      return "LONDON"
        elif h < 20:      return "OVERLAP_LONDON_NY"
        elif h < 22:      return "NEW_YORK"
        else:             return "CLOSED"

    # ── News ──────────────────────────────────────────────────────────
    def _fetch_news(self) -> list:
        try:
            import requests
            r = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=10)
            return [e for e in r.json()
                    if e.get("country") in ["USD", "JPY"]
                    and e.get("impact") in ["High", "Medium"]
                    ] if r.status_code == 200 else []
        except Exception:
            return []

    def _news_blackout(self, events: list) -> tuple[bool, str]:
        now = datetime.utcnow()
        for e in events:
            if e.get("impact") != "High":
                continue
            try:
                et   = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                diff = et - now
                if timedelta(minutes=-15) <= diff <= timedelta(minutes=30):
                    return True, f"HIGH impact: {e.get('title','?')}"
            except Exception:
                pass
        return False, ""

    def _news_text(self, events: list) -> str:
        now   = datetime.utcnow()
        lines = []
        for e in events:
            try:
                et   = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                diff = et - now
                if timedelta(0) <= diff <= timedelta(hours=2):
                    lines.append(
                        f"  - {e.get('title','?')} | "
                        f"{e.get('country','?')} | "
                        f"{e.get('impact','?')} | "
                        f"In {int(diff.total_seconds()/60)} min")
            except Exception:
                pass
        return "\n".join(lines) if lines else "  None in next 2 hours"

    # ── DXY context ───────────────────────────────────────────────────
    def _dxy_text(self) -> str:
        if not self.dollar_broadcast:
            return "DXY unavailable — reduce confidence by 15 points"
        b      = self.dollar_broadcast
        usd    = b.get("usd_bias", "NEUTRAL")
        regime = b.get("risk_regime", "MIXED")
        # USDJPY is DIRECT with USD but INVERSE with risk
        usd_impl  = "BULLISH" if usd == "BULLISH_USD" else (
                    "BEARISH" if usd == "BEARISH_USD" else "NEUTRAL")
        risk_impl = "BEARISH (Yen safe haven)" if regime == "RISK_OFF" else (
                    "BULLISH (risk-on, Yen sells)" if regime == "RISK_ON" else "NEUTRAL")
        final = "BEARISH" if regime == "RISK_OFF" else usd_impl
        return (f"USD Bias          : {usd}\n"
                f"USD → USDJPY      : {usd_impl} (DIRECT correlation)\n"
                f"Risk Regime       : {regime}\n"
                f"Risk → USDJPY     : {risk_impl}\n"
                f"FINAL USDJPY BIAS : {final}\n"
                f"Note: RISK_OFF overrides USD strength for USDJPY")

    # ── Volatility Regime ─────────────────────────────────────────────
    def _get_volatility_regime(self) -> dict:
        """Current H4 ATR vs 20-day avg ATR — identifies compressed/extreme markets."""
        try:
            rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H4, 0, 200)
            if rates is None or len(rates) < 30:
                return {"available": False}
            df = pd.DataFrame(rates)
            hi, lo, cl = df["high"], df["low"], df["close"]
            tr = pd.concat([
                hi - lo,
                (hi - cl.shift()).abs(),
                (lo - cl.shift()).abs()
            ], axis=1).max(axis=1)
            atr       = tr.ewm(span=14, adjust=False).mean()
            current   = float(atr.iloc[-1])
            lookback  = atr.iloc[-121:-1] if len(atr) >= 122 else atr.iloc[:-1]
            avg_20d   = float(lookback.mean()) if len(lookback) > 0 else current
            if avg_20d == 0:
                return {"available": False}
            ratio = round(current / avg_20d, 2)
            if   ratio < 0.65:  regime = "COMPRESSED"
            elif ratio <= 1.50: regime = "NORMAL"
            elif ratio <= 2.50: regime = "ELEVATED"
            else:               regime = "EXTREME"
            return {
                "available":   True,
                "current_atr": round(current, 5),
                "avg_atr_20d": round(avg_20d, 5),
                "ratio":       ratio,
                "regime":      regime,
            }
        except Exception:
            return {"available": False}

    def _vol_regime_text(self, vr: dict) -> str:
        if not vr.get("available"):
            return "Volatility regime unavailable"
        return (
            f"Regime: {vr['regime']} | Current ATR: {vr['current_atr']} | "
            f"20d avg ATR: {vr['avg_atr_20d']} | Ratio: {vr['ratio']}x avg"
        )

    # ── Claude AI ─────────────────────────────────────────────────────
    def _ask_claude(self, h4, h1, m15, session, news_text,
                    spread, round_info, boj, ichimoku, vwap, htf, levels, cot, vol_regime) -> dict:

        system_prompt = """You are USDJPY, a professional USD/JPY forex trader at APEX Capital AI.
You specialise in trend-following on USDJPY — the strongest-trending major forex pair.
Capital protection is always top priority.

=== USDJPY TRADING RULES ===

RULE 1 - DUAL DRIVER SYSTEM (MANDATORY CHECK):
  USDJPY is driven by TWO forces that can conflict:
  
  FORCE 1 — USD Strength (DXY):
  - BULLISH_USD → USDJPY bullish bias (direct correlation)
  - BEARISH_USD → USDJPY bearish bias

  FORCE 2 — Risk Sentiment (Yen safe haven):
  - RISK_OFF → Yen strengthens → USDJPY FALLS (overrides USD strength)
  - RISK_ON  → Yen weakens → USDJPY RISES

  RESOLUTION:
  - RISK_OFF + BULLISH_USD = CONFLICTING → WAIT or SHORT only (risk wins)
  - RISK_OFF + BEARISH_USD = ALIGNED SHORT → strong SHORT signal
  - RISK_ON  + BULLISH_USD = ALIGNED LONG → strong LONG signal
  - RISK_ON  + BEARISH_USD = CONFLICTING → WAIT or LONG only (USD wins)

RULE 2 - BOJ INTERVENTION WARNING:
  - Price above 148.00: CAUTION zone begins — reduce confidence 10 points on LONG
  - Price above 150.00: EXTREME CAUTION — reduce lot size 50%, no new LONG entries
  - Price above 152.00: NO LONG trades — intervention imminent
  - BoJ has intervened multiple times at 150-152 — the danger zone starts earlier than it looks
  - If in danger zone: only SHORT setups allowed

RULE 3 - ICHIMOKU CLOUD (institutional bias):
  - Price CLEARLY ABOVE cloud (at least 0.25x H4 ATR above cloud top): bullish structural bias → LONG allowed
  - Price CLEARLY BELOW cloud (at least 0.25x H4 ATR below cloud bottom): bearish structural bias → SHORT allowed
  - Price just barely broke cloud (within 0.25x ATR): NOT enough — wait for confirmation candle
  - Price IN cloud: NO TRADE — wait for breakout
  - Bullish cloud (Span A > Span B): adds 10 confidence to LONG
  - Bearish cloud (Span A < Span B): adds 10 confidence to SHORT
  - The 0.25x ATR clearance rule eliminates false cloud breaks that immediately reverse — BoJ regime creates many of these

RULE 4 - TREND FILTER (ADX critical for USDJPY):
  - ADX > 30: strong trend → trend follow only — RAISED from 25, only real trends qualify
  - ADX 25-30: moderate trend → reduce confidence 15 points — borderline, BoJ can snap this
  - ADX 20-25: weak trend → reduce confidence 20 points
  - ADX < 20: ranging → WAIT (USDJPY ranging setups are traps)
  - ADX must be RISING (adx > adx_prev) — declining ADX = trend losing acceleration = reduce confidence 15
  - USDJPY trends much stronger than EURUSD — but only trade when the trend is CONFIRMED strong

RULE 5 - EMA STACK + RSI MOMENTUM:
  - LONG: price > EMA20 > EMA50 > EMA200, RSI > 60 (strong momentum required)
  - SHORT: price < EMA20 < EMA50 < EMA200, RSI < 40 (strong momentum required)
  - Price between EMA20 and EMA50: NO TRADE
  - RSI between 40-60 = momentum not confirmed = reduce confidence 15
  - Price above EMA200 H4: ONLY LONG allowed | Price below EMA200 H4: ONLY SHORT allowed

RULE 6 - ROUND NUMBER RULES:
  - 140, 145, 148, 150, 152, 155 = major institutional levels
  - Use as TP targets when price approaching
  - 150.00 = most critical level (BoJ intervention zone)
  - Avoid entries within 30 pips of major round numbers

RULE 7 - SESSION RULES:
  - TOKYO (03:00-09:00 Beirut): Good setups, lower volatility
  - LONDON: Good
  - OVERLAP_LONDON_NY: Best — highest volume
  - NEW_YORK: Good
  - CLOSED: NO TRADE

RULE 8 - CONFIDENCE & LOT:
  - Below 70%: WAIT
  - 70-74%: 0.01 lot
  - 75-84%: 0.02 lot
  - 85%+: 0.03 lot
  - If above 150.00: halve all lot sizes

RULE 9 - SL/TP:
  - SL = 1.5x H4 ATR (wider than EURUSD — USDJPY spikes more)
  - TP = minimum 2x SL distance
  - Use round numbers as TP targets
  - Minimum R:R = 1:2

RULE 10 - ALWAYS WAIT IF:
  - HIGH impact USD or JPY news within 30 min
  - Session CLOSED
  - Price in Ichimoku cloud or within 0.25x ATR of cloud edge
  - ADX < 20
  - Dual drivers conflicting without clear resolution
  - Price above 150.00 and signal is LONG
  - ADX > 30 not met (trend not strong enough)

RULE 11 - KEY PRICE LEVELS (Institutional Order Clusters):
  PDH/PDL = Previous Day High/Low — most watched levels globally
  PWH/PWL = Previous Week High/Low — institutional weekly reference
  Monthly Open = fund manager benchmark level

  Entry AT a key level (nearby = within 0.5x H4 ATR):
  - Price at PDL/PWL acting as SUPPORT → long entry: +10 confidence
  - Price at PDH/PWH acting as RESISTANCE → short entry: +10 confidence
  - Price just broke above PDH/PWH → momentum long: +5 confidence
  - Price just broke below PDL/PWL → momentum short: +5 confidence

  Key level between entry and TP:
  - Use it as TP target — do NOT set TP beyond a major level

  No key levels nearby:
  - Price in open space → reduce confidence -10 (no institutional reference)

RULE 12 - MULTI-TIMEFRAME ALIGNMENT (W1 + D1 — Master Filter):
  W1 is the master trend. NEVER trade against the weekly trend.
  D1 is the daily structure. Avoid trading against the daily trend.

  Alignment rules:
  W1 BULLISH + D1 BULLISH + H4 BULLISH → maximum confidence, all aligned
  W1 BULLISH + D1 MIXED/WEAK + H4 BULLISH → reduce confidence -10 (daily lagging)
  W1 BULLISH + D1 BEARISH               → WAIT (daily conflict — only best setups)
  W1 BEARISH + any BUY signal           → HARD BLOCK (-30 confidence minimum)
  W1 BEARISH + D1 BEARISH + H4 BEARISH → maximum confidence SHORT
  W1 MIXED/WEAK + D1 aligned            → reduce confidence -10

RULE 12 - VWAP FILTER (Session Institutional Reference):
  - Price ABOVE session VWAP → session bullish → only BUY setups
  - Price BELOW session VWAP → session bearish → only SELL setups
  - Direction conflicts with VWAP → REJECT trade
  - Price within 0.3x H1 ATR of VWAP (at_retest=true) → +10 confidence (premium entry)
  - Price > 1.5x H1 ATR from VWAP → chasing the move → -15 confidence

RULE 13 - VOLATILITY REGIME (ATR vs 20-day average):
  - COMPRESSED (ratio < 0.65): market is coiling → reduce confidence -10 on trend entries
    Breakout trades allowed if Bollinger squeeze is active. Mean reversion preferred.
  - NORMAL (0.65–1.50): standard volatility → no adjustment
  - ELEVATED (1.50–2.50): above-average volatility → reduce confidence -15; price may overshoot
  - EXTREME (ratio > 2.50): volatility spike (news/event) → reduce confidence -25

RULE 14 - COT POSITIONING (CFTC Large Speculator Sentiment):
  COT data reflects institutional speculative positioning as of Tuesday each week.
  NOTE: JPY futures are INVERTED — long JPY futures = bullish JPY = BEARISH USD/JPY price.
  The signal here is already direction-adjusted for USD/JPY: BULLISH means bullish price (USD/JPY up).
  - EXTREME_BULLISH (net > +30% OI): USD/JPY longs are crowded → reduce confidence -10 on new longs
  - EXTREME_BEARISH (net < -30% OI): USD/JPY shorts are crowded → reduce confidence -10 on new shorts
  - BULLISH (+15% to +30% OI): confirms USD/JPY bullish bias → +5 confidence if trade direction matches
  - BEARISH (-15% to -30% OI): confirms USD/JPY bearish bias → +5 confidence if trade direction matches
  - NEUTRAL: no COT adjustment

Respond ONLY with valid JSON (no markdown, no backticks):
{
  "action": "BUY" or "SELL" or "WAIT",
  "confidence": 0-100,
  "stop_loss": <price or null>,
  "take_profit": <price or null>,
  "h4_trend": "BULLISH" or "BEARISH" or "RANGING" or "UNCLEAR",
  "ichimoku_bias": "ABOVE_CLOUD" or "BELOW_CLOUD" or "IN_CLOUD",
  "dual_driver_aligned": true or false,
  "boj_risk": "SAFE" or "CAUTION" or "DANGER",
  "adx_strength": "STRONG" or "MODERATE" or "WEAK" or "RANGING",
  "round_level_nearby": true or false,
  "vwap_aligned": true or false,
  "vwap_retest": true or false,
  "w1_bias": "BULLISH" or "BEARISH" or "BULLISH_WEAK" or "BEARISH_WEAK" or "UNKNOWN",
  "d1_bias": "BULLISH" or "BEARISH" or "BULLISH_WEAK" or "BEARISH_WEAK" or "UNKNOWN",
  "htf_aligned": true or false,
  "near_key_level": true or false,
  "key_level_confluence": true or false,
  "vol_regime": "COMPRESSED" or "NORMAL" or "ELEVATED" or "EXTREME" or "UNKNOWN",
  "cot_signal": "EXTREME_BULLISH" or "BULLISH" or "NEUTRAL" or "BEARISH" or "EXTREME_BEARISH" or "UNAVAILABLE",
  "reasoning": "two sentence explanation of setup and key confirmations"
}"""

        user_prompt = f"""Analyse USDJPY and make a trade decision.

=== MARKET CONTEXT ===
Price   : {m15['price']:.3f}
Session : {session}
Spread  : {spread:.3f} ({spread*100:.1f} pips)

=== KEY PRICE LEVELS (Institutional Clusters) ===
{self._levels_text(levels)}

=== HIGHER TIMEFRAME BIAS (Master Trend Filter) ===
{self._htf_text(htf)}

=== SESSION VWAP (Institutional Reference) ===
{"VWAP: " + str(vwap['vwap']) + " | Price is " + vwap['price_vs_vwap'] + " VWAP by " + str(vwap['distance_in_atr']) + "x H1 ATR" + (" | ✅ AT VWAP RETEST — premium entry" if vwap['at_retest'] else "") if vwap['available'] else "VWAP unavailable — reduce confidence 10 points"}

=== DUAL DRIVER ANALYSIS (USD + RISK) ===
{self._dxy_text()}

=== BOJ INTERVENTION WARNING ===
Status   : {boj['warning']}
Distance to 150.00: {boj['distance_to_150']:.3f} points
Extreme Risk (>152): {boj['extreme_risk']}

=== ICHIMOKU CLOUD (H4 — Institutional Bias) ===
Tenkan  : {ichimoku['tenkan']:.3f} | Kijun: {ichimoku['kijun']:.3f}
Span A  : {ichimoku['span_a']:.3f} | Span B: {ichimoku['span_b']:.3f}
Cloud   : {ichimoku['cloud_bot']:.3f} — {ichimoku['cloud_top']:.3f}
Position: {'ABOVE CLOUD — bullish' if ichimoku['above_cloud'] else 'BELOW CLOUD — bearish' if ichimoku['below_cloud'] else 'IN CLOUD — no trade'}
Cloud Type: {'BULLISH (Span A > Span B)' if ichimoku['bullish_cloud'] else 'BEARISH (Span B > Span A)'}

=== ROUND NUMBER CONTEXT ===
Nearest : {round_info['nearest_level']} ({round_info['distance_pips']} pips away)
Near Level: {round_info['price_near_round']}

=== H4 CHART (Primary Trend) ===
Price   : {h4['price']:.3f}
EMA 20  : {h4['ema_fast']:.3f} (prev {h4['ema_fast_prev']:.3f})
EMA 50  : {h4['ema_slow']:.3f} (prev {h4['ema_slow_prev']:.3f})
EMA 200 : {h4['ema_200']:.3f}  (prev {h4['ema_200_prev']:.3f})
vs EMA200: {'ABOVE — structural uptrend' if h4['price'] > h4['ema_200'] else 'BELOW — structural downtrend'}
RSI     : {h4['rsi']:.2f} (prev {h4['rsi_prev']:.2f})
ATR     : {h4['atr']:.3f}
ADX     : {h4['adx']:.2f} (prev {h4['adx_prev']:.2f}) | +DI: {h4['plus_di']:.2f} | -DI: {h4['minus_di']:.2f}
Trend   : {'STRONG TREND (ADX>30)' if h4['adx'] > 30 else 'MODERATE (ADX 25-30) — reduce confidence' if h4['adx'] > 25 else 'WEAK' if h4['adx'] > 20 else 'RANGING — AVOID'}

=== H1 CHART (Confirmation) ===
Price   : {h1['price']:.3f}
EMA 20  : {h1['ema_fast']:.3f} | EMA 50: {h1['ema_slow']:.3f} | EMA 200: {h1['ema_200']:.3f}
vs EMA200: {'ABOVE' if h1['price'] > h1['ema_200'] else 'BELOW'}
RSI     : {h1['rsi']:.2f} (prev {h1['rsi_prev']:.2f})
ATR     : {h1['atr']:.3f}
ADX     : {h1['adx']:.2f} | +DI: {h1['plus_di']:.2f} | -DI: {h1['minus_di']:.2f}

=== M15 CHART (Entry Timing) ===
Price   : {m15['price']:.3f}
EMA 20  : {m15['ema_fast']:.3f} | EMA 50: {m15['ema_slow']:.3f} | EMA 200: {m15['ema_200']:.3f}
RSI     : {m15['rsi']:.2f} (prev {m15['rsi_prev']:.2f})
ATR     : {m15['atr']:.3f}
ADX     : {m15['adx']:.2f}

=== LAST 3 M15 CANDLES ===
{json.dumps(m15['candles_tail'], indent=2)}

=== VOLATILITY REGIME ===
{self._vol_regime_text(vol_regime)}

=== COT DATA (CFTC Large Speculator Positioning — JPY futures, direction-adjusted for USD/JPY) ===
{cot_text(cot)}

=== STRATEGIST EXECUTION PLAN (Daily Top-Down Analysis) ===
{self._strategy_text()}

=== UPCOMING NEWS (USD + JPY, next 2h) ===
{news_text}"""

        response = self.client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        if not text:
            raise ValueError("Empty response from Claude")
        return json.loads(text)

    # ── Build Proposal ────────────────────────────────────────────────
    def build_proposal(self, decision: dict, m15: dict,
                       boj: dict) -> dict | None:
        action = decision.get("action", "WAIT")
        conf   = decision.get("confidence", 0)
        if action == "WAIT" or conf < 70:
            print(f"[{self.NAME}] No trade. {decision.get('reasoning','')[:100]}")
            return None
        sl = float(decision.get("stop_loss") or 0)
        tp = float(decision.get("take_profit") or 0)
        ep = float(m15["price"])
        if sl <= 0 or tp <= 0:
            print(f"[{self.NAME}] Invalid SL/TP — skipping")
            return None
        sl_pts = round(abs(ep - sl), 3)
        tp_pts = round(abs(tp - ep), 3)
        if sl_pts > 0 and (tp_pts / sl_pts) < 1.9:
            print(f"[{self.NAME}] R:R too low — skipping")
            return None
        # Halve lot if in BoJ danger zone
        lot = 0.01 if conf < 75 else (0.02 if conf < 85 else 0.03)
        if boj["in_danger_zone"]:
            lot = 0.01
            print(f"[{self.NAME}] BoJ danger zone — lot capped at 0.01")
        return {
            "agent":             self.NAME,
            "instrument":        self.SYMBOL,
            "direction":         "LONG" if action == "BUY" else "SHORT",
            "confidence":        conf,
            "lot_size_request":  lot,
            "sl_points":         sl_pts,
            "tp_points":         tp_pts,
            "stop_loss_price":   sl,
            "take_profit_price": tp,
            "entry_price":       ep,
            "h4_trend":          decision.get("h4_trend", "UNCLEAR"),
            "ichimoku_bias":     decision.get("ichimoku_bias", "UNKNOWN"),
            "dual_driver_aligned": decision.get("dual_driver_aligned", False),
            "boj_risk":          decision.get("boj_risk", "SAFE"),
            "adx_strength":      decision.get("adx_strength", "UNKNOWN"),
            "vwap_aligned":      decision.get("vwap_aligned", False),
            "vwap_retest":       decision.get("vwap_retest", False),
            "w1_bias":           decision.get("w1_bias", "UNKNOWN"),
            "d1_bias":           decision.get("d1_bias", "UNKNOWN"),
            "htf_aligned":       decision.get("htf_aligned", False),
            "near_key_level":    decision.get("near_key_level", False),
            "key_level_confluence": decision.get("key_level_confluence", False),
            "vol_regime":        decision.get("vol_regime", "UNKNOWN"),
            "cot_signal":        decision.get("cot_signal", "UNAVAILABLE"),
            "risk_regime":       self.dollar_broadcast.get("risk_regime") if self.dollar_broadcast else "UNKNOWN",
            "reasoning":         decision.get("reasoning", ""),
            "timestamp":         datetime.utcnow().isoformat(),
        }

    # ── Main ──────────────────────────────────────────────────────────
    def analyse(self) -> dict | None:
        print(f"\n[{self.NAME}] Starting USDJPY analysis...")

        if not self._connect_mt5():
            return None
        if not self._ensure_connected():
            mt5.shutdown()
            return None

        session = self._get_session()
        if session == "CLOSED":
            print(f"[{self.NAME}] Session CLOSED — no trade.")
            mt5.shutdown()
            return None

        spread_ok, spread_val = self._check_spread()
        if not spread_ok:
            print(f"[{self.NAME}] Spread too wide ({spread_val*100:.1f} pips) — skipping.")
            mt5.shutdown()
            return None

        h4  = self._get_indicators(mt5.TIMEFRAME_H4)
        h1  = self._get_indicators(mt5.TIMEFRAME_H1)
        m15 = self._get_indicators(mt5.TIMEFRAME_M15)

        if any(x is None for x in [h4, h1, m15]):
            print(f"[{self.NAME}] Failed to fetch price data.")
            mt5.shutdown()
            return None

        # Ichimoku (needs more candles — use H4 df)
        rates_h4 = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H4, 0, 150)
        df_h4    = pd.DataFrame(rates_h4) if rates_h4 is not None else None
        try:
            ichimoku = self._calc_ichimoku(df_h4) if df_h4 is not None else {
                "tenkan": 0, "kijun": 0, "span_a": 0, "span_b": 0,
                "cloud_top": 0, "cloud_bot": 0, "above_cloud": False,
                "below_cloud": False, "in_cloud": True, "bullish_cloud": False
            }
        except Exception as e:
            print(f"[USDJPY] Ichimoku error: {e}")
            ichimoku = {
                "tenkan": 0, "kijun": 0, "span_a": 0, "span_b": 0,
                "cloud_top": 0, "cloud_bot": 0, "above_cloud": False,
                "below_cloud": False, "in_cloud": True, "bullish_cloud": False
            }

        # Intraday %
        rates_h1 = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
        if rates_h1 is not None and len(rates_h1) > 0:
            df_h1 = pd.DataFrame(rates_h1)
            df_h1["time"] = pd.to_datetime(df_h1["time"], unit="s")
            today         = pd.Timestamp.now().normalize()
            mask          = df_h1["time"].apply(lambda t: t.normalize()) >= today
            bars          = df_h1[mask]
            self.intraday_pct = round(
                ((bars.iloc[-1]["close"] - bars.iloc[0]["open"])
                 / bars.iloc[0]["open"] * 100), 4) if len(bars) > 1 else 0.0

        boj        = self._boj_warning(m15["price"])
        round_info = self._nearest_round_level(m15["price"])
        htf        = self._get_htf_bias()
        vwap       = self._get_vwap(session, h1["atr"])
        levels     = self._get_key_levels(m15["price"], h4["atr"])
        vol_regime = self._get_volatility_regime()
        news       = self._fetch_news()
        mt5.shutdown()

        cot = get_cot_data("JPY")
        print(f"[{self.NAME}] COT: {cot.get('signal', 'unavailable')} "
              f"(net {cot.get('net_pct_oi', 'N/A')}% OI, "
              f"chg {cot.get('weekly_change', 'N/A')})" if cot.get("available") else
              f"[{self.NAME}] COT: unavailable")
        print(f"[{self.NAME}] Vol regime: {vol_regime.get('regime', 'unavailable')} "
              f"(ratio {vol_regime.get('ratio', 'N/A')}x)" if vol_regime.get("available") else
              f"[{self.NAME}] Vol regime: unavailable")

        if boj["extreme_risk"]:
            print(f"[{self.NAME}] ⚠️  Extreme BoJ intervention risk above 152.00")

        blocked, reason = self._news_blackout(news)
        if blocked:
            print(f"[{self.NAME}] News blackout: {reason}")
            return None

        news_text = self._news_text(news)

        try:
            decision = self._ask_claude(h4, h1, m15, session, news_text,
                                        spread_val, round_info, boj, ichimoku, vwap, htf, levels, cot, vol_regime)
        except Exception as e:
            print(f"[{self.NAME}] Claude API error: {e}")
            return None

        proposal = self.build_proposal(decision, m15, boj)
        if proposal:
            print(f"[{self.NAME}] Proposal: {proposal['direction']} "
                  f"@ {proposal['confidence']}% | "
                  f"H4: {proposal['h4_trend']} | "
                  f"Ichimoku: {proposal['ichimoku_bias']} | "
                  f"BoJ: {proposal['boj_risk']} | "
                  f"Dual aligned: {proposal['dual_driver_aligned']}")
        return proposal

    def on_atlas_decision(self, decision: dict):
        status = decision.get("status")
        lot    = decision.get("lot_size_approved", 0)
        reason = decision.get("reason", "")
        print(f"[{self.NAME}] MANAGER: {status} "
              f"{'— Lot: ' + str(lot) if status == 'APPROVED' else ''} | {reason}")
