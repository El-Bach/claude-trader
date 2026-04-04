"""
GOLD_WATCH — XAUUSD Position Monitor
APEX Capital AI

Single-brain (Claude Sonnet) specialist watcher for open XAUUSD positions.
Activated by MONITOR only when a GOLD position is open.

Spike behaviour:
  ADVERSE spike  → ask Claude: CLOSE early or HOLD and trust SL?
  FAVORABLE spike → ask Claude: MOVE_SL_TP to trail and ride momentum?
  Milestone/News  → standard HOLD / MOVE_SL / CLOSE decision
"""

import os
import json
import requests
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

MAGIC          = 20250401
SYMBOL         = "XAUUSD"
PIP_SIZE       = 0.01
NEWS_COUNTRIES = ["USD"]

SPIKE_THRESHOLD    = float(os.getenv("SPIKE_XAUUSD", 15.0))
PROFIT_MILESTONE_1 = 1.0
PROFIT_MILESTONE_2 = 1.5
PROFIT_MILESTONE_3 = 2.0
FIB_LEVELS         = [0.236, 0.382, 0.500, 0.618, 0.786]


class GoldWatch:
    NAME   = "GOLD_WATCH"
    SYMBOL = "XAUUSD"

    def __init__(self, claude_client):
        self.claude            = claude_client
        self.last_profit_check = datetime.utcnow()

    # ── MT5 Data ──────────────────────────────────────────────────

    def get_last_candle_move(self) -> tuple[float, bool]:
        """Returns (size_in_dollars, is_upward_move)."""
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 2)
        if rates is None or len(rates) < 1:
            return 0.0, False
        last = rates[-1]
        move = float(last["close"]) - float(last["open"])
        return abs(move), move > 0

    def get_market_context(self) -> dict:
        h4_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4, 0, 100)
        h1_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
        if h4_rates is None or h1_rates is None:
            return {}

        def calc(df):
            close = df["close"]
            high  = df["high"]
            low   = df["low"]
            ema20  = close.ewm(span=20, adjust=False).mean()
            ema50  = close.ewm(span=50, adjust=False).mean()
            ema200 = close.ewm(span=200, adjust=False).mean()
            d    = close.diff()
            gain = d.where(d > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-d.where(d < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
            rsi  = 100 - (100 / (1 + gain / loss))
            tr   = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs()
            ], axis=1).max(axis=1)
            atr  = tr.ewm(span=14, adjust=False).mean()
            aa   = 1.0 / 14
            pdm  = high.diff()
            mdm  = -low.diff()
            pdm  = pdm.where((pdm > mdm) & (pdm > 0), 0.0)
            mdm  = mdm.where((mdm > pdm) & (mdm > 0), 0.0)
            atr_adx = tr.ewm(alpha=aa, adjust=False).mean()
            pdi  = 100 * (pdm.ewm(alpha=aa, adjust=False).mean() / atr_adx)
            mdi  = 100 * (mdm.ewm(alpha=aa, adjust=False).mean() / atr_adx)
            dx   = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1)
            adx  = dx.ewm(alpha=aa, adjust=False).mean()
            return {
                "price":    round(float(close.iloc[-1]), 2),
                "ema20":    round(float(ema20.iloc[-1]),  2),
                "ema50":    round(float(ema50.iloc[-1]),  2),
                "ema200":   round(float(ema200.iloc[-1]), 2),
                "rsi":      round(float(rsi.iloc[-1]),    2),
                "atr":      round(float(atr.iloc[-1]),    2),
                "adx":      round(float(adx.iloc[-1]),    2),
                "plus_di":  round(float(pdi.iloc[-1]),    2),
                "minus_di": round(float(mdi.iloc[-1]),    2),
            }

        df_h4      = pd.DataFrame(h4_rates)
        df_h1      = pd.DataFrame(h1_rates)
        swing_high = float(df_h4["high"].tail(50).max())
        swing_low  = float(df_h4["low"].tail(50).min())
        fib_range  = swing_high - swing_low
        fib_levels = {
            f"fib_{int(f*1000)}": round(swing_high - f * fib_range, 2)
            for f in FIB_LEVELS
        }
        return {
            "h4": calc(df_h4), "h1": calc(df_h1),
            "fib": fib_levels,
            "swing_high": round(swing_high, 2),
            "swing_low":  round(swing_low,  2),
        }

    # ── News ──────────────────────────────────────────────────────

    def fetch_news_risk(self) -> tuple[bool, str]:
        try:
            r = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=8)
            if r.status_code != 200:
                return False, "News unavailable"
            now = datetime.utcnow()
            for e in r.json():
                if e.get("impact") != "High":
                    continue
                if e.get("country") not in NEWS_COUNTRIES:
                    continue
                try:
                    et   = datetime.strptime(
                        e["date"], "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                    diff = et - now
                    if timedelta(minutes=-5) <= diff <= timedelta(minutes=30):
                        return True, (f"{e.get('title','?')} in "
                                      f"{int(diff.total_seconds()/60)} min")
                except Exception:
                    pass
            return False, "No high-impact news imminent"
        except Exception as e:
            return False, f"News check failed: {e}"

    # ── Single-brain Claude Decision ──────────────────────────────

    def ask_claude(self, pos, ctx: dict, spike_size: float,
                   spike_type: str, news_risk: bool,
                   news_desc: str, trigger: str) -> dict:
        """
        Ask Claude Sonnet for a position management decision.

        spike_type:
          "ADVERSE"   — spike against position → CLOSE or HOLD
          "FAVORABLE" — spike with position    → MOVE_SL_TP or HOLD
          "MILESTONE" — profit milestone       → HOLD / MOVE_SL / CLOSE
          "NEWS"      — high-impact event      → HOLD / MOVE_SL / CLOSE
        """
        direction  = "BUY" if pos.type == 0 else "SELL"
        pnl        = round(pos.profit, 2)
        open_price = round(pos.price_open, 2)
        cur_price  = round(pos.price_current, 2)
        sl         = round(pos.sl, 2)
        tp         = round(pos.tp, 2)
        lot        = pos.volume
        h4         = ctx.get("h4", {})
        h1         = ctx.get("h1", {})
        fib        = ctx.get("fib", {})
        h1_atr     = h1.get("atr", 15.0)

        sl_dist = abs(cur_price - sl) if sl > 0 else 0
        tp_dist = abs(tp - cur_price) if tp > 0 else 0

        # Pre-compute suggested trail prices for favorable spike
        if spike_type == "FAVORABLE":
            if direction == "BUY":
                suggested_sl = round(cur_price - 0.8 * h1_atr, 2)
                suggested_tp = round(cur_price + 1.5 * h1_atr, 2)
            else:
                suggested_sl = round(cur_price + 0.8 * h1_atr, 2)
                suggested_tp = round(cur_price - 1.5 * h1_atr, 2)
        else:
            suggested_sl = None
            suggested_tp = None

        # Build spike-specific section and JSON schema
        if spike_type == "ADVERSE":
            spike_section = (
                f"=== ADVERSE SPIKE — ACTION REQUIRED ===\n"
                f"Spike size  : ${spike_size:.2f} AGAINST our {direction}\n"
                f"SL distance : ${sl_dist:.2f} remaining to SL at ${sl}\n"
                f"Current P&L : ${pnl:+.2f}\n"
                f"News nearby : {'YES — ' + news_desc if news_risk else 'No'}\n"
                f"\nDecide: CLOSE now to take a smaller loss, "
                f"or HOLD and trust the SL?\n"
                f"If the spike has moved >60% of SL distance, leaning CLOSE "
                f"avoids slippage risk on a hard stop-out."
            )
            json_schema = (
                '{\n'
                '  "decision": "CLOSE" or "HOLD",\n'
                '  "reason": "one sentence — be specific",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        elif spike_type == "FAVORABLE":
            spike_section = (
                f"=== FAVORABLE SPIKE — TRAIL OPPORTUNITY ===\n"
                f"Spike size    : ${spike_size:.2f} IN OUR FAVOR ({direction})\n"
                f"Current P&L   : ${pnl:+.2f}\n"
                f"TP remaining  : ${tp_dist:.2f} to ${tp}\n"
                f"H1 ATR        : ${h1_atr:.2f}\n"
                f"Suggested SL  : ${suggested_sl} "
                f"(current − 0.8×ATR, locks profit)\n"
                f"Suggested TP  : ${suggested_tp} "
                f"(current + 1.5×ATR, extends target)\n"
                f"\nDecide: MOVE_SL_TP to ride the spike momentum, "
                f"or HOLD the original levels?\n"
                f"Only trail if the spike has genuine momentum "
                f"(not a wick reversal)."
            )
            json_schema = (
                '{\n'
                '  "decision": "MOVE_SL_TP" or "HOLD",\n'
                f'  "new_sl": {suggested_sl} (or adjust),\n'
                f'  "new_tp": {suggested_tp} (or adjust),\n'
                '  "reason": "one sentence — be specific",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        else:
            # Milestone or news — standard three-way decision
            spike_section = (
                f"=== CONTEXT ===\n"
                f"Spike    : ${spike_size:.2f} "
                f"(threshold ${SPIKE_THRESHOLD:.2f})\n"
                f"News risk: {'YES ⚠️ — ' + news_desc if news_risk else 'No'}"
            )
            json_schema = (
                '{\n'
                '  "decision": "HOLD" or "MOVE_SL" or "CLOSE",\n'
                '  "new_sl": <price if MOVE_SL, else null>,\n'
                '  "reason": "one sentence",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        system = f"""You are GOLD_WATCH, an expert XAUUSD position manager at APEX Capital AI.
You monitor an open Gold position and make fast, decisive decisions.

GOLD KNOWLEDGE:
- Gold moves INVERSE to DXY (USD up = gold down)
- Gold is a safe haven — spikes up in RISK_OFF / geopolitical events
- Gold respects Fibonacci levels strongly (38.2%, 50%, 61.8%)
- H4 ATR for gold is typically $15-40 — do not use tight stops
- A $15+ move in 1 M1 candle = genuine spike event
- London/NY overlap (15:30-19:00 Beirut) = highest volatility

CAPITAL PROTECTION IS PRIORITY. Be decisive.
Respond ONLY in valid JSON:
{json_schema}"""

        user = (
            f"Trigger: {trigger}\n\n"
            f"=== OPEN POSITION ===\n"
            f"#{pos.ticket} {direction} XAUUSD {lot}lot\n"
            f"Entry: ${open_price} | Current: ${cur_price}\n"
            f"SL: ${sl} (${sl_dist:.2f} away) | "
            f"TP: ${tp} (${tp_dist:.2f} to target)\n"
            f"P&L: ${pnl:+.2f}\n\n"
            f"=== H4 CHART ===\n"
            f"Price: ${h4.get('price','?')} | "
            f"EMA20: ${h4.get('ema20','?')} | "
            f"EMA200: ${h4.get('ema200','?')}\n"
            f"RSI: {h4.get('rsi','?')} | "
            f"ADX: {h4.get('adx','?')} | "
            f"ATR: ${h4.get('atr','?')}\n\n"
            f"=== H1 CHART ===\n"
            f"Price: ${h1.get('price','?')} | "
            f"EMA20: ${h1.get('ema20','?')} | "
            f"RSI: {h1.get('rsi','?')} | "
            f"ATR: ${h1.get('atr','?')}\n\n"
            f"=== FIBONACCI (H4 50-candle) ===\n"
            f"High: ${ctx.get('swing_high','?')} | "
            f"Low: ${ctx.get('swing_low','?')}\n"
            f"38.2%: ${fib.get('fib_382','?')} | "
            f"50%: ${fib.get('fib_500','?')} | "
            f"61.8%: ${fib.get('fib_618','?')}\n\n"
            f"{spike_section}\n\n"
            f"Make your decision."
        )

        try:
            resp = self.claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=250,
                system=system,
                messages=[{"role": "user", "content": user}]
            )
            raw    = resp.content[0].text.strip()
            raw    = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            # Ensure new_sl / new_tp keys always exist
            result.setdefault("new_sl", suggested_sl)
            result.setdefault("new_tp", suggested_tp)
            return result
        except Exception as e:
            return {
                "decision": "HOLD", "new_sl": None, "new_tp": None,
                "reason": f"Claude error: {str(e)[:60]}", "confidence": 0,
            }

    # ── Main Watch ────────────────────────────────────────────────

    def watch(self, pos) -> dict | None:
        ctx                    = self.get_market_context()
        spike_size, spike_up   = self.get_last_candle_move()
        news_risk, news_desc   = self.fetch_news_risk()

        is_spike = spike_size >= SPIKE_THRESHOLD

        sl           = pos.sl
        open_p       = pos.price_open
        cur_p        = pos.price_current
        sl_dist      = abs(open_p - sl) if sl > 0 else 0
        profit_dist  = abs(cur_p - open_p)
        profit_ratio = (profit_dist / sl_dist) if sl_dist > 0 else 0
        milestone    = profit_ratio >= PROFIT_MILESTONE_1
        pnl          = round(pos.profit, 2)
        sign         = "+" if pnl >= 0 else ""
        direction    = "BUY" if pos.type == 0 else "SELL"
        now          = datetime.utcnow()
        time_for_check = (now - self.last_profit_check).total_seconds() >= 60

        # Classify spike direction relative to position
        if is_spike:
            pos_is_long  = (pos.type == 0)
            spike_type   = "FAVORABLE" if spike_up == pos_is_long else "ADVERSE"
            dir_label    = "with us ✅" if spike_type == "FAVORABLE" else "AGAINST us ⚠️"
            trigger = (f"⚡ GOLD SPIKE ${spike_size:.2f} in 1 min "
                       f"({dir_label}) — {spike_type}")
        elif news_risk:
            spike_type = "NEWS"
            trigger    = f"⚠️ NEWS RISK — {news_desc}"
        elif milestone and time_for_check:
            spike_type = "MILESTONE"
            trigger    = (f"📈 PROFIT MILESTONE {profit_ratio:.1f}x SL "
                          f"(${pnl:+.2f})")
            self.last_profit_check = now
        else:
            # Mode 1 — no AI, just print status
            mode = ("SPIKE_WATCH"
                    if spike_size > SPIKE_THRESHOLD * 0.5 else "PRICE_CHECK")
            print(f"[{self.NAME}] 👁️  {direction} #{pos.ticket} "
                  f"@ ${cur_p:.2f} | "
                  f"P&L:{sign}${pnl:.2f} | "
                  f"SL:${sl:.2f} (${sl_dist:.2f} away) | "
                  f"Profit:{profit_ratio:.2f}x SL | "
                  f"Spike:${spike_size:.2f} | {mode}")
            return None

        print(f"[{self.NAME}] 🔔 TRIGGER: {trigger}")

        decision = self.ask_claude(
            pos, ctx, spike_size, spike_type,
            news_risk, news_desc, trigger)

        print(f"[{self.NAME}] Decision: {decision.get('decision','?')} "
              f"({decision.get('confidence',0)}%) — "
              f"{decision.get('reason','')[:80]}")

        # Handle breakeven shorthand
        if decision.get("decision") == "MOVE_SL_BREAKEVEN":
            buffer = 0.50  # $0.50 for gold
            decision["new_sl"] = round(
                open_p + buffer if direction == "BUY" else open_p - buffer, 2)
            decision["decision"] = "MOVE_SL"

        decision.update({
            "symbol":     SYMBOL,
            "ticket":     pos.ticket,
            "trigger":    trigger,
            "spike_type": spike_type,
            "pnl":        pnl,
            "open_p":     open_p,
            "cur_p":      round(cur_p, 2),
            "sl_orig":    sl,
            "atr":        ctx.get("h1", {}).get("atr", 0),
        })
        return decision
