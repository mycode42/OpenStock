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
- **Live daily cap.** Live orders may total at most $300,000 of notional value per UTC day. Paper orders are not counted.
- **No cap on monitored names.** OpenStock imposes no count limit on monitored stocks.
- **Session-aware execution gates.** Regular-session quotes become stale after 90 seconds; extended-session quotes after 900 seconds (15 minutes). Extended-hours orders are limit-only; opening shorts use DAY sell-to-open limits and retain the existing risk/borrow checks. Overnight shorts fail closed unless current asset eligibility is verified and the asset is not halted.
- **Short sizing.** Hard-to-borrow shorts are sized to `portfolio.hard_to_borrow_short_risk_multiplier` (default 0.5). Easy-to-borrow is a preference, not a requirement; unavailable overnight asset eligibility still blocks overnight short entries.
- **No moonshot blocklist.**
- **LLM confidence.** Options `llm_driven.min_llm_confidence` is 0.80. Algo `swing_llm_driven.minimum_confidence` is 80.

## Gate categories that remain

These are the rules that still reject or suppress, grouped. The exact messages are in `frozen_gates.json`.

1. **Data freshness.** Stale or missing quotes and signal bars. Trading on stale prices is not allowed in any session.
2. **Fail-closed infrastructure.** Risk validator unavailable, portfolio fetch failed, entry count unavailable, reconciliation uncertain. Entries pause rather than guess.
3. **Position and ownership.** Opposite direction conflicts with an open position or pending order. One strategy owns a ticker. A ticker with a pending entry cannot take another.
4. **Entry budgets.** Shared daily, intraday, and swing entry slices; tier allocation budgets (intraday, weekly, swing). Per-strategy daily entries and open positions. Maximum trades per day and maximum open positions.
5. **Exposure and sizing.** Name, sector, gross, and net caps. Tactical caps. Minimum cash reserve. Maximum short positions and short exposure. Zero shares after sizing.
6. **Loss limits.** Daily and weekly circuit breakers. No buying into a drawdown beyond the configured percentage. The options daily loss limit.
7. **Order protocol.** Entries need a stop and a take-profit. Stop and target levels must match the trade direction. Protective and exit orders may not carry broker-side brackets.
8. **Options risk.** Per-sleeve vega and gamma budgets (`risk.sleeve_limits`; a position the journal cannot attribute counts in every sleeve). Undefined-loss structures are refused. Covered structures need the shares. Per-trade premium risk, combined defined-loss, margin, buying power, delta, vega, gamma (losses only), theta, assignment shares, and stress loss each have one limit. The configured values are in `options/config.default.json`.
9. **Order-level checks.** Alert level must be active and untriggered. A BUY needs a budget. A SELL needs an open position. One live order per alert level, enforced by a database index.

## Approved changes

- **2026-11-09, tier allocation budgets.** At Andreas's explicit request, entries are budgeted by time-range tier as aggregate allocation ceilings of equity: intraday 7%, weekly 20%, swing 30% (57% in total, under the 90% gross cap). Tier membership comes from the strategy catalog; weekly has no strategies yet. A new entry is trimmed to fit the tier's remaining room, and is rejected with `tier allocation budget is full` when less than one share fits. Pending entries cannot be attributed to a tier, so they count against every tier until they fill or cancel (fail-closed). Exits are not affected. Per-position caps (`max_position_pct`, `max_strategy_position_pct` 0.10) and regime gross caps are unchanged. Approved by Andreas in chat ("reject the signal when full. go ahead"). Adds one gate message to `frozen_gates.json`.

- **2026-11-08, LLM confidence floor 80 for options too.** At Andreas's explicit request, `llm_driven.min_llm_confidence` is 0.80 (was 0.70), matching the Algo LLM floor. The LLM strategies trade only when very confident. Approved by Andreas in chat.

- **2026-11-08, Algo LLM confidence floor 80.** At Andreas's explicit request, the Algo `swing_llm_driven.minimum_confidence` gate is 80, not 70. The live configuration already had 80 since v3.1.4; this aligns the policy with it. The LLM strategy trades only when very confident. Approved by Andreas in chat.

- **2026-11-07, high-risk profile position and risk ceilings.** At Andreas's explicit request, raised the sizing caps so the high-risk research and apply profiles can pass: `portfolio.max_position_pct` and `portfolio.max_strategy_position_pct` from 0.07 to 0.10 in `portfolio/algo-strategy-config.default.json`; the per-strategy `max_position_pct` range from 0.05 to 0.10 and the `risk_multiplier` range from 1.0 to 1.25 for trend following, momentum, mean reversion and multi-factor. The live standard values are unchanged (their `max_position_pct` stays 0.07), so nothing trades larger until a profile is applied. The frozen gate messages are unchanged. Approved by Andreas in chat.

- **2026-11-06, extended-hours quote freshness and short entries.** At Andreas's explicit request, increased the extended-session quote freshness cap from 300 to 900 seconds while keeping regular-session quotes at 90 seconds, enabled extended-hours short entries as DAY limit sell-to-open orders, and retained fail-closed risk, borrow, overnight asset-eligibility, and halt checks. The 15-minute quote age is a hard maximum, not an assertion that the quote is executable or that the order will fill promptly; a DAY order may persist beyond the signal.

- **2026-11-01, swing multi-factor research cap.** Raised the adaptive research per-strategy daily-entry cap from 2 to 3 so it matches the existing Algo cap and the coordinated portfolio artifact contract. This changes the research profile only; the live configuration remains unchanged. Approved by Andreas in response to the October 31, 2026 research-run failure.

- **2026-10-29, live opt-in and per-order cap.** Added two rejection messages to `frozen_gates.json`: the live opt-in refusal (`Live trading is off: set ALPACA_ALLOW_LIVE=1 ...`) and the per-order cap (`Order value exceeds the $100,000 per-order limit`). Approved by the repository owner. The cap is also enforced in Python by `algo-trader/broker/order_guard.py`, which raises the same limit with the dollar value. That message is outside the extractor's scope, so `tests/broker/test_order_guard.py` checks it directly.

- **2026-10-29, live daily cap.** Added the live daily limit rejection (`Live daily limit reached: no more than $300,000 of live orders per UTC day`) to `frozen_gates.json`. Approved by the repository owner, who asked for a daily cap of $300,000 in live mode. The check and the order insert run in one transaction. Orders are recorded with their account mode, so paper orders are not counted.

## Changing a gate

1. State the change and the reason in the request.
2. Update the code.
3. Regenerate `frozen_gates.json` and update this document in the same commit.
4. Commit message names the approval.

Do not add a gate as a side effect of a feature. If a feature needs a new rejection, it goes through this process first.
