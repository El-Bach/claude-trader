"""
main.py — APEX Capital AI Entry Point
======================================
TWO SEPARATE LOOPS:

Loop 1 — MAIN LOOP (every 15 min)
  NEWS → TRACKER → MANAGER → DOLLAR → entry agents → new trades

Loop 2 — WATCH LOOP (every 10 sec) ← separate thread
  MONITOR → GOLD_WATCH / EURUSD_WATCH / GBPUSD_WATCH / USDJPY_WATCH

Full team:
  MANAGER  — CEO, capital manager
  NEWS     — news & sentiment analyst (runs FIRST)
  TRACKER  — performance analyst
  DOLLAR   — macro compass
  GOLD     — XAUUSD entry
  EURUSD   — EURUSD entry
  GBPUSD   — GBPUSD entry (Cable)
  USDJPY   — USDJPY entry
  MONITOR  — position risk manager (watch loop)

Usage:
    python main.py          # One cycle
    python main.py --loop   # Both loops 24/7
    python main.py --demo   # No MT5
    python main.py --watch  # Watch loop only
"""

import os
import sys
import time
import argparse
import traceback
from datetime import datetime
from dotenv import load_dotenv

# Windows: reconfigure stdout/stderr to UTF-8 so emoji print() calls don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

from agents.dollar     import DollarAgent
from agents.manager    import ManagerAgent
from agents.gold       import GoldAgent
from agents.eurusd     import EURUSDAgent
from agents.gbpusd     import GBPUSDAgent
from agents.usdjpy     import USDJPYAgent
from agents.monitor    import MonitorAgent
from agents.news       import NewsAgent
from agents.tracker    import TrackerAgent
from agents.strategist import StrategistAgent
from mt5_executor      import execute_trade

CYCLE_INTERVAL_MINUTES = 15
TEAM_NAME = "APEX Capital AI"


def execute_on_mt5(proposal: dict, decision: dict) -> bool:
    result = execute_trade(proposal, decision)
    return result["success"]


