"""
MONITOR — Risk Manager / Manager Assistant
APEX Capital AI

Dual-brain position management system.
Runs in a separate thread every 10 seconds.
Delegates to specialist watch agents per instrument.

Brain 1: Claude Sonnet (Anthropic)
Brain 2: DeepSeek V3.2 or GPT-4o (configurable via .env)

Three watch modes:
- Mode 1: Price check every 10 sec (no AI)
- Mode 2: Profit milestone check every 60 sec (dual AI)
- Mode 3: Spike/news detection — immediate (dual AI)

Decisions: HOLD / MOVE_SL / CLOSE
All decisions executed directly by MONITOR on MT5.
Real-time Telegram alerts (separate from cycle report).
"""

import os
import time
import threading
import requests
import anthropic
import MetaTrader5 as mt5
from datetime import datetime
from dotenv import load_dotenv

from .gold_watch   import GoldWatch
from .eurusd_watch import EURUSDWatch
from .gbpusd_watch import GBPUSDWatch
from .usdjpy_watch import USDJPYWatch

# Import close helpers from executor (lazy import inside methods to avoid cycles)
# from mt5_executor import close_position, close_all_positions

load_dotenv()

MAGIC          = 20250401
WATCH_INTERVAL = 10   # seconds between price checks


class SecondBrain:
    """
    Wrapper for the second AI brain (DeepSeek or OpenAI).
    Uses OpenAI-compatible API format for both providers.
    Switch provider via SECOND_BRAIN_PROVIDER in .env.
    """

    def __init__(self):
        provider = os.getenv("SECOND_BRAIN_PROVIDER", "deepseek").lower()

        if provider == "openai":
            self.api_key  = os.getenv("OPENAI_API_KEY", "")
            self.base_url = "https://api.openai.com/v1/chat/completions"
            self.model    = os.getenv("SECOND_BRAIN_MODEL", "gpt-4o")
            self.name     = "GPT-4o"
        else:
            self.api_key  = os.getenv("DEEPSEEK_API_KEY", "")
            self.base_url = "https://api.deepseek.com/v1/chat/completions"
            self.model    = os.getenv("SECOND_BRAIN_MODEL", "deepseek-chat")
            self.name     = "DeepSeek"

    def ask(self, system: str, user: str) -> str:
        if not self.api_key:
            return '{"decision": "HOLD", "reason": "Second brain API key not set", "confidence": 0}'
        try:
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       self.model,
                    "messages":    [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "max_tokens":  300,
                    "temperature": 0.1,
                },
                timeout=15,
            )
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f'{{"decision": "HOLD", "reason": "Second brain error: {str(e)[:50]}", "confidence": 0}}'


