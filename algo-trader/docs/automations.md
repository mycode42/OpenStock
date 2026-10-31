# Scheduled automations

Part of the [Algo Trader README](../README.md).

The job definitions are recorded in [`ops/automations.json`](../ops/automations.json) as a reference. The live jobs are managed in OpenClaw. Market times are `America/New_York`.

| Job | Schedule | Runs | Kind |
| --- | --- | --- | --- |
| `health` | Hourly | `poetry run python -m portfolio.healthcheck` (read-only equity and options health check) | command |
| `options-opening-capture` | 09:35 weekdays | Alpaca OPRA opening snapshot for the research universe | command |
| `options-history-capture` | 15:45 weekdays | Same-session Alpaca OPRA option-chain capture before the close | command |
| `ThetaData history pipeline` | Every 4 hours until complete (a run already in progress makes the next one exit) | `scripts/options/theta_pipeline.py`: last 20 sessions, then EOD, then opening archives for the research universe. Stops calling ThetaData once complete; status shows on the Algo research page | command |
| `llm-driven` | 06:30, 09:45, 12:30, 15:30, 18:00 ET, weekdays | Silent run: refresh the `swing_llm_driven` snapshot, including owned-position reviews; prompt in `ops/prompts/llm-silent-run.md`. No delivery. | agent turn |
| `llm-digest` | 07:45 ET, weekdays | One briefing from the latest snapshot: owned positions, keep or sell, market context; prompt in `ops/prompts/llm-digest.md`; delivered to Telegram | agent turn |
| `options-research` | 17:00 daily | `python -m research.options`; prompt in `ops/prompts/options-research.md`; delivered to Telegram | agent turn |
| `equity-research` | 20:00 daily | `python -m research.equity`; prompt in `ops/prompts/equity-research.md`; delivered to Telegram | agent turn |

## LLM signal snapshot

The `llm-driven` silent run publishes each validated analysis as a row in `data/algo/llm_driven/signals.sqlite3` (table `llm_signal_snapshot`, last 200 runs kept). The scanner and both LLM briefings read the newest row. To see it, run:

```sh
cd <algo-trader checkout> && poetry run python -m strategies.llm_driven.snapshot_cli
```

The output has `now_utc`, `status`, `fresh`, `expires_at`, and the full payload under `snapshot`. `fresh` is the only freshness check the prompts use. The briefing prompts run this command from `{ROOT}`, so the rendered prompt has an absolute path and does not depend on the working directory of the OpenClaw job.

## Cron (standalone, no OpenClaw)

Every job runs from the local crontab with the project venv at `.venv`, which holds the research dependencies. The LLM calls use the provider in `.env` (`LLM_PROVIDER`, `LLM_MODEL`, and the matching API key). A local model works the same way: `LLM_PROVIDER=Ollama` with a pulled `LLM_MODEL`, or `OPENAI_API_BASE` pointing at a local OpenAI-compatible server.

Create the venv once from `algo-trader/` with `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --with ai,ml,research`. `scripts/llm_report.py` runs a prompt file from `ops/prompts/` through the configured model, adds the data it names, and posts the reply to Telegram with `--telegram`. It uses `TELEGRAM_BOT_TOKEN` and `ALGO_REPORT_TELEGRAM_TO` (or `TELEGRAM_CHAT_ID`) from `.env`. It has no web access, so reports say when a source was not checked. `--dry-run` prints the assembled message without calling a model.

The crontab is written to `algo-trader/ops/crontab`, which has the full set of lines. Install it with `crontab ops/crontab` (this replaces your user crontab), and check it with `crontab -l`. Each job writes to its own log in `data/algo/logs/`, and a daily job caps their size. The only path in the file is the `ALGO` line at the top, so change that line if the checkout moves. The health job sources `scripts/cron-env.sh`, which sets the systemd session bus for the account running cron.

## Install or update

Manage the jobs directly with the OpenClaw CLI. There is no installer script:

```sh
openclaw automations list
openclaw automations get <job-id>
openclaw automations edit <job-id> --cron '0 4-20 * * 1-5' --tz America/New_York
openclaw automations disable <job-id>
openclaw automations runs <job-id>
```

Prompts are stored in the job. When a prompt file under `ops/prompts/` changes, copy the new text into the job with `openclaw automations edit <job-id> --message ...`. The prompt files use `{ROOT}`; substitute the checkout path when you do.

The OpenClaw Gateway is the source of truth for job schedules and prompts; `openclaw cron list --all` and `openclaw cron get <job-id>` show the live declarations. The job notes are in [ops/openclaw-crons.md](../ops/openclaw-crons.md).
