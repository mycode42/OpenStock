# Task 6 — Options and crypto expansion gates

Use the system contract. Do not merge options or crypto into the equity path.
Design separate instrument, data, risk, and paper-execution modules. Start each
asset class in `signal_only` and repeat the full backtest/paper gate.

For options, require a full option-chain and corporate-action model, contract
multiplier, expiration, assignment/exercise, Greeks and implied volatility,
liquidity/spread filters, early-exercise/dividend risk, and margin. Support
defined-risk structures only in paper mode; reject naked short options and
undefined-risk strategies.

For crypto, model 24/7 sessions, exchange-specific symbols, custody and
withdrawal boundaries, venue fragmentation, stablecoin/chain risks, maker/taker
fees, funding, liquidation and leverage. Begin spot-only, no leverage, no
transfers, and no withdrawals. Require separate exchange adapter and incident
controls.

Deliver a gap analysis, data/entitlement requirements, isolated schemas, risk
limits, fixture tests, and promotion criteria. No real exchange or options
broker calls.
