# Algo research benchmark

`profile_algo_research.py` runs a small, isolated slice of the current adaptive
research pipeline: adaptive discovery, rolling verification, and coordinated
portfolio replay. It uses stored SIP bars, does not refresh market data, and
writes its artifacts and profiles to a timestamped directory under `/tmp` by
default. It does not touch the nightly artifact directory or live settings.

Run the default benchmark with:

```bash
poetry run python -m autoresearch.benchmarks.profile_algo_research --strategy momentum --workers 2
```

The default workload uses eight symbols, two candidates, three verification
folds, five out-of-sample sessions per fold, and the selected strategy's full
training-window ladder. Momentum requires at least seven symbols; Multi
Factor automatically uses at least its configured 20-symbol minimum. The
benchmark deliberately profiles one strategy at a time so each run stays
bounded; `--strategy` accepts only the five strategies in the current adaptive
research catalog.

Discovery writes a readable cumulative-time profile and a raw `.pstats` file.
Verification and portfolio replay write both files for each worker fold, so
their worker time can be compared with stage wall time when checking parallel
utilization. `benchmark.json` records the input cohort, timings, worker count,
research summaries, and profile paths.

The benchmark is a performance sample, not a strategy-quality evaluation. It
does not include the network data refresh or the separate factor diagnostics;
those stages are unchanged by the adaptive discovery and rolling verification
workload being measured here.

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
