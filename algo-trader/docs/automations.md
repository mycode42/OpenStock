# Scheduled automations

Part of the [OpenStock README](../../README.md#algo-trader-part-of-openstock-optional).

Every scheduled Algo job is a systemd user timer in `ops/systemd/`, named without a prefix (`health.timer`, `llm-driven.timer`, ...). Nothing depends on OpenClaw or cron. Each timer is written in `America/New_York`, so the jobs fire at Eastern time on any host clock.

| Timer | Schedule (ET) | Runs |
| --- | --- | --- |
| `health` | Hourly | `portfolio.healthcheck` (read-only equity and options health check) |
| `options-opening-capture` | 09:35 weekdays | `scripts.options.alpaca_daily_capture --phase open`: Alpaca OPRA opening snapshot for the research universe |
| `options-history-capture` | 15:45 weekdays | `scripts.options.alpaca_daily_capture`: same-session Alpaca OPRA option-chain capture during the final 15 minutes before the close |
| `theta-pipeline` | Every 4 hours | `scripts/options/theta_pipeline.py`: last 20 sessions, then EOD archives for the research universe. Stops calling ThetaData once complete; status shows on the Algo research page. A run still in progress makes the next one wait. |
| `llm-driven` | 06:30, 09:45, 12:30, 15:30, 18:00 weekdays | `strategies.llm_driven.ai_runner`: silent run that refreshes the `swing_llm_driven` snapshot, including owned-position reviews. No delivery. |
| `llm-digest` | 07:45 weekdays | One briefing from the latest snapshot (`ops/prompts/llm-digest.md`), delivered to Telegram |
| `options-research` | 17:00 daily | `research.options`, then a report from `ops/prompts/options-research.md`, delivered to Telegram |
| `equity-research` | 20:00 daily | `research.equity`, then a report from `ops/prompts/equity-research.md`, delivered to Telegram |
| `rotate-logs` | 04:30 daily | `scripts/rotate_logs.py`: keeps each log in `data/algo/logs/` small |

The two daemons, `equity` and `options`, are separate long-running services, not timers.

The `theta-pipeline` timer is disabled. The ThetaData subscription is cancelled, so nothing calls ThetaData. A fresh install that needs ThetaData history enables it with `THETA_DATA_API_KEY` set; it stops calling ThetaData once the archive is complete.

## LLM signal snapshot

The `llm-driven` silent run publishes each validated analysis as a row in `data/algo/llm_driven/signals.sqlite3` (table `llm_signal_snapshot`, last 200 runs kept). The scanner and both LLM briefings read the newest row. To see it, run:

```sh
cd <algo-trader checkout> && poetry run python -m strategies.llm_driven.snapshot_cli
```

The output has `now_utc`, `status`, `fresh`, `expires_at`, and the full payload under `snapshot`. `fresh` is the only freshness check the prompts use. The briefing prompts run this command from `{ROOT}`, so the rendered prompt has an absolute path and does not depend on the working directory of the job.

## Setup

Create the venv once from `algo-trader/` with `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --with ai,ml,research`. `scripts/install_units.py` renders every unit in `ops/systemd/` into `~/.config/systemd/user` with the venv Python. It writes files only; start them yourself:

```sh
systemctl --user enable --now health.timer theta-pipeline.timer options-research.timer equity-research.timer rotate-logs.timer llm-driven.timer llm-digest.timer options-opening-capture.timer options-history-capture.timer
systemctl --user list-timers
```

Lingering keeps the timers running after logout (`loginctl enable-linger <user>`).

The LLM calls use the provider in `.env` (`LLM_PROVIDER`, `LLM_MODEL`, and the matching API key). A local model works the same way: `LLM_PROVIDER=Ollama` with a pulled `LLM_MODEL`, or `OPENAI_API_BASE` pointing at a local OpenAI-compatible server.

`scripts/llm_report.py` runs a prompt file from `ops/prompts/` through the configured model, adds the data it names, and posts the reply to Telegram with `--telegram`. It uses `TELEGRAM_BOT_TOKEN` and `ALGO_REPORT_TELEGRAM_TO` (or `TELEGRAM_CHAT_ID`) from `.env`. It has no web access, so reports say when a source was not checked. `--dry-run` prints the assembled message without calling a model.

Each job writes to its own log in `data/algo/logs/`. The capture jobs also write a timestamped line per run (`--log-file`): the per-symbol results, any skip, and a closing `ok` or `failed` line with the captured count. The rotate job caps the log sizes.

## Changing a schedule

Edit the `OnCalendar=` lines in the matching `.timer` under `algo-trader/ops/systemd/`, keeping `America/New_York` on each line. Then render and reload:

```sh
poetry run python scripts/install_units.py
systemctl --user daemon-reload
systemctl --user restart <name>.timer
```
