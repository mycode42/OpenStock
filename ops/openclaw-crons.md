# Algo Trader automations

The scheduled Algo jobs run from `/opt/algo-trader`. Market-time jobs use `America/New_York`; the morning report runs at 04:00 ET to cover the overnight session.

| Job | Schedule | Purpose |
| --- | --- | --- |
| `health` | Hourly | Read-only service, scan, state, and broker-protection check. |
| `event-briefing` | 10:00, 13:00, and 15:00 ET, weekdays | Refresh the read-only Event Driven signal snapshot and report market context. |
| `options-history-capture` | 16:15 ET, weekdays | Save same-session Alpaca OPRA option chains for the 117-symbol options research history universe and refresh merged datasets. |
| `options-opening-capture` | 09:35 ET, weekdays | Save a separate Alpaca OPRA opening snapshot for the 117-symbol options research history universe. |
| `Theta options history` | Every 25 hours | Resume ThetaData EOD quotes and open interest across the 117-symbol options research history universe. |
| `algo-research` | 17:00 ET, daily | Run `python -m equity_research.nightly`: options research first, then equity AutoResearch with recent two-year verification. On Sunday, equity also runs full-history verification as a review only; it asks you to keep or demote each live profile. Target 6 hours total; guidance only, no timeout failure. Report equity and options under separate headings. Replaces the former `equity-research` and `options-research` jobs. |
| `morning` | 04:00 ET, weekdays | Send an overnight session, order, risk, and fill-attribution report. |

The Algo daemon records timestamped equity points every 15 minutes during the regular session and a daily close point after the market closes. The close point remains the weekly risk-baseline source, so no separate `close-snapshot` automation is needed.

The research history universe is in `options/research/universe.json`; it contains the existing 24 Algo tickers plus 93 additions and does not alter the live trading universe. The `options-opening-capture` OpenClaw command runs at 09:35 ET on weekdays and writes separate opening snapshots to `data/options/research/alpaca/<SYMBOL>/opening_chains.jsonl`. The `options-history-capture` command runs at 16:15 ET, archives close-side OPRA chains under `data/options/research/alpaca/`, and refreshes merged datasets. ThetaData remains needed until every history-universe manifest reports `archive_complete: true` for the requested date range.

The `event-briefing` analysis command uses a 300-second Gateway exec timeout; the automation retains its separate 20-minute turn timeout. Analyst phases and ticker-level progress are appended to `data/algo/llm_driven/analysis-runs.jsonl`. Missing headline sentiment is classified in one batch request, and LLM requests time out after 30 seconds by default (`EVENT_ANALYSIS_LLM_TIMEOUT_SECONDS` overrides this). A timed-out run does not publish or reuse a signal snapshot.


The live OpenClaw Gateway is the source of truth for job schedules and prompts. Use `openclaw automations list --all` and `openclaw automations get <job-id>` to inspect the declarations. Report templates are in [`openclaw_prompts/`](openclaw_prompts/).

Only the continuously running Algo coordinator can submit Algo orders. The scheduled briefing and report jobs are read-only; the AutoResearch job writes equity artifacts plus separate options research reports under `data/options/research/automation/`. Missing historical options chains are reported as `waiting_for_data` and do not fail the equity research run.
