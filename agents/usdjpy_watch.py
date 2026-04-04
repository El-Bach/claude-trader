"""
USDJPY_WATCH — USDJPY Position Monitor
APEX Capital AI

Single-brain (Claude Sonnet) specialist watcher for open USDJPY positions.

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
SYMBOL         = "USDJPY"
PIP_SIZE       = 0.01
NEWS_COUNTRIES = ["USD", "JPY"]

SPIKE_THRESHOLD    = float(os.getenv("SPIKE_USDJPY", 0.80))  # 80 pips
BOJ_DANGER_LEVEL   = 150.00
BOJ_EXTREME_LEVEL  = 152.00
PROFIT_MILESTONE_1 = 1.0
PROFIT_MILESTONE_2 = 1.5
PROFIT_MILESTONE_3 = 2.0

ROUND_LEVELS = [
    135.00, 138.00, 140.00, 142.00, 144.00,
    145.00, 146.00, 147.00, 148.00, 149.00,
    150.00, 151.00, 152.00, 153.00, 155.00
]


class USDJPYWatch:
    NAME   = "USDJPY_WATCH"
    SYMBOL = "USDJPY"

    def __init__(self, claude_client):
        self.claude            = claude_client
        self.last_profit_check = datetime.utcnow()

    def nearest_round_level(self, price: float) -> dict:
        distances = [(abs(price - lvl), lvl) for lvl in ROUND_LEVELS]
        distances.sort()
        nearest  = distances[0][1]
        distance = distances[0][0]
        return {
            "level":         round(nearest, 2),
            "distance_pips": round(distance / PIP_SIZE, 1),
            "is_near":       distance < 0.50,
            "above":         price > nearest,
            "boj_danger":    price >= BOJ_DANGER_LEVEL,
            "boj_extreme":   price >= BOJ_EXTREME_LEVEL,
        }

    # ── MT5 Data ──────────────────────────────────────────────────

    def get_last_candle_move(self) -> tuple[float, bool]:
        """Returns (size_in_price, is_upward_move)."""
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
            close  = df["close"]
            high   = df["high"]
            low    = df["low"]
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
            tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
            kijun  = (high.rolling(26).max() + low.rolling(26).min()) / 2
            span_a = ((tenkan + kijun) / 2).shift(26)
            span_b = ((high.rolling(52).max() +
                       low.rolling(52).min()) / 2).shift(26)
            sa = float(span_a.iloc[-1]) if not pd.isna(span_a.iloc[-1]) else 0
            sb = float(span_b.iloc[-1]) if not pd.isna(span_b.iloc[-1]) else 0
            lc = float(close.iloc[-1])
            return {
                "price":       round(lc, 3),
                "ema20":       round(float(ema20.iloc[-1]),  3),
                "ema50":       round(float(ema50.iloc[-1]),  3),
                "ema200":      round(float(ema200.iloc[-1]), 3),
                "rsi":         round(float(rsi.iloc[-1]),    2),
                "atr":         round(float(atr.iloc[-1]),    3),
                "adx":         round(float(adx.iloc[-1]),    2),
                "tenkan":      round(float(tenkan.iloc[-1]), 3),
                "kijun":       round(float(kijun.iloc[-1]),  3),
                "above_cloud": bool(lc > max(sa, sb)),
                "below_cloud": bool(lc < min(sa, sb)),
            }

        df_h4 = pd.DataFrame(h4_rates)
        df_h1 = pd.DataFrame(h1_rates)
        price = float(df_h1["close"].iloc[-1])
        return {
            "h4":    calc(df_h4),
            "h1":    calc(df_h1),
            "round": self.nearest_round_level(price),
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
                        return True, (f"{e.get('title','?')} "
                                      f"({e.get('country','?')}) in "
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
        direction  = "BUY" if pos.type == 0 else "SELL"
        pnl        = round(pos.profit, 2)
        open_price = round(pos.price_open, 3)
        cur_price  = round(pos.price_current, 3)
        sl         = round(pos.sl, 3)
        tp         = round(pos.tp, 3)
        h4         = ctx.get("h4", {})
        h1         = ctx.get("h1", {})
        round_info = ctx.get("round", {})
        h1_atr     = h1.get("atr", 0.30)
        boj_danger = round_info.get("boj_danger", False)
        boj_extreme= round_info.get("boj_extreme", False)

        sl_pips    = abs(cur_price - sl) / PIP_SIZE if sl > 0 else 0
        tp_pips    = abs(tp - cur_price) / PIP_SIZE if tp > 0 else 0
        spike_pips = spike_size / PIP_SIZE

        if spike_type == "FAVORABLE":
            if direction == "BUY":
                suggested_sl = round(cur_price - 0.8 * h1_atr, 3)
                suggested_tp = round(cur_price + 1.5 * h1_atr, 3)
            else:
                suggested_sl = round(cur_price + 0.8 * h1_atr, 3)
                suggested_tp = round(cur_price - 1.5 * h1_atr, 3)
        else:
            suggested_sl = None
            suggested_tp = None

        if spike_type == "ADVERSE":
            spike_section = (
                f"=== ADVERSE SPIKE — ACTION REQUIRED ===\n"
                f"Spike      : {spike_pips:.1f} pips AGAINST our {direction}\n"
                f"SL distance: {sl_pips:.1f} pips remaining to SL at {sl}\n"
                f"Current P&L: ${pnl:+.2f}\n"
                f"News nearby: {'YES — ' + news_desc if news_risk else 'No'}\n"
                f"BoJ zone   : {'DANGER ⚠️' if boj_danger else 'OK'}"
                f"{'/ EXTREME 🚨' if boj_extreme else ''}\n"
                f"\nDecide: CLOSE now to take a smaller loss, "
                f"or HOLD and trust the SL?\n"
                f"If spike >60% of SL distance, leaning CLOSE "
                f"avoids slippage risk."
            )
            json_schema = (
                '{\n'
                '  "decision": "CLOSE" or "HOLD",\n'
                '  "reason": "one sentence — USDJPY specific",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        elif spike_type == "FAVORABLE":
            spike_section = (
                f"=== FAVORABLE SPIKE — TRAIL OPPORTUNITY ===\n"
                f"Spike size  : {spike_pips:.1f} pips IN OUR FAVOR ({direction})\n"
                f"Current P&L : ${pnl:+.2f}\n"
                f"TP remaining: {tp_pips:.1f} pips to {tp}\n"
                f"H1 ATR      : {h1_atr:.3f} ({h1_atr/PIP_SIZE:.1f} pips)\n"
                f"Suggested SL: {suggested_sl} (locks profit)\n"
                f"Suggested TP: {suggested_tp} (extends target)\n"
                f"BoJ zone    : {'DANGER — extra caution on LONG trails' if boj_danger else 'OK'}\n"
                f"\nDecide: MOVE_SL_TP to ride momentum, "
                f"or HOLD original levels?\n"
                f"Only trail if spike has genuine momentum."
            )
            json_schema = (
                '{\n'
                '  "decision": "MOVE_SL_TP" or "HOLD",\n'
                f'  "new_sl": {suggested_sl},\n'
                f'  "new_tp": {suggested_tp},\n'
                '  "reason": "one sentence",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        else:
            spike_section = (
                f"=== CONTEXT ===\n"
                f"Spike    : {spike_pips:.1f} pips "
                f"(threshold {SPIKE_THRESHOLD/PIP_SIZE:.0f} pips)\n"
                f"News risk: {'YES ⚠️ — ' + news_desc if news_risk else 'No'}\n"
                f"BoJ zone : {'DANGER ⚠️' if boj_danger else 'OK'}"
                f"{'/ EXTREME 🚨' if boj_extreme else ''}"
            )
            json_schema = (
                '{\n'
                '  "decision": "HOLD" or "MOVE_SL" or "CLOSE",\n'
                '  "new_sl": <price if MOVE_SL, else null>,\n'
                '  "reason": "one sentence",\n'
                '  "confidence": 0-100\n'
                '}'
            )

        system = f"""You are USDJPY_WATCH, expert USD/JPY position manager at APEX Capital AI.

