# OpenClaw trader schedules

All cron expressions below use `America/New_York`, so daylight saving changes do not shift the market-time schedule. The deterministic jobs run on the Gateway with `--command-argv` and `--command-cwd /opt/swarm-trader`. Their normal stdout is not delivered; OpenClaw sends the configured Telegram failure alert when a job fails. Existing report jobs still announce to the same chat and now fail if delivery fails.

| Job | Cron | Action | Failure alert |
| --- | --- | --- | --- |
| `day-open-recovery` | `30-34 9 * * 1-5` | Repeat `execute_trades.py --mode day --flatten` until the broker confirms zero positions and open orders. A successful run records today's account-bound recovery receipt, which Day entries require. | After 2 errors or skipped runs; 30-minute cooldown. |
| `day-flatten-guard` | `*/5 12-16 * * 1-5` | Run `execute_trades.py --mode day --flatten-if-due`. The broker calendar moves the deadline before an early close. Before the deadline this exits successfully without touching orders. | After 1 error or skipped run; 30-minute cooldown. |
| `swing-close-snapshot` | `5 16 * * 1-5` | Record the broker-calendar-verified Swing close snapshot. | After 1 error or skipped run; 1-hour cooldown. |
| `day-close-snapshot` | `6 16 * * 1-5` | Record the broker-calendar-verified Day close snapshot. | After 1 error or skipped run; 1-hour cooldown. |
| `algo-close-snapshot` | `7 16 * * 1-5` | Record the broker-calendar-verified Algo close snapshot. | After 1 error or skipped run; 1-hour cooldown. |
| `algo-health` | `*/10 * * * *` | Run the read-only Algo service, scan and persisted-state health check. | After 1 error or skipped run; 2-hour cooldown. |

The first five jobs run Monday through Friday. `algo-health` runs every day because the Algo service also covers Sunday night. Close snapshots run only after the exchange close; a market holiday is a successful skip. The Day report starts at 09:35 ET, after the 09:30–09:34 recovery window. The existing 15:45 Day report uses [`day_flatten.txt`](openclaw_prompts/day_flatten.txt) to report the calendar-aware flatten result and broker verification.

The four Algo report jobs use [`algo_report.txt`](openclaw_prompts/algo_report.txt) or the close variant [`algo_close_report.txt`](openclaw_prompts/algo_close_report.txt). They distinguish journal submissions from broker-confirmed fills and label delayed BOATS historical bars during overnight sessions. `autoresearch-run` and `autoresearch-report` retain their current behavior and now alert on failures; their code and research configuration were not changed here.

Inspect the live declarations with `openclaw cron list --all --json`. The pre-change and post-change job declarations are backed up as `openclaw-crons-before.json` and `openclaw-crons-after.json` under `data/backups/pre-migration-20261008T073438Z/`.
