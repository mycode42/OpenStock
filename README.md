# OpenStock

**A self-hosted stock dashboard, trading journal, and paper-trading Algo engine, built on Alpaca.**

It also includes a built-in algorithmic trading system for equity and options, under [algo-trader/](algo-trader/).

> **For research and personal use only.** OpenStock is not investment advice and comes with no warranty. Orders it places are real orders on the account you configure, and they can lose money.

<img width="800" alt="Algo-Trader" src="https://github.com/user-attachments/assets/bc19b048-e46d-41b1-bd07-43a6a6cfe98d" />


## Quick start: try the demo

You need Node.js 22 or newer and a C/C++ toolchain with Python 3 (for the `better-sqlite3` module). No broker account is needed.

```bash
git clone https://github.com/mycode42/OpenStock.git
cd OpenStock
npm ci
npm run demo
```

Open http://127.0.0.1:3000. The demo runs the real interface on a simulated paper account. You get a six-stock watchlist with alert levels, held positions and a journal. Open **Configure** to try the real-order switch and budgets, and place a limit order from an alert. Orders are simulated and nothing is sent to a broker. The demo uses its own database under `data/demo/`. Delete that folder to start over. To use another port, add `PORT=3001` before `npm run demo`. See [Demo mode](docs/demo-mode.md).

## Set up for real

The same app starts in demo mode whenever `ALPACA_MANUAL_API_KEY` and `ALPACA_MANUAL_API_SECRET` are empty, so the steps below switch it to your Alpaca account.

```bash
cp .env.example .env
# Edit .env: set ALPACA_MANUAL_API_KEY and ALPACA_MANUAL_API_SECRET.
# Use paper keys unless you mean to trade live.
npm start
```

Open http://127.0.0.1:3000. The server binds to `127.0.0.1` only.

- `.env` is read from the repository directory and is ignored by Git. Never commit credentials.
- Alpaca provides quotes and chart history (SIP when your account has it, IEX otherwise). OpenStock needs no account ID.
- Telegram alerts are optional: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then run `npm run monitor` for one monitoring cycle. A scheduler runs it in production.
- Live trading is off by default. It needs `ALPACA_ALLOW_LIVE=1` and `OPENSTOCK_ORDER_TOKEN`, and every order is capped at $100,000.
- To run it as a service on a Linux host, see [DEPLOY.md](DEPLOY.md).

Check the setup at any time with `curl -s http://127.0.0.1:3000/api/demo-status`. It reports whether the app is in demo mode and what is missing.

## Algo Trader (optional)

The Algo Trader is a research and paper-trading engine: a rule-based equity coordinator with a regime committee, a separate options paper account, and equity and options research with walk-forward validation. The dashboard works without it.

Requirements: Python 3.12 and Poetry 2.x (Python 3.11 and 3.13 are not supported), plus a C compiler toolchain for the optional Cython extensions.

1. Add `ALPACA_EQUITY_API_KEY`, `ALPACA_EQUITY_API_SECRET` and `ALPACA_EQUITY_ACCOUNT_ID` to the root `.env`. For the options sleeve, add the `ALPACA_OPTIONS_*` set. Keep the Alpaca URLs on paper.
2. Leave `ALGO_TRADER_ROOT=./algo-trader` as it is.
3. Run the one-time setup from the subtree, with the Telegram chat that receives the reports:

   ```bash
   cd algo-trader
   scripts/setup.sh
   ```

   This installs the Python dependencies, renders the systemd user units into `~/.config/systemd/user` (no root), and installs the scheduled automations. It does not start the services.
4. Start the services: `systemctl --user enable --now equity options`.

Details: [Algo setup](algo-trader/docs/setup.md), [Scheduled automations](algo-trader/docs/automations.md) and [Operations](algo-trader/docs/operations.md).

## Docker or Podman

