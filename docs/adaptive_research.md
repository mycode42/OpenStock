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

## Exact-candidate validation

Full research runs also select a small, diverse cohort from the persistent
candidate archive. The current discovery winner is included when it is
sample-qualified, alongside up to two strong archive candidates. Each exact
parameter set is replayed on the same chronological out-of-sample folds used by
rolling verification. Fold results are stored by candidate ID, data version,
cost/logic version, and fold range, so later runs reuse unchanged evaluations
and only replay new candidate/fold pairs.

An archived candidate can enter a forward trial only when its exact-parameter
checks cover at least three folds and ten closed trades, have positive median
fitness and return, and at least 60% of the folds have positive fitness. The
candidate must also qualify for the current regime in discovery. The normal
forward trial still pairs it with the current profile and applies the separate
prospective sample and promotion gates. A weak candidate remains in the archive
with its fold evidence while it remains in the bounded archive; it does not
replace a stronger candidate merely because it had a higher score on one
discovery window. Holdout sessions stay outside these repeated searches and
checks.

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

For the current SPY regime, the validator stages a planned cohort of up to
nine model-family candidates per strategy. Every candidate in a cohort starts
on the same future session and is paired with the same currently promoted
profile, symbols, market data, portfolio risk controls, and cost assumptions.
Pre-staging bars can warm up indicators, but no pre-staging trades or P&L enter
the trial score. The validator waits until cohort members meet their sample
requirements or expire, then promotes at most the strongest candidate that
passes the existing forward gates. This keeps comparisons independent of
candidate iteration order.

A candidate needs at least 30 closed trades entered in its target regime,
20 distinct exit sessions, and 15 distinct target-regime entry sessions. A
trial expires after 180 calendar days if it does not collect that sample.

Once the sample is met, the forward window is split in time: the first two
thirds are the search window and the last third is the holdout. Acceptance runs
in four steps:

1. Search gate, for every trial. It uses search-window sessions only and needs
   at least 20 of them, a positive mean excess return over the benchmark, a
   margin of at least 5 bps per session over the paired current profile, at
   least 60% of purged folds positive, a drawdown of the excess no more than
   1.2 times the incumbent's, and a Deflated Sharpe Ratio of at least 0.95
   (see "Multiple-testing deflation").
2. Cohort winner. Candidates staged together compete only among those that
   passed the search gate. The winner has the highest compounded search-window
   return; ties go to discovery fitness, then candidate ID. Holdout results do
   not rank candidates.
3. Holdout gate, for the chosen candidate only. The holdout needs at least 20
   sessions and a positive mean excess return. A winner that fails it is not
   promoted, and no other cohort member is substituted.
4. Promotion cap. At most `max_promotions_per_cycle` promotions run per cycle
   and at most `max_promotions_per_strategy_per_cycle` per strategy (both
   default to 1). Queued promotions are ranked by search-window return before
   anything is written. Any over the cap are marked `skipped` with a reason and
   may be staged again later.

Realized P&L after modeled costs and marked equity against the paired profile
are still computed and stored as `legacy_gate_pass` for comparison. They do not
decide promotion.

The forward replay uses SIP regular-session five-minute bars plus stored SIP
pre-market/after-hours bars and BOATS overnight bars when available. The
auxiliary feeds are backfilled for the maximum 180-day trial lifetime, then
refreshed incrementally. A BOATS permission or feed error is recorded as
unavailable and does not stop regular-session research. Reports include bar
counts by session and source feed so a trial with no auxiliary coverage is
visible. Overnight bars from 20:00 ET are assigned to the following trading
date; daily strategy histories continue to use regular-session closes.

The replay currently applies the same cost model in every session: 5 bps
adverse slippage per side, a 3 bps full spread, $0.005 per-share commissions,
3% annual short borrow, and a 25 bps penalty when historical borrow
availability is unknown. Open positions are marked to market with estimated
exit costs while the realized-P&L gate counts closed trades only. Extended and
overnight fills are approximated by the replay's next-bar model; the simulation
does not have quote-level spreads, queue position, or session-specific fill
rules, so its results are not a live 24/5 execution estimate.

