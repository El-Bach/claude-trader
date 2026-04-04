"""
mt5_executor.py — Live MT5 Trade Execution
APEX Capital AI

Handles all live order placement.
Called by main.py after MANAGER approves a proposal.

Features:
- Pre-execution validation (symbol, margin, duplicate, price drift)
- SL/TP price validation and rounding
- Filling mode auto-detection (IOC → RETURN fallback)
- Post-execution position verification
- Full error logging
- Never crashes the bot on failure
"""

import os
import json
import MetaTrader5 as mt5
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

MAGIC          = 20250401
MAX_PRICE_DRIFT_ATR = 0.5   # If price moved > 0.5 ATR since analysis → skip
LOG_PATH       = "logs/executions.json"


# ================================================================ #
#  CONNECT / DISCONNECT
# ================================================================ #

def _connect() -> bool:
    login    = int(os.getenv("MT5_LOGIN", 0))
    password = os.getenv("MT5_PASSWORD", "")
    server   = os.getenv("MT5_SERVER", "")
    if not mt5.initialize(login=login, password=password, server=server):
        print(f"[MT5] Init failed: {mt5.last_error()}")
        return False
    if mt5.account_info() is None:
        print(f"[MT5] Login failed")
        mt5.shutdown()
        return False
    return True


# ================================================================ #
#  VALIDATION CHECKS
# ================================================================ #

def _check_symbol(symbol: str) -> tuple[bool, str, object]:
    """Verify symbol exists and is tradeable."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, f"Symbol {symbol} not found", None
    if not info.visible:
        # Try to add it to market watch
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    if info is None:
        return False, f"Symbol {symbol} not available", None
    return True, "", info


def _check_margin(symbol: str, lot: float,
                  order_type: int) -> tuple[bool, str]:
    """Check if account has enough free margin."""
    account = mt5.account_info()
    if account is None:
        return False, "Cannot read account info"

    # Calculate required margin
    margin = mt5.order_calc_margin(order_type, symbol, lot,
                                   mt5.symbol_info_tick(symbol).ask)
    if margin is None:
        return True, ""   # Skip check if calc fails

    free = account.margin_free
    if free < margin * 1.5:   # Require 150% of margin as buffer
        return False, (f"Insufficient margin: need ${margin:.2f}, "
                      f"free ${free:.2f}")
    return True, ""


def _check_duplicate(symbol: str, direction: str) -> tuple[bool, str]:
    """Check if bot already has an open position on this symbol+direction."""
    positions = mt5.positions_get(symbol=symbol)
    if positions:
        for p in positions:
            if p.magic != MAGIC:
                continue
            existing_dir = "LONG" if p.type == 0 else "SHORT"
            if existing_dir == direction:
                return False, (f"Already have {existing_dir} {symbol} "
                              f"open (#{p.ticket})")
    return True, ""


def _check_price_drift(symbol: str, entry_price: float,
                       atr: float) -> tuple[bool, str]:
    """Check price hasn't moved too far since analysis."""
    if entry_price <= 0 or atr <= 0:
        return True, ""   # Skip if no reference price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return True, ""
    current = (tick.ask + tick.bid) / 2
    drift   = abs(current - entry_price)
    if drift > MAX_PRICE_DRIFT_ATR * atr:
        return False, (f"Price drifted {drift:.5f} "
                      f"(>{MAX_PRICE_DRIFT_ATR}x ATR={atr:.5f}) "
                      f"since analysis. Skipping.")
    return True, ""


# ================================================================ #
#  SL/TP VALIDATION
# ================================================================ #

