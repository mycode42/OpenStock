# Algo research layers

The scheduled pipeline entry point is `python -m src.algo.research.auto_research`.
For independent per-strategy work and a portfolio simulation built from those
results, use `python -m src.algo.research`:

1. `research` evaluates a bounded, strategy-specific candidate bank over an
   exploratory interval. It saves each candidate's parameters and metrics, and
   deliberately does not select a winner.
2. `optimize` can consume that research artifact and run a chronological
   walk-forward fit. It requires the discovery interval to end before
   optimization starts, and writes `optimization/<strategy>/latest.json` with
   each fold's training-only selection and following out-of-sample result.
3. `portfolio` loads those strategy artifacts, checks their data windows and
   folds align, combines each fold's selected profiles, and tests the shared
   holdout window through the portfolio coordinator. It writes a coordinator
   report under `portfolio/`.

Example sequence:

```bash
# Explore candidate profiles in an earlier interval.
python -m src.algo.research research --strategy momentum-swing --start 2016-01-01 --end 2020-12-31
# Validate those profiles on later walk-forward folds.
python -m src.algo.research optimize --strategy momentum-swing --research-artifact data/research/algo-auto/layers/research/momentum/latest.json --start 2021-01-01 --end 2026-01-01 --workers 8
# Repeat research and optimize for every core family/flavor to include.
python -m src.algo.research portfolio --workers 8
```

There are seven standalone rule strategies, separated by actual live signal
flavor:

- Swing: `trend_following`, `momentum`, `swing_mean_reversion`
- Intraday: `mean_reversion`, `opening_range_breakout`, `intraday_trend`,
  `intraday_momentum`

Each has a baseline and seven coherent alternatives in `profiles.py`. The
search bank is deterministic and bounded; it doesn't synthesize new trading
logic or exhaust a Cartesian product. Each optimization fold selects the
highest-ranked eligible candidate using its training data only, then checks
that choice on the following frozen test interval. The selection and
neighbor-stability policy is recorded in the artifact. The next training fold
is independent of prior test results. Research artifacts can seed this search,
provided their interval ends before optimization begins.

`momentum-swing` aliases to canonical strategy id `momentum`. Other accepted
aliases include `trend-following-swing`, `mean-reversion-swing`,
`mean-reversion-intraday`, `opening-range-breakout-intraday`,
`trend-following-intraday`, and `momentum-intraday`. Each `research` or
`optimize` command runs one strategy; repeat it for each family/flavor being
trained. Without `--research-artifact`, optimize uses the built-in candidate
bank.

The intraday `intraday_trend` and `intraday_momentum` rules are disabled in the
current live runtime config. Individual research and optimization explicitly
enable them only inside replay. The combined portfolio defaults to the active
live strategy set; include disabled flavors with `--strategies` and
`--enable-disabled-intraday` to model a proposed enablement without changing
the service.

The optimization and portfolio stages use the local SIP bar database. Fetch the
required history first with `python -m src.algo.backtest fetch`, using the same
symbol list and date range. Include `multi_factor` in `--strategies` when fetching
so the fetch command includes its historical warmup. The portfolio stage
requires matching symbols, dates, costs, and walk-forward folds across its input
artifacts. Multi Factor and Event Driven are confirmation overlays: they can
confirm core entries but cannot open standalone positions. Multi Factor follows
the live runtime setting. Event Driven also follows live config and builds a
point-in-time historical calendar for the artifact interval when no CSV is
supplied. If that calendar can't be built or safely reused, the result records
that Event Driven was omitted.

Backtests use the live Algo risk and coordinator limits. The research and
optimization artifacts include a configuration snapshot and digest, while the
candidate banks vary signal filters only; they don't optimize position sizing,
stops, or portfolio constraints. This is the live-rule replay twin with bounded
profile selection, not a promise that the selected profile will work in future
markets.

All three layers are research-only. They do not change live configuration or
place orders. Fixed-window `research` comparisons are descriptive; use the
walk-forward optimization and coordinator holdouts to evaluate candidates.

## Scheduled pipeline

`python -m src.algo.research.auto_research` is the existing scheduler entry
point. It now delegates to the layered nightly orchestrator:

1. Refresh and fingerprint the shared market data and effective Algo config.
2. Reuse compatible per-strategy artifacts, and run missing or stale discovery
   and walk-forward optimization jobs concurrently.
3. Store each run in a fingerprinted cohort directory, so the exact research
   and optimization inputs are versioned together.
4. Load the fixed optimization artifacts into the portfolio stage, check that
   their folds and cost assumptions align, and replay them through
   `PortfolioCoordinator` without retraining during replay.
5. Write one nightly report with artifact paths, reuse status, stage timings,
   selected profiles, promotion gates, and portfolio results.

The scheduler still owns the schedule and remains the invocation point.
`pipeline.py` is a small import facade; `nightly.py` owns orchestration,
`artifact_cache.py` validates reuse, `strategy_research.py` and
`strategy_optimization.py` implement the independent strategy stages, and
`portfolio_simulation.py` runs coordinator replay. `data.py` handles SIP data
and event preparation; `factor_research.py` fits Multi Factor diagnostics;
`historical_events.py` builds point-in-time event history;
`event_calendar_refresh.py` refreshes the live event calendar. `settings.py`
holds paths and fixed policy, while `files.py` handles locks and atomic writes.

The current orchestrator runs discovery on the frozen interval ending
2020-12-31 and optimization from 2021-01-01 through the latest completed
Friday. A completed cohort is reused only when the data fingerprint, strategy
search versions, live configuration, windows, and costs match. Standalone
artifacts are also checked for matching strategy, data window, configuration,
costs, source research artifact, and recency before reuse. Worker processes
parallelize strategy stages, with one walk-forward worker per strategy to
avoid nested oversubscription.

The first uncached run still reads the same SQLite store separately in each
strategy worker. Cohort reuse avoids repeating that work when inputs have not
changed, but a shared range-scoped bar/feature cache remains a separate
efficiency improvement.

The event utilities can be run directly from this package:

```bash
python -m src.algo.research.historical_events --start 2016-01-01
python -m src.algo.research.event_calendar_refresh
```
