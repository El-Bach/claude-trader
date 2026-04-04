"""
GOLD — Gold Specialist (XAUUSD)
APEX Capital AI

Rebuilt using battle-tested TradeAI_Gold_v2.py engine.
Key upgrades: Wilder RSI, ewm ATR, ADX, Fibonacci, Volume ratio,
Bollinger squeeze, 4-pillar system, account snapshot, spread check,
session filter, news filter, MT5 reconnect wrapper.
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

load_dotenv()

BEIRUT_TZ      = "Asia/Beirut"
EMA_FAST       = 20
EMA_SLOW       = 50
EMA_200        = 200
RSI_PERIOD     = 14
ATR_PERIOD     = 14
ADX_PERIOD     = 14
BB_PERIOD      = 20
BB_STD         = 2.0
CANDLES_BACK   = 250
FIB_LOOKBACK   = 50
MAX_SPREAD_PCT = 0.20
MAGIC          = 20250401


# ── SMC Detection ─────────────────────────────────────────────────────────────

def _smc_detect_fvg(df, current_price, lookback=60):
    """Detect Fair Value Gaps — unfilled price imbalances."""
    if len(df) < 3:
        return {'bull': None, 'bear': None, 'in_bull': False, 'in_bear': False}
    tail = df.tail(lookback).reset_index(drop=True)
    n    = len(tail)
    bull_fvg = bear_fvg = None
    for i in range(1, n - 1):
        c0h = float(tail['high'].iloc[i - 1])
        c0l = float(tail['low'].iloc[i - 1])
        c2h = float(tail['high'].iloc[i + 1])
        c2l = float(tail['low'].iloc[i + 1])
        # Bullish FVG
        if c2l > c0h:
            fl, fh = c0h, c2l
            filled = any(
                tail['low'].iloc[k] <= fh and tail['high'].iloc[k] >= fl
                for k in range(i + 2, n)
            )
            if not filled and fh >= current_price * 0.98:
                if bull_fvg is None or fh > bull_fvg['high']:
                    bull_fvg = {'high': round(fh, 5), 'low': round(fl, 5)}
        # Bearish FVG
        if c2h < c0l:
            fh, fl = c0l, c2h
            filled = any(
                tail['low'].iloc[k] <= fh and tail['high'].iloc[k] >= fl
                for k in range(i + 2, n)
            )
            if not filled and fl <= current_price * 1.02:
                if bear_fvg is None or fl < bear_fvg['low']:
                    bear_fvg = {'high': round(fh, 5), 'low': round(fl, 5)}
    in_bull = bull_fvg is not None and bull_fvg['low'] <= current_price <= bull_fvg['high']
    in_bear = bear_fvg is not None and bear_fvg['low'] <= current_price <= bear_fvg['high']
    return {'bull': bull_fvg, 'bear': bear_fvg, 'in_bull': in_bull, 'in_bear': in_bear}


def _smc_detect_ob(df, current_price, atr_val, lookback=60, min_impulse=2):
    """Detect Order Blocks — last candle before a strong impulse move."""
    if len(df) < min_impulse + 3:
        return {'bull': None, 'bear': None, 'at_bull': False, 'at_bear': False}
    tail = df.tail(lookback).reset_index(drop=True)
    n    = len(tail)
    prox = atr_val * 0.5
    bull_ob = bear_ob = None
    for i in range(n - min_impulse - 1):
        close_i = float(tail['close'].iloc[i])
        open_i  = float(tail['open'].iloc[i])
        # Bullish OB: bearish candle before impulse up
        if close_i < open_i:
            run = sum(1 for j in range(i+1, min(i+1+min_impulse+1, n))
                      if tail['close'].iloc[j] > tail['open'].iloc[j])
            if run >= min_impulse:
                ob_h, ob_l = open_i, close_i
                if ob_l < ob_h < current_price:
                    mitigated = any(tail['close'].iloc[k] < ob_l for k in range(i+1, n))
                    if not mitigated:
                        if bull_ob is None or ob_h > bull_ob['high']:
                            bull_ob = {'high': round(ob_h, 5), 'low': round(ob_l, 5)}
        # Bearish OB: bullish candle before impulse down
        if close_i > open_i:
            run = sum(1 for j in range(i+1, min(i+1+min_impulse+1, n))
                      if tail['close'].iloc[j] < tail['open'].iloc[j])
            if run >= min_impulse:
                ob_h, ob_l = close_i, open_i
                if ob_l > current_price > ob_l - prox * 2:
                    mitigated = any(tail['close'].iloc[k] > ob_h for k in range(i+1, n))
                    if not mitigated:
                        if bear_ob is None or ob_l < bear_ob['low']:
                            bear_ob = {'high': round(ob_h, 5), 'low': round(ob_l, 5)}
    at_bull = (bull_ob is not None
               and current_price <= bull_ob['high'] + prox
               and current_price >= bull_ob['low']  - prox)
    at_bear = (bear_ob is not None
               and current_price >= bear_ob['low']  - prox
               and current_price <= bear_ob['high'] + prox)
    return {'bull': bull_ob, 'bear': bear_ob, 'at_bull': at_bull, 'at_bear': at_bear}


def _smc_detect_liquidity(df, current_price, lookback=60, tol_pct=0.0015):
    """Detect equal highs (bear stops above) and equal lows (bull stops below)."""
    if len(df) < 10:
        return {'equal_highs': [], 'equal_lows': [], 'nearest_high': None, 'nearest_low': None}
    recent = df.tail(lookback)
    highs  = recent['high'].values.astype(float)
    lows   = recent['low'].values.astype(float)

    def _clusters(vals, above):
        used, results = [False] * len(vals), []
        for i in range(len(vals)):
            if used[i]: continue
            mask = [abs(vals[i] - vals[j]) / (vals[i] + 1e-10) <= tol_pct
                    for j in range(len(vals))]
            if sum(mask) >= 2:
                avg = float(np.mean([vals[j] for j in range(len(vals)) if mask[j]]))
                if (above and avg > current_price) or (not above and avg < current_price):
                    results.append(round(avg, 5))
                for j in range(len(vals)):
                    if mask[j]: used[j] = True
        return results

    eq_highs = sorted(_clusters(highs, True))
    eq_lows  = sorted(_clusters(lows,  False), reverse=True)
    return {
        'equal_highs':   eq_highs,
        'equal_lows':    eq_lows,
        'nearest_high':  eq_highs[0] if eq_highs else None,
        'nearest_low':   eq_lows[0]  if eq_lows  else None,
    }


class GoldAgent:
    NAME   = "GOLD"
    SYMBOL = "XAUUSD"

    def __init__(self):
        self.client           = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.dollar_broadcast = None
        self.strategy_plan    = None

    # ── Dollar Broadcast ──────────────────────────────────────────────
    def receive_dollar_broadcast(self, broadcast: dict):
        self.dollar_broadcast = broadcast
        usd  = broadcast.get("usd_bias", "NEUTRAL")
        impl = broadcast.get("gold_implication", "NEUTRAL")
        print(f"[{self.NAME}] Dollar signal: USD={usd}, Gold implication={impl}")

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
        return True

    def _ensure_connected(self) -> bool:
        if mt5.account_info() is not None:
            return True
        for attempt in range(1, 4):
            mt5.shutdown()
            time.sleep(5)
            if self._connect_mt5() and mt5.account_info() is not None:
                print(f"[{self.NAME}] MT5 reconnected (attempt {attempt})")
                return True
        return False

    # ── Account Snapshot ──────────────────────────────────────────────
    def _get_account(self) -> dict:
        info = mt5.account_info()
        if info is None:
            return {}
        positions = mt5.positions_get() or []
        trades = []
        total_pnl = 0.0
        for p in positions:
            if p.magic != MAGIC:
                continue
            tick = mt5.symbol_info_tick(p.symbol)
            cur  = (tick.bid if p.type == 0 else tick.ask) if tick else p.price_open
            total_pnl += p.profit
            trades.append({
                "ticket": p.ticket, "symbol": p.symbol,
                "direction": "BUY" if p.type == 0 else "SELL",
                "lot": p.volume, "open_price": round(p.price_open, 5),
                "current_price": round(cur, 5),
                "sl": round(p.sl, 5), "tp": round(p.tp, 5),
                "floating_pnl": round(p.profit, 2),
            })
        return {
            "balance": round(info.balance, 2),
            "equity": round(info.equity, 2),
            "free_margin": round(info.margin_free, 2),
            "margin_level_pct": round(info.margin_level, 1) if info.margin_level else 999.9,
            "total_floating_pnl": round(total_pnl, 2),
            "open_trades_count": len(trades),
            "open_trades": trades,
        }

    # ── Spread Check ──────────────────────────────────────────────────
    def _check_spread(self, atr_m15: float) -> tuple[bool, float]:
        tick = mt5.symbol_info_tick(self.SYMBOL)
        if tick is None:
            return False, 0.0
        spread = tick.ask - tick.bid
        return spread <= MAX_SPREAD_PCT * atr_m15, round(spread, 5)

    # ── Fibonacci Levels ──────────────────────────────────────────────
    def _get_fib(self) -> dict:
        rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H4, 0, FIB_LOOKBACK)
        if rates is None or len(rates) < 10:
            return {"available": False}
        df   = pd.DataFrame(rates)
        high = float(df["high"].max())
        low  = float(df["low"].min())
        diff = high - low
        if diff <= 0:
            return {"available": False}
        cur   = float(df["close"].iloc[-1])
        trend = "BULLISH" if cur >= (low + diff * 0.5) else "BEARISH"
        anchor = high if trend == "BULLISH" else low
        sign   = -1 if trend == "BULLISH" else 1
        return {
            "available": True, "trend_direction": trend,
            "swing_high": round(high, 5), "swing_low": round(low, 5),
            "fib_236": round(anchor + sign * 0.236 * diff, 5),
            "fib_382": round(anchor + sign * 0.382 * diff, 5),
            "fib_500": round(anchor + sign * 0.500 * diff, 5),
            "fib_618": round(anchor + sign * 0.618 * diff, 5),
            "fib_786": round(anchor + sign * 0.786 * diff, 5),
        }

    # ── Indicators (battle-tested from TradeAI_Gold_v2) ───────────────
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

        # ATR (ewm)
        tr  = pd.concat([high - low, (high - close.shift()).abs(),
                         (low - close.shift()).abs()], axis=1).max(axis=1)
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

        # Bollinger
        bbm    = close.rolling(BB_PERIOD).mean()
        bbs    = close.rolling(BB_PERIOD).std()
        bbu    = bbm + BB_STD * bbs
        bbl    = bbm - BB_STD * bbs
        bbw    = bbu - bbl
        bbwa   = bbw.rolling(BB_PERIOD).mean()
        sq     = bool(bbw.iloc[-1] < bbwa.iloc[-1])

        # Volume ratio
        tv   = df["tick_volume"]
        atv  = tv.rolling(BB_PERIOD).mean()
        vr   = tv / atv.replace(0, 1)

        # Last 3 candles
        tail = df.tail(3)[["open", "high", "low", "close"]].copy()
        tail["body"]       = (tail["close"] - tail["open"]).abs()
        tail["upper_wick"] = tail["high"] - tail[["open", "close"]].max(axis=1)
        tail["lower_wick"] = tail[["open", "close"]].min(axis=1) - tail["low"]

        return {
            "price": float(close.iloc[-1]), "price_prev": float(close.iloc[-2]),
            "ema_fast": float(ef.iloc[-1]), "ema_fast_prev": float(ef.iloc[-2]),
            "ema_slow": float(es.iloc[-1]), "ema_slow_prev": float(es.iloc[-2]),
            "ema_200":  float(e2.iloc[-1]), "ema_200_prev":  float(e2.iloc[-2]),
            "rsi": float(rsi.iloc[-1]), "rsi_prev": float(rsi.iloc[-2]),
            "atr": float(atr.iloc[-1]),
            "adx": float(adx.iloc[-1]), "adx_prev": float(adx.iloc[-2]),
            "plus_di": float(pdi.iloc[-1]), "minus_di": float(mdi.iloc[-1]),
            "bb_upper": float(bbu.iloc[-1]), "bb_mid": float(bbm.iloc[-1]),
            "bb_lower": float(bbl.iloc[-1]), "bb_width": float(bbw.iloc[-1]),
            "squeeze_active": sq,
            "vol_ratio": float(vr.iloc[-1]), "vol_ratio_prev": float(vr.iloc[-2]),
            "candles_tail": tail[["open", "high", "low", "close",
                                  "body", "upper_wick", "lower_wick"]
                                 ].round(4).reset_index(drop=True).to_dict(),
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

            prev        = df_d1.iloc[-2]
            pdh         = round(float(prev["high"]),  2)
            pdl         = round(float(prev["low"]),   2)
            pdc         = round(float(prev["close"]), 2)

            now_month   = pd.Timestamp.now().replace(day=1).normalize()
            month_bars  = df_d1[df_d1["time"].dt.normalize() >= now_month]
            monthly_open = round(float(month_bars.iloc[0]["open"]), 2) if len(month_bars) > 0 else None

            pwh = pwl = None
            if w1_rates is not None and len(w1_rates) >= 2:
                prev_w = pd.DataFrame(w1_rates).iloc[-2]
                pwh = round(float(prev_w["high"]), 2)
                pwl = round(float(prev_w["low"]),  2)

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
                        "distance": round(dist, 2),
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
                    anchor = round(e200, 2)
                    anchor_label = "EMA200"
                else:
                    # W1: EMA 50 is the long-term anchor
                    e200 = None
                    bull_stack = p > e20 > e50
                    bear_stack = p < e20 < e50
                    if bull_stack:   bias = "BULLISH"
                    elif bear_stack: bias = "BEARISH"
                    elif p > e50:    bias = "BULLISH_WEAK"
                    else:            bias = "BEARISH_WEAK"
                    anchor = round(e50, 2)
                    anchor_label = "EMA50"

                result[label] = {
                    "available":    True,
                    "bias":         bias,
                    "price":        round(p, 2),
                    "ema20":        round(e20, 2),
                    "ema50":        round(e50, 2),
                    "ema200":       round(e200, 2) if e200 is not None else None,
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
            "LONDON": 8, "OVERLAP_LONDON_NY": 13, "NEW_YORK": 20
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
                "vwap":            round(float(vwap), 2),
                "price_vs_vwap":   "ABOVE" if price > vwap else "BELOW",
                "distance":        round(dist, 2),
                "distance_in_atr": round(dist / h1_atr, 2) if h1_atr > 0 else 0.0,
                "at_retest":       dist < h1_atr * 0.3,
                "session_candles": len(df),
            }
        except Exception:
            return {"available": False}

    # ── Session ───────────────────────────────────────────────────────
    def _get_session(self) -> str:
        h = datetime.now(ZoneInfo(BEIRUT_TZ)).hour
        if h < 8 or h >= 23:    return "CLOSED"
        elif h < 13:             return "LONDON"
        elif h < 20:             return "OVERLAP_LONDON_NY"
        else:                    return "NEW_YORK"

    # ── News ──────────────────────────────────────────────────────────
    def _fetch_news(self) -> list:
        try:
            import requests
            r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
            return [e for e in r.json()
                    if e.get("country") in ["USD", "XAU"]
                    and e.get("impact") in ["High", "Medium"]] if r.status_code == 200 else []
        except Exception:
            return []

    def _news_blackout(self, events: list) -> tuple[bool, str]:
        now = datetime.utcnow()
        for e in events:
            if e.get("impact") != "High":
                continue
            try:
                et = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                diff = et - now
                if timedelta(minutes=-15) <= diff <= timedelta(minutes=30):
                    return True, f"HIGH impact: {e.get('title','?')}"
            except Exception:
                pass
        return False, ""

    def _news_text(self, events: list) -> str:
        now = datetime.utcnow()
        lines = []
        for e in events:
            try:
                et   = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                diff = et - now
                if timedelta(0) <= diff <= timedelta(hours=2):
                    lines.append(f"  - {e.get('title','?')} | {e.get('impact','?')} "
                                 f"| In {int(diff.total_seconds()/60)} min")
            except Exception:
                pass
        return "\n".join(lines) if lines else "  None in next 2 hours"

    # ── Prompt helpers ────────────────────────────────────────────────
    def _dxy_text(self) -> str:
        if not self.dollar_broadcast:
            return "DXY unavailable — reduce confidence by 10 points"
        b = self.dollar_broadcast
        return (f"USD Bias        : {b.get('usd_bias','NEUTRAL')}\n"
                f"DXY Trend       : {b.get('dxy_trend','FLAT')}\n"
                f"Gold Implication: {b.get('gold_implication','NEUTRAL')}\n"
                f"Risk Regime     : {b.get('risk_regime','MIXED')}")

    def _fib_text(self, fib: dict) -> str:
        if not fib.get("available"):
            return "Fibonacci data unavailable"
        t = fib["trend_direction"]
        return (f"Trend: {t} | High: {fib['swing_high']} | Low: {fib['swing_low']}\n"
                f"23.6%: {fib['fib_236']} | 38.2%: {fib['fib_382']} "
                f"| 50%: {fib['fib_500']} | 61.8%: {fib['fib_618']} | 78.6%: {fib['fib_786']}")

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

    # ── SMC ───────────────────────────────────────────────────────────
    def _build_smc(self, df: pd.DataFrame, current_price: float, atr_val: float) -> dict:
        """Run all three SMC detectors on a dataframe slice."""
        fvg = _smc_detect_fvg(df, current_price)
        ob  = _smc_detect_ob(df, current_price, atr_val)
        liq = _smc_detect_liquidity(df, current_price)
        return {'fvg': fvg, 'ob': ob, 'liq': liq}

    def _smc_text(self, smc: dict) -> str:
        """Format SMC detection results as readable text for Claude."""
        lines = []
        fvg = smc.get('fvg', {})
        ob  = smc.get('ob',  {})
        liq = smc.get('liq', {})

        lines.append("H1 FAIR VALUE GAPS (open imbalances):")
        if fvg.get('bull'):
            tag = " <- PRICE INSIDE" if fvg.get('in_bull') else ""
            lines.append(f"  Bullish FVG : {fvg['bull']['low']} - {fvg['bull']['high']}{tag}")
        else:
            lines.append("  Bullish FVG : none detected")
        if fvg.get('bear'):
            tag = " <- PRICE INSIDE" if fvg.get('in_bear') else ""
            lines.append(f"  Bearish FVG : {fvg['bear']['low']} - {fvg['bear']['high']}{tag}")
        else:
            lines.append("  Bearish FVG : none detected")

        lines.append("H1 ORDER BLOCKS:")
        if ob.get('bull'):
            tag = " <- PRICE AT OB" if ob.get('at_bull') else ""
            lines.append(f"  Bullish OB  : {ob['bull']['low']} - {ob['bull']['high']}{tag}")
        else:
            lines.append("  Bullish OB  : none detected")
        if ob.get('bear'):
            tag = " <- PRICE AT OB" if ob.get('at_bear') else ""
            lines.append(f"  Bearish OB  : {ob['bear']['low']} - {ob['bear']['high']}{tag}")
        else:
            lines.append("  Bearish OB  : none detected")

        lines.append("H1 LIQUIDITY POOLS:")
        nh = liq.get('nearest_high')
        nl = liq.get('nearest_low')
        eq_h = liq.get('equal_highs', [])
        eq_l = liq.get('equal_lows',  [])
        lines.append(f"  Equal Highs : {', '.join(str(x) for x in eq_h[:3]) if eq_h else 'none'}"
                     + (f"  <- nearest bear target: {nh}" if nh else ""))
        lines.append(f"  Equal Lows  : {', '.join(str(x) for x in eq_l[:3]) if eq_l else 'none'}"
                     + (f"  <- nearest bull target: {nl}" if nl else ""))

        # SMC summary
        confluence = []
        if fvg.get('in_bull'):  confluence.append("IN bullish FVG (pullback entry zone)")
        if fvg.get('in_bear'):  confluence.append("IN bearish FVG (pullback entry zone)")
        if ob.get('at_bull'):   confluence.append("AT bullish OB (institutional support)")
        if ob.get('at_bear'):   confluence.append("AT bearish OB (institutional resistance)")
        if confluence:
            lines.append(f"SMC CONFLUENCE: {' | '.join(confluence)}")
        else:
            lines.append("SMC CONFLUENCE: none active — price between zones")

        return "\n".join(lines)

    # ── Claude AI ─────────────────────────────────────────────────────
    def _ask_claude(self, h4, h1, m15, session, news_text, account, fib, spread, vwap, htf, levels, cot, vol_regime, smc=None) -> dict:
        daily_limit       = float(os.getenv("ACCOUNT_BALANCE", 1000)) * 0.03
        floating_loss     = abs(min(0, account.get("total_floating_pnl", 0)))
        floating_loss_pct = (floating_loss / daily_limit * 100) if daily_limit > 0 else 0

        trades_text = "\n".join([
            f"  - #{t['ticket']} {t['direction']} | Lot: {t['lot']} "
            f"| Open: {t['open_price']} | P&L: ${t['floating_pnl']}"
            for t in account.get("open_trades", [])
        ]) or "  None"

        system_prompt = """You are GOLD, a professional Gold (XAUUSD) trader at APEX Capital AI.