class MonitorAgent:
    """
    MONITOR — Risk Manager / Manager Assistant

    Runs in a separate background thread.
    Activates specialist watch agents only when positions are open.
    Executes position management decisions directly on MT5.
    Sends real-time Telegram alerts.
    """
    NAME = "MONITOR"

    def __init__(self, manager_agent):
        self.manager      = manager_agent
        self.claude       = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.second_brain = SecondBrain()
        self.running      = False

        # Specialist watch agents — single brain (Claude Sonnet)
        self.watchers = {
            "XAUUSD": GoldWatch(self.claude),
            "EURUSD": EURUSDWatch(self.claude),
            "GBPUSD": GBPUSDWatch(self.claude),
            "USDJPY": USDJPYWatch(self.claude),
        }

        print(f"[{self.NAME}] Initialized")
        print(f"[{self.NAME}] Brain   : Claude Sonnet (single brain mode)")
        print(f"[{self.NAME}] Watchers: GOLD_WATCH | EURUSD_WATCH | GBPUSD_WATCH | USDJPY_WATCH")
        print(f"[{self.NAME}] Spike thresholds:")
        print(f"[{self.NAME}]   XAUUSD: ${float(os.getenv('SPIKE_XAUUSD', 15.0)):.1f} move in 1 M1 candle")
        print(f"[{self.NAME}]   EURUSD: {float(os.getenv('SPIKE_EURUSD', 0.003))*10000:.1f} pips in 1 M1 candle")
        print(f"[{self.NAME}]   GBPUSD: {float(os.getenv('SPIKE_GBPUSD', 0.004))*10000:.1f} pips in 1 M1 candle")
        print(f"[{self.NAME}]   USDJPY: {float(os.getenv('SPIKE_USDJPY', 0.80))/0.01:.1f} pips in 1 M1 candle")

    # ── Telegram ──────────────────────────────────────────────────

    def _telegram(self, message: str):
        """Real-time alert — separate from cycle report."""
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

    # ── MT5 — Get open APEX positions ────────────────────────────

    def _get_open_positions(self) -> list:
        """Get all positions opened by the bot (magic == MAGIC)."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        mt5.shutdown()
        time.sleep(0.3)
        if not mt5.initialize(login=login, password=password, server=server):
            return []
        try:
            positions = mt5.positions_get() or []
            return [p for p in positions if p.magic == MAGIC]
        except Exception:
            return []
        finally:
            mt5.shutdown()

    # ── MT5 — Execute decisions ───────────────────────────────────

    def _execute_close(self, symbol: str, ticket: int,
                       pnl: float) -> bool:
        """Close a position by ticket."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        mt5.shutdown()
        time.sleep(0.3)
        if not mt5.initialize(login=login, password=password, server=server):
            return False
        try:
            positions = mt5.positions_get(symbol=symbol)
            if not positions:
                return False

            pos        = next((p for p in positions if p.ticket == ticket), None)
            if not pos:
                return False

            tick       = mt5.symbol_info_tick(symbol)
            close_price= tick.bid if pos.type == 0 else tick.ask
            sym_info   = mt5.symbol_info(symbol)
            filling    = mt5.ORDER_FILLING_IOC
            if sym_info and sym_info.filling_mode & 1:
                filling = mt5.ORDER_FILLING_FOK

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
                "position":     ticket,
                "price":        close_price,
                "deviation":    20,
                "magic":        MAGIC,
                "comment":      "MONITOR_CLOSE",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[{self.NAME}] ✅ CLOSED #{ticket} @ {close_price} | P&L: ${pnl:+.2f}")
                return True
            else:
                code = result.retcode if result else "None"
                print(f"[{self.NAME}] ❌ Close failed: {code}")
                return False
        except Exception as e:
            print(f"[{self.NAME}] Close error: {e}")
            return False
        finally:
            mt5.shutdown()

    def _execute_move_sl(self, symbol: str, ticket: int,
                         new_sl: float, cur_tp: float) -> bool:
        """Move stop loss for a position."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        mt5.shutdown()
        time.sleep(0.3)
        if not mt5.initialize(login=login, password=password, server=server):
            return False
        try:
            request = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   symbol,
                "position": ticket,
                "sl":       float(new_sl),
                "tp":       float(cur_tp),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[{self.NAME}] ✅ SL MOVED to {new_sl}")
                return True
            else:
                code = result.retcode if result else "None"
                print(f"[{self.NAME}] ❌ SL move failed: {code}")
                return False
        except Exception as e:
            print(f"[{self.NAME}] SL move error: {e}")
            return False
        finally:
            mt5.shutdown()

    def _execute_move_sl_tp(self, symbol: str, ticket: int,
                            new_sl: float, new_tp: float) -> bool:
        """
        Trail BOTH stop loss and take profit simultaneously.
        Used on favorable spikes to lock profit AND extend the target.
        """
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        mt5.shutdown()
        time.sleep(0.3)
        if not mt5.initialize(login=login, password=password, server=server):
            return False
        try:
            request = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   symbol,
                "position": ticket,
                "sl":       float(new_sl),
                "tp":       float(new_tp),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[{self.NAME}] ✅ SL+TP TRAILED — "
                      f"SL→{new_sl} | TP→{new_tp}")
                return True
            else:
                code = result.retcode if result else "None"
                print(f"[{self.NAME}] ❌ SL+TP trail failed: {code}")
                return False
        except Exception as e:
            print(f"[{self.NAME}] SL+TP trail error: {e}")
            return False
        finally:
            mt5.shutdown()

    # ── Process a decision ────────────────────────────────────────

    def _process_decision(self, decision: dict, pos):
        action     = decision.get("decision", "HOLD")
        symbol     = decision.get("symbol")
        ticket     = decision.get("ticket")
        pnl        = decision.get("pnl", 0)
        trigger    = decision.get("trigger", "")
        reason     = decision.get("reason", "")
        spike_type = decision.get("spike_type", "")
        sign       = "+" if pnl >= 0 else ""

        print(f"\n[{self.NAME}] ── DECISION ─────────────────────────────")
        print(f"[{self.NAME}] Action    : {action}")
        print(f"[{self.NAME}] Ticket    : #{ticket} {symbol}")
        print(f"[{self.NAME}] Trigger   : {trigger}")
        print(f"[{self.NAME}] Spike type: {spike_type}")
        print(f"[{self.NAME}] Reason    : {reason}")
        print(f"[{self.NAME}] P&L       : {sign}${pnl}")

        if action == "HOLD":
            return

        if action == "CLOSE":
            success = self._execute_close(symbol, ticket, pnl)
            if success:
                icon = "🔴" if spike_type == "ADVERSE" else "🔴"
                self._telegram(
                    f"<b>{icon} POSITION CLOSED</b>\n"
                    f"#{ticket} {symbol}\n"
                    f"P&amp;L   : {sign}${pnl}\n"
                    f"Trigger: {trigger}\n"
                    f"Reason : {reason[:120]}"
                )

        elif action == "MOVE_SL":
            new_sl = decision.get("new_sl")
            if not new_sl:
                print(f"[{self.NAME}] No new SL price provided")
                return
            success = self._execute_move_sl(symbol, ticket, new_sl, pos.tp)
            if success:
                self._telegram(
                    f"<b>🛡️ STOP LOSS MOVED</b>\n"
                    f"#{ticket} {symbol}\n"
                    f"New SL : {new_sl}\n"
                    f"P&amp;L   : {sign}${pnl}\n"
                    f"Trigger: {trigger}\n"
                    f"Reason : {reason[:120]}"
                )

        elif action == "MOVE_SL_TP":
            # Favorable spike — trail both SL and TP together
            new_sl = decision.get("new_sl")
            new_tp = decision.get("new_tp")
            if not new_sl or not new_tp:
                print(f"[{self.NAME}] MOVE_SL_TP missing prices — skipping")
                return
            success = self._execute_move_sl_tp(symbol, ticket, new_sl, new_tp)
            if success:
                self._telegram(
                    f"<b>🚀 SL+TP TRAILED (favorable spike)</b>\n"
                    f"#{ticket} {symbol}\n"
                    f"New SL : {new_sl} (profit locked)\n"
                    f"New TP : {new_tp} (target extended)\n"
                    f"P&amp;L   : {sign}${pnl}\n"
                    f"Trigger: {trigger}\n"
                    f"Reason : {reason[:120]}"
                )

    # ── Main watch loop ───────────────────────────────────────────

    def watch_loop(self):
        """
        Background thread — runs every 10 seconds.
        Activates specialist watchers only when positions are open.
        """
        print(f"\n[{self.NAME}] ⚡ Watch loop started — "
              f"checking every {WATCH_INTERVAL}s")

        while self.running:
            try:
                positions = self._get_open_positions()

                if not positions:
                    time.sleep(WATCH_INTERVAL)
                    continue

                print(f"[{self.NAME}] 👁️  Watching {len(positions)} position(s): "
                      f"{', '.join(set(p.symbol for p in positions))}")

                # Connect MT5 once for all watchers this cycle
                login    = int(os.getenv("MT5_LOGIN", 0))
                password = os.getenv("MT5_PASSWORD", "")
                server   = os.getenv("MT5_SERVER", "")

                mt5.shutdown()
                time.sleep(0.3)

                if not mt5.initialize(login=login, password=password,
                                      server=server):
                    print(f"[{self.NAME}] MT5 connect failed — retrying")
                    time.sleep(WATCH_INTERVAL)
                    continue

                # Run each watcher
                for pos in positions:
                    symbol  = pos.symbol
                    watcher = self.watchers.get(symbol)
                    if not watcher:
                        continue

                    try:
                        decision = watcher.watch(pos)
                    except Exception as e:
                        print(f"[{self.NAME}] {symbol} watcher error: {e}")
                        decision = None

                    if decision and decision.get("decision") != "HOLD":
                        mt5.shutdown()
                        self._process_decision(decision, pos)
                        # Reconnect for remaining positions
                        time.sleep(0.3)
                        mt5.initialize(login=login, password=password,
                                       server=server)

                mt5.shutdown()

            except Exception as e:
                print(f"[{self.NAME}] Watch loop error: {e}")
                try:
                    mt5.shutdown()
                except Exception:
                    pass

            time.sleep(WATCH_INTERVAL)

        print(f"[{self.NAME}] Watch loop stopped.")

    # ── Telegram command listener ─────────────────────────────────

    def _cmd_positions(self):
        """Reply with all open APEX positions + floating P&L."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")
        mt5.shutdown()
        time.sleep(0.2)
        if not mt5.initialize(login=login, password=password, server=server):
            self._telegram("❌ MT5 connection failed.")
            return
        try:
            positions = mt5.positions_get() or []
            apex = [p for p in positions if p.magic == MAGIC]
            if not apex:
                self._telegram("📭 No open APEX positions.")
                return
            lines     = []
            total_pnl = 0.0
            for p in apex:
                direction = "LONG" if p.type == 0 else "SHORT"
                sign      = "+" if p.profit >= 0 else ""
                comment   = p.comment or "—"
                lines.append(
                    f"<b>#{p.ticket}</b> {direction} {p.symbol} "
                    f"{p.volume}lot [{comment}]\n"
                    f"   Entry: {p.price_open:.5f} | "
                    f"SL: {p.sl:.5f} | TP: {p.tp:.5f}\n"
                    f"   Float: {sign}${p.profit:.2f}"
                )
                total_pnl += p.profit
            total_sign = "+" if total_pnl >= 0 else ""
            msg = (f"<b>📊 APEX Open Positions ({len(apex)})</b>\n\n"
                   + "\n\n".join(lines)
                   + f"\n\n<b>Total floating: {total_sign}${total_pnl:.2f}</b>")
            self._telegram(msg)
        finally:
            mt5.shutdown()

    def _cmd_close_ticket(self, ticket: int):
        """Close a specific position by ticket."""
        from mt5_executor import close_position
        self._telegram(f"⏳ Closing #{ticket}...")
        result = close_position(ticket, reason="APEX_CMD_CLOSE")
        self._telegram(result["message"])

    def _cmd_close_all(self):
        """Close all APEX positions."""
        from mt5_executor import close_all_positions
        # Quick count first
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")
        mt5.shutdown()
        time.sleep(0.2)
        if not mt5.initialize(login=login, password=password, server=server):
            self._telegram("❌ MT5 connection failed.")
            return
        try:
            all_pos = mt5.positions_get() or []
            apex    = [p for p in all_pos if p.magic == MAGIC]
        finally:
            mt5.shutdown()

        if not apex:
            self._telegram("📭 No open APEX positions to close.")
            return

        self._telegram(f"⏳ Closing all {len(apex)} APEX position(s)...")
        results = close_all_positions(reason="APEX_CMD_CLOSE_ALL")

        closed    = sum(1 for r in results if r.get("success"))
        total_pnl = sum(r.get("pnl", 0) for r in results if r.get("success"))
        sign      = "+" if total_pnl >= 0 else ""
        lines     = [r["message"] for r in results]
        summary   = (f"<b>🔴 Close All Done</b>\n"
                     + "\n".join(lines)
                     + f"\n\n<b>{closed}/{len(results)} closed | "
                     + f"Net P&amp;L: {sign}${total_pnl:.2f}</b>")
        self._telegram(summary)

    def _cmd_status(self):
        """Quick bot status reply."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")
        mt5.shutdown()
        time.sleep(0.2)
        n_open = 0
        balance = 0.0
        equity  = 0.0
        if mt5.initialize(login=login, password=password, server=server):
            try:
                info    = mt5.account_info()
                balance = info.balance if info else 0.0
                equity  = info.equity  if info else 0.0
                pos     = mt5.positions_get() or []
                n_open  = sum(1 for p in pos if p.magic == MAGIC)
            finally:
                mt5.shutdown()
        floating = round(equity - balance, 2)
        sign     = "+" if floating >= 0 else ""
        self._telegram(
            f"<b>🤖 APEX Status</b>\n"
            f"Watch loop  : {'✅ Running' if self.running else '❌ Stopped'}\n"
            f"Open pos.   : {n_open}\n"
            f"Balance     : ${balance:,.2f}\n"
            f"Equity      : ${equity:,.2f}\n"
            f"Floating    : {sign}${floating:.2f}\n"
            f"Time (UTC)  : {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        )

    def _telegram_command_listener(self):
        """
        Background thread — polls Telegram for manual commands.

        Commands (send from your authorized Telegram chat):
          /positions       — list all open APEX positions with P&L
          /close <ticket>  — close a specific position by ticket number
          /closeall        — close ALL open APEX positions immediately
          /status          — quick account + bot status
        """
        token   = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = str(os.getenv("TELEGRAM_CHAT_ID", ""))
        if not token or not chat_id:
            print(f"[{self.NAME}] Telegram credentials missing — command listener disabled.")
            return

        base_url       = f"https://api.telegram.org/bot{token}"
        last_update_id = 0

        print(f"[{self.NAME}] 📡 Telegram command listener started.")
        print(f"[{self.NAME}]    Commands: /positions /close <ticket> /closeall /status")

        # Drain any old messages so we don't replay commands from before startup
        try:
            resp = requests.get(f"{base_url}/getUpdates",
                                params={"offset": -1}, timeout=10)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                last_update_id = data["result"][-1]["update_id"]
        except Exception:
            pass

        while self.running:
            try:
                resp = requests.get(
                    f"{base_url}/getUpdates",
                    params={"offset": last_update_id + 1, "timeout": 20},
                    timeout=30,
                )
                data = resp.json()
                if not data.get("ok"):
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    last_update_id = update["update_id"]

                    msg  = update.get("message", {})
                    text = msg.get("text", "").strip()
                    from_id = str(msg.get("chat", {}).get("id", ""))

                    # Only accept commands from the authorized chat
                    if from_id != chat_id:
                        continue
                    if not text.startswith("/"):
                        continue

                    parts = text.split()
                    cmd   = parts[0].lower()

                    print(f"[{self.NAME}] 📥 Command: {text}")

                    if cmd in ("/positions", "/pos"):
                        self._cmd_positions()
                    elif cmd in ("/closeall", "/close_all"):
                        self._cmd_close_all()
                    elif cmd == "/close":
                        if len(parts) < 2:
                            self._telegram("❌ Usage: <code>/close 12345678</code>")
                        else:
                            try:
                                ticket = int(parts[1])
                                self._cmd_close_ticket(ticket)
                            except ValueError:
                                self._telegram("❌ Invalid ticket. Usage: <code>/close 12345678</code>")
                    elif cmd in ("/status", "/ping"):
                        self._cmd_status()
                    else:
                        self._telegram(
                            f"❓ Unknown command: <code>{cmd}</code>\n\n"
                            f"Available:\n"
                            f"/positions — open positions + P&amp;L\n"
                            f"/close &lt;ticket&gt; — close one position\n"
                            f"/closeall — close all positions\n"
                            f"/status — account + bot status"
                        )

            except Exception as e:
                print(f"[{self.NAME}] Command listener error: {e}")
                time.sleep(10)

        print(f"[{self.NAME}] Command listener stopped.")

    # ── Start / Stop ──────────────────────────────────────────────

    def start(self):
        self.running = True

        # Watch loop — position monitoring every 10 sec
        self.watch_thread = threading.Thread(
            target=self.watch_loop,
            daemon=True,
            name="MONITOR_WATCH"
        )
        self.watch_thread.start()

        # Command listener — Telegram commands (/positions, /close, etc.)
        self.cmd_thread = threading.Thread(
            target=self._telegram_command_listener,
            daemon=True,
            name="MONITOR_CMD"
        )
        self.cmd_thread.start()

        print(f"[{self.NAME}] Started — watch loop + Telegram command listener.")

    def stop(self):
        self.running = False
        print(f"[{self.NAME}] Stopping watch loop + command listener...")