On promotion, the candidate becomes the active profile version for its strategy
and regime (see "Profile versions, promotion history, and revert" below) and is
atomically projected into `data/research/algo-auto/promoted-profile.json`. The
Algo scanner reloads that manifest each scan and applies signal parameters for
the matching regime; portfolio risk limits are not promoted. The trial history
and last cycle live in `data/research/algo-auto/forward-validation.json`.
Discovery, verification, and trial artifacts remain under
`data/research/algo-auto/`.

## Multiple-testing deflation

Selecting the best of many evaluated candidates inflates the apparent Sharpe
ratio. The forward gate deflates each candidate by the number of trials
evaluated, using the Deflated Sharpe Ratio of Bailey and Lopez de Prado (2014).

The trial count N is the number of distinct evaluations recorded for the
strategy across all regimes. It is stored in the `trial_evaluation` table of
`data/research/algo-auto/research.sqlite3`. Discovery contributes each distinct
non-baseline parameter set it evaluated in any window attempt, read from the
`evaluated_candidate_ids` list in each discovery artifact. Forward trials
contribute one row per trial ID. Rows are insert-only, and the count also
includes forward trials already in the `forward_trial` ledger table, so the
count cannot shrink when the state file trims its history.

For a candidate with search-window per-session Sharpe `SR`, `T` search sessions,
skewness `g3`, and non-excess kurtosis `g4` (3 for normal returns):

    V      = (1 - g3*SR + (g4 - 1)/4 * SR^2) / (T - 1)
    E[max] = sqrt(V) * ((1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N*e)))
    DSR    = Phi((SR - E[max]) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2))

Here `g` is the Euler-Mascheroni constant (0.5772), `Phi` is the standard normal
CDF, and `e` is Euler's number. `E[max]` is the Sharpe a zero-skill search over N
trials would be expected to reach by chance. With N = 1 it is zero, and it rises
slowly with N. The gate requires DSR >= 0.95 (`min_deflated_sharpe` in
`FORWARD_ACCEPTANCE_POLICY`). `V` uses the candidate's own estimator variance,
not the dispersion across trials, so it is an approximation.

Measured as `SR * sqrt(T - 1)` over the shape term, which is close to a
t-statistic, the requirement is the expected maximum for N plus 1.645 (the 0.95
quantile). The bar does not depend on T. It is 1.6 for N = 1, 2.2 for N = 2,
3.2 for N = 10, 4.2 for N = 100, and 4.9 for N = 1000. It therefore rises as
candidates accumulate for a strategy.

The earlier max-of-noise bound, sqrt(2 ln N), was replaced by this formula. It
was not a Deflated Sharpe Ratio.

## Profile versions, promotion history, and revert

Every profile that has been live is a row in the `profile_versions` table of
`data/research/algo-auto/research.sqlite3`, and every promotion, revert, and
backfill is a row in `profile_events`. A version records its parameters in
canonical JSON, the forward evidence behind a promotion (`fitness_json`: net and
marked P&L, the Deflated Sharpe Ratio, closed trades and sessions, the
acceptance decision, and the deflation trial count), its `parent_version_id`,
and its release ID. `status` is `pending`, `active`, `superseded` (replaced by a
promotion), `reverted` (removed by an operator revert), or `abandoned` (a
crash-interrupted promotion that never went live). A partial unique index allows
at most one `active` version per strategy and regime. Base parameters, which
are not regime-specific, use the regime `*`.