Analyse the data using the 4-pillar framework and make a precise decision.
Capital protection is always top priority.

=== TRADING RULES ===

RULE 1 - H4 IS THE BOSS:
  Price above H4 EMA200 → only BUY | Price below → only SELL
  Price between EMA20 and EMA50 → NO TRADE

RULE 2 - 4-PILLAR SYSTEM:
  PILLAR 1 — STRUCTURE (MANDATORY):
    H4 EMA stack aligned (price > EMA20 > EMA50 > EMA200 for BUY, reverse for SELL)
    H4 ADX > 25 required. ADX < 20 = RANGING = hard block.
    ADX must be RISING (adx > adx_prev) — declining ADX = weakening trend = reduce confidence 15

  PILLAR 2 — MOMENTUM (RSI only — divergences are primary signal):
    BUY: RSI > 58 (strong bullish momentum required — not just above 50)
    SELL: RSI < 42 (strong bearish momentum required — not just below 50)
    RSI divergence vs price = highest-quality reversal signal on Gold
    RSI between 42-58 = momentum not confirmed = reduce confidence 15

  SMC CONFLUENCE RULE:
    - Price inside H1 Bullish FVG OR at H1 Bullish OB -> valid BUY entry zone even if RSI is 50-58
    - Price inside H1 Bearish FVG OR at H1 Bearish OB -> valid SELL entry zone even if RSI is 42-50
    - FVG/OB confluence = structural entry -- RSI threshold relaxed by 8 points
    - Nearest Equal Highs above = primary TP target for BUY (liquidity sweep)
    - Nearest Equal Lows below = primary TP target for SELL (liquidity sweep)
    - SL for FVG entry: just below FVG low (BUY) or above FVG high (SELL)
    - SL for OB entry: just below OB low (BUY) or above OB high (SELL)

  PILLAR 3 — MACRO (DXY):
    Gold implication aligned with direction = PASSES
    Conflicting macro = FAILS (-20 confidence)

  PILLAR 4 — TIMING:
    Price within 0.3x ATR of Fib 38.2%/50%/61.8% = PASSES
    OR M15 volume ratio > 1.5x = PASSES
    Volume ratio < 0.8x = trap risk (-15 confidence)

  MINIMUM: Pillar 1 MUST pass + 2 of Pillars 2/3/4

