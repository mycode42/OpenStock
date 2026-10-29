# Algo Trader

Systematic equity and options trading research with paper execution on Alpaca.

> **Paper trading only.** Broker routes are pinned to Alpaca Paper.

This is the Algo Trader subtree of [OpenStock](../README.md). It runs as its own service and is read by the OpenStock dashboard.

## What it does

- **Algo equity sleeve:** a rule-based coordinator selects strategies by a regime committee and trades the live universe, which is the selector's eligible subset of a 117-name candidate pool, recomputed each session. Strategies cover mean reversion, trend, momentum, multi-factor, and scheduled events on intraday and daily horizons. Multi-factor runs as a sector-neutral, dollar-neutral long/short book.
- **Options sleeve:** a separate paper account for volatility premium, long-volatility, and event structures, with combined equity/options risk checks.
- **Research:** equity AutoResearch (walk-forward validation, execution costs, regime breakdowns) and options research on a 117-symbol history universe, including a point-in-time eligible-universe selector.

Signal → coordinator → risk gate → Alpaca Paper. The risk gate has the final say.

## Documentation

- [Setup](docs/setup.md): requirements, the one-command setup, and the environment
- [Scheduled automations](docs/automations.md): the jobs, the installer, and the OpenClaw or cron backends
- [Operations](docs/operations.md): common commands, the live universe, and state backups
- [Research and configuration](docs/research-and-config.md): promotion, config files, and the shared rule set
- Strategy and research guides: [options research](docs/options_research.md), [options trading](docs/options_trading.md), [equity research](docs/adaptive_research.md), [LLM-driven strategy](docs/llm_driven_strategy.md), [options long volatility](docs/options-long-volatility.md)
