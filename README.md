# AI Hedge Fund — Agent Trading Playbook

A multi-agent AI hedge fund system that uses LLM-powered analyst agents to make trading decisions, with Alpaca paper trading integration for execution.

Built on top of [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), extended with:
- **Alpaca paper trading integration** — live order execution with safety rails
- **Custom analyst agents** — create agents with your own investment philosophy
- **Automated portfolio management** — cron-based daily analysis and rebalancing
- **Telegram reporting** — formatted output for chat-based monitoring

## How It Works

```
Alpaca positions → AI agents analyze → Portfolio Manager decides → Safety rails validate → Alpaca executes
```

Each analyst agent embodies a different investment philosophy (Buffett, Burry, Cathie Wood, etc.). They independently analyze stocks, then a Portfolio Manager agent weighs all signals and makes trading decisions. Those decisions pass through safety rails before executing via Alpaca.

### Built-in Agents

| Agent | Style |
|---|---|
| Warren Buffett | Value investing, moats, margin of safety |
| Michael Burry | Contrarian deep value, FCF analysis |
| Cathie Wood | Disruptive innovation, exponential growth |
| Charlie Munger | Quality at fair prices |
| Peter Lynch | Growth at a reasonable price (GARP) |
| Bill Ackman | Activist, concentrated positions |
| Stanley Druckenmiller | Macro-driven, asymmetric bets |
| Ben Graham | Deep value, net-net analysis |
| Phil Fisher | Scuttlebutt, qualitative growth |
| Aswath Damodaran | Rigorous DCF valuation |
| + 3 data agents | Fundamentals, technicals, sentiment |

Plus a template for creating your own custom agents (see [PLAYBOOK.md](./PLAYBOOK.md)).

## Quick Start

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/) (`curl -sSL https://install.python-poetry.org | python3 -`)
- [Ollama](https://ollama.ai/) with at least one model (`ollama pull llama3:8b`)
- [Alpaca](https://app.alpaca.markets) paper trading account (free)

### Setup

```bash
git clone https://github.com/zhound/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
```

Create `.env` in the project root:

```bash
# Alpaca Paper Trading (required for trade execution)
ALPACA_API_KEY=your-key-here
ALPACA_API_SECRET=your-secret-here

# Financial data — free for AAPL, GOOGL, MSFT, NVDA, TSLA
# For other tickers: https://financialdatasets.ai/
FINANCIAL_DATASETS_API_KEY=

# LLM providers (only needed if NOT using Ollama)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=
DEEPSEEK_API_KEY=
GOOGLE_API_KEY=
```

### Run

```bash
# Dry run — analyze holdings, show what trades WOULD happen
poetry run python run_hedge_fund.py

# Analyze specific tickers
poetry run python run_hedge_fund.py --tickers NVDA,AVGO,TSM

# Show detailed agent reasoning
poetry run python run_hedge_fund.py --show-reasoning

# Use a different model
poetry run python run_hedge_fund.py --model qwen3.5:cloud

# Pick specific analysts
poetry run python run_hedge_fund.py --analysts warren_buffett,michael_burry,technical_analyst

# Telegram-friendly output (no tables)
poetry run python run_hedge_fund.py --telegram

# Actually execute trades (⚠️ places real orders on Alpaca paper)
poetry run python run_hedge_fund.py --execute
```

## Safety Rails

Built into `src/alpaca_integration.py`:

| Rail | Default | Purpose |
|---|---|---|
| Max trade size | 5% of portfolio | No single trade exceeds 5% of total value |
| Max daily trades | 5 per session | Prevents runaway trading loops |
| Min confidence | 70% | Portfolio Manager must be ≥70% confident |
| Min keep | 10% | Never sells entire position |
| Paper only | Enforced | Hardcoded to paper-api endpoint |
| Dry run default | On | Must pass `--execute` to place orders |

## Creating Custom Agents

See [PLAYBOOK.md](./PLAYBOOK.md) for the full guide, including:

- Step-by-step custom agent creation
- Alpaca API reference
- Rebalancing scripts
- Cron automation setup
- Troubleshooting

## File Reference

```
ai-hedge-fund/
├── .env                          # API keys (gitignored)
├── run_hedge_fund.py             # Main runner — analysis + execution
├── rebalance.py                  # Manual rebalance script
├── PLAYBOOK.md                   # Complete setup & operations guide
├── src/
│   ├── main.py                   # Core hedge fund engine
│   ├── alpaca_integration.py     # Alpaca API + safety rails
│   ├── agents/                   # All analyst agents
│   ├── graph/                    # Agent state management
│   ├── llm/                      # Model configuration
│   ├── tools/                    # Financial data API client
│   └── utils/                    # Agent registry, helpers
└── app/                          # Web UI (optional)
```

## For AI Agents

This repo is designed to be consumed by autonomous AI agents (e.g., [OpenClaw](https://openclaw.ai)). The [PLAYBOOK.md](./PLAYBOOK.md) contains everything an agent needs to:

1. Set up the system from scratch
2. Create custom analyst agents with unique investment philosophies
3. Run analysis and execute trades
4. Automate via cron or heartbeat
5. Pipe results to Telegram or other channels

Point your agent at the playbook and let it rip.

## Disclaimer

This project is for **educational and research purposes only**. Not intended for real trading. No investment advice or guarantees. Past performance ≠ future results. Consult a financial advisor for real investment decisions.

## Credits

Built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) — the original multi-agent hedge fund framework.

## License

MIT License — see [LICENSE](./LICENSE) for details.
