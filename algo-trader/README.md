# Algo Trader

Systematic equity and options trading research with paper execution on Alpaca.

> **Paper trading by default.** Each broker base URL defaults to Alpaca Paper. Pointing one at another endpoint is an explicit override through its environment variable.

This is the Algo Trader subtree of [OpenStock](../README.md). It runs as its own service and is read by the OpenStock dashboard.

## What it does

- **Algo equity sleeve:** a rule-based coordinator selects strategies by a regime committee and trades the live universe, which is the selector's eligible subset of a 117-name candidate pool, recomputed each session. Strategies cover mean reversion, trend, momentum, multi-factor, and scheduled events on intraday and daily horizons. Multi-factor runs as a sector-neutral, dollar-neutral long/short book.
- **Options sleeve:** a separate paper account for volatility premium, long-volatility, and event structures, with combined equity/options risk checks.
- **Research:** equity AutoResearch (walk-forward validation, execution costs, regime breakdowns) and options research on a 117-symbol history universe, including a point-in-time eligible-universe selector.

Signal → coordinator → risk gate → Alpaca Paper. The risk gate has the final say.

## Documentation

- [Setup](docs/setup.md): requirements, the one-command setup, and the environment
- [Scheduled automations](docs/automations.md): the jobs, the crontab, and the logs
- [Operations](docs/operations.md): common commands, the live universe, and state backups
- [Research and configuration](docs/research-and-config.md): promotion, config files, and the shared rule set
- Strategy and research guides: [options research](docs/options_research.md), [options trading](docs/options_trading.md), [equity research](docs/adaptive_research.md), [LLM-driven strategy](docs/llm_driven_strategy.md), [options long volatility](docs/options-long-volatility.md)

## Data sources and subscriptions

| Source | Needed for | Cost and access |
| --- | --- | --- |
| Alpaca paper accounts (equity, options, manual) | Quotes, bars, orders, positions, and the daily Alpaca options capture | Free paper account. Keys go in the root `.env`. |
| An LLM provider (`LLM_PROVIDER`, `LLM_MODEL`, and its key) | The LLM-driven signal snapshot and the report jobs | Your provider account, or a local model (Ollama or an OpenAI-compatible server). |
| Telegram bot (`TELEGRAM_BOT_TOKEN`, `ALGO_REPORT_TELEGRAM_TO`) | Reports and health notices | Optional. |
| ThetaData (`THETA_DATA_API_KEY`) | Historical options EOD bid/ask and open interest, for the research history back to 2020 | **Paid. A one-month subscription is required for the backfill.** |

### ThetaData backfill (paid subscription)

The historical options backfill needs an active ThetaData subscription for the whole run. Without one, the collector cannot fetch the monthly archives. Plan for one month:

1. Subscribe to a ThetaData plan that includes historical options EOD data. Check the plan's current price and terms with ThetaData before you subscribe; this repository does not record them.
2. Copy the API key into the root `.env` as `THETA_DATA_API_KEY`. Do not commit it.
3. Install the optional client into the project venv: `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --with ai,ml,research,theta` (Python 3.12 or newer).
4. Start the pipeline once by hand to confirm access: `.venv/bin/python -m scripts.options.theta_pipeline`. It runs the last 20 sessions for every symbol first, then the full EOD archive, then the opening archive. The cron job runs it every four hours and restarts it if it stopped, and a stopped run loses nothing.
5. Watch progress in `data/options/research/theta/<SYMBOL>/`. A symbol is finished when its manifest shows `archive_complete: true`, for the full date range (2020-01-01 onward, per symbol listing dates).
6. When both archives are complete for every symbol, the pipeline sends a Telegram message and shows a notice on the Algo research page. Then cancel the ThetaData subscription and remove `THETA_DATA_API_KEY` from `.env`. The pipeline never calls ThetaData again. Current sessions come from the daily Alpaca capture, which records all 117 symbols.

The last few sessions may not be in Theta's archive yet, because ThetaData publishes EOD with a lag. Those sessions come from the Alpaca capture, but only from the day the capture ran.

The full mechanics, including the lock and the migration commands, are in [options research](docs/options_research.md#thetadata-historical-backfill).

## Credits

This Algo Trader builds on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), an MIT-licensed project by Virat Singh (© 2024). Its history is in this repository, and the original MIT notice is kept in [LICENSE](LICENSE).

## License

The Algo Trader subtree is MIT-licensed ([LICENSE](LICENSE)). The rest of OpenStock, the dashboard and server at the repository root, is ISC-licensed ([../LICENSE](../LICENSE)).
