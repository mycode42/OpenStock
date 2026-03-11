# Swarm Trader

Multi-agent AI trading system. 19 analyst agents — 13 LLM-powered personalities (Buffett, Munger, Burry, and more) plus 6 data/quant specialists — independently analyze stocks. A Portfolio Manager aggregates their signals to make trading decisions. Executes on Alpaca paper trading.

**Multi-provider LLM support** — 13 providers: OpenAI, Anthropic, Google, DeepSeek, Groq, Ollama, xAI, OpenRouter, Azure OpenAI, GigaChat, Alibaba, Meta, Mistral.

**Zero paid data APIs required.** Hybrid data layer tries financialdatasets.ai first, falls back to SEC EDGAR + yfinance. Works fully free out of the box.

> Built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), extended with free data sources, multi-provider LLM support, Alpaca execution, custom agents, and automation.

---

## What's Different

| Feature | Upstream | Swarm Trader |
|---|---|---|
| LLM providers | Single provider | **13 providers (Ollama, OpenAI, Anthropic, Google, etc.)** |
| Financial data | financialdatasets.ai ($200/mo) | **Hybrid: financialdatasets.ai → SEC EDGAR + yfinance (free)** |
| Trade execution | Simulated only | **Alpaca paper trading** |
| Analyst agents | 12 built-in | **19 agents (12 + apex + 6 data/quant)** |
| Custom agents | Not supported | **Create your own analyst agents** |
| Automation | Manual runs | **Cron-based daily pipeline** |
| Agent-native | Interactive CLI | **Fully headless, `.env` config, structured JSON output** |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              Alpaca Portfolio State                   │
│         (positions, cash, market values)              │
└──────────────────────┬──────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌───────────┐ ┌────────────┐
│ Data Agents  │ │ LLM Agents│ │ Risk Agent │
│              │ │           │ │            │
│ Fundamentals │ │ Buffett   │ │ Position   │
│ Technical    │ │ Burry     │ │ Sizing     │
│ Sentiment    │ │ Wood      │ │ Volatility │
│ Growth       │ │ + 10 more │ │            │
│ Valuation    │ │ + custom  │ │            │
│ News         │ │           │ │            │
└──────┬───────┘ └─────┬─────┘ └─────┬──────┘
       │               │             │
       └───────────────┼─────────────┘
                       ▼
              ┌─────────────────┐
              │ Portfolio Manager│
              │ Aggregates all  │
              │ signals, decides│
              │ buy/sell/hold   │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  Safety Rails   │
              │  + Alpaca Exec  │
              └─────────────────┘
```

---

## Data Sources

Hybrid data layer: tries financialdatasets.ai first (if API key is set), falls back to SEC EDGAR + yfinance on failure or empty results. No paid API keys required for data — works fully free out of the box.

| Data Type | Source | Cache TTL |
|---|---|---|
| Prices (OHLCV) | yfinance | 15 min |
| Financial metrics (P/E, margins, etc.) | yfinance `.info` + SEC EDGAR XBRL | 24 hrs |
| Financial statements (line items) | SEC EDGAR XBRL companyfacts | 24 hrs |
| Insider trades | SEC EDGAR Form 4 filings | 7 days |
| Company news | yfinance news feed | 15 min |
| Market cap / company info | yfinance + SEC EDGAR | 24 hrs |
| CIK resolution | SEC EDGAR company_tickers.json | 30 days |

The data layer (`src/tools/api.py`) dispatches to `api_original.py` (paid) first, then `api_free.py` (free). Same function signatures, same return types — agents don't know the difference.

---

## Quick Start

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/)
- [Alpaca](https://app.alpaca.markets) paper trading account (free)
- At least one LLM provider configured. Popular options:
  - [Ollama](https://ollama.ai/) (local, free) — `ollama pull llama3:8b`
  - [OpenAI](https://platform.openai.com/) — set `OPENAI_API_KEY` in `.env`
  - [Anthropic](https://console.anthropic.com/) — set `ANTHROPIC_API_KEY` in `.env`
  - [Google](https://aistudio.google.com/) — set `GOOGLE_API_KEY` in `.env`
  - Any of the other 13 supported providers (see `src/llm/models.py`)

### Setup

```bash
git clone https://github.com/zhound420/swarm-trader.git
cd swarm-trader
poetry install

cp .env.example .env
# Add your Alpaca keys and LLM provider keys to .env
```

### Verify Data Layer

```bash
python test_data.py --ticker NVDA
```

Expected output:
```
============================================================
  SMOKE TEST — NVDA
  Data source: SEC EDGAR + yfinance (api_free.py)
============================================================
  get_prices                     ✅ PASS (20 days)
  get_financial_metrics          ✅ PASS (3 periods, market_cap=...)
  search_line_items              ✅ PASS (4 periods, fields: [...])
  get_insider_trades             ✅ PASS (10 trades, ...)
  get_company_news               ✅ PASS (10 articles)
  get_market_cap                 ✅ PASS ($...)
  get_price_data                 ✅ PASS (20 rows, ...)