RULE 3 - CONFIDENCE & LOT:
  < 70%: WAIT | 70-74%: 0.01 lot | 75-84%: 0.02 lot | 85%+: 0.03 lot

RULE 4 - SL/TP:
  SL = 1.0-1.5x H4 ATR | TP = minimum 2x SL | Fib 61.8% = best TP target

RULE 5 - ALWAYS WAIT IF:
  Structure fails | Session CLOSED | HIGH news within 30 min | < 3 pillars

RULE 6 - KEY PRICE LEVELS (Institutional Order Clusters):
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

RULE 7 - MULTI-TIMEFRAME ALIGNMENT (W1 + D1 — Master Filter):
  W1 is the master trend. NEVER trade against the weekly trend.
  D1 is the daily structure. Avoid trading against the daily trend.

  Alignment rules:
  W1 BULLISH + D1 BULLISH + H4 BULLISH → maximum confidence, all aligned
  W1 BULLISH + D1 MIXED/WEAK + H4 BULLISH → reduce confidence -10 (daily lagging)
  W1 BULLISH + D1 BEARISH               → WAIT (daily conflict — only best setups)
  W1 BEARISH + any BUY signal           → HARD BLOCK (-30 confidence minimum)
  W1 BEARISH + D1 BEARISH + H4 BEARISH → maximum confidence SHORT
  W1 MIXED/WEAK + D1 aligned            → reduce confidence -10