Write order and crash behavior. Forward validation holds the same flock as
before. A promotion first inserts the new version as `pending`, then atomically
writes `promoted-profile.json`, then in one transaction marks the previous
version `superseded`, marks the new one `active`, and writes the promote event
with the replaced parameters. The JSON is the live projection; the database is
the record. Each forward cycle runs `reconcile` before it stages anything: a
pending row whose candidate the JSON already names is activated, and any other
pending row is marked `abandoned`, which lets that candidate be promoted again.
A candidate is promoted at most once per strategy and regime, so a replayed
trial after a crash cannot create a second release. The deflation trial count
comes from the `trial_evaluation` table (see "Multiple-testing deflation"), so
trimming the state file's history never lowers it.

Inspect and restore with the CLI; a read-only listing needs no lock:

```bash
poetry run python -m autoresearch.profile_versions list --strategy swing_momentum
poetry run python -m autoresearch.profile_versions history --strategy swing_momentum --regime range_bound
poetry run python -m autoresearch.profile_versions compare --a <version> --b <version>
poetry run python -m autoresearch.profile_versions revert --strategy swing_momentum --regime range_bound --to <version> --dry-run
poetry run python -m autoresearch.profile_versions revert --strategy swing_momentum --regime range_bound --to <version> --reason "why"
```

`revert` validates the target with the registry validator and refuses pending,
abandoned, or invalid versions. It takes the forward-validation flock, so it
waits for a running forward cycle. It inserts a new `source=revert` version that
copies the target's parameters and points to it through `restores_version_id`,
then writes the JSON and activates the new version. The previously active
version becomes `reverted`, and the revert event records the replaced
parameters. The target row keeps its original promotion provenance. A revert is an operator
action and is not blocked by a cohort holdout. It does not supersede the
holdout, so the live profile can then differ from the frozen record. Supersede
the holdout explicitly if that is intended. The scanner
reads the JSON on every scan, so a revert takes effect on the next scan without
a restart.

`backfill` imports the live `promoted-profile.json` and its legacy history once,
as `source=backfill`. It is idempotent and supports `--dry-run`. A history record
that describes the live profile (same candidate) is attached to that version
rather than duplicated. Legacy records without full parameters are stored with
no parameters and cannot be reverted to.

## Durable run and candidate lineage

Every scheduled invocation is recorded in
`data/research/algo-auto/research.sqlite3`. Candidate selection, search seeds,
champions, validation queues, and candidate history now read from and write to
the SQLite candidate archive document. The old `candidate-archive.json` was
migrated into SQLite and is no longer written. Each invocation still gets an
immutable `invocations/<invocation_id>/report.json` snapshot; `latest.json`,
date-named reports, and fitness-history JSON remain as report exports.

The database links invocations and stage outcomes to candidate evaluations,
training and out-of-sample folds, portfolio folds, forward-trial observations,
promotion events, and source artifacts. Candidate evaluations retain the data
fingerprint, search sequence and seed, date windows, metrics, gate results,
reuse/source links, and artifact path. Forward trials keep their originating
invocation and source candidate, baseline profile fingerprint, simulation
observations, final status, and promotion release ID. Full reports and replay
details are stored as compressed payloads in the ledger as well as JSON exports.
The full candidate archive is mirrored there alongside its normalized candidate
and evaluation rows. Existing linked artifacts can be embedded with
`poetry run python scripts/research/backfill_artifact_payloads.py`.

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
It does not model live quote-level fills, latency, market impact, or actual
overnight BOATS order execution. The historical and forward replays use the
fixed current universe, so they also retain survivorship and listing-availability
bias.

## Strategy-specific candidate families

Research candidates now carry a model-family label and strategy logic version
in their identity and ledger metadata. Parent candidate, full parameter set,
data fingerprint, cost-model fingerprint, and rolling fold evaluations are
stored alongside the candidate. A logic-version change produces a new
candidate ID even when its parameters match an older run.

Mean-reversion discovery adds per-symbol completed-bar Efficiency Ratio and
ADX gates in baseline, ER-only, ADX-only, and combined variants. Swing
mean-reversion also searches standardized distance from its moving average
and a volatility-adaptive lookback that shortens when recent realized
volatility is elevated. Intraday Bollinger candidates can widen their entry
threshold in elevated volatility; the opening-dip baseline remains available
as its own family.

