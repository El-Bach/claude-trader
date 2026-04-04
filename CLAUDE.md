# APEX Capital AI — Company Context for Claude Code

## What This Is
An autonomous AI trading company built in Python.
Connects Claude API to MetaTrader 5 (MT5).
Trades EURUSD, XAUUSD, USDJPY, GBPUSD on MetaQuotes demo account.
Live on MetaQuotes demo account (see .env for credentials).
Balance: ~$12,200 USD (demo starting balance).

---

## How to Start a Claude Code Session

1. Open VS Code in the `trading-team/` folder
2. Open Claude Code
3. Say: "Read CLAUDE.md first. Then read all files in agents/. The system is fully working with live MT5 execution, STRATEGIST daily top-down plans, tightened entry rules (EMA200 gate, RSI 58/42, ADX rising), single-brain spike management, and session reporting running 24/7."
4. Claude Code will understand the full company and build on top without breaking anything.

---

## Two Separate Loops (main.py)

### DAILY STRATEGY LOOP — once per day at 07:00 UTC
```
STRATEGIST → D1+H4+H1 top-down analysis per instrument (Claude Opus, 4 calls)
           → Creates execution plan: bias, structure, key levels, entry zone,
             invalidation, trade idea, TP target, SL suggestion
           → Distributes plans to all 4 entry agents via receive_strategy_plan()
           → Sends Telegram summary of all 4 plans
```

### MAIN LOOP — every 15 minutes
```
NEWS      → analyses news, sentiment, economic calendar (rule-based, no AI)
TRACKER   → checks performance + open/closed MT5 positions, sends alerts to MANAGER
MANAGER   → stores news broadcast, reads real MT5 account (balance, positions, P&L)
DOLLAR    → broadcasts DXY/USD macro signal (4 pillars: technicals + basket + rates + Fed)
GOLD      → analyses XAUUSD for entry (uses STRATEGIST plan as context)
EURUSD    → analyses EURUSD for entry (uses STRATEGIST plan as context)
GBPUSD    → analyses GBPUSD for entry / Cable (uses STRATEGIST plan as context)
USDJPY    → analyses USDJPY for entry (uses STRATEGIST plan as context)
MANAGER   → evaluates all proposals with hard rules + Claude Opus (sees full news + macro)
MT5       → executes approved trades live
MANAGER   → sends ONE consolidated Telegram report
TRACKER   → logs performance
```

### WATCH LOOP — every 10 seconds (background thread)
```
MONITOR → checks all open APEX positions
  GOLD_WATCH   → monitors XAUUSD positions (if open)
  EURUSD_WATCH → monitors EURUSD positions (if open)
  GBPUSD_WATCH → monitors GBPUSD positions (if open)
  USDJPY_WATCH → monitors USDJPY positions (if open)

  Each watcher classifies every spike:
    ADVERSE spike  → ask Claude: CLOSE early or trust the SL?
    FAVORABLE spike → ask Claude: MOVE_SL_TP to trail both and ride momentum?
    Milestone/News  → standard HOLD / MOVE_SL / CLOSE

  Decisions: HOLD / MOVE_SL / MOVE_SL_TP / CLOSE
  → Real-time Telegram alerts (separate from cycle report)
```

### Automatic Session Report
```
At 19:00 UTC (22:00 Beirut = NY close):
  TRACKER sends full session report automatically
  Also fires on manual shutdown (Ctrl+C)
```

### Command Line
```bash
python main.py           # Single main cycle (no watch loop)
python main.py --loop    # Both loops running 24/7
python main.py --watch   # Watch loop only (no new entries)
python main.py --demo    # Team startup only, no MT5
python dashboard_server.py   # Performance dashboard (http://localhost:8080)
python backtest.py --all --from 2025-01-01   # Rule-based backtest all agents
python backtest.py --agent GOLD              # Single agent backtest
```

---

## Complete File Structure

```
trading-team/
├── main.py                  ← entry point, two loops, full team
├── mt5_executor.py          ← live MT5 execution: open + close positions
├── dashboard_server.py      ← performance dashboard HTTP server (port 8080)
├── dashboard.html           ← dashboard frontend (dark theme, Chart.js, auto-refresh 30s)
├── backtest.py              ← rule-based historical backtest (no Claude API cost) — use --csv for 5yr
├── download_histdata.py     ← downloads M1→M15 data from HistData.com (5 years, free)
├── create_backtest_report.py← generates Excel report from backtest results (openpyxl)
├── CLAUDE.md                ← this file
├── .env                     ← all credentials (never commit)
├── requirements.txt
├── agents/
│   ├── __init__.py          ← exports all agents
│   ├── manager.py           ← CEO: account reader, evaluator, Telegram
│   ├── news.py              ← news & sentiment analyst (runs FIRST, no AI)
│   ├── tracker.py           ← performance analyst + session reports
│   ├── strategist.py        ← daily top-down analyst: D1+H4+H1 execution plans (Claude Opus)
│   ├── dollar.py            ← DXY macro compass + EURUSD trades (4 pillars)
│   ├── gold.py              ← XAUUSD entry, 4-pillar system + STRATEGIST plan
│   ├── eurusd.py            ← EURUSD trend + mean reversion + STRATEGIST plan
│   ├── gbpusd.py            ← GBPUSD entry, trend + mean reversion (Cable) + STRATEGIST plan
│   ├── usdjpy.py            ← USDJPY trend following + Ichimoku + STRATEGIST plan
│   ├── monitor.py           ← risk manager, watch loop + Telegram commands
│   ├── gold_watch.py        ← XAUUSD position specialist (single brain)
│   ├── eurusd_watch.py      ← EURUSD position specialist (single brain)
│   ├── gbpusd_watch.py      ← GBPUSD position specialist (single brain)
│   └── usdjpy_watch.py      ← USDJPY position specialist (single brain)
└── logs/
    ├── trades.json              ← all proposals and MANAGER decisions
    ├── executions.json          ← live MT5 execution results
    └── strategist_memory.json   ← STRATEGIST persistent memory (auto-created, grows daily)
```

---

## Complete Environment Variables (.env)

```bash
# ── Anthropic ──────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── Second brain (kept in .env, used by Telegram command listener only) ──
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
SECOND_BRAIN_PROVIDER=deepseek
SECOND_BRAIN_MODEL=deepseek-chat

# ── MT5 ────────────────────────────────────────────────────────
MT5_LOGIN=YOUR_ACCOUNT_NUMBER
MT5_PASSWORD=YOUR_PASSWORD
MT5_SERVER=MetaQuotes-Demo

# ── Telegram ───────────────────────────────────────────────────
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...

# ── Risk management ────────────────────────────────────────────
MAX_DAILY_LOSS_PCT=0.03         # 3% daily loss = full halt
MAX_RISK_PER_TRADE_PCT=0.01     # 1% risk per trade
MAX_OPEN_POSITIONS=3            # max simultaneous positions

# ── Spike detection thresholds (configurable) ──────────────────
SPIKE_XAUUSD=15.0               # $15 move in 1 M1 candle
SPIKE_EURUSD=0.0030             # 30 pips in 1 M1 candle
SPIKE_GBPUSD=0.0040             # 40 pips in 1 M1 candle
SPIKE_USDJPY=0.80               # 80 pips in 1 M1 candle

# ── News API (optional — free key from newsapi.org) ────────────
NEWS_API_KEY=...
```

---

## Full Team — Roles and Models

