# Algo adaptive research

The full scheduled entry point is `python -m equity_research.auto_research`.
It refreshes and fingerprints SIP data, runs archive-backed discovery, performs
rolling verification, then replays verified selections through the portfolio
coordinator when needed. `--optimization-only` runs discovery alone for manual
use; it is not scheduled:

```bash
poetry run python -m equity_research.auto_research --optimization-only --no-refresh --workers 8
```

Each optimizer cycle advances the persistent per-strategy search sequence,
re-scores the strongest archived candidates on the available bars, mutates
those parents, and explores unseen settings. It can run repeatedly against the
same stored sessions; it does not wait for a new trading date or bar. The
SQLite research ledger keeps each invocation and candidate evaluation, while
`optimizer/latest.json` and `optimizer/cycles/` hold optimizer reports. These
cycles do not run rolling verification, portfolio replay, or promotion. The
full scheduled run supplies those validation stages and is the only path that
can promote a candidate.

The candidate archive now keeps a per-strategy champion and a persistent
challenger validation queue. A champion changes only when a candidate passes
the exact rolling out-of-sample gates and ranks above the current champion by
lower-quartile fitness, then median fitness and fold consistency. Every new
optimizer cycle seeds discovery from that champion before adding other diverse
archive parents. The top pending challengers are also seeded into the next
search. A worse deviation therefore stays out of the champion slot; the prior
champion remains the next search anchor while new candidates continue to be
explored.

Each full run evaluates the current live profile, the stored champion, and up
to two queued challengers per strategy on the same chronological folds, with
the same cost model and minimum-sample gates. Live regime overrides are replayed
as part of the live profile. Discovery improvements enter the queue only when
they clear the sample and positive-after-costs bar and beat their paired
discovery baseline. Passed challengers may then enter forward validation;
failed challengers cannot replace the champion. The live profile is included as
a rolling reference, while forward trials continue to compare challengers
against that live profile. Optimizer-only runs only add discovery evidence and
queue entries; they do not spend time on these validation stages.

The pipeline uses dynamic parameter search over bounded signal-parameter
spaces. Discovery tries recent windows (10/20/30 sessions for intraday and
30/60/90 for swing strategies), starting with the middle window and falling
back when a candidate does not beat baseline with enough trades and positive
net results. Each window gets five 16-candidate, training-only refinement
rounds in the scheduled pipeline; an eligible training winner seeds the next
round. Rolling verification runs after discovery and does not steer those
rounds. Rolling verification and the fold-aligned portfolio replay cover
2020 onward. Each fold uses up to 90 sessions for fitting (with strategy
specific 10/20/30 or 30/60/90 session fit windows), leaves a 21-session
outcome-horizon purge, and scores the following 63 sessions. The purge protects
forward outcomes; long feature lookbacks remain available as historical inputs.
Multi Factor diagnostics also start in 2020 and refit weights from a rolling
12-observation training window. Bars before 2020 are loaded only for indicator
warm-up.

There is no longer a global frozen-holdout cutoff. Full research cycles use
the latest completed common session. Each forward challenger is versioned and
scored only on sessions after that version's staging date; changing parameters
creates a new candidate version. Historical walk-forward results are reused
across research cycles, so a separate forward trial is still required before
promotion. The current fixed Algo universe is used throughout the historical
range; point-in-time universe membership is not reconstructed. Multi Factor
remains on its validated live parameters unless a candidate passes the
historical and forward promotion gates.

If verification does not produce a coordinator artifact for every active
strategy, the nightly report is marked partial and portfolio replay is skipped.
There is no fallback research path.

Standalone commands are available for adaptive search, adaptive rolling
verification, and coordinator replay:

```bash
python -m equity_research adaptive-search --strategy swing_momentum --end 2026-10-09
python -m equity_research adaptive-search --strategy swing_momentum \
  --end 2026-10-09 --search-iterations 5
python -m equity_research adaptive-walk-forward --strategy intraday_momentum \
  --start 2020-01-01 --end 2026-10-09 \
  --train-window-options 10 20 30 --test-days 63 --step-days 63
python -m equity_research adaptive-search --strategy intraday_trend_following \
  --end 2026-10-09
python -m equity_research portfolio --workers 8
```

The `research` and `optimize` profile-bank commands and the standalone
candidate-profile walk-forward runner have been removed. The remaining
walk-forward implementation is the rolling verification stage in
`pipeline/adaptive_verification.py`; it owns fold construction, fitting, and
out-of-sample evaluation.

Discovery always advances each strategy's persistent search sequence and uses
archive members as evaluated parents even when the quote window is unchanged.
Archived candidates retain per-window, per-data-version fitness observations;
the search ranks them by rolling OOS evidence or improvement over their paired
training baseline. This lets the optimizer keep learning when all candidates
are still unprofitable in absolute terms. The market data determines candidate
scores and the rolling verification range, but new data is not required to run
another optimizer cycle. Rolling verification continues to fit each historical
fold from its own training data. Verification artifacts are reused only when
their strategy, data range,
universe, window policy, candidate count, and policy version and iteration
count match. Discovery evaluates candidate replays in process batches up to
the configured CPU budget; Bayesian proposals for each batch use the results
from completed batches. Strategy searches run one at a time so their candidate
workers do not oversubscribe the machine. Verification shares its CPU budget
across strategies and folds. Multi Factor candidates can change the live signal
profile only after historical verification and forward simulation pass.

Each full invocation adds a fitness scorecard to
`layers/fitness-history.json` and embeds the latest comparison and recent
timeline in `latest.json`. Discovery fitness is reported against its training
baseline; verified fitness is computed from stitched out-of-sample results.
Run-to-run comparisons are shown only when the strategy set, fixed universe,
research start, configuration, and score policy match. Since the rolling OOS
range expands over time, those changes show performance trends rather than a
controlled test of the newest parameters. Each invocation advances discovery
seeds and explores archived parents plus independent variants, even if
the quote window is unchanged. Rolling verification and forward simulation
still gate promotion to live settings.
Equity compounds across OOS folds within a run, not across nightly reports.

AutoResearch logs stage changes and per-strategy completions to the console and
to a rotating file at `data/research/algo-auto/logs/auto-research.log`. Follow
the current run with `tail -f data/research/algo-auto/logs/auto-research.log`.
When `--output-dir` is set, the log is written under that directory instead.

`auto_research.py` owns the scheduled entry point and top-level orchestration.
The `pipeline/` package runs adaptive discovery and rolling verification;
`artifacts/` owns research ledgers, candidate archives, caches, and scorecards;
`validation/` owns out-of-sample checks, robustness analysis, and portfolio
replay. `backtest/` owns deterministic replay, while `benchmarks/` contains
profiling tools. `data.py` handles SIP data preparation and `factor_research.py`
fits Multi Factor diagnostics. Event history and live calendar refresh remain
available through their package commands:

```bash
python -m equity_research.historical_events --start 2020-01-01
python -m equity_research.event_calendar_refresh
```
