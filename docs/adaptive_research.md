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
historical data range. A strategy must pass its existing walk-forward gates
before one of its regime candidates can be staged. That historical gate
evaluates the adaptive search process across unseen folds; it does not claim
that the exact newly discovered parameter set was selected in every historical
fold. The staged profile then has to prove itself on data that arrives after
staging.

## Forward simulation gates

For the current SPY regime, the validator stages at most one profile per
strategy. It starts paired replays on the first stored trading session after
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

This is forward simulation, not a Shadow account or a broker paper-order path.
It does not model live quote-level fills, latency, market impact, or overnight
BOATS execution. The historical and forward replays use the fixed current
universe, so they also retain survivorship and listing-availability bias.
