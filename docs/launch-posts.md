# Claude Trader — Launch Posts
# Copy-paste ready. Replace [YOUR_GITHUB_URL] with your actual repo link.
# Post in this order: Twitter first → Reddit → LinkedIn (LinkedIn last gets most eyes if others link to it)

---

# TWITTER / X
# Target: 280 chars max. Tag @AnthropicAI for retweet potential.
# Post with a screenshot of the dashboard or Telegram report if you have one.

---

## Tweet 1 — Main announcement (post this first)

I built an autonomous AI trading system using Claude API.

14 specialized agents run 24/7:
→ Daily market structure analysis (Claude Opus)
→ 5 entry specialists (Claude Sonnet)
→ Real-time position manager (10-sec loop)
→ Capital manager with 9 hard risk checks

2-year backtest: +41.4% on $12,200

Open source 👇
[YOUR_GITHUB_URL]

@AnthropicAI #Claude #AlgoTrading #Python #OpenSource

---

## Tweet 2 — Thread continuation (reply to Tweet 1)

How it works:

Every 15 min:
• NEWS reads ForexFactory + RSS + Fear/Greed
• STRATEGIST plan guides entries (runs daily at 07:00 UTC)
• GOLD / EURUSD / GBPUSD / USDJPY each propose setups
• MANAGER (Claude Opus) runs 9 risk checks + final review
• MT5 executes the approved trade live

Every 10 sec:
• MONITOR classifies spikes as ADVERSE or FAVORABLE
• Claude decides: close early? or trail stop to ride momentum?

---

## Tweet 3 — Thread continuation (reply to Tweet 2)

Backtest results (2 years, 1% risk per trade):

GOLD    → +$2,645  PF: 3.71  MaxDD: 1.6%
EURUSD  → +$1,236  PF: 1.56  MaxDD: 6.2%
GBPUSD  →   +$549  PF: 1.50  MaxDD: 4.2%
USDJPY  →   +$616  PF: 1.79  MaxDD: 2.9%
─────────────────────────────────────────
Total   → +$5,046  (+41.4%)

Rule-based engine only — Claude's filtering layer on top of this.

---

## Tweet 4 — Thread continuation (reply to Tweet 3)

Key design decisions that matter:

✅ EMA200 gate — only trade WITH the structural trend
✅ SMC detection — enters at FVGs and Order Blocks (pullback entries)
✅ STRATEGIST memory — learns which levels held across days
✅ Single Telegram report per cycle (not spam)
✅ Pyramiding rules — never adds to a losing trade
✅ 3% daily loss → full halt automatically

Everything configurable via .env

Stack: Python · Claude API · MetaTrader 5 · Telegram
[YOUR_GITHUB_URL]

---
---

# REDDIT — r/algotrading
# Tone: Technical, humble, show your work, invite criticism
# Title should be specific and include numbers

---

## Title:
I built an autonomous trading system with 14 Claude AI agents — open source, 2-year backtest +41.4%

## Body:

Hey r/algotrading,

I've been building an autonomous trading system for the past few months and finally made it public. Here's an honest breakdown.

**What it does**

Claude Trader runs a full trading desk as specialized AI agents: a daily strategist, macro analysts, entry specialists per instrument, a capital manager, and real-time position watchers. It trades EURUSD, XAUUSD, USDJPY, GBPUSD on MetaTrader 5, fully autonomously.

Two loops run simultaneously:
- **Main loop (15 min):** News → Macro → Entry proposals → Risk checks → Execution → Telegram report
- **Watch loop (10 sec):** Monitors every open position for adverse/favorable price spikes

**The AI layer**

- Claude Opus runs as MANAGER (final trade approval) and STRATEGIST (daily D1+H4+H1 top-down analysis)
- Claude Sonnet runs 5 entry/macro agents and 4 real-time position watchers
- Each agent has a specific role and doesn't do anything outside it

**Risk management (9 hard checks before every trade)**

1. Consecutive losses → 1hr pause
2. Daily loss limit (3%) → full halt
3. Max open positions (3)
4. Free margin check
5. Min confidence (70%)
6. Regime filter (RISK_OFF blocks most longs)
7. DXY correlation check
8. Min R:R (1.95)
9. Pyramiding rules (no adding to losers)

**Entry logic**

- EMA200 gate: price above H4 EMA200 = only BUY setups, below = only SELL
- RSI 58/42 threshold (tighter than typical 50/50)
- ADX must be rising (trend acceleration confirmed)
- SMC signals (FVGs, Order Blocks, Liquidity pools) relax RSI threshold on structural pullbacks
- STRATEGIST daily plan gives each agent bias, entry zone, and invalidation level as context

**Backtest results (Jan 2024 – Mar 2026, $12,200 start, 1% risk/trade)**

