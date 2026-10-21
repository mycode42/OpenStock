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
discovery window. The final holdout remains outside these repeated searches and
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
trial expires after 180 calendar days if it does not collect that sample. Once
the sample is met, promotion requires all of the following:

- Positive realized P&L after modeled costs on target-regime trades.
- Higher target-regime realized P&L than the paired current-profile replay.
- Positive end-of-replay marked P&L after estimated exit costs, and higher
  marked P&L than the paired current-profile replay.

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
window. Historical discovery can nominate a candidate for the cohort, but the
prospective promotion gate remains unchanged.

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
sessions clear it. Drift alerts never change live sizing. When a prospective
holdout is scored, its sessions are skipped by the drift monitor so they do
not become an input to later adaptation.

Coordinator-level, regime-conditioned exponential weighting remains a shadow
diagnostic. Strategy weights are learned only from prior-day, after-cost sleeve
results and clipped to 0.5x–1.5x, with a neutral 20-session warm-up for each
regime. Forward candidate trials also exclude all sessions through the final
holdout end date from their promotion sample. Any later fill-level coordinator
experiment must go through the same rolling folds and costs before it can be
considered for promotion; the current report does not deploy these weights.

## Prospective final holdout

The first complete coordinator verification freezes the latest training-
selected profile, strategy set, cost model, risk/coordinator policy, regime
configuration, source artifact hashes, and a data cutoff in
`final-holdout.json`. The holdout begins strictly after that cutoff. It is
scored once, after at least 252 SPY sessions and three distinct rule-based
regimes. The result reports overall and by-regime portfolio returns, drawdown,
trades, benchmark comparison, and a block-bootstrap distribution. Sparse event
driven overlays are excluded from this first frozen profile.

While the holdout is collecting, market bars continue to refresh, but
discovery, rolling verification, factor fitting, coordinator replay, forward
trial staging, and promotions remain capped at the frozen cutoff. This fence
keeps the accumulating holdout from leaking into candidate selection. The
frozen portfolio is replayed only when its session and regime gates are met.
The drift monitor also stays at the cutoff, then begins its next reference
period after the scored holdout end date.
If live risk, coordinator, runtime, research-control, regime settings, or the
database path change before scoring, the holdout is marked invalidated rather
than silently replayed under a different policy. After the one-time score, new
research can use accumulated bars; the frozen result is retained and never
recomputed.
# Ridge learned factor-weight replay

Multi Factor candidates can select `factor_weight_mode: ridge_walk_forward`. AutoResearch fits a dated Ridge weight timeline before discovery, rolling verification, coordinator replay, and forward trials. Every replay uses the latest snapshot whose training labels had ended by that feature date. The final holdout freezes the timeline available at its cutoff, so its profile and learned factor weights are scored together. The fit is positive-coefficient Ridge weights normalized to sum to one, with an equal-weight fallback when all fitted coefficients are non-positive.

Live ranking reads `current_candidate_weights` from the latest research report only after a candidate carrying this mode has passed the existing forward promotion gate. A missing, future-dated, or more than seven calendar days old model fails closed for this mode and is exposed in multi-factor diagnostics. Static candidates continue to use their configured weights. Promotion therefore selects the weight mode; the nightly research artifact supplies the point-in-time weights.

The current static baseline is retained alongside a sector-neutralized, expanded-factor equal-weight control and the Ridge-weight variant. The latter two share the neutralization and universe threshold, so their paired replay isolates the learned weights while the original baseline shows the total change from today's implementation.
