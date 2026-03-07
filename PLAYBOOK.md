# AI Hedge Fund + Alpaca Trading — Complete Playbook

**Purpose:** Set up multi-agent AI trading with Alpaca paper execution, from scratch. Designed for AI agents and humans alike.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Setup from Scratch](#setup-from-scratch)
4. [Alpaca Paper Trading](#alpaca-paper-trading)
5. [How the Multi-Agent System Works](#how-the-multi-agent-system-works)
6. [Custom Agent Creation](#custom-agent-creation)
7. [Running Analysis](#running-analysis)
8. [Executing Trades](#executing-trades)
9. [Rebalancing](#rebalancing)
10. [Safety Rails](#safety-rails)
11. [Automation (Cron)](#automation-cron)
12. [Telegram Integration](#telegram-integration)
13. [Known Limitations](#known-limitations)
14. [Troubleshooting](#troubleshooting)
15. [File Reference](#file-reference)

---

## Overview

This system combines:
- **virattt/ai-hedge-fund** — Open-source multi-agent hedge fund framework
- **Alpaca Markets** — Paper trading API for order execution
- **Ollama** — Local LLM inference (zero API cost)
- **Custom agents** — Your own investment philosophy as an analyst agent

Each analyst agent embodies a different investment philosophy (Warren Buffett, Michael Burry, Cathie Wood, etc.). They independently analyze stocks, then a Portfolio Manager agent weighs all signals and makes trading decisions. Those decisions feed through safety rails and execute via Alpaca.

**Flow:**
```
Alpaca positions → AI agents analyze → Portfolio Manager decides → Safety rails validate → Alpaca executes
```

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11+ (3.14 works but shows Pydantic warnings) |
| Poetry | Python dependency manager (`curl -sSL https://install.python-poetry.org \| python3 -`) |
| Ollama | Local LLM server (`ollama serve`) with at least one model pulled |
| Alpaca account | Free paper trading account at https://app.alpaca.markets |
| Git | For cloning the repo |

### Ollama Models

The system uses Ollama for LLM inference. You need at least one model available:

```bash
# Local models (run on your hardware)
ollama pull llama3:8b          # Fast, decent quality (recommended default)
ollama pull phi3:mini           # Lightweight

# Cloud models via Ollama (routed through Ollama's cloud, free tier available)
# These appear automatically if configured in your Ollama setup:
# qwen3.5:cloud, glm-4.7:cloud, kimi-k2.5:cloud
```

---

## Setup from Scratch

### Step 1: Clone the repo

```bash
cd ~/your-workspace/projects
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
```

### Step 2: Install dependencies

```bash
# Install Poetry if you don't have it
curl -sSL https://install.python-poetry.org | python3 -

# Add Poetry to PATH (add to your shell profile)
export PATH="$HOME/.local/bin:$PATH"

# Install project dependencies
poetry install
```

### Step 3: Configure environment

Create `.env` in the project root:

```bash
cat > .env << 'EOF'
# Financial data - free for AAPL, GOOGL, MSFT, NVDA, TSLA
# For other tickers, get a key from https://financialdatasets.ai/
FINANCIAL_DATASETS_API_KEY=

# LLM providers (only needed if NOT using Ollama)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=
DEEPSEEK_API_KEY=
GOOGLE_API_KEY=
EOF
```

### Step 4: Configure Ollama models

Edit `src/llm/ollama_models.json` to match your available models:

```json
[
  {
    "display_name": "Llama 3 (8B Local)",
    "model_name": "llama3:8b",
    "provider": "Ollama"
  }
]
```

Add any models available in your Ollama instance. The `model_name` must match exactly what `ollama list` shows.

### Step 5: Verify setup

```bash
# Quick test — analyze NVDA with 3 agents
poetry run python src/main.py --tickers NVDA --ollama --model llama3:8b \
  --analysts warren_buffett,michael_burry,cathie_wood --show-reasoning
```

If you see agent analysis output and a portfolio summary, you're good.

---

## Alpaca Paper Trading

### Getting API Keys

1. Sign up at https://app.alpaca.markets (free)
2. Go to Paper Trading → API Keys
3. Generate a new key pair
4. You get: **API Key ID** and **API Secret Key**

### API Endpoints

| Endpoint | URL |
|---|---|
| Paper Trading | `https://paper-api.alpaca.markets/v2` |
| Live Trading | `https://api.alpaca.markets/v2` (⚠️ NEVER use for automated trading without extreme caution) |

### Test Your Keys

```bash
curl -s "https://paper-api.alpaca.markets/v2/account" \
  -H "APCA-API-KEY-ID: YOUR_KEY_HERE" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET_HERE" | python3 -m json.tool
```

You should see your account info with `status: ACTIVE`.

### Key API Calls

```bash
# Get account info
GET /v2/account

# Get all positions
GET /v2/positions

# Get open orders
GET /v2/orders?status=open

# Place a market order
POST /v2/orders
{
  "symbol": "NVDA",
  "qty": "10",
  "side": "buy",       # or "sell"
  "type": "market",
  "time_in_force": "day"
}

# Cancel all open orders
DELETE /v2/orders

# Cancel specific order
DELETE /v2/orders/{order_id}
```

### Setting Up the Integration

The file `src/alpaca_integration.py` reads credentials from environment variables. **Never hardcode keys in source files.**

Add your keys to the `.env` file in the project root:

```bash
# In .env (gitignored, never committed)
ALPACA_API_KEY=YOUR_KEY_HERE
ALPACA_API_SECRET=YOUR_SECRET_HERE
```

The `.env` file is automatically loaded by `python-dotenv` in `run_hedge_fund.py` and `rebalance.py`. The `alpaca_integration.py` module will raise an error if keys are missing.

Alternatively, export them as environment variables:

```bash
export ALPACA_API_KEY="YOUR_KEY_HERE"
export ALPACA_API_SECRET="YOUR_SECRET_HERE"
```

For OpenClaw agents, you can also use OpenClaw's SecretRef system:
```bash
openclaw secrets configure
```
This supports `env`, `file`, and `exec` providers for secret resolution.

---

## How the Multi-Agent System Works

### Architecture

```
┌─────────────────────────────────────────────────┐
│                   Input Layer                     │
│  Tickers + Date Range + Portfolio State           │
└──────────────────────┬──────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌───────────┐ ┌────────────┐
│ Data Agents  │ │ LLM Agents│ │ Risk Agent │
│              │ │           │ │            │
│ Fundamentals │ │ Buffett   │ │ Volatility │
│ Technical    │ │ Burry     │ │ Correlation│
│ Growth       │ │ Cathie    │ │ Position   │
│ Sentiment    │ │ Mordecai  │ │ Sizing     │
│ News         │ │ + 9 more  │ │            │
└──────┬───────┘ └─────┬─────┘ └─────┬──────┘
       │               │             │
       └───────────────┼─────────────┘
                       ▼
              ┌─────────────────┐
              │ Portfolio Manager│
              │                 │
              │ Weighs all      │
              │ signals, decides│
              │ buy/sell/hold   │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  Safety Rails   │
              │ + Alpaca Exec   │
              └─────────────────┘
```

### Agent Types

**Data-driven agents** (no LLM, pure calculation):
- `fundamentals_analyst` — ROE, margins, growth rates, P/E, P/B, P/S
- `technical_analyst` — Trend following, mean reversion, momentum, volatility, statistical arbitrage
- `growth_analyst` — Revenue acceleration, R&D investment, operating leverage
- `news_sentiment` — Recent news sentiment analysis
- `sentiment_analyst` — Market sentiment indicators

**LLM personality agents** (each has a distinct investment philosophy):
- `warren_buffett` — Value investing, margin of safety, moats
- `michael_burry` — Contrarian deep value, FCF analysis
- `cathie_wood` — Disruptive innovation, exponential growth
- `charlie_munger` — Quality companies at fair prices
- `peter_lynch` — Growth at a reasonable price (GARP)
- `bill_ackman` — Activist investing, concentrated positions
- `stanley_druckenmiller` — Macro-driven, asymmetric bets
- `ben_graham` — Deep value, net-net analysis
- `phil_fisher` — Scuttlebutt, qualitative growth analysis
- `aswath_damodaran` — Rigorous DCF valuation
- `rakesh_jhunjhunwala` — Emerging market growth
- `mohnish_pabrai` — Dhandho framework, low risk/high uncertainty

**Custom agents** (your own philosophy):
- `mordecai` — Aggressive growth, AI infrastructure heavy, contrarian (see below)

### Signal Format

Every agent returns the same structure per ticker:

```json
{
  "signal": "bullish|bearish|neutral",
  "confidence": 0-100,
  "reasoning": "Why this signal"
}
```

The Portfolio Manager aggregates all signals and decides:

```json
{
  "action": "buy|sell|hold",
  "quantity": 10,
  "confidence": 85,
  "reasoning": "Consensus analysis"
}
```

---

## Custom Agent Creation

To create your own analyst agent (like we did with Mordecai):

### Step 1: Create the agent file

Create `src/agents/your_agent.py`:

```python
"""Your Agent — describe the investment philosophy here."""

import json
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel
from typing_extensions import Literal

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.llm import call_llm
from src.utils.progress import progress


class YourSignal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    confidence: float
    reasoning: str


def your_agent(state: AgentState, agent_id: str = "your_agent"):
    """Your agent — describe what it does."""
    
    data = state["data"]
    tickers = data["tickers"]
    portfolio = data["portfolio"]
    
    analysis: dict = {}
    
    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Analyzing")
        
        # Build your analysis context
        # Access portfolio["positions"][ticker] for current holdings
        # Access data["analyst_signals"] for other agents' signals (if they ran first)
        
        analysis_context = {
            "ticker": ticker,
            # Add whatever context your agent needs
        }
        
        # Call the LLM with your prompt
        template = ChatPromptTemplate.from_messages([
            ("system", """Your agent's system prompt here.
            
Define the investment philosophy, signal rules, and output format."""),
            ("human", """Analyze ticker: {ticker}
            
Context: {analysis_context}

Return JSON:
{{
  "signal": "bullish|bearish|neutral",
  "confidence": <float 0-100>,
  "reasoning": "<your reasoning>"
}}"""),
        ])
        
        prompt = template.invoke({
            "ticker": ticker,
            "analysis_context": json.dumps(analysis_context, indent=2),
        })
        
        def default_signal():
            return YourSignal(signal="neutral", confidence=40.0, reasoning="Analysis error")
        
        output = call_llm(
            prompt=prompt,
            pydantic_model=YourSignal,
            agent_name=agent_id,
            state=state,
            default_factory=default_signal,
        )
        
        analysis[ticker] = {
            "signal": output.signal,
            "confidence": output.confidence,
            "reasoning": output.reasoning,
        }
        progress.update_status(agent_id, ticker, "Done", analysis=output.reasoning)
    
    message = HumanMessage(content=json.dumps(analysis), name=agent_id)
    
    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(analysis, agent_id)
    
    state["data"]["analyst_signals"][agent_id] = analysis
    progress.update_status(agent_id, None, "Done")
    
    return {"messages": [message], "data": state["data"]}
```

### Step 2: Register the agent

Edit `src/utils/analysts.py`:

1. Add the import at the top:
```python
from src.agents.your_agent import your_agent
```

2. Add to ANALYST_CONFIG dict (use the next `order` number):
```python
"your_agent_name": {
    "display_name": "Your Agent Display Name",
    "description": "One-line description",
    "investing_style": "Detailed investment philosophy description.",
    "agent_func": your_agent,
    "type": "analyst",
    "order": 18,  # Next available number
},
```

### Step 3: Use it

```bash
poetry run python run_hedge_fund.py --analysts your_agent_name,warren_buffett --tickers NVDA
```

---

## Running Analysis

### Using run_hedge_fund.py (recommended)

This is the all-in-one runner that connects Alpaca + Analysis + Execution:

```bash
# Dry run — analyze all current holdings, show what trades WOULD happen
poetry run python run_hedge_fund.py

# Analyze specific tickers only
poetry run python run_hedge_fund.py --tickers NVDA,AVGO,TSM

# Show detailed reasoning from each agent
poetry run python run_hedge_fund.py --show-reasoning

# Use a different model
poetry run python run_hedge_fund.py --model qwen3.5:cloud

# Use specific analysts
poetry run python run_hedge_fund.py --analysts warren_buffett,mordecai,technical_analyst

# Telegram-friendly output (no tables, bullet lists)
poetry run python run_hedge_fund.py --telegram

# Actually execute trades (⚠️ places real orders on Alpaca)
poetry run python run_hedge_fund.py --execute
```

### Using the original CLI (standalone, no Alpaca)

```bash
# Interactive mode — prompts for model, analysts, etc.
poetry run python src/main.py

# Non-interactive with all flags
poetry run python src/main.py --tickers NVDA,AVGO --ollama --model llama3:8b \
  --analysts warren_buffett,michael_burry,cathie_wood --show-reasoning
```

### Performance Notes

| Tickers | Agents | Model | Approx Time |
|---|---|---|---|
| 1 | 3 | llama3:8b | ~30 seconds |
| 1 | 6 | llama3:8b | ~1 minute |
| 20 | 6 | llama3:8b | ~5-8 minutes |
| 1 | all (18) | llama3:8b | ~3-5 minutes |

Cloud models (qwen3.5:cloud) are slower due to network latency but may give better analysis quality.

---

## Executing Trades

### Automated (via run_hedge_fund.py)

```bash
# This will actually place orders
poetry run python run_hedge_fund.py --execute
```

The system:
1. Fetches your current Alpaca positions
2. Runs multi-agent analysis
3. Portfolio Manager generates buy/sell/hold decisions
4. Safety rails validate each trade
5. Valid trades are placed as market orders

### Manual Rebalance (rebalance.py)

For planned portfolio restructuring (selling multiple positions outside your strategy):

```bash
# Edit rebalance.py to set:
# - SELL_TICKERS: list of tickers to sell
# - API credentials
# - MIN_KEEP_PCT: minimum % to keep (default 10%)

poetry run python rebalance.py
```

### Direct API (for one-off trades)

```bash
# Buy 10 shares of NVDA
curl -X POST "https://paper-api.alpaca.markets/v2/orders" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"NVDA","qty":"10","side":"buy","type":"market","time_in_force":"day"}'

# Check order status
curl "https://paper-api.alpaca.markets/v2/orders?status=open" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET"

# Cancel all open orders
curl -X DELETE "https://paper-api.alpaca.markets/v2/orders" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET"
```

---

## Safety Rails

Built into `src/alpaca_integration.py`. These protect against catastrophic trades:

| Rail | Default | What It Does |
|---|---|---|
| Max trade size | 5% of portfolio | No single trade can exceed 5% of total portfolio value |
| Max daily trades | 5 per session | Prevents runaway trading loops |
| Min confidence | 70% | Portfolio Manager must be ≥70% confident |
| Min keep | 10% | Never sell entire position (always keep at least 10%) |
| Paper only | Enforced | Hardcoded to paper-api endpoint |
| Dry run default | On | Must explicitly pass `--execute` to place orders |

### Adjusting Rails

Edit the constants at the top of `src/alpaca_integration.py`:

```python
MAX_TRADE_PCT = 0.05       # Max 5% of portfolio per trade
MAX_DAILY_TRADES = 5       # Max 5 trades per run
MIN_KEEP_PCT = 0.10        # Keep at least 10% of any position when selling
MIN_CONFIDENCE = 70        # Minimum confidence % to execute a trade
```

For a planned rebalance, you may want to temporarily increase `MAX_TRADE_PCT` or `MAX_DAILY_TRADES`, then reset them after.

---

## Automation (Cron)

### OpenClaw Cron (recommended)

Add a daily morning analysis cron:

```bash
openclaw cron add morning-analysis \
  --schedule "0 6 * * 1-5" \
  --command "cd ~/projects/ai-hedge-fund && poetry run python run_hedge_fund.py --telegram" \
  --model google/gemini-2.5-flash
```

This runs at 6:00 AM Mon-Fri (before market open at 6:30 AM PST).

### Heartbeat Integration

Add to your `HEARTBEAT.md`:

```markdown
### Alpaca Portfolio Check (daily, morning)
- API: `https://paper-api.alpaca.markets/v2`
- Key: `YOUR_KEY`
- Secret: `YOUR_SECRET`
- Check positions, daily P/L, total portfolio value
- Alert on big movers (>5% single position swing)
- Post summary to Telegram
- Track in `memory/heartbeat-state.json` under `alpacaPortfolio`
```

### Manual Check Script

```bash
# Quick portfolio check (no analysis, just positions)
curl -s "https://paper-api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET" | \
  python3 -c "
import sys,json
positions = json.load(sys.stdin)
total = sum(float(p['market_value']) for p in positions)
print(f'Portfolio: \${total:,.2f} ({len(positions)} positions)')
for p in sorted(positions, key=lambda x: abs(float(x['market_value'])), reverse=True)[:5]:
    pl = float(p['unrealized_pl'])
    print(f'  {p[\"symbol\"]}: \${float(p[\"market_value\"]):,.2f} ({(\"+\" if pl>=0 else \"\")}{pl:,.2f})')
"
```

---

## Telegram Integration

The `--telegram` flag on `run_hedge_fund.py` outputs a clean format suitable for Telegram. To pipe it to your bot:

### From a cron/heartbeat

```python
import subprocess

# Run analysis
result = subprocess.run(
    ["poetry", "run", "python", "run_hedge_fund.py", "--telegram"],
    capture_output=True, text=True,
    cwd="/path/to/ai-hedge-fund"
)

# Send to Telegram via OpenClaw message tool
# (from within an agent session)
```

### From an OpenClaw agent

```python
# Use the message tool directly
message(
    action="send",
    channel="telegram",
    target="YOUR_CHAT_ID",
    message=analysis_output
)
```

---

## Known Limitations

1. **Financial data coverage**: Free tier of financialdatasets.ai only covers AAPL, GOOGL, MSFT, NVDA, TSLA. All other tickers get "insufficient data" from fundamentals-dependent agents (Buffett, Burry, Cathie Wood). Fix: Get a paid API key from https://financialdatasets.ai/

2. **ETFs and leveraged products**: TQQQ, SOXL, UPRO, XLE don't have company fundamentals. Agents that rely on company data will return bearish-by-default or neutral. The technical analyst and custom agents (like Mordecai) work fine on ETFs.

3. **Weekend/after-hours**: Market orders placed when market is closed get `status: accepted` and execute at next market open. This is fine for paper trading but be aware of gap risk.

4. **Fractional shares**: Alpaca supports fractional shares but the system casts to `int`. Positions with fractional shares (e.g., 0.35 shares of PLTR) may show as 0 shares in the analysis.

5. **Rate limiting**: financialdatasets.ai has rate limits. With 20+ tickers and multiple agents, you may hit 429 errors. The API client has built-in retry with backoff (60s, 90s, 120s).

6. **Model quality**: `llama3:8b` is fast but sometimes gives weak analysis. For important decisions, use a larger/smarter model like `qwen3.5:cloud` or `kimi-k2.5:cloud`.

---

## Troubleshooting

### "Insufficient data" for most tickers
**Cause:** No `FINANCIAL_DATASETS_API_KEY` set, or using free tier.
**Fix:** Get a paid key, or rely on technical_analyst + your custom agent (these don't need financial data APIs).

### "insufficient qty available for order"
**Cause:** Shares are held by existing open orders.
**Fix:** Cancel existing orders first: `DELETE /v2/orders`

### "Model not found" with Ollama
**Cause:** Model name in `ollama_models.json` doesn't match `ollama list`.
**Fix:** Run `ollama list` and update `ollama_models.json` to match exactly.

### Process hangs on LLM agents
**Cause:** Cloud model timeout or Ollama not running.
**Fix:** Check `ollama serve` is running. Use local models for reliability.

### Pydantic V1 warning with Python 3.14
**Harmless.** The warning is: "Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater." Everything still works.

---

## File Reference

```
ai-hedge-fund/
├── .env                          # API keys (don't commit!)
├── pyproject.toml                # Python dependencies (Poetry)
├── run_hedge_fund.py               # ⭐ Main runner — analysis + Alpaca execution
├── rebalance.py                  # Manual rebalance script
├── PLAYBOOK.md                   # This file
├── src/
│   ├── main.py                   # Core hedge fund engine
│   ├── alpaca_integration.py     # ⭐ Alpaca API + safety rails
│   ├── agents/
│   │   ├── mordecai.py           # ⭐ Custom agent (our investment philosophy)
│   │   ├── warren_buffett.py     # Buffett agent
│   │   ├── michael_burry.py      # Burry agent
│   │   ├── cathie_wood.py        # Cathie Wood agent
│   │   ├── fundamentals.py       # Data-driven fundamentals
│   │   ├── technicals.py         # Data-driven technicals
│   │   └── ... (12 more agents)
│   ├── graph/
│   │   └── state.py              # Agent state management
│   ├── llm/
│   │   ├── models.py             # Model configuration
│   │   └── ollama_models.json    # ⭐ Available Ollama models
│   ├── tools/
│   │   └── api.py                # Financial data API client
│   └── utils/
│       ├── analysts.py           # ⭐ Agent registry (add new agents here)
│       ├── llm.py                # LLM call helper with retries
│       └── progress.py           # Progress display
└── app/                          # Web UI (optional)
```

**⭐ = Files you'll likely need to modify when setting up on a new agent.**

---

## Quick Start Checklist

For a new OpenClaw agent to get trading:

- [ ] Clone repo: `git clone https://github.com/virattt/ai-hedge-fund.git`
- [ ] Install: `poetry install`
- [ ] Create `.env` with API keys
- [ ] Update `ollama_models.json` with available models
- [ ] Add Alpaca credentials to `.env` file
- [ ] (Optional) Create custom agent in `src/agents/` and register in `src/utils/analysts.py`
- [ ] Test: `poetry run python run_hedge_fund.py --tickers NVDA`
- [ ] Go live: `poetry run python run_hedge_fund.py --execute`
- [ ] Automate: Add cron for daily morning analysis
- [ ] Monitor: Add Alpaca portfolio check to heartbeat
