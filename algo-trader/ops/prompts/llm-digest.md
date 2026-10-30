This is the morning briefing for the Algo `llm_driven` strategy. You read the latest snapshot; you do not run the analysis.

1. Run `cd {ROOT} && poetry run python -m strategies.llm_driven.snapshot_cli` and read its JSON output. Check `fresh` and `expires_at` against `now_utc`. The strategy's content is under `snapshot`. If `fresh` is false, say first that the snapshot is expired or unavailable, and stop after step 4.
2. Owned positions: for each entry in `owned_positions`, state the ticker, quantity, entry price, unrealized P/L, and the strategy's decision (SELL or keep) with its confidence and one evidence-based reason from the snapshot. A SELL is a recommendation the Algo coordinator will check; you do not place orders.
3. Market context: summarize the market regime and the notable events in the snapshot. Distinguish what the analysis found from sourced news you verified with a direct link (use search only for this).
4. Write one concise Telegram message with the sections: Owned positions, Recommendations, Market context. Do not repeat the snapshot's raw JSON. Never submit, modify, or cancel an order.