RULE 7 - VWAP FILTER (Session Institutional Reference):
  - Price ABOVE session VWAP → session bullish → only BUY setups
  - Price BELOW session VWAP → session bearish → only SELL setups
  - Direction conflicts with VWAP → REJECT trade
  - Price within 0.3x H1 ATR of VWAP (at_retest=true) → +10 confidence (premium entry)
  - Price > 1.5x H1 ATR from VWAP → chasing the move → -15 confidence

RULE 8 - VOLATILITY REGIME (ATR vs 20-day average):
  - COMPRESSED (ratio < 0.65): market is coiling → reduce confidence -10 on trend entries
    Breakout trades allowed if Bollinger squeeze is active. Mean reversion preferred.
  - NORMAL (0.65–1.50): standard volatility → no adjustment
  - ELEVATED (1.50–2.50): above-average volatility → reduce confidence -15; price may overshoot levels
  - EXTREME (ratio > 2.50): volatility spike (news/event) → reduce confidence -25; trade only with overwhelming confluence

RULE 9 - COT POSITIONING (CFTC Large Speculator Sentiment):
  COT data reflects institutional speculative positioning as of Tuesday each week.
  Use as a macro confirmation/warning filter — not a primary entry signal.
  - EXTREME_BULLISH (net > +30% OI): long side is crowded → reduce confidence -10 on new longs
  - EXTREME_BEARISH (net < -30% OI): short side is crowded → reduce confidence -10 on new shorts
  - BULLISH (+15% to +30% OI): confirms bullish bias → +5 confidence if trade direction matches
  - BEARISH (-15% to -30% OI): confirms bearish bias → +5 confidence if trade direction matches
  - NEUTRAL: no COT adjustment
  - Weekly change matters: large position increases = conviction; reversals = potential shift