def _validate_sl_tp(symbol: str, order_type: int,
                    price: float, sl: float, tp: float,
                    sym_info) -> tuple[float, float, str]:
    """
    Validate and correct SL/TP prices.
    Returns (corrected_sl, corrected_tp, warning_message)
    """
    digits      = sym_info.digits
    stops_level = sym_info.trade_stops_level * sym_info.point
    warning     = ""

    # Round to correct decimals
    price = round(price, digits)
    sl    = round(sl, digits)    if sl > 0 else 0.0
    tp    = round(tp, digits)    if tp > 0 else 0.0

    is_buy = (order_type == mt5.ORDER_TYPE_BUY)

    # Validate SL direction
    if sl > 0:
        if is_buy and sl >= price:
            sl = round(price - stops_level * 2, digits)
            warning += f"SL corrected (was above price for BUY). "
        elif not is_buy and sl <= price:
            sl = round(price + stops_level * 2, digits)
            warning += f"SL corrected (was below price for SELL). "

    # Validate TP direction
    if tp > 0:
        if is_buy and tp <= price:
            tp = 0.0
            warning += f"TP removed (was below price for BUY). "
        elif not is_buy and tp >= price:
            tp = 0.0
            warning += f"TP removed (was above price for SELL). "

    # Check minimum stop distance
    if sl > 0:
        if is_buy and (price - sl) < stops_level:
            sl = round(price - stops_level * 2, digits)
            warning += f"SL adjusted to meet min stop distance. "
        elif not is_buy and (sl - price) < stops_level:
            sl = round(price + stops_level * 2, digits)
            warning += f"SL adjusted to meet min stop distance. "

    return sl, tp, warning


# ================================================================ #
#  ORDER SENDING WITH FILLING MODE FALLBACK
# ================================================================ #

def _send_order(request: dict) -> object:
    """
    Auto-detect correct filling mode for this broker.
    Tries FOK → IOC → RETURN until one works.
    MetaQuotes demo typically uses FOK.
    """
    # First: detect broker's supported filling mode from symbol info
    sym_info = mt5.symbol_info(request["symbol"])
    if sym_info is not None:
        filling_mode = sym_info.filling_mode
        # filling_mode is a bitmask: 1=FOK, 2=IOC, 4=RETURN
        if filling_mode & 1:    # FOK supported
            request["type_filling"] = mt5.ORDER_FILLING_FOK
        elif filling_mode & 2:  # IOC supported
            request["type_filling"] = mt5.ORDER_FILLING_IOC
        else:                   # RETURN
            request["type_filling"] = mt5.ORDER_FILLING_RETURN
        
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return result

    # Fallback: try all modes in order
    for mode_name, mode in [
        ("FOK",    mt5.ORDER_FILLING_FOK),
        ("IOC",    mt5.ORDER_FILLING_IOC),
        ("RETURN", mt5.ORDER_FILLING_RETURN),
    ]:
        request["type_filling"] = mode
        result = mt5.order_send(request)
        if result is None:
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] Order filled using {mode_name} mode")
            return result
        if result.retcode not in (mt5.TRADE_RETCODE_INVALID_FILL, 10030):
            # Failed for a different reason — return this result
            return result
        print(f"[MT5] {mode_name} fill mode rejected — trying next...")

    return result


# ================================================================ #
#  POST-EXECUTION VERIFICATION
# ================================================================ #

def _verify_position(symbol: str, ticket: int) -> bool:
    """Verify the position actually opened after order sent."""
    positions = mt5.positions_get(symbol=symbol)
    if positions:
        for p in positions:
            if p.ticket == ticket and p.magic == MAGIC:
                return True
    return False


# ================================================================ #
#  LOGGING
# ================================================================ #

def _log_execution(proposal: dict, decision: dict,
                   result: dict):
    os.makedirs("logs", exist_ok=True)
    entry = {
        "timestamp":  datetime.utcnow().isoformat(),
        "proposal":   proposal,
        "lot":        decision.get("lot_size_approved"),
        "result":     result,
    }
    try:
        with open(LOG_PATH, "r") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    existing.append(entry)
    with open(LOG_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)


# ================================================================ #
#  MAIN EXECUTION FUNCTION
# ================================================================ #

