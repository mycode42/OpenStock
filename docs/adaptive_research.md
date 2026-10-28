# Algo adaptive research

Equity AutoResearch improves the seven researchable Algo strategies over time:
intraday mean reversion, intraday trend following, intraday momentum, swing
trend following, swing momentum, swing mean reversion, and swing Multi Factor.
Event Driven is not researched.

Entry points:

```bash
poetry run python -m research                        # options, then equity; target about 6h
poetry run python -m equity_research.auto_research --optimization-only --no-refresh --workers 8
```

## The loop

1. **Discovery** runs one pass per strategy on one window (20 sessions for
   intraday, 60 for swing). It seeds from the current champion and the top
   pending challengers, then adds archive parents and unseen settings. Budget:
   8 candidates × 2 iterations, so 16 evaluations per strategy per night.
2. **Champion and challengers.** Each strategy has one champion. A candidate
   replaces it only after passing exact rolling out-of-sample gates and ranking
   above it on lower-quartile fitness, then median fitness and fold consistency.
   Discovery improvements that beat their paired baseline join a bounded
   challenger queue.
3. **Rolling verification** walks forward from 2020 in 63-session test folds,
   with a 21-session purge and up to 90 sessions of fitting per fold. It is the
   only stage that judges out-of-sample results. Exact-candidate validation
   replays the champion, the live profile, and up to two challengers on the
   same folds.
4. **Forward trials** (nightly only) stage up to nine candidates per strategy
   and regime against the currently promoted profile. Each trial needs at least
   30 closed trades in its regime, 20 exit sessions, and 15 entry sessions, and
   expires after 180 days. The acceptance rules run on every forward session.
5. **Promotion.** At most one promotion per cycle and one per strategy. Winners
   need a deflated Sharpe ratio of at least 0.95, where the trial count covers
   all distinct evaluations. The chosen profile becomes the active version in
   `data/research/algo-auto/promoted-profile.json`. Only the nightly run can
   promote.

**History promotion (current path).** A candidate that passes exact rolling
out-of-sample validation and ranks above the live profile on the same folds is
promoted: lower-quartile fitness first, then median fitness, then fold
consistency. Forward trials keep running as monitoring. The Sunday full-history
review gives the keep or demote decision (`python -m equity_research.weekly_review`).
A demoted candidate is blocked from automatic re-promotion.

Options champions follow the same rule on the same folds: a candidate replaces
the champion only if it ranks above it on OOS folds (`runtime/fold_ranking.py`).
Neither pipeline has a holdout.

`--optimization-only` runs step 1 alone against stored bars, for manual use. It adds
evidence to the archive and challenger queue and does not verify, replay, or
promote. It is not scheduled.

## Outputs

- `data/research/algo-auto/latest.json` and `layers/fitness-history.json`: the nightly report and fitness trend.
- `data/research/algo-auto/optimizer/`: reports from manual optimization-only runs.
- `data/research/algo-auto/forward-validation.json`: trial history.
- `data/research/algo-auto/research.sqlite3`: ledger, archive, profile versions, and trial counts.
- `data/research/algo-auto/logs/auto-research.log`: stage timings (UTC).

Profile history and revert:

```bash
poetry run python -m equity_research.profile_versions list --strategy swing_momentum
poetry run python -m equity_research.profile_versions revert --strategy swing_momentum --regime range_bound --to <version> --dry-run
```

Costs in the replay: 5 bps slippage per side, 3 bps full spread, $0.005 per
share commission, 3% annual short borrow, and a 25 bps penalty when borrow
availability is unknown. Replays use the next-bar fill model, so they are not a
live execution estimate.