============================================================
  Result: 7/7 passed
============================================================
```

### Run

```bash
# Dry run — analyze holdings, show what trades would happen
poetry run python run_hedge_fund.py

# Analyze specific tickers with reasoning
poetry run python run_hedge_fund.py --tickers NVDA,AVGO,TSM --show-reasoning

# Actually execute trades on Alpaca paper
poetry run python run_hedge_fund.py --execute

# Chat-friendly output (for Telegram/Discord)
poetry run python run_hedge_fund.py --telegram
```

---

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--execute` | off (dry run) | Place real orders on Alpaca paper |
| `--tickers X,Y,Z` | all holdings | Analyze specific tickers |
| `--model NAME` | inherited from `openclaw.json` (fallback: `qwen3.5:397b-cloud`) | LLM model to use |
| `--analysts a,b,c` | `warren_buffett,michael_burry,cathie_wood,apex,fundamentals_analyst,technical_analyst` | Comma-separated analyst list |
| `--show-reasoning` | off | Print detailed reasoning from each agent |
| `--telegram` | off | Bullet-list output for chat |

---

## Analyst Agents

### LLM Agents (13)

12 legendary investor personalities + 1 custom:

| Agent | Philosophy |
|---|---|
| `warren_buffett` | Value investing, moats, margin of safety |
| `charlie_munger` | Quality at fair prices |
| `michael_burry` | Contrarian deep value, FCF |
| `cathie_wood` | Disruptive innovation |
| `peter_lynch` | Growth at reasonable price |
| `bill_ackman` | Activist, concentrated positions |
| `stanley_druckenmiller` | Macro, asymmetric bets |
| `ben_graham` | Deep value, net-nets |
| `phil_fisher` | Qualitative growth |
| `aswath_damodaran` | DCF valuation |
| `rakesh_jhunjhunwala` | Emerging market growth |
| `mohnish_pabrai` | Dhandho framework |
| `apex` | Aggressive growth, AI infrastructure, momentum plays |

### Data/Quant Agents (6)

| Agent | What it does |
|---|---|
| `fundamentals_analyst` | ROE, margins, P/E, P/B |
| `technical_analyst` | Trend, momentum, volatility |
| `sentiment_analyst` | Market sentiment |
| `growth_analyst` | Revenue acceleration, R&D |
| `valuation_analyst` | Fair value models |
| `news_sentiment_analyst` | News-driven sentiment signals |

### Custom Agents

Create your own — see `src/agents/apex.py` as a template. Register in `src/utils/analysts.py`. Full guide in [PLAYBOOK.md](./PLAYBOOK.md).

---

## Safety Rails

Every trade passes through all rails before execution:

| Rail | Value | Purpose |
|---|---|---|
| Max trade size | 10% of portfolio | No single trade too large |
| Max daily trades | 8 per session | Prevents runaway loops |
| Min confidence | 60% | Must be confident to act |
| Min keep | 5% | Never sells entire position |
| Paper only | Enforced | Hardcoded to paper endpoint |
| Dry run default | On | Must pass `--execute` to trade |

---

## Automation

Built for headless, cron-driven operation. Example daily schedule:

| Time | Job |
|---|---|
| 6:30 AM | Pre-market full analysis |
| 9:00 AM | Portfolio check + P/L report |
| 12:00 PM | Midday pulse (lighter agents) |
| 2:00 PM | Afternoon analysis |
| 4:30 PM | Post-close deep research |

See [PLAYBOOK.md](./PLAYBOOK.md) for complete cron setup.

---

## Project Structure

```
swarm-trader/
├── run_hedge_fund.py          # Main runner — analysis + execution
├── check_portfolio.py         # Quick Alpaca portfolio check
├── rebalance.py               # Bulk sell positions outside universe
├── test_data.py               # Data layer smoke test
├── PLAYBOOK.md                # Complete operations guide
├── .env.example               # Secret template
├── src/
│   ├── tools/
│   │   ├── api.py             # Hybrid dispatcher (paid → free fallback)
│   │   ├── api_free.py        # Free data layer (SEC EDGAR + yfinance)
│   │   └── api_original.py    # Original financialdatasets.ai client
│   ├── agents/                # 13 LLM + 6 data/quant agents + custom
│   ├── alpaca_integration.py  # Alpaca API + safety rails
│   └── llm/
│       ├── models.py          # 13 LLM provider definitions
│       ├── api_models.json    # API provider model catalog
│       └── ollama_models.json # Local Ollama model catalog
└── .cache/                    # Disk cache (gitignored)
```

---

## Credits

Built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund). Free data layer replaces the paid `financialdatasets.ai` dependency with SEC EDGAR XBRL + yfinance.

## Disclaimer

Educational and research purposes only. Not investment advice. Paper trading only.

## License

MIT