def run_cycle(manager, dollar, gold, eurusd, gbpusd, usdjpy,
              news_agent, tracker):

    print(f"\n{'='*60}")
    print(f"  {TEAM_NAME} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    manager.reset_cycle_report()

    # ── STEP 1: Read real account ────────────────────────────────
    if not manager.refresh_account():
        print(f"[MANAGER] ⚠️  MT5 read failed — skipping.")
        manager.send_cycle_report()
        return

    if manager.is_halted():
        print(f"\n[MANAGER] ⛔ TEAM HALTED.")
        manager.add_alert("TEAM HALTED — daily loss limit reached")
        manager.send_cycle_report()
        return

    # ── STEP 2: NEWS analyses FIRST ──────────────────────────────
    news_broadcast = news_agent.analyse()
    manager.receive_news_broadcast(news_broadcast)
    # cycle_report["news"] is set inside receive_news_broadcast — full broadcast, all fields

    # Block all new entries if NEWS says HIGH/CRITICAL risk
    if news_broadcast.get("block_new_entries", False):
        risk = news_broadcast.get("risk_level", "HIGH")
        reason = news_broadcast.get("risk_reason", "High risk event")
        print(f"\n[MANAGER] 🚨 NEWS BLOCK — {risk}: {reason}")
        print(f"[MANAGER] All new entries blocked this cycle.")
        manager.add_alert(f"NEWS BLOCK ({risk}): {reason}")
        manager.send_cycle_report()
        tracker.analyse()
        return

    # ── STEP 3: TRACKER feedback ─────────────────────────────────
    tracker_feedback = tracker.get_manager_feedback()
    if tracker_feedback.get("alerts"):
        for alert in tracker_feedback["alerts"]:
            print(f"[TRACKER] ⚡ {alert}")
            manager.add_alert(f"TRACKER: {alert}")

    # ── STEP 4: DOLLAR analyses ──────────────────────────────────
    dollar_broadcast = dollar.analyse(news_broadcast)

    manager.receive_dollar_broadcast(dollar_broadcast)

    # ── STEP 5: DOLLAR trade proposal ───────────────────────────
    if dollar_broadcast.get("trade_proposal"):
        proposal = dollar_broadcast["trade_proposal"]
        decision = manager.evaluate_proposal(proposal)
        dollar.on_atlas_decision(decision)
        if decision["status"] == "APPROVED":
            success = execute_on_mt5(proposal, decision)
            if success:
                manager.record_execution(proposal,
                                         decision["lot_size_approved"])
            else:
                manager.add_alert("DOLLAR order FAILED — check logs")
    else:
        manager.record_agent_result("DOLLAR", "NO TRADE", "No setup")

    # ── STEP 6: Specialists receive broadcasts ───────────────────
    for agent in [gold, eurusd, gbpusd, usdjpy]:
        agent.receive_dollar_broadcast(dollar_broadcast)

    # ── STEP 7: Specialists analyse ─────────────────────────────
    specialists = [("GOLD", gold), ("EURUSD", eurusd), ("GBPUSD", gbpusd), ("USDJPY", usdjpy)]
    proposals   = []

    for name, agent in specialists:
        try:
            proposal = agent.analyse()
            if proposal:
                # Attach news context to proposal
                proposal["news_risk"]    = news_broadcast.get("risk_level")
                proposal["news_sentiment"]= news_broadcast.get("sentiment")
                proposals.append((name, agent, proposal))
            else:
                manager.record_agent_result(name, "NO TRADE", "No setup")
        except Exception as e:
            traceback.print_exc()
            manager.record_agent_result(name, "ERROR", str(e)[:60])

    manager.update_nasdaq_performance(eurusd.intraday_pct)

    # All agents proposing = possible news event
    if len(proposals) >= 4:
        print(f"\n[MANAGER] ⚠️  All agents proposing — possible event.")
        manager.add_alert("All agents proposing simultaneously — skipped")
        for name, agent, _ in proposals:
            agent.on_atlas_decision({
                "status": "REJECTED",
                "reason": "All agents simultaneously active.",
                "lot_size_approved": 0
            })
            manager.record_agent_result(name, "REJECTED",
                                        "All agents active simultaneously")
        manager.send_cycle_report()
        tracker.analyse()
        return

    # ── STEP 8: Evaluate proposals ───────────────────────────────
    for name, agent, proposal in proposals:
        decision = manager.evaluate_proposal(proposal)
        agent.on_atlas_decision(decision)
        if decision["status"] == "APPROVED":
            success = execute_on_mt5(proposal, decision)
            if success:
                manager.record_execution(proposal,
                                         decision["lot_size_approved"])
            else:
                manager.add_alert(f"{name} order FAILED — check logs")

    # ── STEP 9: Send consolidated Telegram report ────────────────
    manager.send_cycle_report()

    # ── STEP 10: TRACKER analyses performance ───────────────────
    tracker.analyse()
    manager.session_summary()


def main():
    parser = argparse.ArgumentParser(description=TEAM_NAME)
    parser.add_argument("--loop",  action="store_true",
                        help="Run both loops 24/7")
    parser.add_argument("--demo",  action="store_true",
                        help="No MT5")
    parser.add_argument("--watch", action="store_true",
                        help="Watch loop only")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  {TEAM_NAME} — Starting up")
    print(f"{'='*60}")

    # ── Initialize all agents ────────────────────────────────────
    manager     = ManagerAgent()
    dollar      = DollarAgent()
    gold        = GoldAgent()
    eurusd      = EURUSDAgent()
    gbpusd      = GBPUSDAgent()
    usdjpy      = USDJPYAgent()
    monitor     = MonitorAgent(manager)
    news_agent  = NewsAgent()
    tracker     = TrackerAgent()
    strategist  = StrategistAgent()

    print(f"\n[SYSTEM] Team online:")
    print(f"  MANAGER    — Capital Manager       (Claude Opus)")
    print(f"  NEWS       — News & Sentiment       (rule-based)")
    print(f"  TRACKER    — Performance Analyst    (Log + MT5)")
    print(f"  STRATEGIST — Daily Top-Down Analyst (Claude Opus, once/day)")
    print(f"  DOLLAR     — US Dollar / DXY        (Claude Sonnet)")
    print(f"  GOLD       — Gold / XAUUSD          (Claude Sonnet)")
    print(f"  EURUSD     — Euro / US Dollar       (Claude Sonnet)")
    print(f"  GBPUSD     — British Pound / USD    (Claude Sonnet)")
    print(f"  USDJPY     — US Dollar / Yen        (Claude Sonnet)")
    print(f"  MONITOR    — Risk Manager           "
          f"(Claude Sonnet + {monitor.second_brain.name})")

    # ── Entry agents dict — for STRATEGIST plan distribution ─────
    entry_agents = {
        "GOLD":   gold,
        "EURUSD": eurusd,
        "GBPUSD": gbpusd,
        "USDJPY": usdjpy,
    }

    if args.demo:
        print(f"\n[SYSTEM] Demo mode — no MT5.")
        return

    if args.watch:
        print(f"\n[SYSTEM] Watch-only mode.")
        monitor.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop()
            print(f"\n[SYSTEM] Stopped.")
        return

    if args.loop:
        print(f"\n[SYSTEM] Full loop mode — "
              f"main every {CYCLE_INTERVAL_MINUTES} min, "
              f"watch every 10 sec.")

        if manager.refresh_account():
            manager.send_startup_message()

        # ── Run STRATEGIST once at startup ────────────────────────
        print(f"\n[SYSTEM] Running STRATEGIST — initial daily analysis...")
        try:
            strategist.run_daily()
            strategist.distribute_plans(entry_agents)
            tg_token = os.getenv("TELEGRAM_TOKEN", "")
            tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "")
            if tg_token and tg_chat:
                strategist.send_telegram_summary(tg_token, tg_chat)
        except Exception as e:
            print(f"[SYSTEM] STRATEGIST startup error: {e}")

        # Start WATCH LOOP in background
        monitor.start()

        # Track which date we already sent the session-end report for
        session_report_sent_date   = None
        strategy_sent_date         = None

        # Start MAIN LOOP in foreground
        while True:
            try:
                # ── STRATEGIST: run once per day at 07:00 UTC ─────
                now_utc = datetime.utcnow()
                today   = now_utc.date()
                if (now_utc.hour >= 7
                        and strategy_sent_date != today):
                    print(f"\n[SYSTEM] Running STRATEGIST — daily analysis...")
                    try:
                        strategist.run_daily()
                        strategist.distribute_plans(entry_agents)
                        tg_token = os.getenv("TELEGRAM_TOKEN", "")
                        tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "")
                        if tg_token and tg_chat:
                            strategist.send_telegram_summary(tg_token, tg_chat)
                    except Exception as e:
                        print(f"[SYSTEM] STRATEGIST daily run error: {e}")
                    strategy_sent_date = today

                run_cycle(manager, dollar, gold, eurusd, gbpusd, usdjpy,
                          news_agent, tracker)

                # ── Auto session-end report at NY close (19:00 UTC = 22:00 Beirut) ──
                now_utc = datetime.utcnow()
                today   = now_utc.date()
                if (now_utc.hour >= 19
                        and session_report_sent_date != today):
                    print(f"\n[SYSTEM] NY session closed — sending session report...")
                    tracker.send_session_report(
                        account=manager.account,
                        session_start_balance=manager.session_start_balance,
                    )
                    session_report_sent_date = today

                print(f"\n[SYSTEM] Next cycle in "
                      f"{CYCLE_INTERVAL_MINUTES} min...")
                time.sleep(CYCLE_INTERVAL_MINUTES * 60)
            except KeyboardInterrupt:
                print(f"\n[SYSTEM] Shutdown by user.")
                monitor.stop()
                tracker.send_session_report(
                    account=manager.account,
                    session_start_balance=manager.session_start_balance,
                )
                manager.send_daily_summary()
                break
            except Exception as e:
                print(f"\n[SYSTEM] Error: {e}")
                time.sleep(CYCLE_INTERVAL_MINUTES * 60)
    else:
        # Single cycle — run STRATEGIST first then the cycle
        print(f"\n[SYSTEM] Running STRATEGIST — daily analysis...")
        try:
            strategist.run_daily()
            strategist.distribute_plans(entry_agents)
        except Exception as e:
            print(f"[SYSTEM] STRATEGIST error: {e}")
        run_cycle(manager, dollar, gold, eurusd, gbpusd, usdjpy,
                  news_agent, tracker)


if __name__ == "__main__":
    main()