| Agent | Model | Role |
|---|---|---|
| MANAGER | Claude Opus | CEO — capital manager, MT5 reader, final approver (sees full news + macro) |
| NEWS | No AI (rule-based) | External world monitor — ForexFactory + RSS + Fear/Greed |
| TRACKER | No AI (MT5 + logs) | Performance analyst — decisions + positions + session reports |
| STRATEGIST | Claude Opus | Daily top-down analyst — D1+H4+H1 market structure, execution plans per instrument |
| DOLLAR | Claude Sonnet | DXY macro compass — 4 pillars: basket + rate differential + Fed + technicals |
| GOLD | Claude Sonnet | XAUUSD entry specialist — 4-pillar system + STRATEGIST plan |
| EURUSD | Claude Sonnet | EURUSD entry — trend + mean reversion + STRATEGIST plan |
| GBPUSD | Claude Sonnet | GBPUSD entry — trend + mean reversion (Cable) + STRATEGIST plan |
| USDJPY | Claude Sonnet | USDJPY entry — trend following + Ichimoku + STRATEGIST plan |
| MONITOR | Thread manager | Risk manager — watch loop + Telegram command listener |
| GOLD_WATCH | Claude Sonnet | XAUUSD position monitor — spike direction aware |
| EURUSD_WATCH | Claude Sonnet | EURUSD position monitor — spike direction aware |
| GBPUSD_WATCH | Claude Sonnet | GBPUSD position monitor — spike direction aware |
| USDJPY_WATCH | Claude Sonnet | USDJPY position monitor — spike direction aware |

---

## MANAGER — Full Decision Pipeline

Every cycle MANAGER runs 9 hard checks on every proposal:

```python
1. _check_paused()         # consecutive losses → 1hr pause
2. _check_daily_loss()     # daily loss limit → full halt
3. _check_positions()      # max 3 open positions
4. _check_margin()         # free margin > $200, level > 200%
5. _check_confidence()     # minimum 70% confidence
6. _check_regime()         # RISK_OFF → direction-aware filter:
                           #   GOLD/DOLLAR: always allowed
                           #   EURUSD SHORT: allowed (USD long = consistent with RISK_OFF)
                           #   EURUSD LONG:  blocked (USD short = against RISK_OFF)
                           #   USDJPY SHORT: allowed (JPY long = consistent with RISK_OFF)
                           #   USDJPY LONG:  blocked (JPY short = against RISK_OFF)
                           #   GBPUSD SHORT: allowed (USD long = consistent with RISK_OFF)
                           #   GBPUSD LONG:  blocked (USD short = against RISK_OFF)
7. _check_correlation()    # DXY vs instrument direction conflict
8. _check_rr()             # minimum R:R 1.95
9. _check_pyramiding()     # pyramiding rules (see below)
```

If all pass → `_apply_lot_modifiers()` → Claude Opus senior review → final APPROVED/REJECTED/HOLD

Claude Opus sees: full account state + NEWS broadcast (risk level, sentiment,
Fear/Greed, key events) + DOLLAR macro (basket, rate spread, Fed stance) + proposal.

### Pyramiding Rules
```
0 positions on instrument    → normal trade, full lot
1 position WINNING           → allow, lot reduced 50%
1 position LOSING            → REJECT (never add to loser)
2+ positions same instrument → REJECT always
Opposite direction open      → REJECT (conflicting)
```

### Risk Constants
```python
MIN_CONFIDENCE         = 70      # below = no trade
MIN_RR                 = 1.95    # minimum risk:reward
MAX_OPEN_POSITIONS     = 3
MAX_DAILY_LOSS_PCT     = 0.03    # 3% = full halt
MAX_RISK_PER_TRADE_PCT = 0.01    # 1% per trade
MAX_PORTFOLIO_RISK_PCT = 0.02    # 2% total open risk ceiling
MAX_CONSECUTIVE_LOSSES = 3       # then 1hr pause
MAX_LOT                = 0.05    # safety cap all instruments
MAGIC                  = 20250401
```

### Dynamic Lot Sizing (_apply_lot_modifiers)
Applied after `calculate_lot()`, before Claude Opus review:
```
1. Portfolio risk cap  — scale lot if total open risk near 2% ceiling
2. GOLD ADX scaling   — −50% if ADX < 20, −25% if ADX < 25 (weak trend)
3. USDJPY BoJ zone    — −50% if LONG and entry_price ≥ 148.00
```
Never increases lot — only reduces. Returns (adjusted_lot, notes[]).

### Daily P&L Calculation
```python
# Account-level (all sources) — used for display + daily loss limit
closed_pnl_today = sum(d.profit for d in deals if d.entry == DEAL_ENTRY_OUT)

# APEX-only — used for per-agent attribution in session report
apex_closed_pnl  = sum(d.profit for d in deals
                       if d.magic == MAGIC and d.entry == DEAL_ENTRY_OUT)

daily_pnl = closed_pnl_today + floating_pnl   # all closed + all open
```

---

## NEWS Agent — Data Sources and Risk Levels

**No AI required — fully rule-based synthesis.**

### Data Sources
```
ForexFactory   → economic calendar (USD, EUR, GBP, JPY events) — PRIMARY risk driver
RSS feeds      → FXStreet, ForexLive, Investing.com headlines
NewsAPI.org    → breaking financial news (optional API key)
CNN Fear/Greed → market sentiment index (0-100)
```

### Risk Levels
```
CRITICAL → HIGH impact ForexFactory event in < 15 min → block ALL entries
HIGH     → HIGH impact ForexFactory event in < 60 min → block all entries
MEDIUM   → MEDIUM event in < 30 min, OR headline keywords → reduce confidence -10pts
LOW      → no significant events → normal operation
```

Important: RSS headline keywords only raise risk to MEDIUM at most.
Only timed ForexFactory events can trigger HIGH or CRITICAL blocks.

---

## TRACKER Agent — Performance Monitoring

Reads `logs/trades.json`, `logs/executions.json`, AND connects to MT5.

### Session Report (send_session_report)
Triggered automatically at 19:00 UTC (NY close) and on shutdown.
Sections:
```
💰 ACCOUNT       — balance, equity, free margin, closed P&L today,
                   floating P&L, session start balance
📈 CLOSED TODAY  — every closed trade: agent, direction, entry, P&L,
                   close reason, time
🔓 OPEN NOW      — every live position: entry, SL, TP, floating P&L
🤖 DECISIONS     — proposals → approved → executed per agent, win rate
━━━━━━━━━━━━━━━━
💵 SESSION P&L   — equity now vs session start balance (clear summary)
```
Splits automatically into 2 Telegram messages if over 4000 chars.

---

## STRATEGIST Agent — Daily Top-Down Analyst

```
Role      : Runs ONCE per day at 07:00 UTC (before London open)
            First run also happens at bot startup.
Model     : Claude Opus (4 calls/day — one per instrument)
Data      : D1 (60 bars) + H4 (120 bars) + H1 (96 bars) from MT5
Instruments: GOLD, EURUSD, GBPUSD, USDJPY

Workflow (per instrument):
  1. D1 structure: dominant trend, HH/HL or LH/LL, major swing points
  2. H4 structure: current leg, session trend, intermediate S/R
  3. Key levels: PDH/PDL, PWH/PWL, Monthly Open, nearest round numbers
  4. Backtest fit: ADX/RSI vs historical best zones
  5. Web intelligence: latest macro/central bank headlines
  6. Memory: observations from previous days
  7. Entry zone: specific price range (not a single price)
  8. Trade idea: clear 1-2 sentence thesis
  9. Invalidation: single H4 close level that breaks the idea
  10. Memory update: writes new insights back to persistent memory

Execution plan output per instrument:
  bias           — BULLISH / BEARISH / NEUTRAL / WAIT
  structure      — D1+H4 market structure notes
  key_levels     — 3-4 most important S/R levels
  entry_zone     — price range to watch (e.g., "2280-2300")
  invalidation   — one level that makes the idea wrong
  trade_idea     — brief thesis
  tp_target      — primary TP price
  sl_suggestion  — SL price
  session_notes  — timing considerations
  backtest_fit   — how current conditions compare to historical best zones
  confluence_score — 0-100
  memory_update  — {insight, level_note, regime_note, global_regime}

Distribution:
  strategist.distribute_plans(entry_agents) → calls agent.receive_strategy_plan(plan)
  Each entry agent injects the plan as === STRATEGIST EXECUTION PLAN === in user prompt

Telegram:
  Sends summary of all 4 plans after daily run
```

