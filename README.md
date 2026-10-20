# Algo Trader

Algo Trader is a paper-trading system with a continuous portfolio coordinator,
risk-checked Alpaca execution, and a separate research pipeline.

## Core workflows

- The live coordinator in portfolio/ owns account state, reconciliation, and safety checks.
- Strategies in strategies/ are shared by live trading, replay, and research.
- autoresearch/ contains adaptive research and deterministic backtests.
- execution/ owns order routing and hard risk validation.
- broker/ and market/ contain account, calendar, and market-data integrations.
- reporting/ contains account, journal, monitoring, fill, and validation reports.
- runtime/ contains paths shared by live and research code.
- strategies/event_driven/analysis/ contains the LLM analyst workflow that creates event-driven signals.

The strategy names `swing_*` and `intraday_*` describe holding periods. The live
service uses one Algo paper-account route.

## Setup

Requirements: Python 3.11+, Poetry, and an Alpaca paper account. Copy .env.example
to .env, then configure the Algo paper credentials, market-data tier, and any LLM
keys you use. Keep .env private; it is ignored by Git.

    git clone git@github.com:mycode42/algo-trader.git
    cd algo-trader
    poetry install --with ai,ml,research
    cp .env.example .env

The systemd unit is algo-trader.service and runs from /opt/algo-trader.

    sudo install -m 0644 ops/systemd/algo-trader.service /etc/systemd/system/algo-trader.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now algo-trader.service

## Useful commands

    # Read-only service, scan, and broker-protection status
    poetry run python -m portfolio.healthcheck

    # Read-only account and strategy report
    poetry run python -m reporting.monitor

    # Adaptive research cycle; writes artifacts but does not trade
    poetry run python -m autoresearch.auto_research --workers 8

    # Research-only portfolio replay
    poetry run python -m autoresearch.backtest --help

    # Validate and execute decisions from standard input or a file
    poetry run python -m execution --dry-run --file decisions.json

Scheduled jobs and report prompts are documented in ops/. Research behavior is
described in docs/adaptive_research.md and docs/event_driven_strategy.md.

The optional `ai`, `ml`, and `research` dependency groups add the event analyst,
statistical/tree models, and yfinance-backed research/reporting features. A
minimal coordinator install can omit them with `poetry install`; install the
groups used by the configured strategies and scheduled jobs.

## Layout

    autoresearch/ Research pipelines, historical events, backtesting, and benchmarks
    broker/       Alpaca account and portfolio adapters
    execution/    Order routing, risk validation, and execution adapters
    market/       Market data, API models, calendars, sessions, and regimes
    portfolio/    Live daemon, config, state, and protection
    reporting/    Monitoring, performance, fills, journal, and validation reports
    runtime/      Shared runtime paths and local-state locations
    strategies/   Strategy implementations, coordination, and event analysis
    data/         Local runtime state and research artifacts (ignored by Git)
    docs/         Current guides
    ops/          Automations, prompts, and systemd service unit
    scripts/      Build and validation utilities
    tests/        Unit and integration tests

All trading commands use the Algo paper account. Only the Algo coordinator may
submit Algo orders.