The image runs the web app. It does not include the Algo Trader source or any data; you mount them when you run it. The server binds to `127.0.0.1` inside the container, so the container uses host networking. That needs a Linux host. Docker Desktop on macOS or Windows does not support it, so run those machines with `npm start` instead.

Commands below use `docker`. Rootless Podman accepts the same arguments; replace `docker` with `podman`.

```bash
# Build the image from the repository root.
docker build -t openstock .

# Demo: no credentials, so the app starts in demo mode. The container has an empty watchlist and refuses orders;
# for the full simulated demo with orders, use npm run demo.
docker run --rm --network host -e PORT=3000 -e DATABASE_PATH=/data/openstock.db \
  -v openstock-data:/data openstock
```

Open http://127.0.0.1:3000. The database lives in the `openstock-data` volume.

For your real account, pass the same `.env` you use locally and mount the Algo tree read-only:

```bash
docker run -d --name openstock --network host --env-file .env \
  -e DATABASE_PATH=/data/openstock.db \
  -v openstock-data:/data \
  -v "$PWD/algo-trader:/app/algo-trader:ro" \
  openstock

docker logs -f openstock          # follow the server log
docker stop openstock             # stop it
```

Notes:

- Start the web container before the monitor, because it creates the database on first start. For Telegram alerts, run the monitor as a second container that uses the same volume:

  ```bash
  docker run --rm --network host --env-file .env -e DATABASE_PATH=/data/openstock.db \
    -v openstock-data:/data --entrypoint node openstock scripts/monitor.js
  ```

  Run that command on a schedule of your choice; cron's shortest interval is one minute.
- Mounting `algo-trader` read-only is what lets the Algo pages read their data and strategy settings. Without it, those pages show an error. The mount is read-only, so the container cannot change the Algo files.
- Keep `ALGO_TRADER_ROOT` as `./algo-trader`, and keep `ALPACA_OPTIONS_ACCOUNT_ID` in `.env`. The options history is keyed by that ID.
- To update, pull the repository, rebuild the image, and recreate the container with the same arguments. The data volume is kept.

## Checks

```bash
npm test          # the full Node test suite
npm run check     # syntax and PWA asset checks
npm run audit:prod
```

The Algo Trader has its own suite: `cd algo-trader && poetry run pytest`.

## Configuration

`.env` holds every setting. Start from `.env.example`, and set `OPENSTOCK_CONFIG_FILE` to load a different file.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_MANUAL_API_KEY` / `_SECRET` | empty | Alpaca keys for the dashboard account. Empty means demo mode. |
| `ALPACA_MANUAL_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Trading endpoint. Any other URL is live trading and needs `ALPACA_ALLOW_LIVE=1` and `OPENSTOCK_ORDER_TOKEN`. |
| `ALPACA_DATA_FEED` | `sip` | Stock-data feed. Falls back to `iex` when SIP access is denied. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Alert delivery. |
| `DATABASE_PATH` | `database/openstock.db` | SQLite database. Relative paths resolve from the checkout. |
| `PORT` | `3000` | Web server port. The server always binds to `127.0.0.1`. |
| `ALGO_TRADER_ROOT` | `./algo-trader` | Algo Trader directory. |
| `ALPACA_EQUITY_*`, `ALPACA_OPTIONS_*` | empty | Keys and account IDs for the Algo sleeves. |

The full list, with defaults and notes, is in [Configuration](docs/configuration.md).

Prices, amounts and performance values are shown rounded to two decimals. Journal fields keep the precision you enter, up to six decimals. The Dashboard polls `/api/live-prices` while visible; that endpoint is display-only and never writes stock state, history, alert state or equity snapshots.

## Features

- **Dashboard:** Alpaca USD quotes, alert levels, and Alpaca price-history charts.

  <img width="800" alt="Dashboard" src="https://github.com/user-attachments/assets/4a7ef91f-980e-4b50-ac99-9d9f05f11172" />

