# Algo adaptive research

The scheduled entry point is `python -m src.algo.research.auto_research`.
It refreshes and fingerprints SIP data, reuses compatible adaptive artifacts,
runs missing parameter discovery and rolling verification, then replays the
verified selections through the portfolio coordinator.

The pipeline uses dynamic parameter search over bounded signal-parameter
spaces. Discovery tries recent windows (10/20/30 sessions for intraday and
30/60/90 for swing strategies), starting with the middle window and falling
back when a candidate does not beat baseline with enough trades and positive
net results. Each window gets five 16-candidate, training-only refinement
rounds in the scheduled pipeline; an eligible training winner seeds the next
round. Rolling verification runs after discovery and does not steer those
rounds. Rolling verification and the fold-aligned portfolio replay cover
2024 onward. The first 90 sessions establish the initial fit; each fold then
tests its selection on the following 63 sessions. Profiles are re-fit for each
fold, so the first scored fold begins around May 2024. The stitched OOS results
cover the periods after that initial fit. Bars before 2024 are loaded only for
indicator warm-up; older stored history does not enter training or scored replay.
Multi Factor diagnostics also start in 2024 and refit weights from a rolling
12-observation training window. Multi Factor is active in the live coordinator
using its validated default parameters; research candidates do not change its
live settings automatically.

If verification does not produce a coordinator artifact for every active
strategy, the nightly report is marked partial and portfolio replay is skipped.
There is no fallback research path.

Standalone commands are available for adaptive search, adaptive rolling
verification, and coordinator replay:

```bash
python -m src.algo.research adaptive-search --strategy momentum --end 2026-10-09
python -m src.algo.research adaptive-search --strategy momentum \
  --end 2026-10-09 --search-iterations 5
python -m src.algo.research adaptive-walk-forward --strategy momentum \
  --start 2024-01-01 --end 2026-10-09 \
  --train-window-options 10 20 30 --test-days 63 --step-days 63
python -m src.algo.research portfolio --workers 8
```

The `research` and `optimize` profile-bank commands and the standalone
candidate-profile walk-forward runner have been removed. The remaining
walk-forward implementation is the rolling verification stage in
`adaptive.py`; it owns its fold construction, fitting, and out-of-sample
evaluation.

Adaptive discovery and verification artifacts are reused only when their
strategy, data range, universe, window policy, candidate count, and policy
version and iteration count match. Discovery evaluates candidate replays in process batches up to
the configured CPU budget; Bayesian proposals for each batch use the results
from completed batches. Strategy searches run one at a time so their candidate
workers do not oversubscribe the machine. Verification shares its CPU budget
across strategies and folds. Research reports Multi Factor candidates but
never changes the profile used by its live route.

Each nightly invocation adds a fitness scorecard to
`layers/fitness-history.json` and embeds the latest comparison and recent
timeline in `latest.json`. Discovery fitness is reported against its training
baseline; verified fitness is computed from stitched out-of-sample results.
Run-to-run comparisons are shown only when the strategy set, fixed universe,
research start, configuration, and score policy match. Since the rolling OOS
range expands over time, those changes show performance trends rather than a
controlled test of the newest parameters. The optimizer may seed a later
research search from an eligible positive discovery, but each candidate still
needs rolling verification and never changes live settings automatically.
Equity compounds across OOS folds within a run, not across nightly reports.

AutoResearch logs stage changes and per-strategy completions to the console and
to a rotating file at `data/research/algo-auto/logs/auto-research.log`. Follow
the current run with `tail -f data/research/algo-auto/logs/auto-research.log`.
When `--output-dir` is set, the log is written under that directory instead.

`auto_research.py` owns the scheduled entry point and top-level orchestration.
`adaptive_pipeline.py` runs discovery, rolling verification, and coordinator
artifact generation; `fitness_timeline.py` records scorecards and run comparisons.
`adaptive.py` keeps the CLI and compatibility entry points. Its implementation
lives in `adaptive_search.py` (scoring, replay, candidate search, and fit-window
selection), `adaptive_discovery.py` (recent-window search and incumbent reuse),
`adaptive_verification.py` (rolling out-of-sample folds and stitched scoring),
and `adaptive_common.py` (argument validation and artifact writing).
`portfolio_simulation.py` replays verified selections, `data.py` handles SIP
data and event preparation, and `factor_research.py` fits Multi Factor
diagnostics. Event history and live calendar refresh remain available through
their package commands:

```bash
python -m src.algo.research.historical_events --start 2024-01-01
python -m src.algo.research.event_calendar_refresh
```