### STRATEGIST Learning System

The STRATEGIST accumulates knowledge across days via two mechanisms:

#### 1. Local Memory (`logs/strategist_memory.json`)
Persistent JSON file that grows every daily run. Stores per-instrument:
```
insights      — key observations about instrument behaviour (last 10)
level_notes   — specific S/R levels that proved significant or broke (last 10)
regime_notes  — macro/regime context notes (last 10)
global_regime — overall market regime notes (last 5)
performance   — live 30-day win/loss counts from executions.json
web_cache     — cached web headlines (refreshed every 6 hours)
```
Claude writes `memory_update` fields in every plan response.
These are saved and shown back to Claude on the next daily run.
Over weeks, STRATEGIST builds a real picture of: which levels hold,
which regimes produce winning setups, and how each instrument behaves.

#### 2. Web Intelligence (RSS feeds, cached 6 hours)
```
Source        Instruments        Content
Fed RSS       All 4              Monetary policy press releases
ForexLive     All 4              Real-time macro headlines
ECB RSS       EURUSD, GBPUSD     ECB press releases
BoE RSS       GBPUSD             Bank of England publications
FXStreet      All 4              Forex analysis & news
```
Filtered by instrument keywords → injected as `=== WEB INTELLIGENCE ===`
Cache is refreshed every 6 hours (not every run) to avoid hammering servers.

#### Memory Flow Per Daily Run
```
run_daily()
  │
  ├── _load_memory()              ← load logs/strategist_memory.json
  ├── _load_performance_feedback() ← read executions.json, update win/loss counts
  ├── _fetch_web_insights()       ← fetch RSS feeds (or use cache if fresh)
  │
  ├── per instrument:
  │     _memory_text()   → formats memory for prompt
  │     _web_text()      → formats headlines for prompt
  │     _ask_claude()    → Claude sees: chart + backtest + memory + web
  │     _update_memory() → saves memory_update fields from Claude's response
  │
  └── _save_memory()              ← persist updated memory to disk
```

#### Memory File Location
```
logs/strategist_memory.json   ← auto-created on first run, grows daily
```

---

## DOLLAR Agent — 4-Pillar Macro Compass

```
Role      : Runs after NEWS every cycle, broadcasts to ALL agents
Pillars   :
  1. Technicals    — EMA20/50/200, Wilder RSI, ATR, ADX on EURUSD H4+H1
  2. DXY Basket    — weighted USD momentum: EURUSD(57.6%) + USDJPY(13.6%)
                     + GBPUSD(11.9%) + USDCAD(9.1%) from MT5
  3. Rate Diff     — US 10Y (FRED API) vs EU 10Y German Bund (ECB API)
                     spread >1.5% = BULLISH_USD | <0.5% = BEARISH_USD
  4. Fed Rhetoric  — hawkish/dovish keyword count from Fed RSS feed

Broadcasts: usd_bias, dxy_trend, risk_regime, basket score, rate spread,
            fed stance, per-instrument implications, confidence
Also trades: EURUSD when setup confirms
             SL = max(1.2x H1 ATR, 20 pips) | TP = 2.5x SL
Field names: gold_implication / eurusd_implication / usdjpy_implication
```

---

## MT5 Executor (mt5_executor.py)

Handles ALL live order placement AND position closing.

### Open Trade Flow
```
1. Connect MT5
2. Check symbol, get live price, check price drift (>0.5 ATR → skip)
3. Check margin (need 150% buffer)
4. Set SL/TP (agent values, fallback to ATR-based)
5. Auto-detect filling mode (FOK → IOC → RETURN)
6. Send order, verify position opened
7. Log to logs/executions.json
```

### Close Position Functions
```python
close_position(ticket, reason="MANUAL_CLOSE")
  → Closes one position by ticket
  → Returns {success, ticket, symbol, pnl, price, message}

close_all_positions(reason="MANUAL_CLOSE_ALL")
  → Closes all APEX positions (magic == 20250401)
  → Returns list of result dicts
```

### Order Comment Format
```
comment = "APEX_{agent}"   # e.g. "APEX_GOLD", "APEX_EURUSD", "APEX_DOLLAR"
magic   = 20250401
```

---

## MONITOR — Single Brain Position Management

### Architecture
```
MONITOR (thread manager)
├── Brain: Claude Sonnet only (single decision maker)
├── Telegram command listener (second background thread)
└── Specialist watchers (activate only when position open):
    ├── GOLD_WATCH   → XAUUSD specialist
    ├── EURUSD_WATCH → EURUSD specialist
    ├── GBPUSD_WATCH → GBPUSD specialist
    └── USDJPY_WATCH → USDJPY specialist
```

### Three Watch Modes
```
Mode 1 (every 10 sec, NO AI):
  → Price check only
  → Prints status: direction, P&L, SL distance, profit ratio, spike size
  → Spike size shown as "SPIKE_WATCH" when >50% of threshold

Mode 2 (every 60 sec, AI call — milestone triggered):
  → Profit milestone check
  → Milestone 1.0x SL → MOVE_SL to breakeven
  → Milestone 1.5x SL → MOVE_SL to +0.5x SL
  → Milestone 2.0x SL → trail at 1x H1 ATR

Mode 3 (immediate, AI call — spike or news triggered):
  → Classify spike direction vs position direction
  → ADVERSE spike  → ask Claude: CLOSE early or trust SL?
  → FAVORABLE spike → ask Claude: MOVE_SL_TP to trail + extend TP?
  → Real-time Telegram alert on any action
```

### Spike Direction Logic
```python
spike_up    = candle_close > candle_open   # M1 candle direction
pos_is_long = (pos.type == 0)              # BUY position

spike_type = "FAVORABLE" if spike_up == pos_is_long else "ADVERSE"
```

### Spike Responses
```
ADVERSE spike:
  Claude asked: "Close now to limit loss, or trust the SL?"
  If >60% of SL distance consumed → lean CLOSE
  Decision: CLOSE or HOLD

FAVORABLE spike:
  Claude asked: "Trail SL+TP to ride momentum?"
  Suggested SL  = current_price − 0.8 × H1_ATR  (locks profit)
  Suggested TP  = current_price + 1.5 × H1_ATR  (extends target)
  Decision: MOVE_SL_TP or HOLD
```

### Decisions and Telegram Alerts
```
HOLD        → no action, no alert
MOVE_SL     → _execute_move_sl() → 🛡️ STOP LOSS MOVED alert
MOVE_SL_TP  → _execute_move_sl_tp() → 🚀 SL+TP TRAILED alert
CLOSE       → _execute_close() → 🔴 POSITION CLOSED alert
```

### Spike Thresholds (configurable in .env)
```
XAUUSD: $15 move in 1 M1 candle
EURUSD: 30 pips in 1 M1 candle
GBPUSD: 40 pips in 1 M1 candle
USDJPY: 80 pips in 1 M1 candle
```

---

## Telegram Commands (from your chat)

MONITOR listens for commands from your authorized `TELEGRAM_CHAT_ID`.
Commands drain old messages on startup — only processes new commands.

