# Algo Trader automations

The scheduled Algo jobs run from `/opt/algo-trader`. Market-time jobs use `America/New_York`; the morning report runs at 04:00 ET to cover the overnight session.

| Job | Schedule | Purpose |
| --- | --- | --- |
| `health` | Hourly | Read-only service, scan, state, and broker-protection check. |
| `event-briefing` | 10:00, 13:00, and 15:00 ET, weekdays | Refresh the read-only Event Driven signal snapshot and report market context. |
| `midday` | 11:35 ET, weekdays | Send a concise Algo account and strategy update. |
| `close-snapshot` | 16:07 ET, weekdays | Save a broker-calendar-verified account close snapshot. |
| `autoresearch` | 20:15 ET, daily | Run one research cycle and report its verification results. Research may write artifacts; it does not place trades or alter live settings. |
| `morning` | 04:00 ET, weekdays | Send an overnight session, order, risk, and fill-attribution report. |

The live OpenClaw Gateway is the source of truth for job schedules and prompts. Use `openclaw automations list --all` and `openclaw automations get <job-id>` to inspect the declarations. Report templates are in [`openclaw_prompts/`](openclaw_prompts/).

Only the continuously running Algo coordinator can submit Algo orders. The scheduled briefing and report jobs are read-only; the AutoResearch job writes only its normal research artifacts.
