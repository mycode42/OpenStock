# Algo Trader automations

The scheduled Algo jobs run from `/opt/algo-trader`. Market-time jobs use `America/New_York`; the morning report runs at 04:00 ET to cover the overnight session.

| Job | Schedule | Purpose |
| --- | --- | --- |
| `health` | Hourly | Read-only service, scan, state, and broker-protection check. |
| `event-briefing` | 10:00, 13:00, and 15:00 ET, weekdays | Refresh the read-only Event Driven signal snapshot and report market context. |
| `midday` | 11:35 ET, weekdays | Send a concise Algo account and strategy update. |
| `optimization` | 08:00 ET, daily | Run an archive-backed AutoResearch optimizer cycle on stored bars, without refreshing, rolling verification, or portfolio replay. |
| `autoresearch` | 20:15 ET, daily | Run equity AutoResearch alongside the independent VRP and event-driven options research batch, then report them in separate sections. Research may write artifacts; it does not place trades or alter live settings. |
| `morning` | 04:00 ET, weekdays | Send an overnight session, order, risk, and fill-attribution report. |

The Algo daemon records timestamped equity points every 15 minutes during the regular session and a daily close point after the market closes. The close point remains the weekly risk-baseline source, so no separate `close-snapshot` automation is needed.

The `event-briefing` analysis command uses a 300-second Gateway exec timeout; the automation retains its separate 20-minute turn timeout. Analyst phases and ticker-level progress are appended to `data/algo/event_driven/analysis-runs.jsonl`. Missing headline sentiment is classified in one batch request, and LLM requests time out after 30 seconds by default (`EVENT_ANALYSIS_LLM_TIMEOUT_SECONDS` overrides this). A timed-out run does not publish or reuse a signal snapshot.

Trigger `optimization` on demand with `openclaw automations run 3731fb23-c610-4b7a-9032-61c7beefe55e`. It shares the AutoResearch single-run lock with `autoresearch`, so do not start it while another AutoResearch process is active. Reports are written to `data/research/algo-auto/optimizer/`.

The live OpenClaw Gateway is the source of truth for job schedules and prompts. Use `openclaw automations list --all` and `openclaw automations get <job-id>` to inspect the declarations. Report templates are in [`openclaw_prompts/`](openclaw_prompts/).

Only the continuously running Algo coordinator can submit Algo orders. The scheduled briefing and report jobs are read-only; the AutoResearch job writes equity artifacts plus separate options research reports under `data/options/research/automation/`. Missing historical options chains are reported as `waiting_for_data` and do not fail the equity research run.
