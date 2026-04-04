"""
TRACKER — Performance Analyst
APEX Capital AI

Monitors the bot's own performance from two sources:
  1. logs/trades.json    — decision pipeline (proposals, approvals, rejections)
  2. logs/executions.json — MT5 order placement results
  3. MT5 live data       — open positions + closed deal history (P&L per agent)

Tracks:
- Decision stats (approved / rejected / held per agent)
- Execution success rate
- MT5 open positions summary (which agent, P&L so far)
- Closed position results (which agent opened it, win or loss, P&L)
- Daily win rate per agent
- Sends daily Telegram report with full position breakdown
"""

import os
import json
import requests
import MetaTrader5 as mt5
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

LOG_TRADES     = "logs/trades.json"
LOG_EXECUTIONS = "logs/executions.json"
MAGIC          = 20250401


class TrackerAgent:
    NAME = "TRACKER"

    def __init__(self):
        self.stats         = {}
        self.last_analysis = None

    # ================================================================ #
    #  MT5 CONNECTION
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

    # ================================================================ #
    #  DATA LOADING — LOG FILES
    # ================================================================ #

    def _load_trades(self) -> list:
        try:
            with open(LOG_TRADES, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _load_executions(self) -> list:
        try:
            with open(LOG_EXECUTIONS, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    # ================================================================ #
    #  DATA LOADING — MT5 POSITIONS & DEALS
    # ================================================================ #

    def _parse_agent(self, comment: str) -> str:
        """Extract agent name from MT5 comment field 'APEX_GOLD' → 'GOLD'."""
        if comment and comment.upper().startswith("APEX_"):
            return comment[5:].strip().upper()
        return "UNKNOWN"

    def _get_open_positions(self) -> list:
        """
        Read all currently open APEX positions from MT5.
        Returns list of dicts with agent, symbol, direction, P&L.
        """
        try:
            positions = mt5.positions_get() or []
            result = []
            for p in positions:
                if p.magic != MAGIC:
                    continue
                agent = self._parse_agent(p.comment)
                result.append({
                    "ticket":    p.ticket,
                    "agent":     agent,
                    "symbol":    p.symbol,
                    "direction": "LONG" if p.type == 0 else "SHORT",
                    "lot":       p.volume,
                    "open_price": round(p.price_open, 5),
                    "sl":        round(p.sl, 5),
                    "tp":        round(p.tp, 5),
                    "profit":    round(p.profit, 2),
                    "open_time": datetime.fromtimestamp(p.time).strftime("%H:%M UTC"),
                })
            return result
        except Exception as e:
            print(f"[{self.NAME}] Open positions read error: {e}")
            return []

    def _get_closed_deals(self, days: int = 1) -> list:
        """
        Read closed APEX deals from MT5 history.
        Returns one entry per closed position (DEAL_ENTRY_OUT only)
        matched to the opening deal to get the agent name.

        MT5 deal flow:
          DEAL_ENTRY_IN  → position opens  (has APEX_{agent} comment)
          DEAL_ENTRY_OUT → position closes (has SL/TP/close comment, same position_id)
        """
        try:
            from_time = datetime.now() - timedelta(days=days)
            deals = mt5.history_deals_get(from_time, datetime.now()) or []

            # Filter to APEX deals only
            apex_deals = [d for d in deals if d.magic == MAGIC]

            # Build position_id → agent map from opening deals
            agent_map = {}
            for d in apex_deals:
                if d.entry == mt5.DEAL_ENTRY_IN:
                    agent_map[d.position_id] = self._parse_agent(d.comment)

            # Collect closing deals (these have the actual P&L)
            closed = []
            for d in apex_deals:
                if d.entry != mt5.DEAL_ENTRY_OUT:
                    continue
                agent = agent_map.get(d.position_id, "UNKNOWN")
                close_time = datetime.fromtimestamp(d.time)
                closed.append({
                    "ticket":      d.position_id,
                    "deal_ticket": d.ticket,
                    "agent":       agent,
                    "symbol":      d.symbol,
                    "direction":   "LONG" if d.type == mt5.DEAL_TYPE_SELL else "SHORT",
                    "lot":         round(d.volume, 2),
                    "open_price":  round(d.price, 5),   # best available
                    "profit":      round(d.profit, 2),
                    "won":         d.profit > 0,
                    "close_time":  close_time.strftime("%Y-%m-%d %H:%M UTC"),
                    "close_date":  close_time.date().isoformat(),
                    "close_reason": self._classify_close(d.comment),
                })
            return closed
        except Exception as e:
            print(f"[{self.NAME}] Closed deals read error: {e}")
            return []

    def _classify_close(self, comment: str) -> str:
        """Classify how a position was closed from the MT5 comment."""
        c = (comment or "").lower()
        if "sl" in c:
            return "Stop Loss"
        if "tp" in c:
            return "Take Profit"
        if "so" in c or "margin" in c:
            return "Stop Out"
        return "Manual/Monitor"

    # ================================================================ #
    #  ANALYSIS
    # ================================================================ #

    def analyse(self) -> dict:
        """
        Full performance analysis — decision logs + MT5 position data.
        Called at end of each main cycle.
        Returns stats dict for MANAGER feedback.
        """
        trades     = self._load_trades()
        executions = self._load_executions()

        # ── MT5 live data ─────────────────────────────────────────────
        open_positions = []
        closed_deals   = []
        mt5_available  = False

        if self._connect_mt5():
            try:
                open_positions = self._get_open_positions()
                closed_deals   = self._get_closed_deals(days=1)
                mt5_available  = True
            finally:
                mt5.shutdown()

        # ── Decision stats ────────────────────────────────────────────
        total    = len(trades)
        approved = sum(1 for t in trades
                       if t.get("decision", {}).get("status") == "APPROVED")
        rejected = sum(1 for t in trades
                       if t.get("decision", {}).get("status") == "REJECTED")
        held     = sum(1 for t in trades
                       if t.get("decision", {}).get("status") == "HOLD")

        # ── Per-agent decision stats ──────────────────────────────────
        agent_stats = defaultdict(lambda: {
            "proposed": 0, "approved": 0, "rejected": 0,
            "wins": 0, "losses": 0, "pnl": 0.0})

        for t in trades:
            agent  = t.get("proposal", {}).get("agent", "UNKNOWN")
            status = t.get("decision", {}).get("status", "UNKNOWN")
            agent_stats[agent]["proposed"] += 1
            if status == "APPROVED":
                agent_stats[agent]["approved"] += 1
            elif status == "REJECTED":
                agent_stats[agent]["rejected"] += 1

        # Enrich agent stats with closed position results
        for deal in closed_deals:
            agent = deal["agent"]
            agent_stats[agent]["wins"]   += 1 if deal["won"] else 0
            agent_stats[agent]["losses"] += 0 if deal["won"] else 1
            agent_stats[agent]["pnl"]    += deal["profit"]

        # ── Execution stats ───────────────────────────────────────────
        exec_total   = len(executions)
        exec_success = sum(1 for e in executions
                           if e.get("result", {}).get("success", False))

        # ── Rejection reasons ─────────────────────────────────────────
        rejection_reasons = defaultdict(int)
        for t in trades:
            if t.get("decision", {}).get("status") == "REJECTED":
                reason = t.get("decision", {}).get("reason", "Unknown").lower()
                if "confidence" in reason:
                    rejection_reasons["Low confidence"] += 1
                elif "margin" in reason:
                    rejection_reasons["Margin"] += 1
                elif "risk" in reason and "off" in reason:
                    rejection_reasons["RISK_OFF regime"] += 1
                elif "r:r" in reason:
                    rejection_reasons["Poor R:R"] += 1
                elif "daily loss" in reason:
                    rejection_reasons["Daily loss limit"] += 1
                elif "position" in reason:
                    rejection_reasons["Max positions"] += 1
                elif "loser" in reason or "losing" in reason:
                    rejection_reasons["Adding to loser"] += 1
                else:
                    rejection_reasons["Other"] += 1

        # ── Today's decision stats ────────────────────────────────────
        today_str    = datetime.utcnow().date().isoformat()
        today_trades = [t for t in trades
                        if t.get("decision", {}).get("timestamp", "")[:10] == today_str]
        today_approved = sum(1 for t in today_trades
                             if t.get("decision", {}).get("status") == "APPROVED")
        today_rejected = sum(1 for t in today_trades
                             if t.get("decision", {}).get("status") == "REJECTED")

        # ── Today's closed position P&L summary ──────────────────────
        today_closed = [d for d in closed_deals if d["close_date"] == today_str]
        today_pnl    = round(sum(d["profit"] for d in today_closed), 2)
        today_wins   = sum(1 for d in today_closed if d["won"])
        today_losses = len(today_closed) - today_wins

        # ── Build stats dict ──────────────────────────────────────────
        stats = {
            "timestamp":       datetime.utcnow().isoformat(),
            "total_decisions": total,
            "approved":        approved,
            "rejected":        rejected,
            "held":            held,
            "approval_rate":   round(approved / total * 100, 1) if total > 0 else 0,
            "today": {
                "decisions": len(today_trades),
                "approved":  today_approved,
                "rejected":  today_rejected,
                "closed_positions": today_closed,
                "pnl":       today_pnl,
                "wins":      today_wins,
                "losses":    today_losses,
            },
            "agents":           dict(agent_stats),
            "instruments":      {},
            "executions": {
                "total":        exec_total,
                "success":      exec_success,
                "failed":       exec_total - exec_success,
                "success_rate": round(exec_success / exec_total * 100, 1)
                                if exec_total > 0 else 0,
            },
            "rejection_reasons": dict(rejection_reasons),
            "open_positions":    open_positions,
            "closed_deals":      closed_deals,
            "mt5_available":     mt5_available,
        }

        self.stats         = stats
        self.last_analysis = stats

        self._print_summary(stats)
        return stats

    def _print_summary(self, stats: dict):
        print(f"\n[{self.NAME}] ── PERFORMANCE SUMMARY ─────────────────")
        print(f"[{self.NAME}] Total decisions : {stats['total_decisions']}")
        print(f"[{self.NAME}] Approved        : {stats['approved']} "
              f"({stats['approval_rate']:.1f}%)")
        print(f"[{self.NAME}] Rejected        : {stats['rejected']}")
        print(f"[{self.NAME}] Today           : "
              f"{stats['today']['approved']} approved / "
              f"{stats['today']['rejected']} rejected")
        print(f"[{self.NAME}] Executions      : "
              f"{stats['executions']['success']}/{stats['executions']['total']} "
              f"successful ({stats['executions']['success_rate']:.1f}%)")

        # ── Agent breakdown with win rate ─────────────────────────────
        print(f"[{self.NAME}] By agent:")
        for agent, s in stats["agents"].items():
            rate  = round(s["approved"] / s["proposed"] * 100, 1) \
                    if s["proposed"] > 0 else 0
            total_closed = s["wins"] + s["losses"]
            if total_closed > 0:
                win_rate = round(s["wins"] / total_closed * 100, 1)
                pnl_sign = "+" if s["pnl"] >= 0 else ""
                print(f"[{self.NAME}]   {agent}: "
                      f"{s['proposed']} proposed → {s['approved']} approved ({rate}%) | "
                      f"Win rate: {win_rate}% ({s['wins']}W/{s['losses']}L) | "
                      f"P&L: {pnl_sign}${s['pnl']:.2f}")
            else:
                print(f"[{self.NAME}]   {agent}: "
                      f"{s['proposed']} proposed → {s['approved']} approved ({rate}%)")

        # ── Top rejection reasons ─────────────────────────────────────
        print(f"[{self.NAME}] Top rejection reasons:")
        for reason, count in sorted(stats["rejection_reasons"].items(),
                                    key=lambda x: x[1], reverse=True)[:3]:
            print(f"[{self.NAME}]   {reason}: {count}x")

        # ── Open positions ────────────────────────────────────────────
        if stats["open_positions"]:
            print(f"[{self.NAME}] Open positions ({len(stats['open_positions'])}):")
            for p in stats["open_positions"]:
                sign = "+" if p["profit"] >= 0 else ""
                print(f"[{self.NAME}]   #{p['ticket']} [{p['agent']}] "
                      f"{p['direction']} {p['symbol']} "
                      f"Lot:{p['lot']} @ {p['open_price']} | "
                      f"P&L: {sign}${p['profit']:.2f}")
        elif stats["mt5_available"]:
            print(f"[{self.NAME}] Open positions : None")

        # ── Today's closed positions ──────────────────────────────────
        today = stats["today"]
        if today["closed_positions"]:
            print(f"[{self.NAME}] Closed today "
                  f"({today['wins']}W / {today['losses']}L | "
                  f"P&L: {'+'if today['pnl']>=0 else ''}${today['pnl']:.2f}):")
            for d in today["closed_positions"]:
                icon = "✅" if d["won"] else "❌"
                sign = "+" if d["profit"] >= 0 else ""
                print(f"[{self.NAME}]   {icon} #{d['ticket']} [{d['agent']}] "
                      f"{d['direction']} {d['symbol']} "
                      f"→ {sign}${d['profit']:.2f} "
                      f"({d['close_reason']}) @ {d['close_time']}")

        print(f"[{self.NAME}] ──────────────────────────────────────────")

    # ================================================================ #
    #  TELEGRAM DAILY REPORT
    # ================================================================ #

    def _telegram(self, message: str):
        token   = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message,
                      "parse_mode": "HTML"},
                timeout=10)
        except Exception:
            pass

    def send_daily_report(self):
        """Backwards-compatible alias — calls send_session_report()."""
        self.send_session_report()

    def send_session_report(self, account: dict = None,
                            session_start_balance: float = None):
        """
        Send a comprehensive end-of-session report to Telegram.
        Pulls from both MT5 (live data) and local logs (decision history).

        Parameters
        ----------
        account              : dict from manager.account (balance, equity, etc.)
        session_start_balance: balance when bot first started this session
        """
        stats = self.analyse()
        today = stats["today"]
        now   = datetime.utcnow()

        # ── Account section (from MT5 via manager, or fallback) ───────
        if account:
            bal         = account.get("balance", 0)
            eq          = account.get("equity", 0)
            free_margin = account.get("free_margin", 0)
            floating    = account.get("floating_pnl", 0)
            closed_pnl  = account.get("closed_pnl_today", 0)
            apex_closed = account.get("apex_closed_pnl_today", 0)
            sess_start  = session_start_balance or account.get("session_start_balance", bal)
            session_pnl = round(eq - sess_start, 2)   # equity now vs balance at session start
        else:
            bal = eq = free_margin = floating = closed_pnl = apex_closed = 0
            sess_start  = 0
            session_pnl = today["pnl"]

        sess_sign  = "+" if session_pnl >= 0 else ""
        float_sign = "+" if floating >= 0 else ""
        closed_sign = "+" if closed_pnl >= 0 else ""

        account_section = (
            f"<b>💰 ACCOUNT</b>\n"
            f"Balance      : ${bal:,.2f}\n"
            f"Equity       : ${eq:,.2f}\n"
            f"Free Margin  : ${free_margin:,.2f}\n"
            f"Closed P&L   : {closed_sign}${closed_pnl:,.2f} (today, all sources)\n"
            f"Floating P&L : {float_sign}${floating:,.2f} (open positions)\n"
            f"Session start: ${sess_start:,.2f}"
        )

        # ── Closed positions section ──────────────────────────────────
        closed_positions = today["closed_positions"]
        if closed_positions:
            net_sign = "+" if today["pnl"] >= 0 else ""
            closed_lines = ""
            for d in closed_positions:
                icon     = "✅" if d["won"] else "❌"
                ps       = "+" if d["profit"] >= 0 else ""
                closed_lines += (
                    f"\n{icon} <b>[{d['agent']}]</b> {d['direction']} "
                    f"{d['symbol']} {d['lot']}lot"
                    f"\n   Entry: {d['open_price']} | "
                    f"P&L: {ps}${d['profit']:.2f} | "
                    f"{d['close_reason']} @ {d['close_time']}"
                )
            closed_section = (
                f"<b>📈 CLOSED POSITIONS ({len(closed_positions)})</b>\n"
                f"Result: {today['wins']}W / {today['losses']}L | "
                f"APEX Net: {net_sign}${today['pnl']:.2f}"
                f"{closed_lines}"
            )
        else:
            closed_section = "<b>📈 CLOSED POSITIONS</b>\nNone closed this session."

        # ── Open positions section ────────────────────────────────────
        open_positions = stats["open_positions"]
        if open_positions:
            open_lines = ""
            for p in open_positions:
                ps = "+" if p["profit"] >= 0 else ""
                open_lines += (
                    f"\n▶ <b>[{p['agent']}]</b> {p['direction']} "
                    f"{p['symbol']} {p['lot']}lot"
                    f"\n   Entry: {p['open_price']} | "
                    f"SL: {p['sl']} | TP: {p['tp']}"
                    f"\n   Float: {ps}${p['profit']:.2f} (since {p['open_time']})"
                )
            open_section = (
                f"<b>🔓 OPEN POSITIONS ({len(open_positions)})</b>"
                f"{open_lines}"
            )
        else:
            open_section = "<b>🔓 OPEN POSITIONS</b>\nNone open."

        # ── Decision stats section ────────────────────────────────────
        agent_lines = ""
        for agent, s in stats["agents"].items():
            rate         = round(s["approved"] / s["proposed"] * 100, 1) \
                           if s["proposed"] > 0 else 0
            total_closed = s["wins"] + s["losses"]
            if total_closed > 0:
                wr       = round(s["wins"] / total_closed * 100, 1)
                ps       = "+" if s["pnl"] >= 0 else ""
                agent_lines += (
                    f"\n  {agent}: {s['approved']}/{s['proposed']} approv. ({rate}%) | "
                    f"{wr}% WR ({s['wins']}W/{s['losses']}L) | "
                    f"P&L {ps}${s['pnl']:.2f}"
                )
            else:
                agent_lines += (
                    f"\n  {agent}: {s['approved']}/{s['proposed']} approv. ({rate}%) "
                    f"| No closed trades"
                )

        top_rejection = ""
        if stats["rejection_reasons"]:
            sorted_reasons = sorted(stats["rejection_reasons"].items(),
                                    key=lambda x: x[1], reverse=True)[:3]
            lines = " | ".join([f"{r}: {c}x" for r, c in sorted_reasons])
            top_rejection = f"\nTop blocks: {lines}"

        decision_section = (
            f"<b>🤖 DECISIONS</b>\n"
            f"Total    : {stats['total_decisions']} proposals\n"
            f"Approved : {stats['approved']} ({stats['approval_rate']:.1f}%)\n"
            f"Rejected : {stats['rejected']}\n"
            f"Today    : {today['approved']} approv / {today['rejected']} rej\n"
            f"Exec     : {stats['executions']['success']}/"
            f"{stats['executions']['total']} OK "
            f"({stats['executions']['success_rate']:.0f}%)\n"
            f"\nBy agent:{agent_lines}"
            f"{top_rejection}"
        )

        # ── Summary P&L line ──────────────────────────────────────────
        total_net = round(today["pnl"] + floating, 2)
        net_sign  = "+" if total_net >= 0 else ""
        summary_section = (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>💵 SESSION P&amp;L: {sess_sign}${session_pnl:.2f}</b>\n"
            f"  Closed APEX trades : {net_sign.replace('+','') if today['pnl']<0 else '+'}"
            f"${today['pnl']:.2f}\n"
            f"  Floating (open)    : {float_sign}${floating:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )

        header = (
            f"<b>📊 APEX Capital AI — Session Report</b>\n"
            f"🕐 {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        )

        # Build full message and split if over Telegram's 4096-char limit
        parts = [
            header + account_section,
            closed_section,
            open_section,
            decision_section,
            summary_section,
        ]

        # Try to send as one message; if too long, split into two
        full_msg = "\n\n".join(parts)
        if len(full_msg) <= 4000:
            self._telegram(full_msg)
        else:
            # Send in two chunks: account + trades in msg1, decisions + summary in msg2
            msg1 = "\n\n".join([header + account_section, closed_section, open_section])
            msg2 = "\n\n".join([decision_section, summary_section])
            self._telegram(msg1)
            self._telegram(msg2)

    # ================================================================ #
    #  FEEDBACK FOR MANAGER
    # ================================================================ #

    def get_manager_feedback(self) -> dict:
        """
        Returns actionable feedback for MANAGER at start of each cycle.
        """
        if not self.stats:
            self.analyse()

        stats = self.stats
        if not stats or stats.get("status") == "no_data":
            return {"status": "no_data", "alerts": []}

        alerts = []

        # Low approval rate
        approval_rate = stats.get("approval_rate", 100)
        if approval_rate < 20:
            alerts.append(f"Low approval rate: {approval_rate:.1f}% — "
                          f"agents may be too conservative")

        # High execution failure rate
        exec_rate = stats.get("executions", {}).get("success_rate", 100)
        if exec_rate < 80 and stats["executions"]["total"] > 3:
            alerts.append(f"High execution failure rate: "
                          f"{100-exec_rate:.1f}% failing")

        # Dominant rejection reason
        reasons = stats.get("rejection_reasons", {})
        if reasons:
            top_reason, count = max(reasons.items(), key=lambda x: x[1])
            total_rej = stats.get("rejected", 0)
            if total_rej > 0 and count / total_rej > 0.5:
                alerts.append(f"50%+ rejections due to: {top_reason}")

        # Low win rate alert (if enough closed trades to judge)
        all_closed = stats.get("closed_deals", [])
        if len(all_closed) >= 5:
            wins     = sum(1 for d in all_closed if d["won"])
            win_rate = wins / len(all_closed) * 100
            if win_rate < 40:
                alerts.append(f"Low overall win rate: {win_rate:.1f}% "
                              f"({wins}/{len(all_closed)} trades won)")

        return {
            "status":        "ok",
            "alerts":        alerts,
            "approval_rate": approval_rate,
            "total_trades":  stats.get("total_decisions", 0),
            "today_trades":  stats.get("today", {}).get("decisions", 0),
            "open_positions": stats.get("open_positions", []),
            "today_pnl":     stats.get("today", {}).get("pnl", 0.0),
        }
