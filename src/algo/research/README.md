# Algo research layers

The scheduled pipeline entry point is `python -m src.algo.research.auto_research`.

Strategy IDs, bar intervals, execution groups, portfolio-cap groups, runtime
feature switches, and nightly workflow membership come from
`src/algo/strategies/catalog.py`. `settings.py` keeps the scheduled pipeline's
historical five-strategy `CORE_STRATEGIES` cohort explicit because that subset
is a separate policy from the larger set of strategies available to research.

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
artifacts. Multi Factor is a core strategy in the portfolio replay. Event Driven
remains a confirmation overlay and cannot open standalone positions. Multi
Factor follows the live runtime enable setting. Event Driven also follows live config and builds a
point-in-time historical calendar for the artifact interval when no CSV is
supplied. If that calendar can't be built or safely reused, the result records
that Event Driven was omitted.

Backtests use the live Algo risk and coordinator limits. The research and
optimization artifacts include a configuration snapshot and digest, while the
candidate banks vary signal filters only; they don't optimize position sizing,
stops, or portfolio constraints. This is the live-rule replay twin with bounded
profile selection, not a promise that the selected profile will work in future
markets.

The research and optimization layers do not change live configuration or place
orders. The scheduled pipeline also writes a recent Multi Factor candidate for
the shadow book; the scanner consumes only fresh, positive, sample-qualified
profiles while broker execution remains disabled. Fixed-window `research`
comparisons are descriptive; use walk-forward optimization and coordinator
holdouts to evaluate candidates.

## Scheduled pipeline

`python -m src.algo.research.auto_research` is the existing scheduler entry
point. It now delegates to the layered nightly orchestrator:

1. Refresh and fingerprint the shared market data and effective Algo config.
2. Reuse compatible per-strategy artifacts, and run missing or stale discovery
   and walk-forward optimization jobs concurrently.
3. Store each run in a fingerprinted cohort directory, so the exact research
   and optimization inputs are versioned together.
4. Load the verified adaptive short-window fold selections into the portfolio
   stage when every live-enabled strategy has a compatible artifact, check
   that their folds and cost assumptions align, and replay them through
   `PortfolioCoordinator` without retraining during replay. If adaptive
   verification is incomplete, the report identifies the fixed-profile
   fallback used for that cohort.
5. Write one nightly report with artifact paths, reuse status, stage timings,
   selected profiles, promotion gates, and portfolio results.
6. Run recent-window adaptive discovery for all eight strategies once per new
   trading session. Intraday profiles try 10/20/30 sessions and swing profiles
   try 30/60/90; when a window does not beat its baseline with a positive,
   sample-qualified result, the next window is tried.
7. Run rolling short-window walk-forward verification for all eight strategies
   for each new weekly cohort, reserving the final 252 sessions as a locked
   holdout. The fold search repeats the same window fallback using training
   data only and tests each selected candidate on the following unseen window.
   The per-strategy artifacts include discovery, verification, and coordinator
   paths. Multi Factor discovery may feed its separate shadow book; the other
   discovery results remain research evidence until reviewed.

The scheduler still owns the schedule and remains the invocation point.
`pipeline.py` is a small import facade; `nightly.py` owns orchestration,
`artifact_cache.py` validates reuse, `strategy_research.py` and
`strategy_optimization.py` implement the independent strategy stages, and
`portfolio_simulation.py` runs coordinator replay. `data.py` handles SIP data
and event preparation; `factor_research.py` fits Multi Factor diagnostics;
`historical_events.py` builds point-in-time event history;
`event_calendar_refresh.py` refreshes the live event calendar. `settings.py`
holds paths and fixed policy, while `files.py` handles locks and atomic writes.

The orchestrator retains the frozen-profile cohort discovery ending
2020-12-31 as a comparison stream. Adaptive verification starts at the earliest
overlapping stored SIP session and ends at the latest completed Friday. Its
aligned coordinator artifacts use 90-session outer windows so the portfolio
folds line up across both intraday and swing strategies; each strategy still
fits only within its configured 10/20/30 or 30/60/90-session window. A final
252-session holdout stays outside all rolling folds. Adaptive verification
artifacts are reused only when the strategy, data range, universe, window
policy, candidate count, and nightly policy version match. Worker processes
share the configured CPU budget across strategies and walk-forward folds.

The first uncached run still reads the same SQLite store separately in each
strategy worker. Cohort reuse avoids repeating that work when inputs have not
changed, but a shared range-scoped bar/feature cache remains a separate
efficiency improvement.

The event utilities can be run directly from this package:

```bash
python -m src.algo.research.historical_events --start 2016-01-01
python -m src.algo.research.event_calendar_refresh
```
