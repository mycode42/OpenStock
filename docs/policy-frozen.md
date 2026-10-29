# Frozen trading policy

This is the approved state of the trading gates. It is immutable: a gate is added, removed, or changed only with an explicit approval, recorded here and in the commit.

Enforcement: `algo-trader/tests/policy/test_gate_freeze.py` extracts every rejection and suppression message from the Algo code and the OpenStock order library, and compares them with `algo-trader/tests/policy/frozen_gates.json`. Any new or changed message fails the Algo test suite until the snapshot and this document are updated together.

## Decisions in force

- **Limit orders only, with separate bands.** Exits use `execution.marketable_limit_band_bps` (25) and protective stops use `execution.protective_limit_band_bps` (50).
- **Orders are limit orders only.** Entries, exits, protective closes, and flattens are limit orders. A market order is refused before any broker call. Marketable limits are priced `execution.marketable_limit_band_bps` (default 25) through the live bid or ask.
- **Paper is the default.** The Algo sleeves and OpenStock's manual account default to the paper URL. Setting `ALPACA_*_BASE_URL` overrides it.
- **Real-order changes are confirmed in the UI.** Switching real orders on asks for a USD budget (BUY) or a confirmation (SELL). The web app has no order preview.
- **Live trading is an explicit opt-in.** Any non-paper endpoint needs `ALPACA_ALLOW_LIVE=1`. Live manual orders and turning real orders on also need `OPENSTOCK_ORDER_TOKEN`.
- **Per-order cap.** No single order may exceed $100,000 of notional value, on paper or live.
- **No cap on monitored names.** OpenStock imposes no count limit on monitored stocks.
- **No session or timing gates.** Orders are not refused for being outside regular hours. The broker decides what it accepts.
- **Shorts are not gated on borrow.** Hard-to-borrow shorts are sized to `portfolio.hard_to_borrow_short_risk_multiplier` (default 0.5). Easy-to-borrow is a preference, not a requirement.
- **No moonshot blocklist.**
- **LLM confidence.** Options `llm_driven.min_llm_confidence` is 0.70. Algo `swing_llm_driven.minimum_confidence` is 70.

## Gate categories that remain

These are the rules that still reject or suppress, grouped. The exact messages are in `frozen_gates.json`.

1. **Data freshness.** Stale or missing quotes and signal bars. Trading on stale prices is not allowed in any session.
2. **Fail-closed infrastructure.** Risk validator unavailable, portfolio fetch failed, entry count unavailable, reconciliation uncertain. Entries pause rather than guess.
3. **Position and ownership.** Opposite direction conflicts with an open position or pending order. One strategy owns a ticker. A ticker with a pending entry cannot take another.
4. **Entry budgets.** Shared daily, intraday, and swing entry slices. Per-strategy daily entries and open positions. Maximum trades per day and maximum open positions.
5. **Exposure and sizing.** Name, sector, gross, and net caps. Tactical caps. Minimum cash reserve. Maximum short positions and short exposure. Zero shares after sizing.
6. **Loss limits.** Daily and weekly circuit breakers. No buying into a drawdown beyond the configured percentage. The options daily loss limit.
7. **Order protocol.** Entries need a stop and a take-profit. Stop and target levels must match the trade direction. Protective and exit orders may not carry broker-side brackets.
8. **Options risk.** Per-sleeve vega and gamma budgets (`risk.sleeve_limits`; a position the journal cannot attribute counts in every sleeve). Undefined-loss structures are refused. Covered structures need the shares. Per-trade premium risk, combined defined-loss, margin, buying power, delta, vega, gamma (losses only), theta, assignment shares, and stress loss each have one limit. The configured values are in `options/config.default.json`.
9. **Order-level checks.** Alert level must be active and untriggered. A BUY needs a budget. A SELL needs an open position. One live order per alert level, enforced by a database index.

## Approved changes

- **2026-10-29, live opt-in and per-order cap.** Added two rejection messages to `frozen_gates.json`: the live opt-in refusal (`Live trading is off: set ALPACA_ALLOW_LIVE=1 ...`) and the per-order cap (`Order value exceeds the $100,000 per-order limit`). Approved by the repository owner. The cap is also enforced in Python by `algo-trader/broker/order_guard.py`, which raises the same limit with the dollar value. That message is outside the extractor's scope, so `tests/broker/test_order_guard.py` checks it directly.

## Changing a gate

1. State the change and the reason in the request.
2. Update the code.
3. Regenerate `frozen_gates.json` and update this document in the same commit.
4. Commit message names the approval.

Do not add a gate as a side effect of a feature. If a feature needs a new rejection, it goes through this process first.
