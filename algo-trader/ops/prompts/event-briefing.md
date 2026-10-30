This is the scheduled AI research cycle for the active Algo `llm_driven` strategy.

1. Run exactly: `cd {ROOT} && poetry run python -m strategies.llm_driven.ai_runner --ttl-minutes 90`. Use the configured 300-second Gateway exec timeout for this command; do not impose a shorter shell or subprocess timeout. This invokes the shared analyst graph for the configured Algo universe and writes the validated, short-lived signal snapshot. It is analysis-only. After the runner succeeds, obtain the Gateway's authoritative time and verify freshness with this command:
`cd {ROOT} && poetry run python -m strategies.llm_driven.snapshot_cli`.
Use that `now_utc` and `fresh` result as the sole authority for the current date and snapshot expiry. Never use the model's remembered date to decide freshness. If `fresh` is true, do not call the run stale or failed. Never submit, modify, or cancel an order, and do not call order-execution or broker-action tools. If the command fails or returns invalid JSON, inspect the latest entry in `data/algo/llm_driven/analysis-runs.jsonl` and report its last phase/progress; then stop without reusing an older snapshot.
2. Search current, reputable sources for market-wide stock-market news and trading-community sentiment. Use direct source links and distinguish sourced reporting from the analyst model's summary.
3. Write a concise Telegram briefing with these sections:

Stock Market Briefing
- Summarize the broad market, notable news, and sentiment shifts across the trading community.

Ticker Universe Analysis
- Cover every ticker in the runner's `decisions` object. Start each line with the ticker and its exact BUY, SELL, or WATCH action, then give confidence and a short evidence-based reason. Add only notable, current ticker-specific news or events. Do not invent a catalyst or upgrade WATCH to an actionable signal.

Treat the JSON actions and confidence as the strategy snapshot's source of truth. A BUY is a long-entry recommendation; a SELL can close a long position only when `llm_driven` owns it. Positions owned by other strategies are left alone. The live Algo scanner still applies its risk and portfolio checks. The analysis itself does not place orders. If markets are closed, state that and note that the 90-minute snapshot will expire before the next session.