Respond ONLY with valid JSON (no markdown, no backticks):
{
  "action": "BUY" or "SELL" or "WAIT",
  "confidence": 0-100,
  "stop_loss": <price or null>,
  "take_profit": <price or null>,
  "h4_trend": "BULLISH" or "BEARISH" or "UNCLEAR",
  "pillar_structure": true or false,
  "pillar_momentum": true or false,
  "pillar_macro": true or false,
  "pillar_timing": true or false,
  "pillars_confirmed": 0-4,
  "adx_trend_strength": "STRONG" or "WEAK" or "RANGING",
  "dxy_alignment": true or false,
  "fib_confluence": true or false,
  "volume_conviction": "HIGH" or "NORMAL" or "LOW",
  "vwap_aligned": true or false,
  "vwap_retest": true or false,
  "w1_bias": "BULLISH" or "BEARISH" or "BULLISH_WEAK" or "BEARISH_WEAK" or "UNKNOWN",
  "d1_bias": "BULLISH" or "BEARISH" or "BULLISH_WEAK" or "BEARISH_WEAK" or "UNKNOWN",
  "htf_aligned": true or false,
  "near_key_level": true or false,
  "key_level_confluence": true or false,
  "vol_regime": "COMPRESSED" or "NORMAL" or "ELEVATED" or "EXTREME" or "UNKNOWN",
  "cot_signal": "EXTREME_BULLISH" or "BULLISH" or "NEUTRAL" or "BEARISH" or "EXTREME_BEARISH" or "UNAVAILABLE",
  "reasoning": "two sentence explanation"
}"""

        user_prompt = f"""Analyse XAUUSD and make a trade decision.

