# Algo Trader automations

The scheduled Algo jobs run from this `algo-trader/` subtree, wherever OpenStock is checked out. Market-time jobs use `America/New_York`. The definitions are in [`ops/automations.json`](automations.json), installed by `scripts/setup_automations.py`, and described in the README under "Scheduled automations".

| Job | Schedule | Purpose |
| --- | --- | --- |
| `health` | Hourly | Combined read-only equity and options service, account, state, and risk-cycle check. |
| `llm-driven` | 10:00, 13:00, and 15:00 ET, weekdays | Refresh the read-only `swing_llm_driven` signal snapshot (SQLite, read with `python -m strategies.llm_driven.snapshot_cli`) and report market context. |
| `options-history-capture` | 15:45 ET, weekdays | Start the same-session Alpaca OPRA option-chain capture before the market close so the 117-symbol archive can finish on time. |
| `options-opening-capture` | 09:35 ET, weekdays | Save a separate Alpaca OPRA opening snapshot for the 117-symbol options research history universe. |
| `ThetaData historical options backfill` | Every 25 hours | Resume historical ThetaData EOD quote and open-interest archives across the 117-symbol research universe. |
| `options-research` | 17:00 ET, daily | Run `python -m research.options` for options research only; it does not change orders or equity state. |
| `equity-research` | 20:00 ET, daily | Run `python -m research.equity` for equity AutoResearch only, separate from the options job. |

The Algo daemon records timestamped equity points every 15 minutes during the regular session and a daily close point after the market closes. The close point remains the weekly risk-baseline source, so no separate `close-snapshot` automation is needed.

The research history universe is in `options/research/universe.json`; it contains the existing 24 Algo tickers plus 93 additions and does not alter the live trading universe. Both Alpaca capture jobs write directly to `data/options/research/options_history.sqlite3`, with opening and close-side phases kept separate. The close-side job starts at 15:45 ET; its capture timestamp is capped at the session close, and a stock daily bar fetched before 16:00 ET may still be forming. If SPY's stored market date is missing or stale, the live selector retries from completed Alpaca SPY bars, preferring SIP and falling back to IEX; the fallback records price and volume without option-chain liquidity metrics. ThetaData is used for historical chain backfill, not this live fallback.

The `llm-driven` analysis command uses a 300-second Gateway exec timeout; the automation retains its separate 20-minute turn timeout. Analyst phases and ticker-level progress are appended to `data/algo/llm_driven/analysis-runs.jsonl`. Missing headline sentiment is classified in one batch request, and LLM requests time out after 30 seconds by default (`EVENT_ANALYSIS_LLM_TIMEOUT_SECONDS` overrides this). A timed-out run does not publish or reuse a signal snapshot.


The live OpenClaw Gateway is the source of truth for job schedules and prompts. All Algo automations run under the OpenClaw `main` agent (Stocky) in isolated sessions; no separate Algo agent is configured. Use `openclaw cron list --all` and `openclaw cron get <job-id>` to inspect the declarations. The equity report template is in [`ops/openclaw_prompts/`](openclaw_prompts/).

Only the continuously running Algo coordinator can submit Algo orders. The scheduled briefing and research jobs are read-only; the equity job writes equity artifacts, and the options job writes separate reports under `data/options/research/automation/`. Missing historical options chains are reported as `waiting_for_data` and do not fail the equity research run.