```
/positions        → list all open APEX positions with entry, SL, TP, floating P&L
/close <ticket>   → close one specific position by ticket number
/closeall         → close ALL open APEX positions immediately
/status           → balance, equity, floating P&L, open count, bot running status
```

---

## Entry Agents — Indicators and Strategy

### Universal Entry Rules (Applied to All 4 Entry Agents)
These 3 rules are enforced in the system prompt of every entry agent:

```
1. EMA200 MACRO GATE (mandatory):
   - Price above H4 EMA200 → ONLY BUY setups allowed (trading against it is FORBIDDEN)
   - Price below H4 EMA200 → ONLY SELL setups allowed
   This filters out counter-trend trades that go against the dominant structural bias.

2. RSI MOMENTUM THRESHOLD (tightened):
   - BUY: RSI > 58 required (not just > 50 — strong momentum needed)
   - SELL: RSI < 42 required (not just < 50)
   - RSI 42-58 = momentum ambiguous = reduce confidence 15 points
   Eliminates weak-momentum entries that historically gave the most false signals.

3. ADX RISING FILTER:
   - ADX must be RISING (adx > adx_prev) to confirm trend acceleration
   - Declining ADX = trend losing steam = reduce confidence 15 points
   Prevents entering trends that are already peaking or exhausting.

USDJPY uses slightly tighter: RSI > 60 for LONG / RSI < 40 for SHORT (stronger trends).

4. SMC CONFLUENCE OVERRIDE (GOLD, EURUSD, GBPUSD only — not USDJPY):
   - Price inside H1 Bullish FVG OR at H1 Bullish OB → valid BUY even if RSI is in 50-58 range
   - Price inside H1 Bearish FVG OR at H1 Bearish OB → valid SELL even if RSI is in 42-50 range
   - RSI threshold relaxed 7-8 points when structural SMC confluence is present
   - Nearest Equal Highs = primary TP target for BUY (liquidity sweep)
   - Nearest Equal Lows  = primary TP target for SELL (liquidity sweep)
   - SL tightened to below OB.low / FVG.low (BUY) or above OB.high / FVG.high (SELL)
   Backtest result: EURUSD -$168 → +$1,236 | GBPUSD -$36 → +$469 | USDJPY unchanged (kept baseline)
```

### SMC Detection Functions (GOLD, EURUSD, GBPUSD)
Each of these three agents runs three detectors on the H1 dataframe every cycle:

```
_smc_detect_fvg(df, price)      → bullish/bearish FVGs (unfilled imbalances)
                                   in_bull / in_bear = price currently inside the gap
_smc_detect_ob(df, price, atr)  → bullish/bearish Order Blocks (last candle before impulse)
                                   at_bull / at_bear = price within 0.5 ATR of OB zone
_smc_detect_liquidity(df, price) → equal highs (bear stops above) / equal lows (bull stops below)
                                   nearest_high / nearest_low = closest liquidity target

Claude receives a === PRICE ACTION STRUCTURE === section in every prompt:

  H1 FAIR VALUE GAPS:
    Bullish FVG : 2285.0 – 2292.0  ← PRICE INSIDE     (entry zone)
    Bearish FVG : 2318.0 – 2324.0                      (resistance)

  H1 ORDER BLOCKS:
    Bullish OB  : 2278.0 – 2283.0  ← PRICE AT OB      (support)
    Bearish OB  : 2330.0 – 2335.0                      (resistance)

  H1 LIQUIDITY POOLS:
    Equal Highs : 2310.5            ← nearest bear target: 2310.5
    Equal Lows  : 2274.0            ← nearest bull target: 2274.0

  SMC CONFLUENCE: IN bullish FVG (pullback entry zone) | AT bullish OB (institutional support)
```

### Indicator Design Philosophy
Each agent uses only indicators that provide non-redundant information for that specific instrument.
MACD is removed from all agents — it is a lagging EMA derivative, redundant when EMA stack + RSI are
already shown. Adding MACD creates noise and causes Claude to over-weight lagging momentum signals.

### GOLD (gold.py) — 4-Pillar System
```
Strategy  : Trend following with Fibonacci confluence
Pillars   : Structure / Momentum / Macro / Timing (need 3/4)
Timeframes: H4 (trend) + H1 (confirmation) + M15 (entry)
Sessions  : London + NY overlap (Beirut time)
SL        : 1.0-1.5x H4 ATR | TP: 2.0-2.5x SL (ADX-scaled)

Indicator set (7 — each with distinct role):
  EMA 20/50/200   Trend structure. Gold respects EMAs as institutional support/resistance.
                  EMA 200 = long-term bias filter (above = macro bull, below = macro bear).
  ATR (Wilder 14) Volatility normalization. Essential for SL/TP sizing on Gold's wide ranges.
  ADX + DI (14)   Trend strength gate. ADX > 25 = strong trend, < 20 = ranging, block entries.
                  +DI/-DI crossover confirms trend direction independently of price.
  RSI (Wilder 14) Momentum + divergence. RSI divergences on Gold are the most reliable leading
                  reversal signal. RSI 50-line crossover = trend continuation confirmation.
  Bollinger Bands Squeeze detection only. BB width < 20-period avg = energy coiling for breakout.
                  Not used for direction — only for timing (squeeze = move imminent).
  Volume ratio    Tick volume as institutional participation proxy. Spike > 1.5x avg on a Gold
                  move = conviction, not a trap. Low volume on breakout = fade risk.
  Fibonacci       Gold respects Fibonacci levels better than any other asset due to deep
  (H4 50-candle)  institutional use. 61.8% = primary TP target. 38.2%/50% = entry zones.
```

### EURUSD (eurusd.py) — Trend + Mean Reversion
```
Strategy  : H4 trend + H1 pullback entries (trending) + BB/Stoch fades (ranging)
DXY       : Mandatory inverse correlation from DOLLAR broadcast
Sessions  : London + NY overlap
SL        : ATR-based | TP: 2x SL minimum, round number targets

Indicator set (6 — each with distinct role):
  EMA 20/50/200   Structural trend. EURUSD EMAs are widely watched institutional benchmarks.
                  EMA 200 H4 = the dividing line between structural bull and bear.
  ATR (Wilder 14) SL/TP sizing. EURUSD ATR is stable and directly maps to pip distances.
  ADX + DI (14)   Critical market type filter. ADX < 20 = ranging (use mean reversion only).
                  ADX > 25 = trending (use EMA pullback entries only). ADX determines strategy.
  RSI (Wilder 14) Momentum + 50-line filter. RSI > 50 = bullish momentum, < 50 = bearish.
                  RSI divergences on H1 are high-probability reversal signals in ranging markets.
  Stochastic      Pullback entry timing. H4 uptrend + H1 Stochastic oversold (<20) crossing up
  (14, 3, 3)      = best long entry. H4 downtrend + H1 Stoch overbought (>80) = best short.
                  This is EURUSD's primary entry trigger — more precise than RSI alone.
  Bollinger Bands Ranging market entries + breakout detection. ADX < 20 + price at lower BB
  (20, 2σ)        with Stoch oversold = mean reversion long. Squeeze = imminent breakout.
```

