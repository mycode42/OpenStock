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
| `algo-event-calendar` | `5 4 * * 1-5` | Refresh upcoming Nasdaq earnings events for the fixed 22-symbol Algo universe and merge published BLS/Fed dates into `data/event_calendar.csv`; report macro schedule coverage gaps. | After 1 error or skipped run; 1-hour cooldown. |
| `algo-auto-research-run` | `15 17 * * 1-5` | Refresh fixed-universe Plus SIP bars and the SEC/BLS/Fed historical event calendar, fit purged factor weights, then run eight bounded walk-forward profiles and core-vs-six-strategy holdout replays with eight lower-priority workers. Writes research candidates only. | After 1 error or skipped run; 1-hour cooldown. |
| `algo-auto-research-report` | `30 20 * * 1-5` | Report data coverage, selected walk-forward profiles and holdouts, factor diagnostics, event-calendar coverage, and four-core vs six-strategy replay results to Telegram. | After 1 error or skipped run; 1-hour cooldown. |

The Day recovery/flatten, three close snapshots, and both AutoResearch jobs run Monday through Friday. `algo-health` runs every day because the Algo service also covers Sunday night. Close snapshots run only after the exchange close; a market holiday is a successful skip. The Day report starts at 09:35 ET, after the 09:30–09:34 recovery window. The existing 15:45 Day report uses [`day_flatten.txt`](openclaw_prompts/day_flatten.txt) to report the calendar-aware flatten result and broker verification.

The four Algo trading reports use [`algo_report.txt`](openclaw_prompts/algo_report.txt) or the close variant [`algo_close_report.txt`](openclaw_prompts/algo_close_report.txt). They distinguish journal submissions from broker-confirmed fills and label delayed BOATS historical bars during overnight sessions. The Algo AutoResearch report job carries its read-only Telegram instructions in its OpenClaw declaration; it cannot write live settings or submit orders. The Swing/Day `autoresearch-run` and `autoresearch-report` jobs retain their existing LLM-driven behavior and separate experiment log.

Day morning, opening, midday, and afternoon prompts gather data from the configured fixed Day universe and keep that ticker list unchanged.

Inspect the live declarations with `openclaw cron list --all --json`. The pre-change and post-change job declarations are backed up as `openclaw-crons-before.json` and `openclaw-crons-after.json` under `data/backups/pre-migration-20261008T073438Z/`.