Swing trend discovery compares SMA/EMA with KAMA, then tests ATR stops and
KAMA-plus-ATR as a separate combination. ATR candidates send their stop
distance into the existing risk-per-trade sizing rule, so wider stops reduce
share count while the portfolio and per-symbol allocation caps still apply.
Momentum keeps cross-sectional ranking as the baseline and adds a separate
time-series mode with 21, 63, 126, and 252-session horizons. Research loads up
to 390 calendar days of warm-up history for those variants. Online Hedge-style
weighting is not enabled; it would need explicit replay state and strictly
after-cost, realized-return updates.

Multi Factor now supports price-volume momentum, point-in-time value and
quality ratios, sector demeaning, price/history/liquidity masks, configured
regime-specific factor weights, and an optional diagonal-shrinkage minimum
variance sleeve. The fixed-universe sector labels are explicitly marked as
current labels in historical reports; point-in-time sector history is not
available. The factor path now loads annual SEC EDGAR CompanyFacts through the
existing cached SEC client. Each filing becomes a fundamental row with its SEC
filing date, accession, reporting period, and `available_at` set to the next
calendar day; this avoids using a filing on its unknown intraday acceptance
date. Earnings yield, book-to-price, and free-cash-flow yield are recomputed
from reported per-share values and the historical close. Rows older than 550
days after the reporting period are masked. Feed status, symbol/field coverage,
cache hits, failures, and a content version are recorded in the research
artifact. Missing issuer facts stay omitted, and no current yfinance snapshot is
used as historical data. The factor-only model lowers its minimum cross-section
from the 20-symbol bar-coverage threshold to the narrowest current fundamental
field coverage, bounded at 12; the run records that threshold and each factor's
observation coverage.

The factor research artifact compares equal-weight and regularized Ridge
weights with a purged 21-session walk-forward, per-factor IC, IC stability by
year and regime, and an equal-weight top/bottom shadow portfolio after modeled
spread, slippage, commissions, and short borrow. It records model family,
logic version, parent candidate, data version, cost-model version, and feature
importance. A multi-factor candidate also needs at least 20 IC observations,
positive net IC in at least 55% of observations, and positive after-cost
target-basket return. Changed factor-score variants must improve net IC by
0.005 over the baseline; portfolio-only variants must still beat baseline
fitness and costed return. Replay IC labels overlap and are reported as such;
the separate Ridge artifact uses purged, non-overlapping 21-session labels.
These are research candidates; the live profile is not replaced automatically.

Discovery retains a planned one-feature cohort for each family before adding
other parameter proposals. Candidate metrics report after-cost return,
drawdown, trade count, turnover, and regime-specific outcomes so baseline,
single-feature, and combined variants can be compared on the same future
window. Historical discovery can nominate a candidate for the cohort. The prospective
gate, with its search rules, Deflated Sharpe Ratio, and holdout, applies
unchanged.

## Walk-forward discipline and robustness

Adaptive verification uses rolling windows with at least five folds. The fit
endpoint is separated from each OOS test by a 252-session embargo, matching the
longest searched momentum horizon. Each fold records its train and test dates,
purged dates, selected candidate, training plateau, and OOS result. Selection
uses a central candidate from the near-best training plateau when adjacent
near-best parameter settings support it; a lone peak is not treated as a
stable improvement. Reports include per-fold and aggregate walk-forward
efficiency (OOS fitness divided by positive in-sample fitness) and require
positive OOS returns and positive efficiency in at least 60% of folds.

