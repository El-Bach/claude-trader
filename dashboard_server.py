"""
dashboard_server.py — APEX Capital AI Performance Dashboard
============================================================
Serves a live HTML dashboard on http://localhost:8080

Data sources:
  - logs/trades.json     → proposals + decisions
  - logs/executions.json → executed trades
  - MT5 (live)           → account, open positions, deal history, balance curve

Usage:
    python dashboard_server.py
"""

import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

MAGIC = 20250401
PORT  = 8080


# ── MT5 live data ─────────────────────────────────────────────────────────────

def get_mt5_data() -> dict | None:
    try:
        import MetaTrader5 as mt5
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        if not mt5.initialize(login=login, password=password, server=server):
            return None

        try:
            acct = mt5.account_info()
            if not acct:
                return None

            # ── Open positions ───────────────────────────────────────────
            positions = mt5.positions_get() or []
            open_pos  = []
            for p in positions:
                open_pos.append({
                    "ticket":  p.ticket,
                    "symbol":  p.symbol,
                    "type":    "BUY" if p.type == 0 else "SELL",
                    "lot":     p.volume,
                    "entry":   p.price_open,
                    "sl":      p.sl,
                    "tp":      p.tp,
                    "pnl":     round(p.profit, 2),
                    "comment": p.comment,
                    "time":    datetime.fromtimestamp(p.time).strftime("%m/%d %H:%M"),
                })

            # ── Deal history ─────────────────────────────────────────────
            from_date = datetime(datetime.utcnow().year, 1, 1)
            to_date   = datetime.utcnow() + timedelta(hours=1)
            all_deals = mt5.history_deals_get(from_date, to_date) or []

            # Build position_id → agent map from IN deals (DEAL_ENTRY_IN = 0)
            # IN deals carry the "APEX_GOLD" comment; OUT deals carry "TP"/"SL" etc.
            pos_agent = {}
            pos_type  = {}  # position_id → "BUY" / "SELL"
            for d in all_deals:
                if d.entry == 0 and d.comment.startswith("APEX_"):
                    pos_agent[d.position_id] = d.comment.replace("APEX_", "")
                    pos_type[d.position_id]  = "BUY" if d.type == 0 else "SELL"

            out_deals = [d for d in all_deals if d.entry == 1]  # DEAL_ENTRY_OUT

            # Balance curve: start balance = current balance - sum(all closed P&L)
            total_closed = sum(d.profit for d in out_deals)
            start_bal    = acct.balance - total_closed
            cumulative   = start_bal

            balance_curve = [{"time": "Start", "value": round(start_bal, 2)}]
            deal_list     = []

            for d in sorted(out_deals, key=lambda x: x.time):
                cumulative += d.profit
                balance_curve.append({
                    "time":  datetime.fromtimestamp(d.time).strftime("%m/%d %H:%M"),
                    "value": round(cumulative, 2),
                })
                # Look up agent via position_id → IN deal comment
                agent = pos_agent.get(d.position_id, "OTHER")
                deal_list.append({
                    "time":      datetime.fromtimestamp(d.time).strftime("%m/%d %H:%M"),
                    "ticket":    d.ticket,
                    "symbol":    d.symbol,
                    "profit":    round(d.profit, 2),
                    "agent":     agent,
                    "direction": pos_type.get(d.position_id, ""),
                })

            # Per-agent win/loss from deal history
            agent_wl = {}
            for d in deal_list:
                a = d["agent"]
                if a not in agent_wl:
                    agent_wl[a] = {"wins": 0, "losses": 0, "pnl": 0.0}
                if d["profit"] > 0:
                    agent_wl[a]["wins"] += 1
                else:
                    agent_wl[a]["losses"] += 1
                agent_wl[a]["pnl"] = round(agent_wl[a]["pnl"] + d["profit"], 2)

            total_wins   = sum(v["wins"]   for v in agent_wl.values())
            total_losses = sum(v["losses"] for v in agent_wl.values())
            total_trades = total_wins + total_losses
            overall_wr   = round(total_wins / total_trades * 100, 1) if total_trades else None

            # Today's closed P&L
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today_deals = [d for d in out_deals
                           if datetime.fromtimestamp(d.time) >= today_start]
            daily_closed = round(sum(d.profit for d in today_deals), 2)
            floating_pnl = round(sum(p.profit for p in positions), 2)

            return {
                "connected":      True,
                "balance":        round(acct.balance, 2),
                "equity":         round(acct.equity, 2),
                "free_margin":    round(acct.margin_free, 2),
                "margin_level":   round(acct.margin_level, 1) if acct.margin_level else 0,
                "daily_closed":   daily_closed,
                "floating_pnl":   floating_pnl,
                "daily_pnl":      round(daily_closed + floating_pnl, 2),
                "open_count":     len(open_pos),
                "open_positions": open_pos,
                "balance_curve":  balance_curve,
                "deal_list":      deal_list[-50:],  # last 50 closed trades
                "agent_wl":       agent_wl,
                "session_start":  round(start_bal, 2),
                "overall_winrate": overall_wr,
                "total_wins":     total_wins,
                "total_losses":   total_losses,
            }

        finally:
            mt5.shutdown()

    except Exception as e:
        print(f"[DASHBOARD] MT5 error: {e}")
        return None


