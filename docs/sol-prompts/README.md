# OpenStock: Sol implementation prompt pack

Use these prompts in order, in one persistent Sol session when possible. Keep
`00-system-contract.md` as the stable instruction prefix. Then submit one task
prompt at a time and require its tests/review before moving on.

## Target architecture

OpenStock remains the **control plane**: dashboard, configuration, audit view,
and human approvals. A separate, containerized trading core owns research,
backtests, paper portfolios, risk controls, and (only eventually) execution.

Evaluate **LEAN** as the trading core instead of implementing market calendars,
corporate actions, option assignment, fees, margin, slippage, and broker order
semantics from scratch. Its event-driven model and cross-asset support match
equities, options, and crypto. The integration boundary must remain replaceable.

## Non-negotiable rollout

`signal-only` -> `backtest` -> `paper` -> `limited-live` -> `live`.

Every new asset class starts again at `signal-only`. Live execution is disabled
by default and is not enabled by code, environment defaults, or a UI toggle
without a separately reviewed change and explicit owner authorization.

## How to judge a strategy

Do not accept a strategy because it has an attractive historical return. It
must use point-in-time data, realistic fees/slippage/spread/latency, and a
strictly out-of-sample walk-forward result. Report return, volatility,
maximum drawdown, turnover, exposure, hit rate, tail loss, and sample size.
Reject strategies whose results depend on unavailable data, look-ahead bias,
survivorship bias, or a few concentrated trades.

## LLM boundary

An LLM may normalize news, extract cited events, classify text, or help
implement/test code. It must not invent prices, fundamentals, dividends,
news, sentiment, or targets; it must not directly emit executable orders; and
it cannot override deterministic risk checks. Treat news/sentiment as a
versioned, optional feature with source, timestamp, confidence, and a safe
fallback of "unknown".

## Model settings

Start Sol at `reasoning.effort: medium`; compare `low` on routine coding and
`high` only for architecture, security review, or difficult debugging. Keep
the system contract stable for prompt caching. Use structured output for plans,
risk decisions, and test reports.

## Prompts

1. `01-architecture.md` — repository audit and approved architecture.
2. `02-data-and-backtesting.md` — data contracts plus a reproducible research
   and backtest baseline.
3. `03-signals-and-thresholds.md` — deterministic, explainable target and
   stop methodology; no live orders.
4. `04-paper-risk-operations.md` — paper trading, reconciliation, kill switch,
   observability, and security.
5. `05-execution-gate.md` — a future design review for a bounded broker
   connection; it explicitly stops before enabling execution.
6. `06-options-and-crypto.md` — later asset-class additions, each isolated
   behind its own validation gate.