### GBPUSD (gbpusd.py) — Trend + Mean Reversion ("Cable")
```
Strategy  : H4 trend + H1 deep pullback entries (trending) + BB/Stoch fades (ranging)
DXY       : Mandatory inverse correlation from DOLLAR broadcast (same as EURUSD)
Sessions  : London (primary — 80% of Cable volume) + NY overlap
SL        : max(1.5x H4 ATR, 30 pips) — wider than EURUSD due to higher volatility
TP        : 2.5x SL

Indicator set (6 — each with distinct role):
  EMA 20/50/200   Structural trend. Cable respects EMA stack as institutional anchors.
                  EMA200 H4 = structural bull/bear dividing line for GBPUSD.
  ATR (Wilder 14) Higher volatility than EURUSD (ATR ~1.4-1.6x larger). Critical for
                  SL sizing — too tight stops hit by normal Cable noise. Floor = 30 pips.
  ADX + DI (14)   Cable either trends hard (ADX > 28) or chops (ADX < 20) — little
                  middle ground. ADX > 28 required for trend following (raised from 25 to
                  filter Cable chop). Primary filter to choose strategy (trend vs mean reversion).
  RSI (Wilder 14) Momentum + divergence. RSI 50-line = trend filter. H1 RSI divergences
                  are powerful for Cable — sharp reversals after exhaustion moves.
  Stochastic      Cable makes DEEP pullbacks in trends (50-70 pips on H4). H4 uptrend +
  (14, 3, 3)      H1 Stochastic < 20 crossing up = highest-probability Cable entry.
                  Primary entry trigger — more important for Cable than for EURUSD.
  Bollinger Bands GBPUSD exhibits strong BB squeeze patterns before explosive moves
  (20, 2σ)        (especially pre-BoE). Squeeze = breakout imminent. In ranges: BB
                  extremes + Stoch reversal = mean reversion entry.
```

### USDJPY (usdjpy.py) — Trend Following
```
Strategy  : Pure trend following — only trades strong directional moves
BoJ       : Caution above 150.00, block LONG above 152.00
Sessions  : Tokyo + London + NY overlap
SL        : 1.5-2.0x H1 ATR | TP: 2.0x SL minimum

Indicator set (5 — minimal, high-conviction only):
  EMA 20/50/200   Trend structure. USDJPY makes large sustained EMA-following moves.
                  Full EMA stack alignment (price > EMA20 > EMA50 > EMA200) = only entry.
  ATR (Wilder 14) SL/TP sizing. USDJPY has high ATR volatility; proper sizing is critical
                  especially near BoJ intervention levels (150+).
  ADX + DI (14)   Mandatory trend gate. ADX > 30 required — raised from 25, only genuine
                  strong trends qualify. If ADX < 30, confidence reduced or no trade.
  RSI (Wilder 14) Momentum confirmation. RSI 50-line = trend filter. RSI > 65 (not just 50)
                  in a strong uptrend = strong momentum. Overbought/oversold near BoJ zones.
  Ichimoku (H4)   The definitive JPY indicator. Tenkan/Kijun cross = entry signal.
                  Cloud = dynamic institutional support/resistance. Price above cloud = bull.
                  Kumo twist = early trend change signal. Used by all major JPY institutions.
  Volume          REMOVED — forex tick volume is not reliable for USDJPY. BoJ interventions
                  create false volume spikes. Ichimoku + ADX provide better conviction signals.
```

---

## Telegram Structure

### Main Cycle Report (once per cycle, from MANAGER)
```
📊 APEX Capital AI — Cycle Report
💰 ACCOUNT — Balance / Equity / Free Margin / Daily P&L / Open positions
⚡ NEWS & SENTIMENT — Risk / Sentiment / Fear&Greed
💵 DOLLAR SIGNAL — USD Bias / Regime / Confidence
🤖 AGENT DECISIONS — per agent result + reason
⚡ EXECUTED — trade details if any
🚨 ALERTS — warnings
```

### Session Report (auto at NY close + on shutdown, from TRACKER)
```
📊 APEX Capital AI — Session Report
💰 ACCOUNT — balance, equity, closed P&L, floating, session start
📈 CLOSED POSITIONS — every trade with agent, entry, P&L, reason
🔓 OPEN POSITIONS — every live position with SL, TP, floating
🤖 DECISIONS — funnel per agent, win rate, P&L
━━━ 💵 SESSION P&L — clear net result ━━━
```

### Real-Time MONITOR Alerts
```
🔴 POSITION CLOSED — ticket, P&L, trigger, reason
🛡️ STOP LOSS MOVED — ticket, new SL, P&L, reason
🚀 SL+TP TRAILED   — ticket, new SL + new TP, P&L, reason (favorable spike)
```

---

## Key Constants (global)

```python
MAGIC                  = 20250401   # all bot orders tagged
CYCLE_INTERVAL_MINUTES = 15         # main loop frequency — aligned with M15 entry timeframe
WATCH_INTERVAL_SEC     = 10         # watch loop frequency
PROFIT_CHECK_SEC       = 60         # AI profit review interval
PROFIT_MILESTONE_1     = 1.0        # 1x SL → breakeven
PROFIT_MILESTONE_2     = 1.5        # 1.5x SL → lock profit
PROFIT_MILESTONE_3     = 2.0        # 2x SL → trail ATR
SESSION_END_UTC_HOUR   = 19         # NY close — triggers session report
```

---

## What's Working Right Now ✅

```
✅ Real MT5 account reading every cycle
✅ Live trade execution (FOK/IOC/RETURN auto-detect)
✅ Proper ATR-based SL/TP per instrument
✅ Single consolidated Telegram report per cycle
✅ NEWS analysis — fully rule-based (ForexFactory + RSS + Fear/Greed, no AI cost)
✅ NEWS blocks entries only on timed HIGH/CRITICAL ForexFactory events
✅ MANAGER receives full NEWS broadcast before every trade decision
✅ Claude Opus final review sees: news risk + sentiment + macro + proposal
✅ TRACKER tracks open MT5 positions per agent (live P&L)
✅ TRACKER tracks closed deals per agent (win/loss, P&L, close reason)
✅ DOLLAR rebuilt with 4 macro pillars (basket + rate diff + Fed rhetoric + technicals)
✅ All 6 entry agents working (DOLLAR/GOLD/EURUSD/GBPUSD/USDJPY)
✅ MONITOR single-brain (Claude Sonnet) watching positions 24/7
✅ Spike direction classification — ADVERSE vs FAVORABLE
✅ ADVERSE spike → Claude asked: close early or trust SL?
✅ FAVORABLE spike → Claude asked: trail both SL+TP to ride momentum?
✅ MOVE_SL_TP action — moves both SL and TP in one MT5 order
✅ Profit milestone SL management (breakeven → lock → trail)
✅ Dynamic lot sizing: portfolio risk cap + GOLD ADX + USDJPY BoJ zone
✅ Daily P&L fixed — uses all account deals (not just MAGIC filtered)
✅ Session start balance tracked — accurate session P&L calculation
✅ Session report auto-sent at NY close (19:00 UTC = 22:00 Beirut)
✅ Session report also fires on Ctrl+C shutdown
✅ close_position(ticket) in mt5_executor.py
✅ close_all_positions() in mt5_executor.py
✅ Telegram commands: /positions /close /closeall /status
✅ Pyramiding rules (no adding to losers)
✅ Regime rules (RISK_OFF blocks most agents)
✅ GBPUSD agent (Cable) — trend + mean reversion, 6-indicator set
✅ GBPUSD_WATCH — single brain watcher with 40-pip spike threshold
✅ RISK_OFF regime now direction-aware (EURUSD/GBPUSD/USDJPY SHORT allowed)
✅ Performance Dashboard — dashboard_server.py + dashboard.html (port 8080)
✅ Rule-based backtest — backtest.py (free, uses MT5 historical data, no Claude cost)
✅ GBP COT data added to cot.py (British Pound Sterling CME futures)
✅ Agent P&L attribution fixed — uses position_id→IN deal lookup (not OUT deal comment)
✅ BoJ intervention warning (USDJPY)
✅ Session filter (Beirut/GMT+3 timezone)
✅ Consecutive loss protection (3 losses → 1hr pause)
✅ Daily loss limit (3% → full halt)
✅ STRATEGIST agent — daily top-down analyst (D1+H4+H1, Claude Opus, once/day at 07:00 UTC)
✅ STRATEGIST distributes execution plans to all 4 entry agents (bias/entry_zone/invalidation/TP)
✅ All 4 entry agents receive STRATEGIST plan in user prompt (=== STRATEGIST EXECUTION PLAN ===)
✅ EMA200 macro gate enforced in all 4 agents: ONLY BUY above EMA200, ONLY SELL below
✅ RSI threshold tightened: BUY requires RSI > 58, SELL requires RSI < 42 (was 50/50)
✅ ADX rising filter: ADX must be rising (adx > adx_prev) in all 4 agents
✅ Historical data pipeline: download_histdata.py — 5 years M1→M15 from HistData.com
✅ Rule-based 5-year backtest with --csv flag (bypasses MT5 data cache limit)
✅ Backtest improvements: 60-bar time exit, EMA200 gate, RSI 58/42, ADX rising, ATR expanding
✅ GBPUSD backtest tuning: Stoch <25 (was <20), RSI >55/<45 (was >58/<42), ADX >28 (was >22)
✅ USDJPY backtest tuning: ADX >30 (was >25), Kumo clearance 0.25x ATR, BoJ block 148 (was 149)
✅ STRATEGIST learning system: local memory (logs/strategist_memory.json) + web RSS intelligence
✅ Memory persists across days — insights, level notes, regime notes, live performance feedback
✅ Web intelligence: Fed/ECB/BoE/ForexLive/FXStreet RSS filtered per instrument (6hr cache)
✅ Memory update loop: Claude writes observations back after each daily plan → grows smarter over time
✅ SMC detection: FVG + Order Block + Liquidity added to GOLD, EURUSD, GBPUSD agents
✅ SMC backtest comparison framework (--smc / --compare flags in backtest.py)
✅ SMC validated: EURUSD +$1,236 vs -$168 baseline | GBPUSD +$469 vs -$36 | USDJPY unchanged
✅ RSI gate relaxed 7-8 pts when price inside FVG or at OB (structural pullback entry)
✅ Liquidity targets (equal highs/lows) used as primary TP when ≥2x SL away
✅ OB/FVG-based SL tighter than ATR SL when structural zone is identified
```