# ── Log file stats ─────────────────────────────────────────────────────────────

def get_log_stats() -> dict:
    trades     = []
    executions = []

    try:
        with open("logs/trades.json") as f:
            trades = json.load(f)
    except Exception:
        pass

    try:
        with open("logs/executions.json") as f:
            executions = json.load(f)
    except Exception:
        pass

    # ── Agent stats from trades.json ─────────────────────────────────
    agent_stats = {}
    for entry in trades:
        proposal = entry.get("proposal", {})
        decision = entry.get("decision", {})
        agent    = proposal.get("agent", "UNKNOWN")
        status   = decision.get("status", "UNKNOWN")

        if agent not in agent_stats:
            agent_stats[agent] = {
                "proposals": 0, "approved": 0, "rejected": 0, "executed": 0
            }

        agent_stats[agent]["proposals"] += 1
        if status == "APPROVED":
            agent_stats[agent]["approved"] += 1
        elif status == "REJECTED":
            agent_stats[agent]["rejected"] += 1

    for entry in executions:
        if entry.get("result", {}).get("success"):
            agent = entry.get("proposal", {}).get("agent", "UNKNOWN")
            if agent in agent_stats:
                agent_stats[agent]["executed"] += 1

    # ── Last signal per agent ────────────────────────────────────────
    last_signals = {}
    agents_to_track = {"GOLD", "EURUSD", "GBPUSD", "USDJPY", "DOLLAR"}
    for entry in trades:
        proposal = entry.get("proposal", {})
        decision = entry.get("decision", {})
        agent    = proposal.get("agent", "")
        if agent in agents_to_track:
            ts = proposal.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
            except Exception:
                pass
            last_signals[agent] = {
                "direction":  proposal.get("direction", ""),
                "confidence": proposal.get("confidence", 0),
                "status":     decision.get("status", ""),
                "time":       ts,
            }

    # ── Recent decisions (last 20) ───────────────────────────────────
    recent = []
    for entry in trades[-20:]:
        proposal = entry.get("proposal", {})
        decision = entry.get("decision", {})
        ts = proposal.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
        except Exception:
            pass
        recent.append({
            "time":       ts,
            "agent":      proposal.get("agent", ""),
            "instrument": proposal.get("instrument", ""),
            "direction":  proposal.get("direction", ""),
            "confidence": proposal.get("confidence", 0),
            "status":     decision.get("status", ""),
            "reason":     (decision.get("reason") or "")[:70],
        })
    recent.reverse()

    return {
        "agent_stats":      agent_stats,
        "recent_decisions": recent,
        "last_signals":     last_signals,
        "total_proposals":  len(trades),
        "total_success_exec": sum(
            1 for e in executions if e.get("result", {}).get("success")
        ),
    }


# ── Combined data builder ─────────────────────────────────────────────────────

def build_dashboard_data() -> dict:
    mt5_data  = get_mt5_data()
    log_stats = get_log_stats()

    # Merge win/loss from MT5 into agent_stats from logs
    if mt5_data and mt5_data.get("agent_wl"):
        for agent, wl in mt5_data["agent_wl"].items():
            if agent in log_stats["agent_stats"]:
                log_stats["agent_stats"][agent].update(wl)
            else:
                log_stats["agent_stats"][agent] = {
                    "proposals": 0, "approved": 0, "rejected": 0,
                    "executed": wl.get("wins", 0) + wl.get("losses", 0),
                    **wl
                }

    return {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "mt5":       mt5_data,
        "logs":      log_stats,
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default access log

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("dashboard.html", "text/html; charset=utf-8")
        elif self.path == "/api/data":
            self._serve_json(build_dashboard_data())
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, filename, content_type):
        try:
            with open(filename, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"File not found")
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass  # client disconnected mid-send — normal on Windows

    def _serve_json(self, data):
        content = json.dumps(data, default=str).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass  # client closed connection before response finished — normal on Windows


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import threading
    import webbrowser

    url = f"http://localhost:{PORT}"
    print(f"\n{'='*50}")
    print(f"  APEX Capital AI — Performance Dashboard")
    print(f"{'='*50}")
    print(f"  URL  : {url}")
    print(f"  Data : logs/ + MT5 (if connected)")
    print(f"  Stop : Ctrl+C")
    print(f"{'='*50}\n")

    server = HTTPServer(("localhost", PORT), DashboardHandler)

    def open_browser():
        import time
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[DASHBOARD] Stopped.")


if __name__ == "__main__":
    main()
