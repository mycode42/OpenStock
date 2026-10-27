# Algo Trader

Systematic equity and options trading research with paper execution on Alpaca.

> **Paper trading only.** Broker routes are pinned to Alpaca Paper.

## What it does

- **Algo equity sleeve:** a rule-based coordinator selects strategies by a
  regime committee and trades the live universe, which is the selector's
  eligible subset of a 117-name candidate pool, recomputed each session.
  Strategies cover mean reversion, trend, momentum, multi-factor, and scheduled
  events on intraday and daily horizons. Multi-factor runs as a sector-neutral,
  dollar-neutral long/short book.
- **Options sleeve:** a separate paper account for volatility premium,
  long-volatility, and event structures, with combined equity/options risk
  checks.
- **Research:** equity AutoResearch (walk-forward validation, execution costs,
  regime breakdowns) and options research on a 117-symbol history universe,
  including a point-in-time eligible-universe selector.

Signal → coordinator → risk gate → Alpaca Paper. The risk gate has the final say.

## Setup

Requires Python 3.11+ and Poetry.

```sh
poetry install --with ai,ml,research
cp .env.example .env   # add Alpaca paper keys; .env is git-ignored
```

Equity uses `ALPACA_ALGO_API_KEY`, `ALPACA_ALGO_API_SECRET`, and
`ALPACA_ALGO_ACCOUNT_ID`. The options sleeve uses the `ALPACA_OPTIONS_*`
equivalents.

## Common commands

```sh
poetry run python -m portfolio.healthcheck                 # coordinator and broker health
poetry run python -m portfolio.daemon --mode algo          # run the Algo coordinator
poetry run python -m reporting.performance --mode algo --json --fill-report
poetry run python -m autoresearch.auto_research --workers 8
poetry run python -m options.paper                         # options dry run
poetry run python -m options.paper --submit                # submit to options paper
poetry run python -m options.research.universe_selection simulate --universe-scope history_117
```

Production runs as `ops/systemd/algo-trader.service`; scheduled research and
reports are described in [ops/openclaw-crons.md](ops/openclaw-crons.md).

## Live universe

Each session the daemon runs the eligible-universe selector over the 117-name
pool in `options/research/universe.json`. The result is stored as a dated
snapshot in `data/options/research/options_history.sqlite3`. Entries pause if
the snapshot is more than one session old, or if fewer than six names are
eligible. Positions that leave the universe are still managed until they exit.
Regime context stays on the static 24-name set in `portfolio/config.py`.

## State and backups

Coordinator state, the broker order ledger, and the research checkpoints all
live in SQLite. The state database at `data/algo_state.sqlite3` is backed up
once per trading day to `data/backups/algo_state/`; the newest 30 are kept. To
restore, stop the daemon and copy a backup over the database.

## Research and promotion

Research runs nightly. Parameter profiles that pass rolling walk-forward and
forward-simulation gates go live automatically, without human confirmation; a
detected drift warning pauses promotion. Every promotion is recorded in
`data/research/algo-auto/promoted-profile.json`.

Configuration is file-based:

| Setting | File |
| --- | --- |
| Equity limits and strategy switches | `portfolio/config.py`, `data/algo-strategy-config.json` |
| Candidate pool | `options/research/universe.json` |
| Selector rules and live gates | `options/research/eligibility.default.json` |
| Options paper settings | `options/config.default.json` (copy to `data/options/config.json`) |
| Options research windows, costs, and grids | `options/research/config.default.json` |

Options research guides: [options_research.md](docs/options_research.md),
[options_trading.md](docs/options_trading.md). Equity research:
[adaptive_research.md](docs/adaptive_research.md).

## Acknowledgments

Inspired by [AI Hedge Fund](https://github.com/virattt/ai-hedge-fund).
