# Scheduled automations

Part of the [Algo Trader README](../README.md).

The definitions live in [`ops/automations.json`](../ops/automations.json). Market times are `America/New_York`.

| Job | Schedule | Runs | Kind |
| --- | --- | --- | --- |
| `health` | Hourly | `poetry run python -m portfolio.healthcheck` (read-only equity and options health check) | command |
| `options-opening-capture` | 09:35 weekdays | Alpaca OPRA opening snapshot for the research universe | command |
| `options-history-capture` | 15:45 weekdays | Same-session Alpaca OPRA option-chain capture before the close | command |
| `ThetaData historical options backfill` | Every 25 hours (cron fallback: 03:00 daily) | Resume historical ThetaData EOD archives for the research universe | command |
| `llm-driven` | Hourly 04:00–20:00 ET, weekdays | Silent run: refresh the `swing_llm_driven` snapshot, including owned-position reviews; prompt in `ops/openclaw_prompts/llm-silent-run.md`. No delivery. | agent turn |
| `llm-digest` | 07:45 ET, weekdays | One briefing from the latest snapshot: owned positions, keep or sell, market context; prompt in `ops/openclaw_prompts/llm-digest.md`; delivered to Telegram | agent turn |
| `options-research` | 17:00 daily | `python -m research.options`; prompt in `ops/openclaw_prompts/options-research.md`; delivered to Telegram | agent turn |
| `equity-research` | 20:00 daily | `python -m research.equity`; prompt in `ops/openclaw_prompts/equity-research.md`; delivered to Telegram | agent turn |

## Install or update

`scripts/setup.sh` runs this for you, so every job is set up in one step. To run only the installer:

```sh
ALGO_REPORT_TELEGRAM_TO=<chat id> python scripts/setup_automations.py --dry-run
ALGO_REPORT_TELEGRAM_TO=<chat id> python scripts/setup_automations.py
```

With the OpenClaw CLI on `PATH`, the script creates or edits the jobs by name (`--backend openclaw`). Otherwise it writes a marked block to your user crontab (`--backend cron`), which runs the command jobs only.

Agent turns need OpenClaw to run the prompt and deliver the report, so the cron backend skips them. The theta backfill's 25-hour interval cannot be expressed in cron, so it runs daily at 03:00 there. The report destination is read from `ALGO_REPORT_TELEGRAM_TO` and is not stored in the repository.

The OpenClaw Gateway is the source of truth for job schedules and prompts; `openclaw cron list --all` and `openclaw cron get <job-id>` show the live declarations. The job notes are in [ops/openclaw-crons.md](../ops/openclaw-crons.md).