def execute_trade(proposal: dict, decision: dict) -> dict:
    """
    Place a live trade on MT5.

    Returns a result dict:
    {
        "success": True/False,
        "ticket": int or None,
        "price": float or None,
        "error": str or None,
        "message": str (human readable summary)
    }
    """
    agent      = proposal.get("agent", "UNKNOWN")
    instrument = proposal.get("instrument", "XAUUSD")
    direction  = proposal.get("direction", "LONG")
    lot        = float(decision.get("lot_size_approved", 0.01))
    sl_price   = float(proposal.get("stop_loss_price", 0))
    tp_price   = float(proposal.get("take_profit_price", 0))
    entry_price= float(proposal.get("entry_price", 0))
    atr        = float(proposal.get("atr", 0))

    print(f"\n[MT5] ── LIVE EXECUTION ─────────────────────────")
    print(f"[MT5] Agent    : {agent}")
    print(f"[MT5] Trade    : {direction} {instrument} | Lot: {lot}")
    print(f"[MT5] SL Price : {sl_price} | TP Price: {tp_price}")
    print(f"[MT5] ─────────────────────────────────────────────")

    # ── Connect ──────────────────────────────────────────────────────
    if not _connect():
        result = {"success": False, "ticket": None, "price": None,
                  "error": "MT5 connection failed",
                  "message": f"❌ {agent} {direction} {instrument} — MT5 connection failed"}
        _log_execution(proposal, decision, result)
        return result

    try:
        # ── 1. Check symbol ──────────────────────────────────────────
        ok, err, sym_info = _check_symbol(instrument)
        if not ok:
            raise Exception(err)

        # ── 2. Determine order type ──────────────────────────────────
        order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL

        # ── 3. Get current price ─────────────────────────────────────
        tick = mt5.symbol_info_tick(instrument)
        if tick is None:
            raise Exception(f"Cannot get tick for {instrument}")
        price = tick.ask if direction == "LONG" else tick.bid

        # ── 4. Check price drift ─────────────────────────────────────
        ok, err = _check_price_drift(instrument, entry_price, atr)
        if not ok:
            raise Exception(err)

        # ── 5. Duplicate check — MANAGER decides, executor trusts ──
        # MANAGER already evaluated portfolio before approving
        # We only warn, not block
        positions = mt5.positions_get(symbol=instrument)
        if positions:
            for p in positions:
                if p.magic == MAGIC:
                    existing = "LONG" if p.type == 0 else "SHORT"
                    if existing == direction:
                        print(f"[MT5] Note: Adding to existing "
                              f"{direction} {instrument} (#{p.ticket}) "
                              f"— MANAGER approved this")

        # ── 6. Check margin ──────────────────────────────────────────
        ok, err = _check_margin(instrument, lot, order_type)
        if not ok:
            raise Exception(err)

        # ── 7. Set default SL/TP if not provided ────────────────────
        if sl_price <= 0:
            digits = sym_info.digits
            # Instrument-specific pip-based SL/TP as safety net
            # This should rarely fire — agents should always provide SL/TP
            if atr > 0:
                sl_dist = 1.5 * atr
                tp_dist = 3.0 * atr
            elif digits == 5 or digits == 3:
                # Forex pairs (EURUSD=5, USDJPY=3)
                pip = 10 ** -(digits - 1)
                sl_dist = 25 * pip    # 25 pips SL
                tp_dist = 60 * pip    # 60 pips TP
            elif digits == 2:
                # Gold (XAUUSD) or indices
                sl_dist = price * 0.008   # ~0.8% SL
                tp_dist = price * 0.020   # ~2% TP
            else:
                sl_dist = price * 0.005
                tp_dist = price * 0.012

            if direction == "LONG":
                sl_price = round(price - sl_dist, sym_info.digits)
                tp_price = round(price + tp_dist, sym_info.digits)
            else:
                sl_price = round(price + sl_dist, sym_info.digits)
                tp_price = round(price - tp_dist, sym_info.digits)
            print(f"[MT5] ⚠️  Auto SL/TP (safety net): SL={sl_price} TP={tp_price}")

        # ── 7. Validate SL/TP ────────────────────────────────────────
        sl_validated, tp_validated, sl_tp_warning = _validate_sl_tp(
            instrument, order_type, price, sl_price, tp_price, sym_info)

        if sl_tp_warning:
            print(f"[MT5] ⚠️  SL/TP warning: {sl_tp_warning}")

        # ── 8. Build order request ───────────────────────────────────
        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    instrument,
            "volume":    lot,
            "type":      order_type,
            "price":     price,
            "sl":        sl_validated,
            "tp":        tp_validated,
            "deviation": 20,
            "magic":     MAGIC,
            "comment":   f"APEX_{agent}",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        print(f"[MT5] Sending order: {direction} {instrument} "
              f"@ {price} | SL:{sl_validated} | TP:{tp_validated}")

        # ── 9. Send order ────────────────────────────────────────────
        result_mt5 = _send_order(request)

        if result_mt5 is None:
            raise Exception("order_send returned None")

        # ── 10. Check result ─────────────────────────────────────────
        if result_mt5.retcode != mt5.TRADE_RETCODE_DONE:
            error_codes = {
                10004: "Requote",
                10006: "Request rejected",
                10007: "Request cancelled by trader",
                10008: "Order placed",
                10009: "Request completed",
                10010: "Only part of request completed",
                10011: "Request processing error",
                10012: "Request cancelled by timeout",
                10013: "Invalid request",
                10014: "Invalid volume",
                10015: "Invalid price",
                10016: "Invalid stops",
                10017: "Trade disabled",
                10018: "Market closed",
                10019: "Insufficient funds",
                10020: "Prices changed",
                10021: "No quotes",
                10022: "Invalid expiration date",
                10023: "Order state changed",
                10024: "Too frequent requests",
                10025: "No changes",
                10026: "Autotrading disabled by server",
                10027: "Autotrading disabled by client",
                10028: "Request locked",
                10029: "Order or position frozen",
                10030: "Invalid fill type",
                10031: "No connection",
                10032: "Only allowed for live",
                10033: "Limit of pending orders reached",
                10034: "Volume limit reached",
                10035: "Invalid or prohibited order type",
            }
            err_msg = error_codes.get(result_mt5.retcode,
                                      f"Unknown error {result_mt5.retcode}")
            raise Exception(f"Order failed: {err_msg} "
                          f"(code {result_mt5.retcode}) — "
                          f"{result_mt5.comment}")

        ticket = result_mt5.order
        actual_price = result_mt5.price

        # ── 11. Verify position opened ───────────────────────────────
        verified = _verify_position(instrument, ticket)
        if not verified:
            print(f"[MT5] ⚠️  Position #{ticket} not found after execution — "
                  f"may be filled and closed immediately")

        # ── 12. Success ──────────────────────────────────────────────
        result = {
            "success":  True,
            "ticket":   ticket,
            "price":    actual_price,
            "sl":       sl_validated,
            "tp":       tp_validated,
            "lot":      lot,
            "error":    None,
            "message":  (f"✅ {agent} {direction} {instrument} "
                        f"#{ticket} @ {actual_price} "
                        f"Lot:{lot} SL:{sl_validated} TP:{tp_validated}")
        }

        print(f"[MT5] ✅ ORDER EXECUTED")
        print(f"[MT5] Ticket  : #{ticket}")
        print(f"[MT5] Price   : {actual_price}")
        print(f"[MT5] SL      : {sl_validated}")
        print(f"[MT5] TP      : {tp_validated}")

    except Exception as e:
        result = {
            "success": False,
            "ticket":  None,
            "price":   None,
            "error":   str(e),
            "message": f"❌ {agent} {direction} {instrument} — {str(e)}"
        }
        print(f"[MT5] ❌ EXECUTION FAILED: {e}")

    finally:
        mt5.shutdown()

    _log_execution(proposal, decision, result)
    print(f"[MT5] ──────────────────────────────────────────────")
    return result


