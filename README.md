# Algo Trader

**Research-led automation for systematic equities and options.**

[![Release](https://img.shields.io/badge/release-v3.0.0-2563eb)](https://github.com/mycode42/algo-trader/releases/tag/v3.0.0)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE)

Algo Trader brings strategy research, portfolio coordination, paper execution,
and reporting into one Python platform. Equity strategies run under a persistent
portfolio coordinator. Options trade through a separate paper account, with
their exposure included in combined portfolio risk checks.

> **Paper trading only.** Broker routes are pinned to Alpaca Paper. Research
> results and parameter candidates are never promoted automatically.

## What it does

| | Capability |
| --- | --- |
| **Equity strategies** | Regime-aware trend, momentum, mean-reversion, multi-factor, and scheduled-event strategies; long and short signals within portfolio limits. |
| **Options sleeves** | Volatility risk premium, event-driven structures, and an optional directional overlay. Supports spreads, iron condors, straddles, strangles, protective puts, and collars. |
| **Portfolio controls** | Persistent strategy state, broker reconciliation, entry coordination, exposure budgets, and a final risk-manager approval before Algo orders. |
| **Options risk** | Separate options account and journal; combined equity/options delta, Greeks, defined loss, buying power, stress, and assignment checks. |
| **Research** | Independent equity and options research loops, with walk-forward validation, execution costs, regime breakdowns, stress checks, and holdouts. |
| **Reporting** | Health and account reports, order/fill reconciliation, strategy attribution, and FIFO realized P&L for long and short trades. |

**Signal → portfolio coordinator → risk checks → Alpaca Paper.** Strategies
propose trades; the coordinator manages portfolio context and ownership; the
risk manager has the final say. The options paper runner is independently
invoked and verifies its own account route.

## Quick start

Requires Python 3.11+, Poetry, and an Alpaca paper account.

```sh
git clone https://github.com/mycode42/algo-trader.git
cd algo-trader
poetry install --with ai,ml,research
cp .env.example .env
```

Configure `ALPACA_ALGO_API_KEY`, `ALPACA_ALGO_API_SECRET`, and
`ALPACA_ALGO_ACCOUNT_ID` for the equity coordinator. To use the options sleeve,
also configure `ALPACA_OPTIONS_API_KEY`, `ALPACA_OPTIONS_API_SECRET`, and
`ALPACA_OPTIONS_ACCOUNT_ID`. Keep `.env` private; it is ignored by Git.

The `ai`, `ml`, and `research` dependency groups are optional. They provide
event analysis, learned models, and additional research/data features.

## Run it

```sh
# Check coordinator, account, and broker-protection health
poetry run python -m portfolio.healthcheck

# Start the continuous Algo coordinator
poetry run python -m portfolio.daemon --mode algo

# Account report with broker fill-based realized P&L
poetry run python -m reporting.performance --mode algo --json --fill-report

# Equity research and portfolio replay
poetry run python -m autoresearch.auto_research --workers 8

# Options paper scan (dry run by default)
poetry run python -m options.paper

# Submit approved options orders to the options paper account
poetry run python -m options.paper --submit

# Research one options sleeve using historical chains or an IV-surface dataset
poetry run python -m options.research --strategy vrp --data data/options/research/chains.jsonl
```

For a continuously managed host, the systemd unit is
[`ops/systemd/algo-trader.service`](ops/systemd/algo-trader.service). Scheduled
OpenClaw jobs can run equity and options research in parallel and publish the
results in separate report sections; see [automation notes](ops/openclaw-crons.md).

## Research and configuration

Equity AutoResearch and options research are intentionally separate. The
options pipeline searches one sleeve at a time and validates rolling,
purged/embargoed folds plus an untouched holdout. It models spread-crossing
slippage and commissions, filters weak chain data, tracks Greeks, and checks
assignment, cost sensitivity, and trade-sequence risk. Missing historical
chains are reported as missing data; they do not become synthetic evidence of
strategy performance.

Paper and research settings are file-based:

- Equity strategy profiles and portfolio limits: `strategies/registry.py` and
  `portfolio/config.py`.
- Options paper settings: copy `options/config.default.json` to
  `data/options/config.json`.
- Options research windows, costs, filters, and candidate grids:
  `options/research/config.default.json`.
- Local state, journals, reports, and datasets live under `data/` and are not
  part of the source release.

## Project map

| Package | Role |
| --- | --- |
| `strategies/` | Equity, event-driven, and options strategy modules; coordinator logic |
| `portfolio/`, `execution/` | Persistent runtime, reconciliation, order routing, and risk gates |
| `options/`, `broker/`, `market/` | Options models and paper route, Alpaca adapters, and market data |
| `autoresearch/`, `options/research/` | Separate equity and options validation pipelines |
| `reporting/`, `ops/` | Account/fill reports, service unit, and scheduled automation notes |

## Guides

- [Options paper trading](docs/options_trading.md)
- [Options research and validation](docs/options_research.md)
- [Equity adaptive research](docs/adaptive_research.md)
- [Event-driven strategy](docs/event_driven_strategy.md)
- [Automation schedules](ops/openclaw-crons.md)

## Acknowledgments

This project is inspired by [AI Hedge Fund](https://github.com/virattt/ai-hedge-fund).
