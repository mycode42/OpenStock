# Algo research profiling jobs

`profile_algo_research.py` is a separate, read-only harness for measuring the
Algo research path. It does not refresh Alpaca data, write strategy artifacts,
or change live configuration. Logs, JSON summaries, and `cProfile` output go
under a timestamped `/tmp/algo-research-profile-*` directory by default.

Run it only after the current nightly research run has exited, so the benchmark
does not compete with that run for CPU, memory, or SQLite cache.

```bash
poetry run python benchmarks/profile_algo_research.py --help
poetry run python benchmarks/profile_algo_research.py --job all --timeout-seconds 120
```

To collect a complete baseline, run the suite without installing a per-job
timer:

```bash
poetry run python benchmarks/profile_algo_research.py --job all --no-timeout
```

This can run for several hours. Keep the output directory and `suite.json` as
the baseline record before changing the profiled code.

The bounded jobs cover:

- SQLite intraday and daily bar reads.
- Backtest construction (daily aggregation and derived history) separately
  from the replay loop.
- One strategy's fixed-window research candidate comparisons.
- A reduced one-year walk-forward run: one strategy, one profile, shorter
  train/test windows, and one worker so `cProfile` sees the full fold work.
- Factor-history loading and factor-model fitting over the stored history the
  model needs for its chronological train/development/holdout periods.

Each job has its own default 120-second budget, log, summary JSON, raw `.pstats`
file, process peak RSS, and readable top-function report. The `all` suite
starts each job in a fresh process so peak RSS is job-local and one profiler or
RSS monitor cannot contaminate another job. A timed-out job keeps the partial
profile. The walk-forward job deliberately uses one worker for call-level
profiling; worker-count scaling should be measured separately after these
profiles identify the expensive work.

For a shorter replay, pass explicit dates and symbols, for example:

```bash
poetry run python benchmarks/profile_algo_research.py \
  --job replay --strategy trend_following --days 90 --timeout-seconds 120
```

The factor-model job uses the complete stored daily history because its
chronological holdout is part of the computation. The other jobs default to
the latest year shared by the configured Algo symbols. These measurements are
diagnostic; the reduced walk-forward run is not a promotion or strategy-quality
evaluation.

To compare the Cython OHLCV aggregation loop directly with the Python fallback
on the stored split-adjusted SIP bars, run:

```bash
poetry run python benchmarks/benchmark_intraday_aggregation.py
```

This checks exact daily-row parity per symbol and writes a timing summary to
`/tmp`. It defaults to every symbol with covered intraday history; pass
`--symbols` and date bounds for a smaller comparison.

To compare replay timeline construction and a full replay against the Python
fallback, run:

```bash
poetry run python benchmarks/benchmark_replay_timeline.py
```

This checks exact event-map parity and exact replay-metric parity, reports
median timeline timings, and writes the comparison JSON under `/tmp`.

To compare Python and Cython daily-bar cleanup on the stored histories, run:

```bash
poetry run python benchmarks/benchmark_daily_bar_cleanup.py
```

This checks parity for stored and unsorted/invalid input rows, then reports
median timings over a workload sized to the profiled daily-cleaning call count.