- **Monitoring:** a one-shot monitor cycle (run by `npm run monitor` or a scheduler) that checks BUY and SELL thresholds against Alpaca quotes. Alerts fire once per episode and use a durable outbox for Telegram delivery.

  <img width="800" alt="Configure-Overview" src="https://github.com/user-attachments/assets/893362cf-b60f-4689-b47c-aeaea166181c" />

- **Manual trading:** a triggers can be equipped with real trades that are executed when fired.

  <img width="800" alt="Configure" src="https://github.com/user-attachments/assets/e3d24bb3-ae35-4e2a-9485-c252f85baf31" />

- **Trading journal:** reads Alpaca account balances, positions, and fills in USD.

  <img width="800" alt="Journal" src="https://github.com/user-attachments/assets/40321332-f0d0-4ef6-9571-f0c3b2d5b7b1" />

- **Universe:** shows the current selected dynamic algo trading universe.

  <img width="1179" height="647" alt="Universe" src="https://github.com/user-attachments/assets/1f382cd4-24d9-4d2f-aa49-f341acf6badf" />

- **Performance:** per strategy P&L and latest coordinator & trading activity 

  <img width="800" alt="Algo-Performance" src="https://github.com/user-attachments/assets/39136276-48c6-452b-8795-de5a7604d480"/>

- **Research Lab:** Shows nightly research history and provides manual research and refining option.

  <img width="800" alt="Research" src="https://github.com/user-attachments/assets/287d75c0-c1cb-4ffe-9840-0490c4167d8c"/>

- **Themes:** twelve colour themes, each in light, dark or system mode. The choice is saved in the browser.

  <img width="800" alt="Themes" src="https://github.com/user-attachments/assets/0d195d8d-4d29-469c-87d4-0a062f31791a"/>



## What's inside

| Area | What it does | Read more |
|------|--------------|-----------|
| Demo mode | Full interface on simulated data, no broker | [Demo mode](docs/demo-mode.md) |
| Dashboard | Alpaca USD quotes, alert levels, price-history charts | [Features](docs/features.md) |
| Monitoring | One-shot cycle checks BUY and SELL levels; alerts fire once per episode via Telegram | [Monitoring](docs/monitoring.md) |
| Trading journal | Alpaca balances, positions and fills; capital events kept apart from P/L | [Features](docs/features.md) |
| Analytics | P/L, risk, equity curve, drawdown, per-stock performance | [Performance data](docs/performance-data.md) |
| Manual limit orders | Limit orders from alert levels; paper by default | [Features](docs/features.md) |
| Algo Trader | Systematic equity and options research with paper execution | [algo-trader/](algo-trader/README.md) |
| Configuration | Environment variables, price sources, watchlist migration | [Configuration](docs/configuration.md) |
| API | Endpoints for the dashboard, journal, orders and health | [API](docs/api.md) |
| Architecture | Layout, server construction, tests | [Architecture](docs/architecture.md) |
| Deployment | systemd, Tailscale, Telegram credentials | [DEPLOY.md](DEPLOY.md) |
| History | Authors and AI-assisted commits | [Project history](docs/project-history.md) |

## Features at a glance

- **Alerts that fire once.** Each level queues one alert in a durable outbox. The outbox retries delivery and never sends twice.
- **A journal that never invents P/L.** Deposits and withdrawals are tracked separately from trading results.
- **Figures that admit missing data.** Metrics show `N/A` or `Insufficient data` until enough history exists.

Screenshots of every screen are in [Features](docs/features.md).

## License and credits

OpenStock's own code is licensed under the ISC licence ([LICENSE](LICENSE)).

The Algo Trader in [algo-trader/](algo-trader/) is MIT-licensed ([its LICENSE](algo-trader/LICENSE)). 
It builds on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), an MIT-licensed project by Virat Singh (© 2024). 
That MIT notice is kept in the subtree's LICENSE. 
Credits are also in the [Algo Trader README](algo-trader/README.md#credits).