---

## Backtest Results

All backtests use: $12,200 starting balance | 1% risk per trade | CSV mode (HistData.com local files)
Run command: `python backtest.py --all --compare --from 2024-01-01 --csv`

### 2-Year Baseline Results (Jan 2024 – Mar 2026)

```
Agent    Symbol   Trades  Wins  Losses  TimeExit  WinRate*  PF     P&L         P&L%    MaxDD
──────────────────────────────────────────────────────────────────────────────────────────────
GOLD     XAUUSD     44     4      8       32       33.3%    3.62  +$2,554     +20.9%   1.6%
EURUSD   EURUSD     19     2      7       10       22.2%    0.80   -$168       -1.4%   4.2%
GBPUSD   GBPUSD     13     0      2       11        0.0%    0.85    -$36       -0.3%   3.4%
USDJPY   USDJPY     38     1      7       30       12.5%    1.94  +$804       +6.6%    3.4%
──────────────────────────────────────────────────────────────────────────────────────────────
TOTAL                                                             +$3,154     +25.8%
```
*Win rate excludes time exits (15-hour cap)

### 2-Year SMC-Enhanced Results (Jan 2024 – Mar 2026)

```
Agent    Symbol   Trades  Wins  Losses  TimeExit  WinRate*  PF     P&L         P&L%    MaxDD
──────────────────────────────────────────────────────────────────────────────────────────────
GOLD     XAUUSD     44     4      8       32       33.3%    3.71  +$2,645     +21.7%   1.6%
EURUSD   EURUSD     58    13     10       35       35.7%    1.56  +$1,236     +10.1%   6.2%
GBPUSD   GBPUSD     46     6     17       23       17.6%    1.27   +$469       +3.8%   6.9%
USDJPY   USDJPY     38     1      7       30       11.1%    1.81   +$788       +6.5%   3.4%  ← baseline kept
──────────────────────────────────────────────────────────────────────────────────────────────
TOTAL                                                             +$5,137     +42.1%
```

### SMC vs Baseline Comparison

```
Agent    Baseline P&L   SMC P&L     Change    Score   Verdict
────────────────────────────────────────────────────────────────────────
GOLD       +$2,554      +$2,645      +4%       3/5    MARGINAL
EURUSD      -$168       +$1,236     +837%      4/5    SMC BETTER ✅
GBPUSD       -$36        +$469     +1410%      4/5    SMC BETTER ✅
USDJPY      +$804        +$788       -2%       0/5    BASELINE KEPT ✅
────────────────────────────────────────────────────────────────────────
TOTAL      +$3,154      +$5,137     +63%
```

### SMC Decision Per Agent
```
GOLD    → SMC added to live agent (marginal improvement, zero drawdown increase)
EURUSD  → SMC added to live agent (clear improvement — turned negative to +10.1%)
GBPUSD  → SMC added to live agent (clear improvement — turned 0 wins to 17.6% WR)
USDJPY  → SMC NOT added (Ichimoku already handles structure; SMC hurt performance)
```

### 2-Year Tuned Results (Jan 2024 – Mar 2026) — ADX thresholds raised

```
Agent    Symbol   Trades  Wins  Losses  TimeExit  WinRate*  PF     P&L         P&L%    MaxDD
──────────────────────────────────────────────────────────────────────────────────────────────
GOLD     XAUUSD     44     4      8       32       33.3%    3.71  +$2,645     +21.7%   1.6%   (unchanged)
EURUSD   EURUSD     58    13     10       35       35.7%    1.56  +$1,236     +10.1%   6.2%   (unchanged)
GBPUSD   GBPUSD     29     2      8       19       20.0%    1.50   +$549       +4.5%   4.2%   ← ADX 22→28
USDJPY   USDJPY     20     1      3       16       25.0%    1.79   +$616       +5.0%   2.9%   ← ADX 25→30 + cloud clearance
──────────────────────────────────────────────────────────────────────────────────────────────
TOTAL                                                             +$5,046     +41.4%
```

### Tuning Changes (29 Mar 2026)
```
GBPUSD  ADX threshold: 22 → 28   — filters Cable chop, fewer but higher-quality entries
        Result: losses 17→8 (−53%), MaxDD 6.9%→4.2%, PF 1.27→1.50
USDJPY  ADX threshold: 25 → 30   — only genuine strong trends qualify
        Kumo breakout confirmed: price must clear cloud by 0.25x ATR (not just touch)
        BoJ block tightened: 149.00 → 148.00 for LONG entries
        Result: losses 7→3 (−57%), WR 11%→25%, PF 1.53→1.79, MaxDD 3.4%→2.9%
```

### Backtest Commands
```bash
python backtest.py --all --from 2024-01-01 --csv              # baseline, all agents
python backtest.py --all --smc --from 2024-01-01 --csv        # SMC signals only
python backtest.py --all --compare --from 2024-01-01 --csv    # side-by-side comparison
python backtest.py --agent GOLD --compare --from 2024-01-01   # single agent via MT5
```