=== ACCOUNT ===
Balance: ${account.get('balance',0):.2f} | Free Margin: ${account.get('free_margin',0):.2f}
Margin Level: {account.get('margin_level_pct',0):.1f}% | Floating P&L: ${account.get('total_floating_pnl',0):.2f}
Floating vs Daily Limit: {floating_loss_pct:.1f}%
Open Trades:
{trades_text}

=== MARKET CONTEXT ===
Price: {m15['price']:.5f} | Session: {session} | Spread: {spread:.5f}

=== KEY PRICE LEVELS (Institutional Clusters) ===
{self._levels_text(levels)}

=== HIGHER TIMEFRAME BIAS (Master Trend Filter) ===
{self._htf_text(htf)}

=== SESSION VWAP (Institutional Reference) ===
{"VWAP: " + str(vwap['vwap']) + " | Price is " + vwap['price_vs_vwap'] + " VWAP by " + str(vwap['distance_in_atr']) + "x H1 ATR" + (" | ✅ AT VWAP RETEST — premium entry" if vwap['at_retest'] else "") if vwap['available'] else "VWAP unavailable — reduce confidence 10 points"}

=== DXY (USD STRENGTH) ===
{self._dxy_text()}

=== FIBONACCI (H4 50-candle swing) ===
{self._fib_text(fib)}