| Agent | Trades | Win Rate | Profit Factor | P&L | Max DD |
|---|---|---|---|---|---|
| GOLD (XAUUSD) | 44 | 33.3% | 3.71 | +$2,645 | 1.6% |
| EURUSD | 58 | 35.7% | 1.56 | +$1,236 | 6.2% |
| GBPUSD | 29 | 20.0% | 1.50 | +$549 | 4.2% |
| USDJPY | 20 | 25.0% | 1.79 | +$616 | 2.9% |
| **Total** | | | | **+$5,046** | |

Important caveat: ~75% of trades are time-exits (15h cap). In live trading, the watch loop trails SL+TP which converts many of these. The backtest is the conservative floor.

**Honest limitations**

- Backtest has no slippage or spread modeling
- The AI filtering layer (Claude's actual reasoning) is unvalidated — only the mechanical signals have been backtested
- Not yet deployed 24/7 (VPS next step)
- Win rates are low — works by having high R:R, not high win rate

**Tech stack**

Python, Claude API (Anthropic), MetaTrader 5, ForexFactory, FRED API, Telegram Bot API, feedparser for RSS

**Repo:** [YOUR_GITHUB_URL]

Happy to answer any questions about the architecture, the AI prompt design, or the entry logic. Would especially appreciate feedback from anyone who has gone through the demo → live account transition.

---
---

# REDDIT — r/MachineLearning
# Tone: Research-oriented, focus on the multi-agent architecture and Claude API use
# Less trading, more AI system design

---

## Title:
Multi-agent autonomous trading system using Claude API — 14 specialized agents, open source

## Body:

I built a multi-agent system where each agent has a specific role in an autonomous decision pipeline. The domain is forex trading, but the architecture pattern is general. Sharing because there aren't many open-source examples of production multi-agent Claude systems.

**Architecture**

14 agents, two loops:

**Slow loop (15 min):** Sequential pipeline where each agent specializes:
- `NEWS` — rule-based only, ForexFactory + RSS + Fear/Greed index. No AI. Produces a risk broadcast.
- `DOLLAR` — 4-pillar macro compass (technicals + DXY basket + rate differential + Fed rhetoric). Claude Sonnet.
- `GOLD / EURUSD / GBPUSD / USDJPY` — entry specialists. Each has a distinct indicator set for its instrument. Claude Sonnet.
- `MANAGER` — receives all proposals, runs deterministic checks, then calls Claude Opus with full context (news risk + macro regime + proposal). Final APPROVED/REJECTED/HOLD.

**Fast loop (10 sec):** 4 position watchers, one per instrument.
- Classifies every price spike as ADVERSE or FAVORABLE
- ADVERSE → Claude: close early to limit loss, or trust the SL?
- FAVORABLE → Claude: trail both SL and TP to ride momentum?
- Decisions execute immediately on MT5

**Daily (07:00 UTC):** STRATEGIST agent runs D1+H4+H1 analysis for each instrument with Claude Opus. Writes a structured execution plan distributed to all entry agents. Accumulates persistent memory across days — learns which levels held, which regimes produced wins.

**Key design decisions**

1. **Single brain per decision.** No vote-averaging. MANAGER is the single approver. This avoids contradictory actions.

2. **Separation of AI and deterministic logic.** Risk checks (daily loss limit, margin, position count, R:R) are hardcoded Python. Claude only sees proposals that passed all deterministic gates. Claude never calculates lot sizes.

3. **Broadcast pattern over shared state.** NEWS and DOLLAR produce read-only broadcasts consumed by downstream agents. Agents don't call each other — they receive structured data.

4. **Minimal context per agent.** Each Claude call sees only what that agent needs: its own indicators + the relevant broadcast. MANAGER is the only agent that sees everything.

5. **Persistent memory without a vector store.** STRATEGIST writes structured observations (insight, level_note, regime_note) to a JSON file after each daily run. These are read back as plain text on the next run. Simple and effective for low-frequency strategic memory.

**What I learned**

- Silent failures kill observability. Several bugs were invisible because exceptions were caught with empty handlers and defaulted values. Add logging everywhere.
- Formulaic prompts produce formulaic outputs. Agents that receive the same context every cycle give nearly identical responses. The STRATEGIST's daily plan + persistent memory helps break this by injecting day-specific context.
- Rule-based preprocessing beats AI preprocessing. Having NEWS do rule-based event detection before any Claude call means ForexFactory event blocking is deterministic, fast, and costs nothing.

**Repo (MIT):** [YOUR_GITHUB_URL]

Stack: Python, Claude API, MetaTrader 5, pandas/numpy for indicators

---
---

# REDDIT — r/Python
# Tone: Developer-focused, highlight code quality, architecture, tools used

---

## Title:
I built a 14-agent autonomous trading system in Python using the Claude API — open source

## Body:

Built this over several months and just open-sourced it. It's a complete autonomous trading system written in Python, connecting the Claude API to MetaTrader 5.

**What's interesting from a Python perspective**

**Two concurrent loops:**
```python
# Main loop — every 15 min (foreground)
while True:
    run_cycle(manager, dollar, gold, eurusd, gbpusd, usdjpy, news_agent, tracker)
    time.sleep(CYCLE_INTERVAL_MINUTES * 60)

# Watch loop — every 10 sec (background thread)
monitor.start()  # starts daemon thread
```

**14 agents, each a class with a single public method:**
```python
class GoldAgent:
    def analyse(self) -> dict | None:
        # fetch MT5 data → compute indicators → call Claude → return proposal
```

**Wilder smoothing for all indicators (RSI, ATR, ADX) — consistent across all agents:**
```python
# Wilder RSI (matches TradingView and MT5 exactly)
alpha = 1.0 / period
gain = delta.clip(lower=0).ewm(alpha=alpha, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=alpha, adjust=False).mean()
rs   = gain / loss.replace(0, np.nan)
rsi  = 100 - (100 / (1 + rs))
```

**MT5 always in try/finally:**
```python
try:
    mt5.initialize(login=login, password=password, server=server)
    # ... operations
finally:
    mt5.shutdown()
```

**Claude responses always stripped of markdown fences before JSON parsing:**
```python
raw = response.content[0].text.strip()
raw = raw.replace("```json", "").replace("```", "").strip()
result = json.loads(raw)
```

**Tech stack**
- `anthropic` — Claude API (Sonnet 4.6 + Opus 4.6)
- `MetaTrader5` — live broker connectivity and data
- `pandas / numpy` — indicator calculation
- `feedparser` — RSS feeds (ForexFactory, Reuters, FXStreet)
- `requests` — FRED API, ECB API, Fear/Greed index
- `python-dotenv` — config
- Pure stdlib HTTP server for the dashboard (no Flask/FastAPI needed)

**Dashboard** is a single `dashboard.html` served by a 50-line `http.server` subclass — no framework, no dependencies.

**Repo (MIT, Python 3.10+):** [YOUR_GITHUB_URL]

---
---

# REDDIT — r/Forex
# Tone: Trader-focused, practical, MT5 integration, risk management, real results
# Less code, more trading logic

---

## Title:
I automated my entire trading process with AI — 14 Claude agents running 24/7 on MT5, open source

## Body:

I spent several months building and backtesting a fully automated trading system. Just made it open source. Here's what it actually does.

**The core idea**

Instead of one bot with a fixed strategy, it's structured like a trading desk:
- A **strategist** does the daily D1/H4/H1 top-down analysis every morning at 07:00 UTC
- **Instrument specialists** look for setups every 15 minutes based on that daily plan
- A **capital manager** reviews every proposal and applies risk rules before approving anything
- A **position monitor** watches every open trade every 10 seconds and manages it in real time

All built on Claude AI (by Anthropic) connected to MetaTrader 5.

**Instruments:** EURUSD, XAUUSD, USDJPY, GBPUSD

**Entry rules (applied to all instruments)**
- EMA200 on H4: above = BUY only, below = SELL only (no counter-trend)
- RSI must be above 58 for longs, below 42 for shorts (not just 50)
- ADX must be rising (trend acceleration, not exhaustion)
- SMC zones (Fair Value Gaps, Order Blocks) relax these thresholds for pullback entries

**Risk management**
- 1% risk per trade (SL-based, dynamic lot sizing)
- Max 3 open positions simultaneously
- 3% daily loss → all trading stops for the day automatically
- 3 consecutive losses → 1 hour pause
- Never adds to a losing position (pyramiding blocked)
- Regime filter: if market is RISK_OFF, most long positions are blocked

**Position management (the part that matters most)**
- Monitors every position every 10 seconds
- Big adverse move → AI decides: close early or trust the stop?
- Big favorable move → AI decides: trail stop + extend TP to ride momentum?
- Profit milestones: SL moves to breakeven at 1x risk, locks profit at 1.5x, trails ATR at 2x

**Backtest results (2 years, Jan 2024 – Mar 2026)**

Starting balance: $12,200 | Risk: 1% per trade

| Pair | Trades | Win Rate | P&L | Max Drawdown |
|---|---|---|---|---|
| XAUUSD | 44 | 33.3% | +$2,645 | 1.6% |
| EURUSD | 58 | 35.7% | +$1,236 | 6.2% |
| GBPUSD | 29 | 20.0% | +$549 | 4.2% |
| USDJPY | 20 | 25.0% | +$616 | 2.9% |
| **Total** | | | **+$5,046 (+41.4%)** | |

Gold is the best performer by far (Profit Factor 3.71).

**Telegram integration**
- One consolidated report every cycle (not spam per agent)
- Real-time alerts when a position is opened, stop moved, or closed
- Commands you can send from your phone: `/positions`, `/close <ticket>`, `/closeall`, `/status`

**It's free, self-hosted, open source (MIT)**

You need: Python 3.10+, MetaTrader 5 (Windows), Anthropic API key (~$5-15/month for API costs at 15-min cycles)

**Repo:** [YOUR_GITHUB_URL]

Happy to answer questions about setup or how any part of the system works.

---
---

# LINKEDIN
# Tone: Professional, story-driven, results first, broad audience
# Use line breaks generously — LinkedIn punishes walls of text
# The backtest table is the visual anchor

---

## Post:

I spent 4 months building an autonomous AI trading system from scratch.

Today I'm open-sourcing it.

Here's what I built and what I learned 👇

---

**The problem I wanted to solve:**

Manual trading requires constant attention.
Algorithmic bots use fixed rules that can't adapt to changing market conditions.

I wanted something in between — a system that thinks like a trader but runs autonomously.

---

**The solution: Claude Trader**

A team of 14 specialized Claude AI agents (by Anthropic) that operate like a real trading desk:

→ A **Strategist** does daily top-down analysis (D1 → H4 → H1) every morning
→ **Instrument specialists** look for entries every 15 minutes
→ A **Capital Manager** (Claude Opus) reviews every trade proposal through 9 risk checks
→ A **Risk Monitor** watches every open position every 10 seconds

Connected live to MetaTrader 5. Trades EURUSD, Gold, USDJPY, GBPUSD.

---

**2-year backtest results (Jan 2024 – Mar 2026):**

| Instrument | Win Rate | Profit Factor | P&L |
|---|---|---|---|
| Gold (XAUUSD) | 33.3% | 3.71 | +$2,645 |
| EURUSD | 35.7% | 1.56 | +$1,236 |
| GBPUSD | 20.0% | 1.50 | +$549 |
| USDJPY | 25.0% | 1.79 | +$616 |
| **Portfolio** | | | **+$5,046 (+41.4%)** |

Starting capital: $12,200 | Risk per trade: 1%

---

**Key design decisions:**

✅ EMA200 macro gate — only trade in direction of the dominant trend
✅ SMC detection — entries at institutional Fair Value Gaps and Order Blocks
✅ Single AI approver — no vote averaging, one decision maker prevents conflict
✅ Rule-based risk gates run before any AI call — no AI can override hard limits
✅ Persistent memory — the Strategist learns which levels held across days

---

**What I learned building this:**

1. **Silent failures are dangerous.** Several bugs were invisible because exceptions defaulted quietly. Always log failures explicitly.

2. **Separation of concerns matters.** Risk management must be deterministic (Python). AI handles judgment. Never mix them.

3. **Low win rate + high R:R works.** The system wins 20-35% of trades but averages 2x+ reward on winners vs losers.

4. **Position management is where money is made.** The 10-second watch loop that trails stops on winning trades is more impactful than the entry logic.

---

**Built with:** Python · Claude API · MetaTrader 5 · Telegram · ForexFactory · FRED API

**Open source (MIT):** [YOUR_GITHUB_URL]

---

If you're working on AI agents, autonomous systems, or algorithmic trading, I'd love to connect and compare notes.

What's the most interesting autonomous AI system you've seen recently?

#AI #AlgorithmicTrading #Claude #Python #OpenSource #FinTech #MachineLearning #Forex

---
---

# POSTING ORDER & TIMING

1. **GitHub** → Add topics (settings → topics on your repo page):
   trading, algorithmic-trading, claude-api, anthropic, metatrader5,
   forex, ai-agent, python, autonomous-agent

2. **Twitter/X** → Post the thread (Tweet 1-4) — best time: 9am-11am EST weekday
   Tag @AnthropicAI in Tweet 1. Anthropic reposts Claude API projects regularly.

3. **r/algotrading** → Post within 1 hour of Twitter (cross-traffic helps)

4. **r/MachineLearning** → Same day, separate post

5. **r/Python** → Same day or next day

6. **r/Forex** → Same day or next day

7. **LinkedIn** → 24 hours after Twitter (gives GitHub time to accumulate stars to mention)

# TIPS TO MAXIMIZE STARS

- Respond to EVERY comment in the first 24 hours — Reddit rewards engagement
- If @AnthropicAI reposts your tweet, that alone can drive 50-200 stars
- Add a screenshot of the dashboard or a Telegram report screenshot to the tweets/posts
- Pin the Twitter thread to your profile
- After posting, update the LinkedIn post with "X stars in 24 hours" if traction is good