### Key Observations
```
1. GOLD is the system's primary profit engine — PF 3.71 over 2 years
2. 70-85% of trades are TIME_EXITs (15h cap) — positions drift correctly but slowly
   → In live trading MONITOR trails SL+TP, converting many time-exits to wins
3. EURUSD/GBPUSD had near-zero wins with strict RSI>58 filter
   → SMC (FVG/OB entries on pullbacks) fixed this: RSI cools during pullbacks
4. USDJPY's Ichimoku already captures structure — SMC is redundant
5. GBPUSD/USDJPY ADX tuning (29 Mar 2026): fewer trades, fewer losses, better PF
   → GBPUSD: losses −53%, MaxDD 6.9%→4.2% | USDJPY: WR 11%→25%, losses −57%
6. Combined 2-year tuned P&L: +$5,046 (+41.4%) on $12,200 — rule-based only, no Claude AI
   → Live Claude AI layer adds quality filtering on top of these mechanical signals
```

---

## Next Priorities (in order)

### 1. VPS Deployment
Deploy for 24/7 production operation:
- Recommended: Contabo VPS ($5/month, Windows)
- MT5 terminal on VPS
- Auto-start on boot (Task Scheduler)
- Monitoring/alerting if process dies

### 2. Live Account Migration
Switching from demo to live:
- Reduce MAX_RISK_PER_TRADE_PCT=0.005 (0.5% for live)
- Reduce MAX_OPEN_POSITIONS=2
- Lower spike thresholds (more sensitive)
- Run demo for min 2 weeks with positive P&L first

### 3. STRATEGIST Memory Review (after 1 week live)
After running live for 1 week, review `logs/strategist_memory.json`:
- Are insights specific and useful?
- Are level notes accumulating correctly?
- Is web intelligence fetching relevant headlines?
Tune INSTRUMENT_KEYWORDS in strategist.py if headlines are off-target.

### 4. Real Claude Backtest
Same as rule-based but calls real Claude agents on each historical bar.
Cost: ~$160 for 6 months all agents.
Run when budget allows — validates the AI layer specifically.

### 5. COT Data Integration
`agents/cot.py` exists but COT data is not feeding into agent decisions yet.
Adding institutional positioning as a 5th pillar to DOLLAR or GOLD could improve signal quality — especially for GOLD which responds strongly to institutional flows.

---

## How to Add a New Agent (Template)

```python
# 1. Copy agents/eurusd.py as entry agent template
# 2. Change at top:
SYMBOL         = "XAGUSD"   # example — replace with target instrument
NAME           = "XAGUSD"
PIP_SIZE       = 0.01
NEWS_COUNTRIES = ["USD"]

# 3. Update _get_session() for instrument sessions
# 4. Update _ask_claude() system prompt
# 5. Add to agents/__init__.py
# 6. Add to main.py (import, initialize, pass to run_cycle)

# 7. Create watch agent (copy agents/eurusd_watch.py):
#    - Change SYMBOL, PIP_SIZE, NEWS_COUNTRIES
#    - Update spike threshold in .env: SPIKE_<SYMBOL>=<value>
#    - Update ask_claude() system prompt for instrument dynamics
#    - Constructor is __init__(self, claude_client)  ← single brain, no second_brain

# 8. Add to monitor.py watchers dict:
#    self.watchers["GBPUSD"] = GBPUSDWatch(self.claude)   ← example, already done

# 9. Test: python main.py --demo
```

---

## API Calls Reference

### Once Per Day at 07:00 UTC — Strategy Loop

| Step | Agent | API / Service | What It Does | Auth |
|---|---|---|---|---|
| 1 | STRATEGIST | MT5 `copy_rates_from_pos()` D1+H4+H1 | 60/120/96 candles per instrument (×4) | MT5 creds |
| 1 | STRATEGIST | **Claude Opus** × 4 | Top-down analysis → execution plan per instrument | ANTHROPIC_API_KEY |
| 2 | STRATEGIST | Telegram `sendMessage` | Daily plan summary for all 4 instruments | TELEGRAM_TOKEN |
| 3 | entry agents | `receive_strategy_plan()` | Plan stored, injected into every subsequent Claude prompt | — |

### Every 15 Minutes — Main Loop

| Step | Agent | API / Service | What It Does | Auth |
|---|---|---|---|---|
| 1 | NEWS | ForexFactory JSON | Economic calendar (USD/EUR/GBP/JPY events) — primary risk driver | None |
| 1 | NEWS | FXStreet RSS | Headline scrape for sentiment keywords | None |
| 1 | NEWS | ForexLive RSS | Headline scrape | None |
| 1 | NEWS | Investing.com RSS | Headline scrape | None |
| 1 | NEWS | CNN Fear/Greed | Sentiment score 0–100 | None |
| 1 | NEWS | NewsAPI.org | Breaking financial news (optional) | NEWS_API_KEY |
| 2 | TRACKER | MT5 `account_info()` | Balance, equity, margin | MT5 creds |
| 2 | TRACKER | MT5 `positions_get()` | All open positions + floating P&L | MT5 creds |
| 2 | TRACKER | MT5 `history_deals_get()` | Closed deals today — win/loss per agent | MT5 creds |
| 3 | MANAGER | MT5 `account_info()` | Real account state before decisions | MT5 creds |
| 3 | MANAGER | MT5 `positions_get()` | Open positions for pyramiding checks | MT5 creds |
| 4 | DOLLAR | MT5 `copy_rates_from_pos()` | H4+H1 candles for EURUSD, USDJPY, GBPUSD, USDCAD | MT5 creds |
| 4 | DOLLAR | FRED API | US 10Y Treasury yield (CSV) | None |
| 4 | DOLLAR | ECB API | German Bund 10Y yield (CSV) | None |
| 4 | DOLLAR | Fed Reserve RSS | Press releases → hawkish/dovish keyword count | None |
| 4 | DOLLAR | **Claude Sonnet** | Synthesizes 4 pillars → DXY broadcast | ANTHROPIC_API_KEY |
| 5 | GOLD | MT5 `copy_rates_from_pos()` | H4 + H1 + M15 candles for XAUUSD | MT5 creds |
| 5 | GOLD | MT5 `symbol_info_tick()` | Live bid/ask for spread check | MT5 creds |
| 5 | GOLD | ForexFactory JSON | News blackout check | None |
| 5 | GOLD | **Claude Sonnet** | 4-pillar analysis → BUY/SELL/NO_TRADE proposal | ANTHROPIC_API_KEY |
| 6 | EURUSD | MT5 `copy_rates_from_pos()` | H4 + H1 + M15 candles | MT5 creds |
| 6 | EURUSD | MT5 `symbol_info_tick()` | Spread check | MT5 creds |
| 6 | EURUSD | ForexFactory JSON | News blackout check | None |
| 6 | EURUSD | **Claude Sonnet** | Trend + mean reversion analysis → proposal | ANTHROPIC_API_KEY |
| 7 | GBPUSD | MT5 `copy_rates_from_pos()` | H4 + H1 + M15 candles | MT5 creds |
| 7 | GBPUSD | MT5 `symbol_info_tick()` | Spread check | MT5 creds |
| 7 | GBPUSD | ForexFactory JSON | News blackout check (USD+GBP) | None |
| 7 | GBPUSD | **Claude Sonnet** | Trend + Cable analysis → proposal | ANTHROPIC_API_KEY |
| 8 | USDJPY | MT5 `copy_rates_from_pos()` | H4 + H1 + M15 + Ichimoku candles | MT5 creds |
| 8 | USDJPY | MT5 `symbol_info_tick()` | Spread check | MT5 creds |
| 8 | USDJPY | ForexFactory JSON | News blackout check | None |
| 8 | USDJPY | **Claude Sonnet** | Trend + Ichimoku analysis → proposal | ANTHROPIC_API_KEY |
| 9 | MANAGER | MT5 `symbol_info()` | tick_value + tick_size for dynamic lot sizing | MT5 creds |
| 9 | MANAGER | **Claude Opus** | Senior review — full news + macro + proposals → APPROVED/REJECTED | ANTHROPIC_API_KEY |
| 10 | MT5_EXECUTOR | MT5 `symbol_info_tick()` | Live price at execution moment | MT5 creds |
| 10 | MT5_EXECUTOR | MT5 `order_calc_margin()` | Margin check before order | MT5 creds |
| 10 | MT5_EXECUTOR | MT5 `order_send()` | Places the live trade | MT5 creds |
| 11 | MANAGER | Telegram `sendMessage` | One consolidated cycle report | TELEGRAM_TOKEN |