=== PRICE ACTION STRUCTURE (Smart Money Concepts) ===
{self._smc_text(smc or {})}

=== H4 (Primary Trend) ===
Price: {h4['price']:.5f} | EMA20: {h4['ema_fast']:.5f} | EMA50: {h4['ema_slow']:.5f} | EMA200: {h4['ema_200']:.5f}
vs EMA200: {'ABOVE — bull' if h4['price'] > h4['ema_200'] else 'BELOW — bear'}
RSI: {h4['rsi']:.2f} (prev {h4['rsi_prev']:.2f})
ATR: {h4['atr']:.4f} | ADX: {h4['adx']:.2f} | +DI: {h4['plus_di']:.2f} | -DI: {h4['minus_di']:.2f}
Trend: {'STRONG' if h4['adx'] > 25 else 'WEAK' if h4['adx'] > 20 else 'RANGING — NO TRADE'}
BB: {h4['bb_upper']:.5f} / {h4['bb_mid']:.5f} / {h4['bb_lower']:.5f} | Squeeze: {'YES' if h4['squeeze_active'] else 'No'}

=== H1 (Confirmation) ===
Price: {h1['price']:.5f} | EMA20: {h1['ema_fast']:.5f} | EMA50: {h1['ema_slow']:.5f} | EMA200: {h1['ema_200']:.5f}
vs EMA200: {'ABOVE' if h1['price'] > h1['ema_200'] else 'BELOW'}
RSI: {h1['rsi']:.2f} (prev {h1['rsi_prev']:.2f})
ATR: {h1['atr']:.4f} | ADX: {h1['adx']:.2f} | +DI: {h1['plus_di']:.2f} | -DI: {h1['minus_di']:.2f}

=== M15 (Entry Timing) ===
Price: {m15['price']:.5f} | EMA20: {m15['ema_fast']:.5f} | EMA50: {m15['ema_slow']:.5f} | EMA200: {m15['ema_200']:.5f}
vs EMA200: {'ABOVE' if m15['price'] > m15['ema_200'] else 'BELOW'}
RSI: {m15['rsi']:.2f} (prev {m15['rsi_prev']:.2f})
ATR: {m15['atr']:.4f} | ADX: {m15['adx']:.2f} | +DI: {m15['plus_di']:.2f} | -DI: {m15['minus_di']:.2f}
BB: {m15['bb_upper']:.5f} / {m15['bb_mid']:.5f} / {m15['bb_lower']:.5f}
Volume: {m15['vol_ratio']:.2f}x avg | {'HIGH' if m15['vol_ratio'] > 1.5 else 'NORMAL' if m15['vol_ratio'] > 0.8 else 'LOW — trap risk'}
Squeeze: {'ACTIVE' if m15['squeeze_active'] else 'No'}

=== LAST 3 M15 CANDLES ===
{json.dumps(m15['candles_tail'], indent=2)}

=== VOLATILITY REGIME ===
{self._vol_regime_text(vol_regime)}

=== COT DATA (CFTC Large Speculator Positioning — Gold futures) ===
{cot_text(cot)}

=== STRATEGIST EXECUTION PLAN (Daily Top-Down Analysis) ===
{self._strategy_text()}

