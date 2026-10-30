This is the silent scheduled run of the Algo `llm_driven` strategy. Nobody reads this run's output; the snapshot is the product.

1. Run exactly: `cd {ROOT} && poetry run python -m strategies.llm_driven.ai_runner --ttl-minutes 90`, with the configured 300-second Gateway exec timeout. It refreshes the signal snapshot, reviews every long position the strategy owns (including tickers outside the live universe), and writes a SELL or keep decision for each.
2. Check freshness with: `cd {ROOT} && poetry run python -m strategies.llm_driven.snapshot_cli`.
3. Never submit, modify, or cancel an order, and do not call broker-action tools. Do not send any message.
4. Reply with exactly `NO_REPLY` when step 1 succeeded and the snapshot is fresh. If the command failed or the output is invalid, reply with one line naming the failure and the last line of the latest entry in `data/algo/llm_driven/analysis-runs.jsonl`.
