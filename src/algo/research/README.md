# Algo adaptive research

The scheduled entry point is `python -m src.algo.research.auto_research`.
It refreshes and fingerprints SIP data, reuses compatible adaptive artifacts,
runs missing parameter discovery and rolling verification, then replays the
verified selections through the portfolio coordinator.

The pipeline uses dynamic parameter search over bounded signal-parameter
spaces. Discovery tries recent windows (10/20/30 sessions for intraday and
30/60/90 for swing strategies), starting with the middle window and falling
back when a candidate does not beat baseline with enough trades and positive
net results. Rolling verification repeats the search inside chronological
training folds, evaluates each selection on the following unseen fold, and
keeps the final 252 sessions locked out of fitting.

If verification does not produce a coordinator artifact for every active
strategy, the nightly report is marked partial and portfolio replay is skipped.
There is no fallback research path.

Standalone commands are available for adaptive search, adaptive rolling
verification, and coordinator replay:

```bash
python -m src.algo.research adaptive-search --strategy momentum --end 2026-10-09
python -m src.algo.research adaptive-walk-forward --strategy momentum \
  --start 2016-01-01 --end 2026-10-09 \
  --train-window-options 10 20 30 --test-days 63 --step-days 63 \
  --holdout-days 252
python -m src.algo.research portfolio --workers 8
```

The `research` and `optimize` profile-bank commands and the standalone
candidate-profile walk-forward runner have been removed. The remaining
walk-forward implementation is the rolling verification stage in
`adaptive.py`; it owns its fold construction, fitting, and out-of-sample
evaluation.

Adaptive discovery and verification artifacts are reused only when their
strategy, data range, universe, window policy, candidate count, and policy
version match. Worker processes share the configured CPU budget across
strategies and verification folds. Multi Factor discovery may feed its
separate shadow book; research does not change live configuration or place
orders.

`nightly.py` owns scheduled orchestration, `adaptive.py` owns parameter
discovery and rolling verification, `portfolio_simulation.py` replays the
verified selections, `data.py` handles SIP data and event preparation, and
`factor_research.py` fits Multi Factor diagnostics. Event history and live
calendar refresh remain available through their package commands:

```bash
python -m src.algo.research.historical_events --start 2016-01-01
python -m src.algo.research.event_calendar_refresh
```