### Every 10 Seconds — Watch Loop

| Trigger | Agent | API / Service | What It Does | Auth |
|---|---|---|---|---|
| Always | WATCH agents | MT5 `copy_rates_from_pos()` M1 | Last candle — spike size + direction | MT5 creds |
| Always | WATCH agents | MT5 `symbol_info_tick()` | Live price, P&L, SL distance | MT5 creds |
| Spike detected | WATCH agents | **Claude Sonnet** | ADVERSE → close or hold? / FAVORABLE → trail SL+TP? | ANTHROPIC_API_KEY |
| Every 60s | WATCH agents | MT5 `copy_rates_from_pos()` H1 | ATR for milestone SL trail | MT5 creds |
| Every 60s | WATCH agents | **Claude Sonnet** | Milestone management (breakeven / lock / trail) | ANTHROPIC_API_KEY |
| Any action | MONITOR | MT5 `order_send()` | Modify SL/TP or close position | MT5 creds |
| Any action | MONITOR | Telegram `sendMessage` | Real-time alert (CLOSED / SL MOVED / TRAILED) | TELEGRAM_TOKEN |

### Event-Driven

| When | Agent | API | What It Does | Auth |
|---|---|---|---|---|
| 19:00 UTC daily | TRACKER | MT5 + Telegram | Full session report — all trades, P&L, win rates | Both |
| Ctrl+C shutdown | TRACKER | MT5 + Telegram | Same session report on manual stop | Both |
| Telegram command | MONITOR | MT5 + Telegram | `/positions` `/close` `/closeall` `/status` | Both |

### Claude Call Count

| Model | Called By | Frequency | Notes |
|---|---|---|---|
| Claude Opus | STRATEGIST | 4 calls/day | Once at 07:00 UTC, one per instrument |
| Claude Sonnet | DOLLAR | Every 15 min | Always |
| Claude Sonnet | GOLD | Every 15 min | Always |
| Claude Sonnet | EURUSD | Every 15 min | Always |
| Claude Sonnet | GBPUSD | Every 15 min | Always |
| Claude Sonnet | USDJPY | Every 15 min | Always |
| Claude Opus | MANAGER | Every 15 min | Most expensive — final approver |
| Claude Sonnet | Watch agents | 0–N per 10 sec | Only on spike or milestone — free in quiet markets |

**6 Claude calls guaranteed per 15-min cycle** (DOLLAR + GOLD + EURUSD + GBPUSD + USDJPY + MANAGER Opus).
**4 Claude Opus calls per day** for STRATEGIST (07:00 UTC, one per instrument).
Watch loop adds Sonnet calls only on spikes or profit milestones — zero cost in quiet markets.

### External APIs — Authentication Summary

| Service | Used By | Key in .env | Cost |
|---|---|---|---|
| Anthropic Claude | All AI agents | `ANTHROPIC_API_KEY` | Per token |
| MetaTrader 5 | All agents + executor | `MT5_LOGIN/PASSWORD/SERVER` | Free |
| Telegram Bot | MANAGER, TRACKER, MONITOR | `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` | Free |
| ForexFactory | NEWS, GOLD, EURUSD, GBPUSD, USDJPY | None | Free |
| FRED (US 10Y yield) | DOLLAR | None | Free |
| ECB (EU 10Y yield) | DOLLAR | None | Free |
| Federal Reserve RSS | DOLLAR | None | Free |
| CNN Fear/Greed | NEWS | None | Free |
| FXStreet / ForexLive / Investing RSS | NEWS | None | Free |
| NewsAPI.org | NEWS | `NEWS_API_KEY` | Free tier (optional) |

---

## Coding Standards

```python
# MT5: always wrap in try/finally with mt5.shutdown()
try:
    mt5.initialize(login=login, password=password, server=server)
    # ... MT5 operations ...
finally:
    mt5.shutdown()

# Claude API: always wrap in try/except, strip markdown fences
try:
    response = self.client.messages.create(...)
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)
except Exception as e:
    # fallback with safe defaults

# Agent errors: catch individually, never crash main loop
for name, agent in specialists:
    try:
        proposal = agent.analyse()
    except Exception as e:
        import traceback
        traceback.print_exc()
        manager.record_agent_result(name, "ERROR", str(e)[:60])

# Never hardcode credentials
api_key = os.getenv("ANTHROPIC_API_KEY")

# Logs go to logs/ (auto-created)
os.makedirs("logs", exist_ok=True)

# Magic number on all trades
MAGIC = 20250401

# Order comment — agent attribution
comment = f"APEX_{agent_name}"   # e.g. "APEX_GOLD", "APEX_DOLLAR"

# Indicator standards (all agents must match):
# Wilder RSI:  alpha = 1.0/period, ewm(alpha=alpha, adjust=False)
# Wilder ATR:  tr.ewm(span=period, adjust=False).mean()
# Wilder ADX:  alpha = 1.0/period, ewm(alpha=alpha, adjust=False)

# Watch agent constructor: single brain only
def __init__(self, claude_client):   # NOT (self, claude_client, second_brain)
    self.claude = claude_client
```

---

## Session Times (Beirut / GMT+3 from March 29 2026)

```
Tokyo overlap  : 03:00 - 09:00 (USDJPY active)
London open    : 10:00 - 13:00 (best EURUSD)
Lunch dead zone: 13:00 - 15:30 (avoid)
NY open        : 15:30
London/NY over : 15:30 - 19:00 (best for all instruments)
NY only        : 19:00 - 22:00 (moderate)
Dead zone      : 22:00 - 03:00 (avoid all)
NY close       : 22:00 Beirut / 19:00 UTC → session report fires
```

---

## Current Account State

- Account  : MetaQuotes-Demo (see .env)
- Balance  : ~$12,200 USD (demo)
- Leverage : 100:1
- Max lot  : 0.05 (safety cap)
- Magic    : 20250401

---

## Architecture Diagram

```
                    ┌─────────────────────────────────┐
                    │      APEX Capital AI              │
                    └─────────────────────────────────┘
                                    │
              ┌─────────────────────┼──────────────────────┐
              │                     │                        │
     ┌────────▼────────┐   ┌────────▼────────┐   ┌─────────▼────────┐
     │   MAIN LOOP     │   │   WATCH LOOP    │   │  TELEGRAM        │
     │   (5 min)       │   │   (10 sec)      │   │  (real-time)     │
     └────────┬────────┘   └────────┬────────┘   └──────────────────┘
              │                     │
     ┌────────▼────────┐   ┌────────▼────────┐
     │ NEWS (no AI)    │   │ MONITOR         │
     │ TRACKER+MT5     │   │ ├ GOLD_WATCH    │
     │ MANAGER         │   │ ├ EURUSD_WATCH  │
     │ DOLLAR (4pill.) │   │ ├ GBPUSD_WATCH  │
     │ GOLD            │   │ └ USDJPY_WATCH  │
     │ EURUSD          │   │  Brain: Sonnet  │
     │ GBPUSD          │   │  ADVERSE→CLOSE? │
     │ USDJPY          │   │  FAVORBL→TRAIL? │
     │ MT5_EXECUTOR    │   │  CMD LISTENER   │
     └─────────────────┘   └─────────────────┘
```