# ================================================================ #
#  CLOSE POSITION — manual / monitor / emergency
# ================================================================ #

def close_position(ticket: int, reason: str = "MANUAL_CLOSE") -> dict:
    """
    Close a specific open position by ticket number.

    Used by:
    - Telegram /close <ticket> command
    - Emergency close from CLI
    - Any future agent that needs to force-close

    Returns:
    {
        "success": True/False,
        "ticket":  int,
        "symbol":  str,
        "pnl":     float,
        "price":   float,
        "message": str (human readable)
    }
    """
    print(f"\n[MT5] ── CLOSE POSITION ──────────────────────────")
    print(f"[MT5] Ticket : #{ticket} | Reason: {reason}")

    if not _connect():
        return {
            "success": False, "ticket": ticket,
            "symbol": "", "pnl": 0.0, "price": 0.0,
            "error":   "MT5 connection failed",
            "message": f"❌ #{ticket} — MT5 connection failed",
        }

    try:
        # Find the position
        all_positions = mt5.positions_get() or []
        pos = next((p for p in all_positions if p.ticket == ticket), None)

        if pos is None:
            return {
                "success": False, "ticket": ticket,
                "symbol": "", "pnl": 0.0, "price": 0.0,
                "error":   f"Position #{ticket} not found",
                "message": f"❌ #{ticket} not found — already closed?",
            }

        symbol      = pos.symbol
        lot         = pos.volume
        pnl         = round(pos.profit, 2)
        is_buy      = (pos.type == 0)   # BUY position → close with SELL

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise Exception(f"Cannot get tick for {symbol}")

        close_price = tick.bid if is_buy else tick.ask
        close_type  = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY

        # Detect filling mode
        sym_info = mt5.symbol_info(symbol)
        filling  = mt5.ORDER_FILLING_IOC
        if sym_info and (sym_info.filling_mode & 1):
            filling = mt5.ORDER_FILLING_FOK

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    20,
            "magic":        MAGIC,
            "comment":      reason[:31],  # MT5 comment max 31 chars
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        print(f"[MT5] Closing: #{ticket} {symbol} {lot}lot "
              f"@ {close_price} | current P&L: ${pnl:+.2f}")

        result_mt5 = mt5.order_send(request)

        if result_mt5 and result_mt5.retcode == mt5.TRADE_RETCODE_DONE:
            sign = "+" if pnl >= 0 else ""
            print(f"[MT5] ✅ CLOSED #{ticket} @ {close_price} | P&L: {sign}${pnl:.2f}")
            return {
                "success": True,
                "ticket":  ticket,
                "symbol":  symbol,
                "pnl":     pnl,
                "price":   close_price,
                "error":   None,
                "message": (f"✅ #{ticket} {symbol} closed @ {close_price} | "
                            f"P&L: {sign}${pnl:.2f}"),
            }
        else:
            code = result_mt5.retcode if result_mt5 else "None"
            raise Exception(f"order_send failed: retcode {code}")

    except Exception as e:
        print(f"[MT5] ❌ Close failed: {e}")
        return {
            "success": False, "ticket": ticket,
            "symbol":  "", "pnl": 0.0, "price": 0.0,
            "error":   str(e),
            "message": f"❌ #{ticket} close failed — {str(e)[:80]}",
        }
    finally:
        mt5.shutdown()
        print(f"[MT5] ──────────────────────────────────────────────")


