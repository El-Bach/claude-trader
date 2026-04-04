# Contributing to Claude Trader

Thank you for your interest in contributing. This document explains how to get started.

## Ways to Contribute

- **Bug fixes** — open an issue first, describe what's broken and how to reproduce it
- **New instruments** — follow the agent template in `CLAUDE.md` ("How to Add a New Agent")
- **Strategy improvements** — backtest before and after using `backtest.py --compare`
- **Infrastructure** — VPS deployment, watchdog, CI/CD
- **Documentation** — clearer setup guides, screenshots, examples

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/claude-trader.git
cd claude-trader
pip install -r requirements.txt
cp .env.example .env
# Add a MetaQuotes demo account and Anthropic API key to .env

# Test without MT5 (dry run)
python main.py --demo

# Single cycle (needs MT5)
python main.py

# Run backtest (no API cost, no MT5 needed with --csv)
python backtest.py --all --from 2024-01-01 --csv
```

## Pull Request Guidelines

1. **One thing per PR** — don't combine unrelated changes
2. **Backtest any strategy change** — show before/after results in the PR description
3. **Don't increase API calls** — every unnecessary Claude call costs money
4. **Match existing code style** — Wilder RSI/ATR/ADX, same indicator standards as other agents
5. **No credentials** — never commit `.env`, account numbers, or API keys
6. **Update CLAUDE.md** if you change architecture, agent behavior, or risk constants

## Reporting Bugs

Open a GitHub issue with:
- Python version and OS
- What you expected vs what happened
- Relevant output from the terminal or Telegram

## Code Standards

```python
# MT5: always wrap in try/finally
try:
    mt5.initialize(...)
    # ... operations ...
finally:
    mt5.shutdown()

# Claude API: always strip markdown fences
raw = response.content[0].text.strip()
raw = raw.replace("```json", "").replace("```", "").strip()
result = json.loads(raw)

# Indicators: Wilder smoothing (consistent across all agents)
rsi_alpha = 1.0 / period
ewm(alpha=rsi_alpha, adjust=False)

# Never hardcode credentials
api_key = os.getenv("ANTHROPIC_API_KEY")
```

## Questions

Open a GitHub Discussion or issue — happy to help.
