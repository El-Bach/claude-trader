# Claude Trader — Installation Guide

Complete setup from zero to live trading in ~30 minutes.

---

## System Requirements

| Requirement | Minimum | Notes |
|---|---|---|
| OS | **Windows 10/11** | MetaTrader 5 is Windows-only. VPS works fine. |
| Python | **3.10 or higher** | 3.11+ recommended |
| RAM | 4 GB | 8 GB recommended for VPS |
| Internet | Stable broadband | Low-latency preferred |
| MT5 Terminal | Latest | Free from MetaQuotes |

> **Linux/Mac users:** MT5 does not run natively. Use a Windows VPS (Contabo $5/mo, Vultr, AWS EC2 Windows) or run MT5 via Wine (advanced, not supported here).

---

## Part 1 — MetaTrader 5

### 1.1 Download and Install MT5

1. Go to [metatrader5.com/en/download](https://www.metatrader5.com/en/download)
2. Download the installer and run it
3. Launch MetaTrader 5 after installation

### 1.2 Create a Free Demo Account

1. In MT5: **File → Open Account**
2. Search for **MetaQuotes** and select **MetaQuotes-Demo**
3. Click **New demo account** → fill in name/email
4. Select **Hedge** account type, set deposit to **$10,000–$50,000**
5. Click **Next** — MT5 will show your **login number** and **password**
6. **Save these credentials** — you will need them in the `.env` file

> **Important:** Write down your login, password, and server name (e.g. `MetaQuotes-Demo`) before closing this window.

### 1.3 Enable Symbols

APEX trades 4 instruments. Make sure they are visible in MT5:

1. Right-click the **Market Watch** panel
2. Click **Symbols**
3. Search for and enable: `EURUSD`, `GBPUSD`, `USDJPY`, `XAUUSD`
4. Click **Show** for each, then **Close**

### 1.4 Enable Algo Trading

In MT5: **Tools → Options → Expert Advisors**
- Check **Allow automated trading**
- Check **Allow DLL imports**
- Click **OK**

---

## Part 2 — Python Environment

### 2.1 Install Python

Download from [python.org/downloads](https://www.python.org/downloads/)

During installation:
- Check **"Add Python to PATH"** (critical)
- Choose **"Install for all users"**

Verify in a terminal:
```bash
python --version
# Expected: Python 3.10.x or higher
```

### 2.2 Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/claude-trader.git
cd claude-trader
```

Or download the ZIP from GitHub and extract it.

### 2.3 Create a Virtual Environment (Recommended)

```bash
python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` in your terminal prompt.

### 2.4 Install Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `anthropic` — Claude API
- `MetaTrader5` — MT5 Python bridge
- `pandas`, `numpy` — data processing
- `requests`, `feedparser` — news feeds
- `python-dotenv` — environment variables
- `openpyxl` — Excel backtest reports (optional)

Full install takes ~2 minutes. Verify:
```bash
python -c "import anthropic, MetaTrader5, pandas; print('All OK')"
```

---

## Part 3 — Telegram Bot

### 3.1 Create a Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. `Claude Trader`)
4. Choose a username ending in `bot` (e.g. `apex_trading_bot`)
5. BotFather will give you a **token** — save it (format: `1234567890:AAABBBCCC...`)

### 3.2 Get Your Chat ID

1. Search for **@userinfobot** in Telegram
2. Send `/start`
3. It replies with your **ID** (e.g. `123456789`) — save this

### 3.3 Start a Conversation with Your Bot

Search for your bot by its username and click **Start**. This is required before the bot can message you.

---

## Part 4 — Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Sign up or log in
3. Go to **API Keys** → **Create Key**
4. Copy the key (format: `sk-ant-api03-...`)

> **Cost estimate:** Running 24/7 costs ~$3–10/day depending on market activity (more trades = more Claude calls). Start on demo with budget alerts enabled.

---

## Part 5 — Configuration (.env)

### 5.1 Create the .env File

```bash
copy .env.example .env
```

### 5.2 Fill in Your Values

Open `.env` in any text editor (Notepad, VS Code):

```env
# ── Anthropic Claude ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-api03-YOUR_REAL_KEY

# ── MetaTrader 5 ──────────────────────────────────────────────────────────────
MT5_LOGIN=12345678                # your MT5 login number
MT5_PASSWORD=your_mt5_password    # your MT5 password
MT5_SERVER=MetaQuotes-Demo        # server name from MT5

# ── Telegram Bot ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN=1234567890:AAABBB-your-bot-token
TELEGRAM_CHAT_ID=123456789        # your personal chat ID

# ── Risk Management ───────────────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT=0.03           # 3% loss = system halts
MAX_RISK_PER_TRADE_PCT=0.01       # 1% per trade (recommended for demo)
MAX_OPEN_POSITIONS=3              # max 3 trades simultaneously

# ── Spike Detection ───────────────────────────────────────────────────────────
SPIKE_XAUUSD=15.0
SPIKE_EURUSD=0.0030
SPIKE_GBPUSD=0.0040
SPIKE_USDJPY=0.80

# ── News API (optional) ───────────────────────────────────────────────────────
NEWS_API_KEY=                     # leave blank — free sources work without this
```

### 5.3 Optional: Second Brain

APEX supports a secondary AI for Telegram command analysis. Leave blank if not needed:

```env
SECOND_BRAIN_PROVIDER=deepseek
SECOND_BRAIN_MODEL=deepseek-chat
DEEPSEEK_API_KEY=sk-YOUR_KEY      # get free at platform.deepseek.com
```

---

## Part 6 — First Run

### 6.1 Test Mode (No Trading)

Verify everything connects without placing any orders:

```bash
python main.py --demo
```

Expected output:
```
[MANAGER] Connected to MT5 — Balance: $10,000.00
[NEWS] ForexFactory: 3 events found
[NEWS] RSS feeds: 18 headlines fetched
[DOLLAR] Analyzing 4 macro pillars...
[GOLD] Fetching XAUUSD H4/H1/M15...
...
[MANAGER] Demo mode — no trades placed
```

If you see errors, check the troubleshooting section below.

### 6.2 Single Cycle (One Analysis Pass)

Runs one full analysis cycle and sends a Telegram report. No looping.

```bash
python main.py
```

Check your Telegram — you should receive a cycle report within ~60 seconds.

### 6.3 Full Production Mode (Both Loops, 24/7)

```bash
python main.py --loop
```

This starts:
- **Main loop** every 15 minutes (news → analysis → entry decisions → MT5 execution)
- **Watch loop** every 10 seconds (monitors open positions for spike management)
- **Session report** automatically at 19:00 UTC (NY close)

Stop with `Ctrl+C` — the system sends a session report before shutting down.

---

## Part 7 — Dashboard

Start the performance dashboard server:

```bash
python dashboard_server.py
```

Open in your browser: **http://localhost:8080**

Shows: balance curve, per-agent P&L, last AI signals, open positions, closed trades table.

---

## Part 8 — Backtest (Optional)

Test the strategy on 2 years of historical data without Claude API cost:

```bash
# Download historical data first (one-time, ~2GB)
python download_histdata.py

# Run backtest for all agents
python backtest.py --all --from 2024-01-01 --csv

# Single agent backtest
python backtest.py --agent GOLD --from 2024-01-01 --csv
```

Generate Excel report:
```bash
python create_backtest_report.py
```

---

## Telegram Commands

Once the bot is running, send these from your Telegram chat:

| Command | Action |
|---|---|
| `/positions` | List all open positions with entry, SL, TP, P&L |
| `/status` | Account balance, equity, floating P&L, bot status |
| `/close 12345678` | Close one position by ticket number |
| `/closeall` | Emergency close all APEX positions |

---

## Troubleshooting

### MT5 connection fails
```
[MANAGER] MT5 init failed
```
- Make sure MetaTrader 5 is running (the desktop app must be open)
- Double-check `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` in `.env`
- The server name must match exactly what MT5 shows (case-sensitive)

### Symbols not found
```
[GOLD] Symbol XAUUSD not found
```
- In MT5: right-click Market Watch → Symbols → search XAUUSD → Show

### Telegram not receiving messages
- Make sure you clicked **Start** on your bot in Telegram
- Verify `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` have no extra spaces
- Test with: `python -c "import requests; r = requests.get('https://api.telegram.org/botYOUR_TOKEN/getMe'); print(r.json())"`

### Claude API error
```
AuthenticationError: Invalid API key
```
- Check `ANTHROPIC_API_KEY` starts with `sk-ant-`
- Verify billing is set up at console.anthropic.com

### Fear/Greed always showing 50
- This is a known issue with CNN's API blocking automated requests
- The system automatically falls back to Alternative.me (crypto-focused but acceptable)
- No action needed — fix is built in

### ModuleNotFoundError
```bash
pip install -r requirements.txt
```
Make sure your virtual environment is activated (`venv\Scripts\activate`).

---

## VPS Deployment (24/7 Production)

### Recommended: Contabo VPS ($5/month)

1. Order a Windows VPS at contabo.com
2. RDP into the server
3. Install MT5, Python, and Git (same steps as above)
4. Clone the repo and configure `.env`

### Auto-Start on Boot (Windows Task Scheduler)

1. Open **Task Scheduler** (search in Start menu)
2. **Create Basic Task** → name it `Claude Trader`
3. Trigger: **When the computer starts**
4. Action: **Start a program**
   - Program: `C:\Python311\python.exe`
   - Arguments: `main.py --loop`
   - Start in: `C:\claude-trader\`
5. Check **Run whether user is logged on or not**
6. Save — the bot will auto-restart after VPS reboots

### Keep-Alive Script (Optional)

Create `watchdog.bat` to restart if the process dies:

```batch
@echo off
:loop
python main.py --loop
echo Bot crashed or stopped. Restarting in 10 seconds...
timeout /t 10
goto loop
```

Run this instead of `main.py` directly in Task Scheduler.

---

## Going Live (Real Money)

Before switching from demo to live, follow these steps:

1. Run demo for **at least 2 weeks** with positive P&L
2. Open a live account with a regulated broker (IC Markets, Pepperstone, etc.)
3. Update `.env` for live trading:
   ```env
   MT5_LOGIN=your_live_login
   MT5_PASSWORD=your_live_password
   MT5_SERVER=ICMarkets-Live01      # your broker's live server
   MAX_RISK_PER_TRADE_PCT=0.005     # reduce to 0.5% for live
   MAX_OPEN_POSITIONS=2             # reduce to 2 for live
   SPIKE_XAUUSD=10.0               # tighten spike thresholds
   SPIKE_EURUSD=0.0020
   SPIKE_GBPUSD=0.0030
   SPIKE_USDJPY=0.60
   ```
4. Run one `python main.py` (single cycle) first to verify connection
5. Monitor closely for first 48 hours

---

## File Structure Reference

```
claude-trader/
├── main.py                  ← entry point (start here)
├── mt5_executor.py          ← live order execution
├── dashboard_server.py      ← performance dashboard
├── dashboard.html           ← dashboard frontend
├── backtest.py              ← rule-based historical backtest
├── download_histdata.py     ← 5-year historical data downloader
├── create_backtest_report.py← Excel report generator
├── .env                     ← your credentials (never commit this)
├── .env.example             ← template (safe to commit)
├── requirements.txt
├── agents/
│   ├── manager.py           ← CEO: capital manager + final approver
│   ├── news.py              ← news & sentiment (rule-based, no AI cost)
│   ├── tracker.py           ← performance analyst
│   ├── strategist.py        ← daily top-down plans (Claude Opus)
│   ├── dollar.py            ← DXY macro compass
│   ├── gold.py              ← XAUUSD entry specialist
│   ├── eurusd.py            ← EURUSD entry specialist
│   ├── gbpusd.py            ← GBPUSD entry specialist
│   ├── usdjpy.py            ← USDJPY entry specialist
│   ├── monitor.py           ← 24/7 position risk manager
│   ├── gold_watch.py        ← XAUUSD spike monitor
│   ├── eurusd_watch.py      ← EURUSD spike monitor
│   ├── gbpusd_watch.py      ← GBPUSD spike monitor
│   └── usdjpy_watch.py      ← USDJPY spike monitor
└── logs/
    ├── trades.json           ← all proposals and decisions
    ├── executions.json       ← live MT5 execution results
    └── strategist_memory.json← STRATEGIST persistent memory
```

---

## Quick Reference

```bash
# Activate environment
venv\Scripts\activate

# Test connection (no trades)
python main.py --demo

# Single cycle
python main.py

# 24/7 production mode
python main.py --loop

# Watch only (no new entries, monitor existing positions)
python main.py --watch

# Dashboard
python dashboard_server.py
# → open http://localhost:8080

# Backtest
python backtest.py --all --from 2024-01-01 --csv
```
