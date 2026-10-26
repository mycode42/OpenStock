# Options research profiling benchmark

Run a bounded sample of the production options research pipeline with cProfile
artifacts for the parent process and its JSONL normalization, point-in-time chain
preparation, and walk-forward fold workers. Artifacts and the benchmark run's
research report stay under `/tmp`; the production archive, run ledger, latest
reports, and live options settings are not changed.

The default run profiles the full GLD history from the SQLite store, uses two
search candidates and two walk-forward periods, and requests up to eight
workers. GLD long volatility is a representative heavy case: the latest nightly
profile timing showed chain preparation taking 173 of 206 seconds for this
ticker and sleeve. SQLite profiles use the full ticker history; JSONL profiles
can use a bounded tail with `--snapshot-count`.

```bash
poetry run python -m options.research.benchmarks.profile_options_research
```

Use `--mode load` to isolate database reading and chain preparation. Pass a
JSONL path with `--snapshot-count 0` to profile the complete archive, or use
`--workers 1` to capture a single-process profile:

```bash
poetry run python -m options.research.benchmarks.profile_options_research \
  --strategy vrp --underlying SPY --mode full --workers 8
```

The collector now writes to SQLite directly; pass the shared
`options_history.sqlite3` store with `--mode load --snapshot-count 0`.
Legacy JSONL input remains supported for external benchmark fixtures.

`benchmark.json` records the workload, wall and parent CPU time, child CPU and
peak child RSS, dataset-loading timings, and paths to profile summaries. Raw
`.pstats` files are kept alongside cumulative-time and self-time summaries.
Full mode reduces candidate, fold, robustness, and Monte Carlo counts so it is
repeatable as a performance sample; its strategy metrics are not a qualification
result. Every generated run is placed below the benchmark output directory.
Run it when the nightly research cycle is idle so worker timings reflect this
benchmark instead of competing jobs.

The nightly launcher now runs the options batch to completion before starting
equity AutoResearch, avoiding overlap between their CPU and memory worker pools.
