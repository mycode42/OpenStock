# Task 3 — Explainable signals and adaptive thresholds

Use the system contract and complete Task 2 first. Replace manual threshold
ideas with a versioned **signal-to-target pipeline**, but keep the existing
manual thresholds supported as an independent rule strategy.

For the first equity strategy, implement only deterministic and explainable
features: trend/momentum, realized volatility, ATR or equivalent volatility
range, drawdown, liquidity proxy, earnings/dividend event proximity, and
point-in-time fundamentals if licensed/available. A valid output is a target
position range and protective exit parameters, not a directional promise.

Suggested initial policy: use diversification-aware target sizing based on a
fixed per-position risk budget; derive stop/exit distance from volatility;
reduce or abstain around stale data and known event risk. Keep parameters in a
versioned strategy configuration, never hidden in prompt text. Treat news
sentiment as an optional veto/uncertainty feature only after source provenance,
injection resistance, and offline evaluation exist. Do not use an LLM to
directly choose a trade.

Implement: strategy interfaces; signal records with feature snapshot and
reason codes; deterministic validation; target/risk proposal APIs/UI; and
backtest comparisons against an explicit buy-and-hold benchmark. Add
walk-forward evaluation with an untouched final test period. Fail a strategy
gate when it lacks enough samples, exceeds drawdown/turnover limits, fails
data-quality checks, or loses robustness across reasonable parameter ranges.

Return all assumptions and an operator-readable explanation for every
proposal. The only permitted result is signal visibility and optional Telegram
notification; no order intent or broker call.
