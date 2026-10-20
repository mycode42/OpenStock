# Adaptive Algo research and simulated promotion

The nightly research pipeline explores five systematic Algo strategies:
Trend Following, Momentum, Swing Mean Reversion, Multi Factor, and intraday
Mean Reversion. Event Driven remains outside this first rollout because its
event samples are sparse.

## How candidates are explored

Discovery uses a persistent candidate archive, varied search seeds, and
previously useful candidates as parents. It evaluates recent candidates in
the current market regime and records regime-specific profiles when they are
sample-qualified, positive after modeled costs, and better than the baseline
on the same discovery window. Every research invocation advances a persistent
per-strategy seed and searches the archive, even if no new quotes have arrived.
It skips parameter sets already tested against that exact quote snapshot and
keeps sampling unseen variants until the configured budget is met or the
bounded parameter space has no unseen candidates. The baseline is replayed
each time for comparison.
Quote updates provide additional data for candidate scoring, historical
verification, and forward simulation; they are not required for search
novelty.

The nightly pipeline also runs rolling out-of-sample verification over the
historical data range. A completed verification is required, but its
strategy-wide `overall_pass` no longer blocks a regime candidate from entering
a forward trial. The candidate still has to improve on the same-window
training baseline for its target regime. Historical verification results and
failed checks are attached to the candidate as evidence; they remain visible
when comparing it with later folds and trials. This lets a promising regime
candidate gather prospective evidence even when the strategy's aggregate
historical score is weak. An errored or missing verification still blocks
staging.

## Forward simulation gates

For the current SPY regime, the validator stages at most one profile per
strategy at a time. It starts paired replays on the first stored trading session after
staging: one uses the candidate for its target regime, and one uses the
currently promoted profile. Both use the same symbols, market data, portfolio
risk controls, and cost assumptions. Pre-staging bars can warm up indicators,
but no pre-staging trades or P&L enter the trial score.

A candidate needs at least 30 closed trades entered in its target regime,
20 distinct exit sessions, and 15 distinct target-regime entry sessions. A
trial expires after 180 calendar days if it does not collect that sample. Once
the sample is met, promotion requires all of the following:

- Positive realized P&L after modeled costs on target-regime trades.
- Higher target-regime realized P&L than the paired current-profile replay.
- Positive end-of-replay marked P&L after estimated exit costs, and higher
  marked P&L than the paired current-profile replay.

The replay uses SIP regular-session five-minute bars and the existing Algo
engine. Its cost model applies 5 bps adverse slippage per side, a 3 bps full
spread, $0.005 per-share commissions, 3% annual short borrow, and a 25 bps
penalty when historical borrow availability is unknown. Open positions are
marked to market with estimated exit costs while the realized-P&L gate counts
closed trades only.

On promotion, the candidate is atomically written to
`data/research/algo-auto/promoted-profile.json`. The Algo scanner reloads that
manifest each scan and applies signal parameters for the matching regime;
portfolio risk limits are not promoted. The trial history and last cycle live
in `data/research/algo-auto/forward-validation.json`. Discovery, verification,
and trial artifacts remain under `data/research/algo-auto/`.

## Durable run and candidate lineage

Every scheduled invocation is recorded in
`data/research/algo-auto/research.sqlite3`. Each invocation also gets an
immutable `invocations/<invocation_id>/report.json` snapshot; `latest.json`,
the date-named reports, and the existing fitness-history JSON remain for
compatibility. The first ledger initialization imports the existing fitness
scorecards and candidate archive, including its saved verification folds.

The database links invocations and stage outcomes to candidate evaluations,
training and out-of-sample folds, portfolio folds, forward-trial observations,
promotion events, and source artifacts. Candidate evaluations retain the data
fingerprint, search sequence and seed, date windows, metrics, gate results,
reuse/source links, and artifact path. Forward trials keep their originating
invocation and source candidate, baseline profile fingerprint, simulation
observations, final status, and promotion release ID. Full reports and replay
details remain in JSON so the ledger stays compact while retaining links and
SHA-256 fingerprints.

For example, this query follows one candidate through its discovery and
historical evaluations:

```sql
SELECT c.strategy, c.regime, e.stage, e.evaluation_kind, e.data_version,
       e.train_start, e.train_end, e.test_start, e.test_end,
       e.net_return_pct, e.sharpe, e.passed, e.artifact_path
FROM candidate_evaluation AS e
JOIN research_candidate AS c USING (candidate_id)
WHERE c.candidate_id = ?
ORDER BY e.recorded_at, e.test_start;
```

To inspect its prospective trials and promotion history, join
`forward_trial_observation` and `research_promotion` on `trial_id`. SQLite can
be queried with the system `sqlite3` command or Python's standard `sqlite3`
module; no additional tracking service is required.

This is forward simulation, not a Shadow account or a broker paper-order path.
It does not model live quote-level fills, latency, market impact, or overnight
BOATS execution. The historical and forward replays use the fixed current
universe, so they also retain survivorship and listing-availability bias.
