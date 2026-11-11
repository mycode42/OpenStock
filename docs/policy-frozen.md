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
- **Idle cash in SGOV is cash.** 2026-11-11, approved by Andreas in chat. The cash-proxy ETF (`cash_reserve.proxy_symbol`, SGOV) is left out of the scan's positions and open orders (`algo-trader/strategies/runner/data.py`, `current_portfolio`), so it does not count toward gross, net, sector, name, or position limits, and it is not an unowned position. Its market value counts as free cash in the risk account (`algo-trader/execution/risk_account.py`). `algo-trader/tests/strategies/test_cash_proxy_is_cash.py` enforces this. It is not a gate message, so `frozen_gates.json` is unchanged.
- **Passive entries time out; pending orders never pause the book.** 2026-11-11, approved by Andreas in chat (option A). A passive entry that is still unfilled after 15 minutes (`PASSIVE_ENTRY_TIMEOUT_SECONDS`, `algo-trader/strategies/state_machine.py`) is cancelled by the daemon (`expire_stale_passive_entries`, `algo-trader/portfolio/daemon.py`). The ticker is returned to flat at once, without the strategy cooldown, but only after the broker confirms the cancel and the ticker holds no shares. It is retried once, on the same signal bar, only if the market is still within the entry band (10 bp) of the signal price, and then at that band limit (`chase_after_timeout`), not at the bid. A timed-out entry whose retry is not allowed spends its signal bar. A pending order no longer adds a book-wide pause (`portfolio/reconciliation.py`). Only that ticker is blocked, and its notional stays reserved in the coordinator until it resolves. Partly filled orders are left alone. This amends the 2026-11-10 passive-entry decision for the timeout and chase; the bid pricing is unchanged for the first attempt.
- **Options risk is options-only.** 2026-11-11, approved by Andreas in chat. The options limits (defined-loss budget, delta, vega, gamma, theta, stress) are sized on the options account's equity alone. The equity account's equity and stock positions are not used. The options daily loss limit is 2% of options-account equity (`options/paper.py`). The six limit messages are renamed from "combined ..." to "options ...", and `frozen_gates.json` is updated to match. The same-account safety check still reads the equity account.
- **Options loss limits, 2026-11-11, approved by Andreas in chat.** The defined-loss budget (`max_total_defined_risk_pct`) is 60% of options equity, up from 10%: more risk for more possible profit. The per-trade risk, margin, delta, vega, gamma, theta and stress limits still apply. The daily loss limit is removed. In its place, new options entries stop when the options account is 8% or more below its highest close over the last 20 sessions, counting today's equity (`max_rolling_drawdown_pct`, `rolling_drawdown_sessions`, `options/paper.py`). With no close history, the gate does not apply.
- **Options history collects every strike.** 2026-11-11, approved by Andreas in chat. ThetaData history is collected with no strike window (`THETA_STRIKE_RANGE=max`) into `data/options/research/theta-max`, and it replaces a symbol's stored history only when that symbol's full history is complete. Partial and recent-only compiles never write to the database.
- **Cash reserve proxy cap 85%.** 2026-11-11, approved by Andreas in chat. `cash_reserve.max_proxy_pct` is 0.85 (was 0.80): at most 85% of equity may sit in SGOV. The reserve target stays 10% cash.
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

- **2026-11-11, one-shot real orders.** At Andreas's explicit request, a real order on an alert level is spent when the level fires. The level's real order is cleared once it has fired, whether or not the order request succeeded. Setting a real order again needs the order token. This adds no gate message.

- **2026-11-11, live manual account.** At Andreas's explicit request, the OpenStock manual account uses the live Alpaca endpoint, opted in with `ALPACA_ALLOW_LIVE=1` and the order token, because Alpaca allows three paper accounts and the manual account is needed for real dashboard trading. The per-order cap and the live daily cap still apply. The algo equity, options and momentum accounts stay on paper.

- **2026-11-11, momentum paper book.** At Andreas's explicit request, a quarterly cross-sectional momentum book (top five by six-month momentum, equal weight) runs on its own paper account (`ALPACA_MOMENTUM_*`), never the algo account. It is dry run by default; `--submit` places orders once per quarter. Exits and trims are marketable limits; buys rest at the bid. Orders pass the existing per-order cap. The book is research on paper and has no live capital.

- **2026-11-10, passive entries.** At Andreas's explicit request, regular-session entries no longer pay the spread: a buy rests at the bid and a short at the ask (`execution.entry_pricing` = `passive`). Exits and protective stops keep their marketable bands. A passive entry is not guaranteed to fill; an unfilled entry expires with the day. A missing two-sided quote suppresses the entry (`no two-sided quote for a passive entry`), which adds one gate message to `frozen_gates.json`. Each entry decision records the quote, mid and limit so fills can be measured against it.

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