USDJPY KNOWLEDGE:
- Driven by BOTH USD strength AND risk sentiment
- Yen = safe haven, strengthens in RISK_OFF (USDJPY falls)
- BoJ intervenes above 150.00 — do not trail LONG into BoJ zone
- Above 152.00: EXTREME — recommend CLOSE for any LONG
- USDJPY spikes more violently than EURUSD — use wider SL tolerance
- Ichimoku Cloud is key institutional reference level

CAPITAL PROTECTION IS PRIORITY. Be decisive.
Respond ONLY in valid JSON:
{json_schema}"""

        user = (
            f"Trigger: {trigger}\n\n"
            f"=== OPEN POSITION ===\n"
            f"#{pos.ticket} {direction} USDJPY\n"
            f"Entry: {open_price} | Current: {cur_price}\n"
            f"SL: {sl} ({sl_pips:.1f} pips away) | "
            f"TP: {tp} ({tp_pips:.1f} pips to target)\n"
            f"P&L: ${pnl:+.2f}\n\n"
            f"=== BOJ RISK ===\n"
            f"Price: {cur_price} | "
            f"Danger (>150): {'YES ⚠️' if boj_danger else 'No'} | "
            f"Extreme (>152): {'YES 🚨' if boj_extreme else 'No'}\n\n"
            f"=== H4 ===\n"
            f"Price: {h4.get('price','?')} | "
            f"EMA20: {h4.get('ema20','?')} | EMA200: {h4.get('ema200','?')}\n"
            f"RSI: {h4.get('rsi','?')} | ADX: {h4.get('adx','?')} | "
            f"ATR: {h4.get('atr','?')}\n"
            f"Cloud: {'ABOVE' if h4.get('above_cloud') else 'BELOW' if h4.get('below_cloud') else 'IN CLOUD'}\n\n"
            f"=== H1 ===\n"
            f"Price: {h1.get('price','?')} | RSI: {h1.get('rsi','?')} | "
            f"ATR: {h1.get('atr','?')}\n\n"
            f"{spike_section}\n\n"
            f"Make your decision."
        )

        try:
            resp   = self.claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=250,
                system=system,
                messages=[{"role": "user", "content": user}]
            )
            raw    = resp.content[0].text.strip()
            raw    = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
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
        ctx                  = self.get_market_context()
        spike_size, spike_up = self.get_last_candle_move()
        news_risk, news_desc = self.fetch_news_risk()
        round_info           = self.nearest_round_level(pos.price_current)

        # BoJ extreme always triggers review
        if round_info["boj_extreme"] and pos.type == 0:
            news_risk  = True
            news_desc  = "EXTREME BoJ risk — LONG above 152.00"

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
        sl_pips      = sl_dist / PIP_SIZE if sl_dist > 0 else 0
        profit_pips  = profit_dist / PIP_SIZE
        now          = datetime.utcnow()
        time_for_check = (now - self.last_profit_check).total_seconds() >= 60

        if is_spike:
            pos_is_long = (pos.type == 0)
            spike_type  = "FAVORABLE" if spike_up == pos_is_long else "ADVERSE"
            dir_label   = "with us ✅" if spike_type == "FAVORABLE" else "AGAINST us ⚠️"
            trigger = (f"⚡ USDJPY SPIKE {spike_size/PIP_SIZE:.1f} pips in 1 min "
                       f"({dir_label}) — {spike_type}")
        elif news_risk:
            spike_type = "NEWS"
            trigger    = f"⚠️ NEWS/BOJ RISK — {news_desc}"
        elif milestone and time_for_check:
            spike_type = "MILESTONE"
            trigger    = (f"📈 PROFIT MILESTONE {profit_ratio:.1f}x SL "
                          f"(${pnl:+.2f})")
            self.last_profit_check = now
        else:
            boj_warn = ("🚨BOJ!" if round_info.get("boj_extreme")
                        else ("⚠️BOJ" if round_info.get("boj_danger") else "OK"))
            mode = ("SPIKE_WATCH"
                    if spike_size > SPIKE_THRESHOLD * 0.5 else "PRICE_CHECK")
            print(f"[{self.NAME}] 👁️  {direction} #{pos.ticket} "
                  f"@ {round(cur_p,3)} | "
                  f"P&L:{sign}${pnl:.2f} | "
                  f"SL:{sl_pips:.1f}pips away | "
                  f"Profit:{profit_pips:.1f}pips ({profit_ratio:.2f}x SL) | "
                  f"BoJ:{boj_warn} | {mode}")
            return None

        print(f"[{self.NAME}] 🔔 TRIGGER: {trigger}")

        decision = self.ask_claude(
            pos, ctx, spike_size, spike_type,
            news_risk, news_desc, trigger)

        print(f"[{self.NAME}] Decision: {decision.get('decision','?')} "
              f"({decision.get('confidence',0)}%) — "
              f"{decision.get('reason','')[:80]}")

        if decision.get("decision") == "MOVE_SL_BREAKEVEN":
            buffer = 3 * PIP_SIZE
            decision["new_sl"] = round(
                open_p + buffer if direction == "BUY" else open_p - buffer, 3)
            decision["decision"] = "MOVE_SL"

        decision.update({
            "symbol":     SYMBOL,
            "ticket":     pos.ticket,
            "trigger":    trigger,
            "spike_type": spike_type,
            "pnl":        pnl,
            "open_p":     open_p,
            "cur_p":      round(cur_p, 3),
            "sl_orig":    sl,
            "atr":        ctx.get("h1", {}).get("atr", 0),
        })
        return decision
