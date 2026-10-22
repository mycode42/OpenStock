# Algo research benchmark

`profile_algo_research.py` profiles the production per-strategy optimizer job
and, in `full` mode, the main compute stages of the research path: factor
diagnostics, archive-backed discovery, rolling verification, exact candidate
validation, a forward challenger simulation, and coordinated portfolio replay.
It reads stored SIP bars, does not refresh market data, and writes all artifacts
and state to a timestamped directory under `/tmp` by default. Forward promotion
is disabled, and it does not touch nightly artifacts or live settings.

Run the default benchmark with:

```bash
poetry run python -m autoresearch.benchmarks.profile_algo_research --strategy momentum --mode full
```

The bounded workload uses eight symbols for the selected strategy, two
candidates, one discovery iteration, three verification folds, 21 test
sessions per fold, and the strategy's configured training windows. Factor
diagnostics use the covered fixed universe and omit SEC fundamentals. Momentum
requires at least seven symbols; Multi Factor uses at least its configured
20-symbol minimum. The benchmark profiles one strategy at a time. The
`--mode optimizer` option profiles factor diagnostics and the same per-strategy
discovery job used by repeated optimizer cycles, without verification or replay.

Each parent stage writes a readable cumulative-time profile and raw `.pstats`
file. Parallel verification, exact candidate validation, and portfolio replay
also write per-fold worker profiles. With one worker, the stage profile includes
the fold work directly. `benchmark.json` records the input cohort, timings,
worker count, research summaries, and profile paths.

The benchmark is a performance sample, not a strategy-quality evaluation. It
does not include SIP refresh, SEC fundamentals collection, all-strategy
concurrency, or live promotion. Its forward challenger uses isolated temporary
state and has promotion disabled.

To compare the Cython OHLCV aggregation loop directly with the Python fallback
on stored split-adjusted SIP bars, run:

```bash
poetry run python -m autoresearch.benchmarks.benchmark_intraday_aggregation
```

It checks exact daily-row parity per symbol and writes a timing summary to
`/tmp`. It defaults to every symbol with covered intraday history; pass
`--symbols` and date bounds for a smaller comparison.

To compare replay timeline construction and a full replay against the Python
fallback, run:

```bash
poetry run python -m autoresearch.benchmarks.benchmark_replay_timeline
```

It checks exact event-map parity and exact replay-metric parity, reports median
timeline timings, and writes the comparison JSON under `/tmp`.

To compare Python and Cython daily-bar cleanup on stored histories, run:

```bash
poetry run python -m autoresearch.benchmarks.benchmark_daily_bar_cleanup
```

It checks parity for stored and unsorted/invalid input rows, then reports
median timings over a workload sized to the profiled daily-cleaning call
count.