=== UPCOMING NEWS (next 2h) ===
{news_text}"""

        response = self.client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        text = response.content[0].text.strip().replace("```json","").replace("```","").strip()
        if not text:
            raise ValueError("Empty response from Claude")
        return json.loads(text)

    # ── Build Proposal for MANAGER ────────────────────────────────────
    def build_proposal(self, decision: dict, m15: dict) -> dict | None:
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
        sl_pts = round(abs(ep - sl), 5)
        tp_pts = round(abs(tp - ep), 5)
        if sl_pts > 0 and (tp_pts / sl_pts) < 1.8:
            print(f"[{self.NAME}] R:R too low — skipping")
            return None
        lot = 0.01 if conf < 75 else (0.02 if conf < 85 else 0.03)
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
            "pillars":           decision.get("pillars_confirmed", 0),
            "adx_strength":      decision.get("adx_trend_strength", "UNKNOWN"),
            "dxy_aligned":       decision.get("dxy_alignment", False),
            "fib_confluence":    decision.get("fib_confluence", False),
            "vwap_aligned":      decision.get("vwap_aligned", False),
            "vwap_retest":       decision.get("vwap_retest", False),
            "w1_bias":           decision.get("w1_bias", "UNKNOWN"),
            "d1_bias":           decision.get("d1_bias", "UNKNOWN"),
            "htf_aligned":       decision.get("htf_aligned", False),
            "near_key_level":    decision.get("near_key_level", False),
            "key_level_confluence": decision.get("key_level_confluence", False),
            "vol_regime":        decision.get("vol_regime", "UNKNOWN"),
            "cot_signal":        decision.get("cot_signal", "UNAVAILABLE"),
            "volume":            decision.get("volume_conviction", "NORMAL"),
            "risk_regime":       self.dollar_broadcast.get("risk_regime") if self.dollar_broadcast else "UNKNOWN",
            "reasoning":         decision.get("reasoning", ""),
            "timestamp":         datetime.utcnow().isoformat(),
        }

    # ── Main ──────────────────────────────────────────────────────────
    def analyse(self) -> dict | None:
        print(f"\n[{self.NAME}] Starting Gold analysis...")
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

        h4  = self._get_indicators(mt5.TIMEFRAME_H4)
        h1  = self._get_indicators(mt5.TIMEFRAME_H1)
        m15 = self._get_indicators(mt5.TIMEFRAME_M15)
        if any(x is None for x in [h4, h1, m15]):
            print(f"[{self.NAME}] Failed to fetch price data.")
            mt5.shutdown()
            return None

        spread_ok, spread_val = self._check_spread(m15["atr"])
        if not spread_ok:
            print(f"[{self.NAME}] Spread too wide — skipping.")
            mt5.shutdown()
            return None

        # Fetch raw H1 DataFrame for SMC detection
        h1_rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_H1, 0, CANDLES_BACK)
        df_h1 = pd.DataFrame(h1_rates) if h1_rates is not None and len(h1_rates) >= 10 else None

        fib        = self._get_fib()
        levels     = self._get_key_levels(m15["price"], h4["atr"])
        htf        = self._get_htf_bias()
        vwap       = self._get_vwap(session, h1["atr"])
        vol_regime = self._get_volatility_regime()
        account    = self._get_account()
        news    = self._fetch_news()
        blocked, reason = self._news_blackout(news)
        if blocked:
            print(f"[{self.NAME}] News blackout: {reason}")
            mt5.shutdown()
            return None

        news_text = self._news_text(news)
        mt5.shutdown()

        # SMC detection on H1
        if df_h1 is not None:
            smc = self._build_smc(df_h1, h1["price"], h1["atr"])
        else:
            smc = {'fvg': {}, 'ob': {}, 'liq': {}}

        cot = get_cot_data("GOLD")
        print(f"[{self.NAME}] COT: {cot.get('signal', 'unavailable')} "
              f"(net {cot.get('net_pct_oi', 'N/A')}% OI, "
              f"chg {cot.get('weekly_change', 'N/A')})" if cot.get("available") else
              f"[{self.NAME}] COT: unavailable")
        print(f"[{self.NAME}] Vol regime: {vol_regime.get('regime', 'unavailable')} "
              f"(ratio {vol_regime.get('ratio', 'N/A')}x)" if vol_regime.get("available") else
              f"[{self.NAME}] Vol regime: unavailable")

        try:
            decision = self._ask_claude(h4, h1, m15, session, news_text, account, fib, spread_val, vwap, htf, levels, cot, vol_regime, smc)
        except Exception as e:
            print(f"[{self.NAME}] Claude API error: {e}")
            return None

        proposal = self.build_proposal(decision, m15)
        if proposal:
            print(f"[{self.NAME}] Proposal: {proposal['direction']} "
                  f"@ {proposal['confidence']}% | "
                  f"Pillars: {proposal['pillars']}/4 | "
                  f"ADX: {proposal['adx_strength']} | "
                  f"Fib: {proposal['fib_confluence']} | "
                  f"Vol: {proposal['volume']}")
        return proposal

    def on_atlas_decision(self, decision: dict):
        status = decision.get("status")
        lot    = decision.get("lot_size_approved", 0)
        reason = decision.get("reason", "")
        print(f"[{self.NAME}] MANAGER: {status} "
              f"{'— Lot: ' + str(lot) if status == 'APPROVED' else ''} | {reason}")
