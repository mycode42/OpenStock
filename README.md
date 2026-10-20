# Algo Trader

Algo Trader is a paper-trading system with a continuous strategy coordinator, broker reconciliation, and risk-checked Alpaca execution. Its research pipeline tests strategy candidates separately from live trading.

## Core modules

- `src/algo/daemon.py` coordinates strategy scans, portfolio state, and execution.
- `src/algo/strategies/` contains the live strategies and portfolio coordinator.
- `src/algo/research/auto_research.py` runs discovery and rolling out-of-sample verification without placing orders.
- `src/algo/strategies/event_driven/ai_runner.py` publishes short-lived analyst recommendations without placing orders.
- `execute_trades.py`, `risk_manager.py`, and `src/execution/` provide shared order execution and risk controls.
- `performance_tracker_v2.py` reports the Algo paper account and broker-confirmed fills.

The old standalone Swing/Day application and its unused analyst agents, reports, account routes, and tools have been removed. The strategy names `swing_*` and `intraday_*` describe holding periods within Algo Trader.

## Setup

Requirements: Python 3.11+, Poetry, and an Alpaca paper account. Copy `.env.example` to `.env`, then configure the Algo paper credentials, market-data tier, and any LLM keys you use. Keep `.env` private; it is ignored by Git.

```bash
git clone git@github.com:mycode42/algo-trader.git
cd algo-trader
poetry install
cp .env.example .env
```

The systemd unit is `algo-trader.service` and runs from `/opt/algo-trader`.

```bash
sudo install -m 0644 deploy/algo-trader.service /etc/systemd/system/algo-trader.service
sudo systemctl daemon-reload
sudo systemctl enable --now algo-trader.service
```

## Useful commands

```bash
# Read-only service, scan, and broker-protection status
poetry run python -m src.algo.healthcheck

# Read-only account and strategy report
poetry run python -m src.algo.monitor_report

# Adaptive research cycle; writes research artifacts but does not trade
poetry run python -m src.algo.research.auto_research --workers 8

# Research-only portfolio replay
poetry run python -m src.algo.backtest --help
```

Scheduled jobs and report prompts are documented in [`ops/`](ops/). Research behavior is described in [`docs/adaptive_research.md`](docs/adaptive_research.md) and [`docs/event_driven_strategy.md`](docs/event_driven_strategy.md).

## Layout

```text
src/algo/       Live coordinator, strategies, backtest, and research
src/agents/     Analyst graph shared with Event Driven research
src/execution/  Broker order routing and safety checks
scripts/        Build and validation utilities
tests/          Algo unit and integration tests
docs/           Current guides and archived historical material
ops/            Automation references and report prompts
deploy/         Systemd service unit
data/           Local runtime state and research artifacts (ignored by Git)
```

All trading commands use the Algo paper account. Only the Algo coordinator may submit Algo orders.