def close_all_positions(reason: str = "MANUAL_CLOSE_ALL") -> list:
    """
    Close ALL open APEX positions (magic == 20250401).

    Used by:
    - Telegram /closeall command
    - Emergency shutdown routine

    Returns list of result dicts, one per position.
    """
    print(f"\n[MT5] ── CLOSE ALL POSITIONS ─────────────────────")

    if not _connect():
        mt5.shutdown()
        return [{"success": False, "error": "MT5 connection failed",
                 "message": "❌ MT5 connection failed"}]

    try:
        all_pos = mt5.positions_get() or []
        apex    = [p for p in all_pos if p.magic == MAGIC]
    except Exception as e:
        apex = []
    finally:
        mt5.shutdown()

    if not apex:
        print(f"[MT5] No APEX positions to close.")
        return [{"success": True, "message": "No APEX positions open."}]

    print(f"[MT5] Closing {len(apex)} APEX position(s)...")
    results = []
    for pos in apex:
        result = close_position(pos.ticket, reason=reason)
        results.append(result)

    closed    = sum(1 for r in results if r.get("success"))
    failed    = len(results) - closed
    total_pnl = sum(r.get("pnl", 0) for r in results if r.get("success"))
    print(f"[MT5] Close all done: {closed} closed, {failed} failed | "
          f"Total P&L: ${total_pnl:+.2f}")
    return results