OOS reports include a 1,000-iteration, five-session moving-block bootstrap,
cost multipliers of 1.5x and 2x, and a one-bar delayed-fill scenario at the
highest cost multiplier. Full coordinator replays also rerun every fold under
those stressed costs and latency, and bootstrap both daily returns and
five-trade blocks. These are sensitivity checks against the recorded base
assumptions; slippage, spread, per-share commissions, short-borrow rates, and
unknown-borrow penalties scale together. Coordinator replays also report a
separate regime-conditioned exponential-weighting shadow. Each rule-based
regime learns bounded weights from prior-session after-cost strategy sleeve
P&L, staying neutral for the first 20 observations in that regime. It does not
rerun fills, account for capital competition, or alter live coordinator weights.

## Drift and coordinator allocation research

The forward drift monitor starts with a cursor at the latest existing stitched
OOS date, then consumes only newly appended sessions. After 63 reference
sessions it applies a one-sided Page-Hinkley-style detector to standardized
after-cost daily returns. A warning pauses profile promotion until 20 healthy
sessions clear it. Drift alerts never change live sizing.

Coordinator-level, regime-conditioned exponential weighting remains a shadow
diagnostic. Strategy weights are learned only from prior-day, after-cost sleeve
results and clipped to 0.5x–1.5x, with a neutral 20-session warm-up for each
regime. Forward candidate trials score only sessions after their staging date. Any later fill-level coordinator
experiment must go through the same rolling folds and costs before it can be
considered for promotion; the current report does not deploy these weights.

## Prospective cohort holdouts

Each live strategy and regime pair is a cohort. `initialize` freezes one
holdout per cohort into the `final_holdout` table of
`data/research/algo-auto/research.sqlite3`. The record stores the window, which
starts the session after the freeze date and is open-ended. It also stores the
live profile versions for the pair (version IDs, candidate IDs, and parameters)
and the SHA-256 of those frozen parameters. Initialize is idempotent. A pair
that already has an active holdout is left unchanged, even if its live profile
has since moved.

While a pair's holdout is active, promotions for that pair are paused. Its
candidates can still be staged and scored, but the live profile for that pair
does not change. Other pairs are unaffected.

Nothing in the nightly run supersedes a holdout. The nightly report only reads
holdout status. A holdout ends only through the CLI. Supersede requires a reason
and writes one `final_holdout_event` per retired holdout:

```bash
poetry run python -m autoresearch.validation.final_holdout initialize [--frozen-on YYYY-MM-DD]
poetry run python -m autoresearch.validation.final_holdout status
poetry run python -m autoresearch.validation.final_holdout supersede --reason "why" [--strategy S] [--regime R]
```

Holdout windows are recorded but not scored by the pipeline. Promotion for a
frozen pair waits for an explicit supersede. Holdout sessions are never used to
rank candidates; the holdout third of each forward window is evaluated only for
the chosen candidate.

`data/research/algo-auto/final-holdout.json` is the earlier whole-profile
manifest. It remains on disk as an audit record and is no longer created or
updated. Its frozen profile records `llm_driven_overlay_included`. A manifest
without that key reads as `false`.

# Ridge learned factor-weight replay

Multi Factor candidates can select `factor_weight_mode: ridge_walk_forward`. AutoResearch fits a dated Ridge weight timeline before discovery, rolling verification, coordinator replay, and forward trials. Every replay uses the latest snapshot whose training labels had ended by that feature date. The legacy whole-profile manifest froze the timeline available at its cutoff. Cohort holdouts record only the live profile versions. The fit is positive-coefficient Ridge weights normalized to sum to one, with an equal-weight fallback when all fitted coefficients are non-positive.

Live ranking reads `current_candidate_weights` from the latest research report only after a candidate carrying this mode has passed the existing forward promotion gate. A missing, future-dated, or more than seven calendar days old model fails closed for this mode and is exposed in multi-factor diagnostics. Static candidates continue to use their configured weights. Promotion therefore selects the weight mode; the nightly research artifact supplies the point-in-time weights.

The current static baseline is retained alongside a sector-neutralized, expanded-factor equal-weight control and the Ridge-weight variant. The latter two share the neutralization and universe threshold, so their paired replay isolates the learned weights while the original baseline shows the total change from today's implementation.
