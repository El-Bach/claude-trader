"""
MANAGER — Capital Manager & Team Lead
APEX Capital AI

- Reads REAL MT5 account at start of every cycle
- Collects all agent decisions internally
- Sends ONE consolidated Telegram report per cycle
- No agent sends Telegram directly — only MANAGER does
"""

import os
import json
import time
import anthropic
import MetaTrader5 as mt5
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

MAGIC = 20250401


class ManagerAgent:
    NAME = "MANAGER"

    # ── Risk Constants ────────────────────────────────────────────────
    MAX_DAILY_LOSS_PCT     = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.03))
    REDUCE_SIZE_PCT        = 0.02
    MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS", 3))
    MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", 0.01))
    MAX_PORTFOLIO_RISK_PCT = 0.02   # 2% total open risk ceiling
    MIN_CONFIDENCE         = 70
    MIN_RR                 = 1.95
    MAX_CONSECUTIVE_LOSSES = 3

    # Dollar value per 1 point move per 1.0 lot (approximations, within ~5%)
    _POINT_VALUES = {
        "EURUSD": 10.0,   # 1 pip = 0.0001, 100k contract → $10/pip/lot
        "XAUUSD": 1.0,    # 1 point = 0.01, 100oz contract → $1/point/lot
        "USDJPY": 9.1,    # 1 pip = 0.01, varies with rate → ~$9/pip/lot
        "GBPUSD": 10.0,
        "USDCAD": 7.5,
    }
    _POINT_SIZES = {
        "EURUSD": 0.0001,
        "GBPUSD": 0.0001,
        "USDCAD": 0.0001,
        "USDJPY": 0.01,
        "XAUUSD": 0.01,
    }

    def __init__(self):
        self.client               = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.dollar_broadcast     = None
        self.news_broadcast       = None
        self.regime               = "MIXED"
        self.nasdaq_intraday_pct  = 0.0
        self.decisions_log        = []
        self.consecutive_losses   = 0
        self.paused_until         = None
        self.account              = {}
        self.session_start_balance = None   # set on first refresh_account()

        # ── Cycle report buffer ──────────────────────────────────────
        # Collects all agent activity during one cycle
        # Sent as ONE Telegram message at end of cycle
        self.cycle_report = {
            "timestamp":      "",
            "account":        {},
            "dollar_signal":  {},
            "agent_results":  [],   # list of {agent, result, reason}
            "executed":       [],   # list of executed trades
            "alerts":         [],   # urgent warnings
        }

    # ================================================================ #
    #  TELEGRAM — SINGLE SEND ONLY
    # ================================================================ #

    def _telegram(self, message: str):
        """Internal send — only called by send_cycle_report()."""
        token   = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            print(f"[{self.NAME}] Telegram error: {e}")

    def send_cycle_report(self):
        """
        Build and send ONE consolidated Telegram report.
        Called once at the end of every cycle.
        """
        r       = self.cycle_report
        account = r["account"]
        ts      = r["timestamp"]

        # ── Account section ──────────────────────────────────────────
        pos_lines = ""
        for p in account.get("open_positions", []):
            pnl   = p["floating_pnl"]
            sign  = "+" if pnl >= 0 else ""
            pos_lines += (f"\n  #{p['ticket']} {p['direction']} "
                         f"{p['symbol']} Lot:{p['lot']} "
                         f"P&L:{sign}${pnl:.2f}")
        if not pos_lines:
            pos_lines = "\n  No open positions"

        account_section = (
            f"<b>💰 ACCOUNT</b>\n"
            f"Balance    : ${account.get('balance', 0):.2f}\n"
            f"Equity     : ${account.get('equity', 0):.2f}\n"
            f"Free Margin: ${account.get('free_margin', 0):.2f}\n"
            f"Daily P&L  : ${account.get('daily_pnl', 0):.2f}\n"
            f"Daily Loss : ${account.get('daily_loss', 0):.2f} "
            f"({account.get('daily_loss_pct', 0):.2f}%)\n"
            f"Positions  : {account.get('open_count', 0)} open"
            f"{pos_lines}"
        )

        # ── News section ──────────────────────────────────────────────
        n = r.get("news", {})
        if n:
            risk  = n.get("risk_level", "LOW")
            icon  = {"CRITICAL":"🚨","HIGH":"⚠️","MEDIUM":"⚡","LOW":"✅"}.get(risk,"❓")
            fg    = n.get("fear_greed_score", 50)   # was 'fear_greed' — wrong key, always 50
            lines = [
                f"<b>{icon} NEWS & SENTIMENT</b>",
                f"Risk      : {risk} | Sentiment: {n.get('sentiment','NEUTRAL')}",
                f"Fear/Greed: {fg:.0f}/100 ({n.get('fear_greed_rating','Neutral')})",
                f"USD bias  : {n.get('usd_bias','NEUTRAL')}",
            ]
            # Key ForexFactory events
            key_events = n.get("key_events", [])
            if key_events:
                lines.append("Events    :")
                for ev in key_events[:2]:
                    lines.append(f"  → {ev}")
            # Top headlines
            top_headlines = n.get("top_headlines", [])
            if top_headlines:
                lines.append("Headlines :")
                for hl in top_headlines[:3]:
                    src   = hl.get("source", "")
                    title = hl.get("title", "")[:80]
                    lines.append(f"  [{src}] {title}")
            news_section = "\n".join(lines)
        else:
            news_section = ""

        # ── Dollar signal section ────────────────────────────────────
        d = r["dollar_signal"]
        dollar_section = (
            f"<b>💵 DOLLAR SIGNAL</b>\n"
            f"USD Bias : {d.get('usd_bias', 'UNKNOWN')} | "
            f"Regime: {d.get('risk_regime', 'UNKNOWN')} | "
            f"Conf: {d.get('confidence', 0)}%"
        )

        # ── Agent decisions section ──────────────────────────────────
        agent_lines = ""
        for a in r["agent_results"]:
            agent  = a["agent"]
            result = a["result"]
            reason = a["reason"][:60]   # truncate long reasons
            if result == "APPROVED":
                icon = "✅"
            elif result == "NO TRADE":
                icon = "⏸"
            elif result == "REJECTED":
                icon = "❌"
            else:
                icon = "⚠️"
            agent_lines += f"\n{icon} {agent}: {result} — {reason}"

        agent_section = f"<b>🤖 AGENT DECISIONS</b>{agent_lines}"

        # ── Executed trades section ──────────────────────────────────
        exec_section = ""
        if r["executed"]:
            exec_lines = "\n".join([
                f"→ {e['direction']} {e['instrument']} "
                f"Lot:{e['lot']} (stub mode)"
                for e in r["executed"]
            ])
            exec_section = f"\n\n<b>⚡ EXECUTED</b>\n{exec_lines}"

        # ── Alerts section ───────────────────────────────────────────
        alert_section = ""
        if r["alerts"]:
            alert_lines = "\n".join([f"⚠️ {a}" for a in r["alerts"]])
            alert_section = f"\n\n<b>🚨 ALERTS</b>\n{alert_lines}"

        # ── Assemble full message ────────────────────────────────────
        message = (
            f"<b>📊 APEX Capital AI — Cycle Report</b>\n"
            f"🕐 {ts}\n\n"
            f"{account_section}\n\n"
            f"{news_section + chr(10) + chr(10) if news_section else ''}"
            f"{dollar_section}\n\n"
            f"{agent_section}"
            f"{exec_section}"
            f"{alert_section}"
        )

        self._telegram(message)
        print(f"[{self.NAME}] Telegram cycle report sent.")

    def send_startup_message(self):
        account = self.account
        pos_lines = ""
        for p in account.get("open_positions", []):
            pnl  = p["floating_pnl"]
            sign = "+" if pnl >= 0 else ""
            pos_lines += f"\n  #{p['ticket']} {p['direction']} {p['symbol']} P&L:{sign}${pnl:.2f}"
        if not pos_lines:
            pos_lines = "\n  No open positions"

        self._telegram(
            f"<b>🚀 APEX Capital AI — Started</b>\n"
            f"Account : #{account.get('login')}\n"
            f"Balance : ${account.get('balance', 0):.2f}\n"
            f"Equity  : ${account.get('equity', 0):.2f}\n"
            f"Team    : MANAGER | DOLLAR | GOLD | EURUSD | USDJPY\n"
            f"Positions: {account.get('open_count', 0)} open{pos_lines}"
        )

    def send_daily_summary(self):
        approved = sum(1 for d in self.decisions_log
                      if d["decision"]["status"] == "APPROVED")
        rejected = sum(1 for d in self.decisions_log
                      if d["decision"]["status"] == "REJECTED")
        self._telegram(
            f"<b>📊 APEX Capital AI — Session End</b>\n"
            f"Balance  : ${self.account.get('balance', 0):.2f}\n"
            f"Daily P&L: ${self.account.get('daily_pnl', 0):.2f}\n"
            f"Approved : {approved}\n"
            f"Rejected : {rejected}\n"
            f"Consec Losses: {self.consecutive_losses}"
        )

    def session_summary(self):
        """Called after each cycle — no-op here, TRACKER handles session reports."""
        pass

    # ================================================================ #
    #  CYCLE REPORT — RESET & COLLECT
    # ================================================================ #

    def reset_cycle_report(self):
        """Call at start of each cycle to clear previous report."""
        self.cycle_report = {
            "timestamp":     datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "account":       {},
            "news":          {},
            "dollar_signal": {},
            "agent_results": [],
            "executed":      [],
            "alerts":        [],
        }

    def record_agent_result(self, agent: str, result: str, reason: str):
        """Called after each agent analyses — records result for cycle report."""
        self.cycle_report["agent_results"].append({
            "agent":  agent,
            "result": result,
            "reason": reason,
        })

    def record_execution(self, proposal: dict, lot: float):
        """Called when a trade is executed."""
        self.cycle_report["executed"].append({
            "direction":  proposal.get("direction"),
            "instrument": proposal.get("instrument"),
            "lot":        lot,
            "agent":      proposal.get("agent"),
        })

    def add_alert(self, message: str):
        """Add an urgent alert to the cycle report."""
        self.cycle_report["alerts"].append(message)

    # ================================================================ #
    #  MT5 — READ REAL ACCOUNT
    # ================================================================ #

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

    def refresh_account(self) -> bool:
        print(f"\n[{self.NAME}] Reading real account from MT5...")

        if not self._connect_mt5():
            self.add_alert("MT5 connection failed — cycle skipped")
            return False

        try:
            info        = mt5.account_info()
            positions   = mt5.positions_get() or []
            open_trades = []
            total_pnl   = 0.0

            for p in positions:
                tick = mt5.symbol_info_tick(p.symbol)
                cur  = (tick.bid if p.type == 0 else tick.ask) if tick else p.price_open
                total_pnl += p.profit
                open_trades.append({
                    "ticket":        p.ticket,
                    "symbol":        p.symbol,
                    "direction":     "BUY" if p.type == 0 else "SELL",
                    "lot":           p.volume,
                    "open_price":    round(p.price_open, 5),
                    "current_price": round(float(cur), 5),
                    "sl":            round(p.sl, 5),
                    "tp":            round(p.tp, 5),
                    "floating_pnl":  round(p.profit, 2),
                    "magic":         p.magic,
                })

            today     = datetime.now().date()
            from_time = datetime.combine(today, datetime.min.time())
            deals     = mt5.history_deals_get(from_time, datetime.now()) or []

            # Account-level closed P&L today (all sources — gives true account change)
            closed_pnl_today = sum(
                d.profit for d in deals
                if d.entry == mt5.DEAL_ENTRY_OUT
            )
            # APEX-only closed P&L today (for attribution reporting)
            apex_closed_pnl_today = sum(
                d.profit for d in deals
                if d.magic == MAGIC and d.entry == mt5.DEAL_ENTRY_OUT
            )

            balance        = info.balance
            # Daily P&L = all closed trades today + current floating P&L on all open positions
            daily_pnl      = round(closed_pnl_today + total_pnl, 2)
            daily_loss     = abs(min(0.0, daily_pnl))
            daily_loss_pct = (daily_loss / balance * 100) if balance > 0 else 0.0

            # Track session start balance (set once per bot session)
            if self.session_start_balance is None:
                self.session_start_balance = balance

            self.account = {
                "login":                  info.login,
                "balance":                round(balance, 2),
                "equity":                 round(info.equity, 2),
                "free_margin":            round(info.margin_free, 2),
                "margin_used":            round(info.margin, 2),
                "margin_level_pct":       round(info.margin_level, 1) if info.margin_level else 999.9,
                "daily_loss":             round(daily_loss, 2),
                "daily_loss_pct":         round(daily_loss_pct, 3),
                "daily_pnl":              round(daily_pnl, 2),
                "closed_pnl_today":       round(closed_pnl_today, 2),
                "apex_closed_pnl_today":  round(apex_closed_pnl_today, 2),
                "floating_pnl":           round(total_pnl, 2),
                "open_positions":         open_trades,
                "open_count":             len(open_trades),
                "session_start_balance":  round(self.session_start_balance, 2)
                                          if self.session_start_balance else round(balance, 2),
            }

        except Exception as e:
            print(f"[{self.NAME}] Account read error: {e}")
            mt5.shutdown()
            return False
        finally:
            mt5.shutdown()

        # Store in cycle report
        self.cycle_report["account"] = self.account

        # Print to terminal
        print(f"[{self.NAME}] ── REAL ACCOUNT (MT5) ────────────────────")
        print(f"[{self.NAME}] Account    : #{self.account['login']}")
        print(f"[{self.NAME}] Balance    : ${self.account['balance']:.2f}")
        print(f"[{self.NAME}] Equity     : ${self.account['equity']:.2f}")
        print(f"[{self.NAME}] Free Margin: ${self.account['free_margin']:.2f}")
        print(f"[{self.NAME}] Margin Used: ${self.account['margin_used']:.2f}")
        print(f"[{self.NAME}] Daily P&L  : ${self.account['daily_pnl']:.2f}")
        print(f"[{self.NAME}] Daily Loss : ${self.account['daily_loss']:.2f} "
              f"({self.account['daily_loss_pct']:.2f}%)")
        print(f"[{self.NAME}] Positions  : {self.account['open_count']} open")
        for t in self.account["open_positions"]:
            sign = "+" if t['floating_pnl'] >= 0 else ""
            print(f"[{self.NAME}]   #{t['ticket']} {t['direction']} "
                  f"{t['symbol']} | Lot:{t['lot']} | "
                  f"P&L:{sign}${t['floating_pnl']:.2f}")
        print(f"[{self.NAME}] ─────────────────────────────────────────")

        # Add alerts for critical account states
        if self.account["daily_loss_pct"] / 100 >= self.REDUCE_SIZE_PCT:
            self.add_alert(f"Daily loss {self.account['daily_loss_pct']:.2f}% — reduced sizing active")
        if 0 < self.account["margin_level_pct"] < 300:
            self.add_alert(f"Margin level low: {self.account['margin_level_pct']:.1f}%")

        return True

    # ================================================================ #
    #  LOT CALCULATOR
    # ================================================================ #

    def calculate_lot(self, symbol: str, sl_points: float,
                      confidence: int) -> float:
        """
        Calculate lot size so that SL distance = exactly risk_pct of equity.

        Formula:
            risk_amount      = equity × risk_pct
            dollar_per_point = tick_value / tick_size   ($ per 1-point move per 1.0 lot)
            sl_dollar_per_lot = sl_points × dollar_per_point
            lot              = risk_amount / sl_dollar_per_lot

        Example EURUSD (tick_value=$10, tick_size=0.0001):
            equity=$12,200 | risk=1% → risk_amount=$122
            dollar_per_point = $10 / 0.0001 = $100,000... wait, that's per full unit
            Actually: dollar_per_point = tick_value / tick_size
            EURUSD: $10 / 0.00010 → $10 per pip (0.0001 move) per lot ✓
            sl = 0.0025 (25 pips) → sl_dollar = 0.0025 / 0.0001 × $10 = $250/lot
            lot = $122 / $250 = 0.49 → capped at 0.05

        Example XAUUSD (tick_value≈$0.01, tick_size=0.01 → $1/point/lot):
            equity=$12,200 | risk=1% → risk_amount=$122
            dollar_per_point = $0.01 / 0.01 = $1 per point per lot...
            but contract=100oz → actual = $100/point/lot on most brokers
            tick_value from MT5 already accounts for contract size ✓
            sl = 15.0 (points) → sl_dollar = 15 / tick_size × tick_value per lot
            lot = $122 / sl_dollar → capped at 0.05
        """
        equity = self.account.get("equity", self.account.get("balance", 0))
        if equity <= 0 or sl_points <= 0:
            return 0.01

        # Adjust risk % based on conditions
        risk_pct = self.MAX_RISK_PER_TRADE_PCT
        if self.account.get("daily_loss_pct", 0) / 100 >= self.REDUCE_SIZE_PCT:
            risk_pct *= 0.5   # Half risk if daily loss building up
        if confidence < 75:
            risk_pct *= 0.75  # Reduce for lower confidence

        risk_amount = equity * risk_pct

        try:
            if not self._connect_mt5():
                raise Exception("MT5 connect failed")

            sym_info = mt5.symbol_info(symbol)
            if sym_info is None:
                raise Exception(f"Symbol {symbol} not found")

            # tick_value = $ profit per 1 tick move per 1.0 lot (accounts for contract size)
            # tick_size  = price change per 1 tick
            # → dollar per 1-point move per lot = tick_value / tick_size
            tick_value = sym_info.trade_tick_value  # $ per tick per 1.0 lot
            tick_size  = sym_info.trade_tick_size   # price distance per tick

            mt5.shutdown()

            if tick_size <= 0:
                raise Exception(f"Invalid tick_size={tick_size} for {symbol}")

            # Dollar risk for 1.0 lot if SL is hit
            # sl_points is in price units (same as tick_size units)
            sl_dollar_per_lot = abs(sl_points / tick_size * tick_value)

            if sl_dollar_per_lot <= 0:
                raise Exception(f"sl_dollar_per_lot={sl_dollar_per_lot}")

            raw_lot = risk_amount / sl_dollar_per_lot

            # Dynamic cap = 2× what full 1% risk gives for this instrument/SL/equity
            # Scales automatically as account grows — hard ceiling at 0.50 for sanity
            base_lot = (equity * self.MAX_RISK_PER_TRADE_PCT) / sl_dollar_per_lot
            dynamic_cap = min(round(base_lot * 2, 2), 0.50)

            print(f"[{self.NAME}] Lot calc: equity=${equity:.2f} × "
                  f"{risk_pct*100:.2f}% = ${risk_amount:.2f} risk | "
                  f"SL={sl_points} pts = ${sl_dollar_per_lot:.2f}/lot | "
                  f"Raw={raw_lot:.3f} | Cap={dynamic_cap}")

        except Exception as e:
            print(f"[{self.NAME}] Lot calc error: {e} — using 0.01")
            try: mt5.shutdown()
            except: pass
            raw_lot = 0.01
            dynamic_cap = self.MAX_LOT   # fallback to hardcoded cap on error

        lot = round(max(0.01, min(round(raw_lot, 2), dynamic_cap)), 2)
        print(f"[{self.NAME}] Final lot: {lot} (raw={raw_lot:.3f}, cap={dynamic_cap})")
        return lot

    # ================================================================ #
    #  PORTFOLIO RISK
    # ================================================================ #

    def _estimate_open_risk_pct(self) -> float:
        """
        Estimate total % of balance currently at risk across all open APEX positions.
        Uses SL distance × point value × lot from self.account (no extra MT5 call).
        Returns a percentage, e.g. 1.4 means 1.4% of balance is at risk.
        """
        balance = self.account.get("balance", 0)
        if balance <= 0:
            return 0.0

        total_risk_usd = 0.0
        for pos in self.account.get("open_positions", []):
            if pos.get("magic") != MAGIC:
                continue
            symbol   = pos.get("symbol", "")
            sl       = pos.get("sl", 0)
            price    = pos.get("current_price", 0)
            lot      = pos.get("lot", 0)

            if sl <= 0 or price <= 0 or lot <= 0:
                continue

            sl_distance  = abs(price - sl)
            point_size   = self._POINT_SIZES.get(symbol, 0.0001)
            point_value  = self._POINT_VALUES.get(symbol, 10.0)
            risk_usd     = (sl_distance / point_size) * point_value * lot
            total_risk_usd += risk_usd

        return round((total_risk_usd / balance) * 100, 3)

    def _apply_lot_modifiers(self, lot: float, proposal: dict) -> tuple[float, list]:
        """
        Apply instrument-specific and portfolio-level lot reducers after
        the base lot is calculated. Never increases lot — only reduces.

        Modifiers applied in order:
          1. Portfolio risk cap   — scale lot so total open risk stays ≤ 2%
          2. GOLD ADX scaling     — reduce lot when trend is weak
          3. USDJPY BoJ proximity — reduce lot when price near intervention zone

        Returns (adjusted_lot, list_of_notes).
        """
        notes  = []
        agent  = proposal.get("agent", "")

        # ── 1. Portfolio risk cap ──────────────────────────────────────
        open_risk_pct = self._estimate_open_risk_pct()
        trade_risk_pct = self.MAX_RISK_PER_TRADE_PCT * 100   # e.g. 1.0%

        if open_risk_pct > 0:
            remaining = self.MAX_PORTFOLIO_RISK_PCT * 100 - open_risk_pct
            if remaining <= 0:
                # Portfolio already at ceiling — minimum lot only
                new_lot = 0.01
                notes.append(
                    f"Portfolio risk {open_risk_pct:.1f}% already at {self.MAX_PORTFOLIO_RISK_PCT*100:.0f}% ceiling "
                    f"— lot floored to 0.01"
                )
                lot = new_lot
            elif remaining < trade_risk_pct:
                # Partial budget left — scale proportionally
                scale   = remaining / trade_risk_pct
                new_lot = max(0.01, round(lot * scale, 2))
                notes.append(
                    f"Portfolio risk {open_risk_pct:.1f}% open, "
                    f"{remaining:.1f}% budget left "
                    f"→ lot {lot}→{new_lot} (scaled {scale:.0%})"
                )
                lot = new_lot

        # ── 2. GOLD: ADX-scaled lot ───────────────────────────────────
        if agent == "GOLD":
            adx = float(proposal.get("adx", 30))
            if adx < 20:
                new_lot = max(0.01, round(lot * 0.50, 2))
                notes.append(f"GOLD ADX {adx:.1f} < 20 (weak trend) → lot {lot}→{new_lot} (−50%)")
                lot = new_lot
            elif adx < 25:
                new_lot = max(0.01, round(lot * 0.75, 2))
                notes.append(f"GOLD ADX {adx:.1f} < 25 (moderate trend) → lot {lot}→{new_lot} (−25%)")
                lot = new_lot

        # ── 3. USDJPY: BoJ proximity reducer ─────────────────────────
        if agent == "USDJPY":
            direction   = proposal.get("direction", "")
            entry_price = float(proposal.get("entry_price", 0))
            if direction == "LONG" and entry_price >= 148.0:   # tuned 149→148 on 29 Mar 2026
                new_lot = max(0.01, round(lot * 0.50, 2))
                notes.append(
                    f"USDJPY LONG near BoJ zone ({entry_price:.2f} ≥ 148.00) "
                    f"→ lot {lot}→{new_lot} (−50%)"
                )
                lot = new_lot

        return lot, notes

    # ================================================================ #
    #  DOLLAR BROADCAST
    # ================================================================ #

    def receive_news_broadcast(self, broadcast: dict):
        """Store NEWS broadcast so Claude Opus can reference it during final review."""
        self.news_broadcast          = broadcast
        self.cycle_report["news"]    = broadcast   # ← was missing: news section was always empty
        risk  = broadcast.get("risk_level", "LOW")
        sent  = broadcast.get("sentiment", "NEUTRAL")
        fg    = broadcast.get("fear_greed_score", 50)
        print(f"[{self.NAME}] News received: Risk={risk} | "
              f"Sentiment={sent} | Fear/Greed={fg:.0f}")

    def receive_dollar_broadcast(self, broadcast: dict):
        self.dollar_broadcast = broadcast
        self.regime           = broadcast.get("risk_regime", "MIXED")
        self.cycle_report["dollar_signal"] = broadcast

        usd  = broadcast.get("usd_bias", "NEUTRAL")
        conf = broadcast.get("confidence", 0)
        gold = broadcast.get("gold_implication", "NEUTRAL")
        eq   = broadcast.get("eurusd_implication", "NEUTRAL")

        print(f"\n[{self.NAME}] Dollar broadcast: USD={usd} | "
              f"Regime={self.regime} | Conf={conf}%")
        print(f"[{self.NAME}] Gold={gold} | Equities={eq}")

        # Record DOLLAR agent result
        if broadcast.get("trade_proposal"):
            self.record_agent_result(
                "DOLLAR",
                "PROPOSAL",
                f"SHORT EURUSD @ {broadcast['trade_proposal'].get('confidence',0)}%"
            )

    def update_nasdaq_performance(self, intraday_pct: float):
        self.nasdaq_intraday_pct = intraday_pct

    # ================================================================ #
    #  HARD RULE CHECKS
    # ================================================================ #

    def _check_paused(self) -> tuple[bool, str]:
        if self.paused_until and datetime.now() < self.paused_until:
            mins = int((self.paused_until - datetime.now()).total_seconds() / 60)
            return False, (f"Bot paused after {self.MAX_CONSECUTIVE_LOSSES} "
                          f"consecutive losses. Resumes in {mins} min.")
        elif self.paused_until and datetime.now() >= self.paused_until:
            self.paused_until       = None
            self.consecutive_losses = 0
            print(f"[{self.NAME}] Pause lifted — resuming.")
        return True, ""

    def _check_daily_loss(self) -> tuple[bool, str]:
        pct = self.account.get("daily_loss_pct", 0) / 100
        if pct >= self.MAX_DAILY_LOSS_PCT:
            return False, (f"Daily loss limit: ${self.account['daily_loss']:.2f} "
                          f"({self.account['daily_loss_pct']:.2f}%). HALTED.")
        return True, ""

    def _check_positions(self) -> tuple[bool, str]:
        n = self.account.get("open_count", 0)
        if n >= self.MAX_OPEN_POSITIONS:
            return False, f"Max positions {n}/{self.MAX_OPEN_POSITIONS} reached."
        return True, ""

    def _check_margin(self) -> tuple[bool, str]:
        free   = self.account.get("free_margin", 999)
        margin = self.account.get("margin_level_pct", 999)
        if free < 200:
            return False, f"Free margin too low: ${free:.2f}"
        if 0 < margin < 200:
            return False, f"Margin level critical: {margin:.1f}%"
        return True, ""

    def _check_confidence(self, proposal: dict) -> tuple[bool, str]:
        conf = proposal.get("confidence", 0)
        if conf < self.MIN_CONFIDENCE:
            return False, f"Confidence {conf}% < minimum {self.MIN_CONFIDENCE}%."
        return True, ""

    def _check_regime(self, proposal: dict) -> tuple[bool, str]:
        agent     = proposal.get("agent", "")
        direction = proposal.get("direction", "").upper()

        if self.regime != "RISK_OFF":
            return True, ""

        # GOLD and DOLLAR always allowed in RISK_OFF
        if agent in ("GOLD", "DOLLAR"):
            return True, ""

        # EURUSD: SHORT = USD long = consistent with RISK_OFF. LONG = blocked.
        if agent == "EURUSD":
            if direction == "SHORT":
                return True, ""
            return False, "RISK-OFF: EURUSD LONG blocked (USD is bid)."

        # USDJPY: SHORT = JPY long = consistent with RISK_OFF. LONG = blocked.
        if agent == "USDJPY":
            if direction == "SHORT":
                return True, ""
            return False, "RISK-OFF: USDJPY LONG blocked (JPY is bid)."

        return False, f"RISK-OFF: {agent} halted."

    def _check_correlation(self, proposal: dict) -> tuple[bool, str]:
        if not self.dollar_broadcast:
            return True, ""
        agent    = proposal.get("agent", "")
        direction= proposal.get("direction", "")
        usd      = self.dollar_broadcast.get("usd_bias", "NEUTRAL")
        impl_gold= self.dollar_broadcast.get("gold_implication", "NEUTRAL")
        if (agent == "GOLD" and direction == "LONG"
                and usd == "BULLISH_USD" and impl_gold == "BEARISH"):
            return False, "Correlation: USD bullish → GOLD LONG contradicts macro."
        return True, ""

    def _check_rr(self, proposal: dict) -> tuple[bool, str]:
        sl = proposal.get("sl_points", 0)
        tp = proposal.get("tp_points", 0)
        if sl > 0 and tp > 0:
            rr = tp / sl
            if rr < self.MIN_RR:
                return False, f"R:R {rr:.2f} < minimum {self.MIN_RR}."
        return True, ""

    def _check_pyramiding(self, proposal: dict) -> tuple[bool, str]:
        """
        Pyramiding rules — controls adding to existing positions.

        - 0 positions on instrument  → allow normally
        - 1 position WINNING         → allow, but reduce lot 50%
        - 1 position LOSING          → reject (never add to loser)
        - 2+ positions on instrument → reject always
        """
        instrument = proposal.get("instrument", "")
        direction  = proposal.get("direction", "")

        # Find existing positions on this instrument
        existing = [
            p for p in self.account.get("open_positions", [])
            if p["symbol"] == instrument
        ]

        if not existing:
            return True, ""   # No existing position — normal trade

        # Count positions on this instrument
        count = len(existing)
        if count >= 2:
            return False, (f"Already {count} positions on {instrument}. "
                          f"Maximum 2 per instrument.")

        # 1 existing position — check if winning or losing
        pos        = existing[0]
        pnl        = pos.get("floating_pnl", 0)
        pos_dir    = pos.get("direction", "")
        lot        = pos.get("lot", 0)

        # Check direction alignment
        pos_apex_dir = "LONG" if pos_dir == "BUY" else "SHORT"
        if pos_apex_dir != direction:
            return False, (f"Cannot open {direction} — already have "
                          f"{pos_apex_dir} {instrument} open. "
                          f"Conflicting directions.")

        if pnl < 0:
            return False, (f"Existing {instrument} position losing "
                          f"(${pnl:.2f}). Never add to a loser.")

        # Position is winning — allow pyramiding with reduced size
        # Reduce proposed lot by 50%
        proposed_lot = proposal.get("lot_size_request", 0.01)
        reduced_lot  = max(0.01, round(proposed_lot * 0.5, 2))
        proposal["lot_size_request"] = reduced_lot

        note = (f"Pyramiding into winning {instrument} position "
               f"(+${pnl:.2f}). Lot reduced to {reduced_lot} "
               f"(50% of normal).")
        print(f"[{self.NAME}] 📈 {note}")
        return True, note

    # ================================================================ #
    #  CLAUDE SENIOR REVIEW
    # ================================================================ #

    def _claude_review(self, proposal: dict, notes: list) -> dict:
        lot = self.calculate_lot(
            proposal.get("instrument", "XAUUSD"),
            proposal.get("sl_points", 0),
            proposal.get("confidence", 70)
        )

        # Apply instrument-specific and portfolio-level lot modifiers
        lot, modifier_notes = self._apply_lot_modifiers(lot, proposal)
        if modifier_notes:
            notes = list(notes) + modifier_notes
            for n in modifier_notes:
                print(f"[{self.NAME}] Lot modifier: {n}")

        open_risk_pct = self._estimate_open_risk_pct()

        trades_text = "\n".join([
            f"  #{t['ticket']} {t['direction']} {t['symbol']} "
            f"Lot:{t['lot']} P&L:${t['floating_pnl']:.2f}"
            for t in self.account.get("open_positions", [])
        ]) or "  None"

        # ── News context ─────────────────────────────────────────────
        n = self.news_broadcast or {}
        news_risk     = n.get("risk_level", "LOW")
        news_sent     = n.get("sentiment", "NEUTRAL")
        news_fg       = n.get("fear_greed_score", 50)
        news_usd      = n.get("usd_bias", "NEUTRAL")
        news_summary  = n.get("summary", "No news data available")
        news_events   = n.get("key_events", [])
        news_block    = n.get("block_new_entries", False)

        events_text = "\n".join(
            f"  → {e}" for e in news_events[:3]
        ) or "  None"

        # ── Dollar context ────────────────────────────────────────────
        db = self.dollar_broadcast or {}
        dxy_basket = db.get("dxy_basket", {})
        rate_diff  = db.get("rate_differential", {})
        fed        = db.get("fed_sentiment", {})

        prompt = f"""You are MANAGER, the Capital Manager at APEX Capital AI.
A trade proposal has passed all 9 hard checks. Make the final APPROVED / REJECTED / HOLD decision.
Weigh the news situation heavily — do not approve trades into high-risk events.

=== REAL MT5 ACCOUNT ===
Balance     : ${self.account.get('balance', 0):.2f}
Equity      : ${self.account.get('equity', 0):.2f}
Free Margin : ${self.account.get('free_margin', 0):.2f}
Daily Loss  : ${self.account.get('daily_loss', 0):.2f} ({self.account.get('daily_loss_pct', 0):.2f}%)
Open Trades :
{trades_text}
Consecutive Losses: {self.consecutive_losses}/{self.MAX_CONSECUTIVE_LOSSES}

=== NEWS & MARKET SENTIMENT (from NEWS agent) ===
Risk Level      : {news_risk}
Sentiment       : {news_sent}
Fear/Greed      : {news_fg:.0f}/100
USD Bias (news) : {news_usd}
Block entries?  : {news_block}
Key events:
{events_text}
Summary: {news_summary}

=== DOLLAR MACRO SIGNAL ===
Regime          : {self.regime}
USD Bias        : {db.get('usd_bias', 'UNKNOWN')}
DXY Basket      : {dxy_basket.get('basket_trend', 'N/A')} (score {dxy_basket.get('weighted_usd_score', 'N/A')})
Rate Spread     : {rate_diff.get('spread', 'N/A')}% → {rate_diff.get('interpretation', 'N/A')}
Fed Stance      : {fed.get('fed_stance', 'N/A')}
Dollar Reasoning: {db.get('reasoning', 'N/A')}

=== TRADE PROPOSAL ===
{json.dumps(proposal, indent=2, default=str)}

=== PORTFOLIO RISK ===
Open risk across all APEX positions : {open_risk_pct:.2f}% of balance
Portfolio risk ceiling               : {self.MAX_PORTFOLIO_RISK_PCT*100:.0f}%
Remaining risk budget                : {max(0.0, self.MAX_PORTFOLIO_RISK_PCT*100 - open_risk_pct):.2f}%

=== CALCULATED LOT (adjusted for risk + modifiers) ===
Lot: {lot}

=== NOTES FROM HARD CHECKS ===
{chr(10).join(notes) if notes else 'All checks passed cleanly.'}

Respond ONLY in valid JSON (no markdown, no backticks):
{{
  "status": "APPROVED" or "REJECTED" or "HOLD",
  "lot_size_approved": {lot},
  "reason": "1-2 sentence explanation referencing news + macro context",
  "conditions": "any conditions or empty string",
  "correlation_note": "how this trade fits the current news + portfolio picture"
}}"""

        response = self.client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        if not raw:
            raise ValueError("Empty response from Claude")
        return json.loads(raw)

    # ================================================================ #
    #  MAIN APPROVAL
    # ================================================================ #

    def evaluate_proposal(self, proposal: dict) -> dict:
        agent      = proposal.get("agent", "UNKNOWN")
        instrument = proposal.get("instrument", "UNKNOWN")
        direction  = proposal.get("direction", "UNKNOWN")
        conf       = proposal.get("confidence", 0)

        print(f"\n[{self.NAME}] Evaluating: {agent} {direction} "
              f"{instrument} @ {conf}%")

        hard_checks = [
            self._check_paused(),
            self._check_daily_loss(),
            self._check_positions(),
            self._check_margin(),
            self._check_confidence(proposal),
            self._check_regime(proposal),
            self._check_correlation(proposal),
            self._check_rr(proposal),
            self._check_pyramiding(proposal),
        ]

        failed = [msg for ok, msg in hard_checks if not ok]

        if failed:
            reason   = " | ".join(failed)
            decision = {
                "status":            "REJECTED",
                "lot_size_approved": 0,
                "reason":            reason,
                "conditions":        "",
                "correlation_note":  "",
                "agent":             agent,
                "instrument":        instrument,
                "direction":         direction,
                "timestamp":         datetime.utcnow().isoformat(),
            }
        else:
            notes = []
            if self.account.get("daily_loss_pct", 0) / 100 >= self.REDUCE_SIZE_PCT:
                notes.append(f"Daily loss {self.account['daily_loss_pct']:.2f}% — reduced sizing.")
            if self.consecutive_losses > 0:
                notes.append(f"Warning: {self.consecutive_losses} consecutive losses.")
            try:
                claude   = self._claude_review(proposal, notes)
                decision = {
                    **claude,
                    "agent":      agent,
                    "instrument": instrument,
                    "direction":  direction,
                    "timestamp":  datetime.utcnow().isoformat(),
                }
            except Exception as e:
                decision = {
                    "status":            "HOLD",
                    "lot_size_approved": 0,
                    "reason":            f"Claude review error: {e}",
                    "conditions":        "",
                    "correlation_note":  "",
                    "agent":             agent,
                    "instrument":        instrument,
                    "direction":         direction,
                    "timestamp":         datetime.utcnow().isoformat(),
                }

        # Record in cycle report
        self._record_decision_in_report(proposal, decision)
        self._log_decision(proposal, decision)
        self._print_decision(decision)
        return decision

    def _record_decision_in_report(self, proposal: dict, decision: dict):
        """Add decision to cycle report for consolidated Telegram message."""
        agent     = proposal.get("agent", "UNKNOWN")
        direction = proposal.get("direction", "")
        instrument= proposal.get("instrument", "")
        status    = decision.get("status", "UNKNOWN")
        reason    = decision.get("reason", "")
        lot       = decision.get("lot_size_approved", 0)
        conf      = proposal.get("confidence", 0)

        if status == "APPROVED":
            result = f"APPROVED {direction} {instrument} Lot:{lot} ({conf}%)"
        elif status == "REJECTED":
            result = f"REJECTED"
        else:
            result = "HOLD"

        self.record_agent_result(agent, result, reason[:80])

    # ================================================================ #
    #  RECORD NO-TRADE from specialist agents
    # ================================================================ #

    def record_no_trade(self, agent: str, reason: str):
        """Called by main.py when an agent returns None (no trade)."""
        self.record_agent_result(agent, "NO TRADE", reason[:80])

    # ================================================================ #
    #  CONSECUTIVE LOSS TRACKER
    # ================================================================ #

    def record_trade_result(self, won: bool):
        if won:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
                self.paused_until = datetime.now() + timedelta(hours=1)
                msg = (f"{self.MAX_CONSECUTIVE_LOSSES} consecutive losses. "
                      f"Paused 1 hour.")
                self.add_alert(msg)
                print(f"[{self.NAME}] ⚠️  {msg}")

    # ================================================================ #
    #  IS HALTED
    # ================================================================ #

    def is_halted(self) -> bool:
        if self.account.get("daily_loss_pct", 0) / 100 >= self.MAX_DAILY_LOSS_PCT:
            return True
        if self.paused_until and datetime.now() < self.paused_until:
            return True
        return False

    # ================================================================ #
    #  LOGGING
    # ================================================================ #

    def _log_decision(self, proposal: dict, decision: dict):
        entry = {
            "proposal":      proposal,
            "decision":      decision,
            "account_state": {k: v for k, v in self.account.items()
                             if k != "open_positions"}
        }
        self.decisions_log.append(entry)
        os.makedirs("logs", exist_ok=True)
        try:
            with open("logs/trades.json", "r") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing = []
        existing.append(entry)
        with open("logs/trades.json", "w") as f:
            json.dump(existing, f, indent=2, default=str)

    def _print_decision(self, decision: dict):
        status    = decision["status"]
        agent     = decision["agent"]
        instrument= decision["instrument"]
        direction = decision["direction"]
        reason    = decision["reason"]
        lot       = decision.get("lot_size_approved", 0)
        icon      = "✅" if status == "APPROVED" else ("⏸️" if status == "HOLD" else "❌")
        print(f"\n[{self.NAME}] {icon} {status} — {agent} {direction} {instrument}")
        if status == "APPROVED":
            print(f"[{self.NAME}]    Lot : {lot}")
            if decision.get("conditions"):
                print(f"[{self.NAME}]    Cond: {decision['conditions']}")
        print(f"[{self.NAME}]    Why : {reason}")

    def session_summary(self):
        approved = sum(1 for d in self.decisions_log
                      if d["decision"]["status"] == "APPROVED")
        rejected = sum(1 for d in self.decisions_log
                      if d["decision"]["status"] == "REJECTED")
        held     = sum(1 for d in self.decisions_log
                      if d["decision"]["status"] == "HOLD")
        print(f"\n[{self.NAME}] ===== SESSION SUMMARY =====")
        print(f"[{self.NAME}] Balance    : ${self.account.get('balance', 0):.2f}")
        print(f"[{self.NAME}] Daily P&L  : ${self.account.get('daily_pnl', 0):.2f}")
        print(f"[{self.NAME}] Daily Loss : ${self.account.get('daily_loss', 0):.2f} "
              f"({self.account.get('daily_loss_pct', 0):.2f}%)")
        print(f"[{self.NAME}] Decisions  : {len(self.decisions_log)} | "
              f"{approved} approved | {rejected} rejected | {held} held")
        print(f"[{self.NAME}] Consec Loss: {self.consecutive_losses}")
        print(f"[{self.NAME}] ===========================\n")
