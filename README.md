# OpenStock

**A self-hosted stock dashboard, trading journal, and paper-trading Algo engine, built on Alpaca.**

**It also features a built in algorithmic trading system for equity and options**

> **For research and personal use only.** OpenStock is not investment advice and comes with no warranty. Orders it places are real orders on the account you configure, and they can lose money.

<img width="800" alt="Screenshot 2026-10-28 at 13 40 08" src="https://github.com/user-attachments/assets/51ec9bbc-aff4-44e4-ae12-9d2480047462" />

Try it without any broker account: `npm run demo` runs the full interface on simulated quotes.

## Quick start

Requirements: Node.js 22 or newer, and a C/C++ toolchain with Python 3 for the `better-sqlite3` native module.

```bash
git clone https://github.com/mycode42/OpenStock.git
cd OpenStock
npm ci
cp .env.example .env
# Add ALPACA_MANUAL_API_KEY and ALPACA_MANUAL_API_SECRET for the dashboard (paper keys by default),
# and ALPACA_EQUITY_API_KEY and ALPACA_EQUITY_API_SECRET for Algo Trader
npm start
```

Then open http://127.0.0.1:3000. The `.env` file is ignored by Git; never commit live credentials. To use another port, set `PORT=3001` before `npm start`.

### Algo Trader (optional)
For a no-account demo, run `npm run demo` instead. It uses a separate database and ignores broker credentials.

### Demo mode

`npm run demo` starts OpenStock with simulated quotes and charts. It:

- ignores any local `.env` file and every broker credential variable, so it never contacts a broker;
- stores its data in `data/demo/openstock.db`, separate from any real database;
- fills the trading journal with sample lots, deposits and a 150-day equity curve, so Journal and Analytics have data to show. Delete `data/demo/` to start over;
- shows a banner in the interface listing the setup still missing.

Normal startup uses Alpaca when its API key and secret are configured. Without Alpaca credentials, OpenStock starts in demo mode. Algo Trader is optional and does not control the stock dashboard's demo state. The application reports missing setup through `GET /api/demo-status`.

## Features

- **Dashboard:** Alpaca USD quotes, alert levels, and Alpaca price-history charts.
  
  <img width="800" alt="Screenshot 2026-10-28 at 13 48 03" src="https://github.com/user-attachments/assets/8a077760-4125-43cc-83d0-3141a5f4bd0c" />

- **Monitoring:** a one-shot monitor cycle (run by `npm run monitor` or a scheduler) that checks BUY and SELL thresholds against Alpaca quotes. Alerts fire once per episode and use a durable outbox for Telegram delivery.

  <img width="800" alt="Screenshot 2026-10-28 at 15 15 38" src="https://github.com/user-attachments/assets/249c61da-0a47-489f-8650-b91414a8fc06" />

- **Trading journal:** reads Alpaca account balances, positions, and fills in USD. OpenStock does not submit orders.
  
  <img width="800" alt="Screenshot 2026-10-28 at 13 34 18" src="https://github.com/user-attachments/assets/9a7c47a9-cffd-4236-98e1-7b2860158c21" />

- **Capital events:** initial capital, deposits, withdrawals and financing are tracked separately, so they never count as trading P/L.

  <img width="800" alt="Screenshot 2026-10-28 at 13 38 43" src="https://github.com/user-attachments/assets/daeb17d4-47c6-4541-be2f-2d83fbcab2c8" />

- **Analytics:** P/L, risk, equity curve, drawdown, monthly and per-stock performance. Figures show `N/A` or `Insufficient data` until enough history exists.
  
  <img width="800" alt="Screenshot 2026-10-28 at 13 35 05" src="https://github.com/user-attachments/assets/7c4db67e-85fc-40b0-8472-8a50cacef214" />

- **Algo Trader pages:** performance research and universe views for a separate Algo Trader installation, with its strategy settings editable from the UI.

  <img width="800" alt="Screenshot 2026-10-28 at 13 20 51" src="https://github.com/user-attachments/assets/b2bdc6a4-3884-49e0-890b-bb00282a4274" />

- **Algo Trader pages:** research
  
  <img width="800" alt="Screenshot 2026-10-28 at 13 21 27" src="https://github.com/user-attachments/assets/ae0d0fc7-2303-4786-ab2f-91b396c69b73" />

- **Algo Trader pages:** universe

  <img width="800" alt="Screenshot 2026-10-28 at 13 45 58" src="https://github.com/user-attachments/assets/34114b8a-af13-4e7d-8a9a-67d89ca7b0ab" />


OpenStock does not place, change or cancel orders. Broker endpoints are read-only: they retrieve quotes, accounts, portfolio, orders and transactions.

  <img width="800" alt="Screenshot 2026-10-28 at 13 22 44" src="https://github.com/user-attachments/assets/0d99c880-96bb-4782-bb4b-71c1e6569a4e" />


## Configuration

Copy the example file to configure a real installation:

```bash
cp .env.example .env
```

`.env` is read from the repository directory. Set `OPENSTOCK_CONFIG_FILE` to load a different file. Keep credentials out of source control.

Add the Alpaca Trading API key and secret to `.env`. The account is resolved from those credentials; OpenStock does not need an account ID. `ALPACA_DATA_FEED` defaults to `sip` and falls back to `iex` if SIP access is denied.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_ALGO_API_KEY` | empty | Alpaca Trading API key for the USD account. |
| `ALPACA_ALGO_API_SECRET` | empty | Alpaca Trading API secret. Keep it in the ignored `.env` file. |
| `ALPACA_DATA_FEED` | `sip` | Preferred Alpaca stock-data feed. OpenStock falls back between SIP and IEX when feed access is denied. |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from @BotFather for alert delivery. |
| `TELEGRAM_CHAT_ID` | empty | Chat that receives alerts. |
| `DATABASE_PATH` | `database/openstock.db` | SQLite database path. Relative paths resolve from the checkout, not the current directory. |
| `PORT` | `3000` | Web server port. The server always binds to `127.0.0.1`. |
| `MONITOR_CONCURRENCY` | `3` | Maximum parallel quote requests per monitor cycle. |
| `MONITOR_CYCLE_DEADLINE_MS` | `45000` | Stop starting new stock work after this cycle budget. |
| `LIVE_PRICE_REFRESH_MS` | `1000` | Display-only Dashboard refresh cadence. Minimum and default 1000 ms. |
| `HISTORY_BACKFILL_DELAY_MS` | `1500` | Pause between chart-history backfill requests. |
| `ALGO_TRADER_ROOT` | `../algo-trader` | Data directory of the optional Algo Trader installation. |

### Price sources

Alpaca provides the quote and chart-history data. 

OpenStock requests SIP quotes when available and falls back to the free IEX version when the account cannot access SIP.

Prices, amounts, fees and performance values are displayed rounded to two decimals. Journal fields keep the precision you enter, up to six decimals; calculations and stored values use full precision.

The Dashboard polls `/api/live-prices` while visible. That endpoint is display-only: it never writes stock state, history, alert state or equity snapshots.

To copy an existing watchlist and its alert levels into the Alpaca USD watchlist, configure Alpaca credentials first, then run this once:

```bash
cd algo-trader
export ALGO_REPORT_TELEGRAM_TO=<chat id>
scripts/setup.sh
```

This installs the Python dependencies, the scheduled automations, and the Algo systemd user units. Nothing needs root. Start the services with `systemctl --user enable --now equity options`. Details are in [Algo setup](algo-trader/docs/setup.md) and [Scheduled automations](algo-trader/docs/automations.md).

### No account? Run the demo

```bash
npm run demo
```

It uses a separate database and ignores broker credentials. See [Demo mode](docs/demo-mode.md).

## What's inside

| Area | What it does | Read more |
|------|--------------|-----------|
| Dashboard | Alpaca USD quotes, alert levels, price-history charts | [Features](docs/features.md) |
| Monitoring | One-shot cycle checks BUY and SELL levels; alerts fire once per episode via Telegram | [Monitoring](docs/monitoring.md) |
| Trading journal | Alpaca balances, positions and fills; capital events kept apart from P/L | [Features](docs/features.md) |
| Analytics | P/L, risk, equity curve, drawdown, per-stock performance | [Performance data](docs/performance-data.md) |
| Manual limit orders | Limit orders from alert levels, behind a flag and a token; paper by default | [Features](docs/features.md#manual-limit-orders) |
| Algo Trader | Systematic equity and options research with paper execution | [algo-trader/](algo-trader/README.md) |
| Configuration | Environment variables, price sources, watchlist migration | [Configuration](docs/configuration.md) |
| API | Endpoints for the dashboard, journal, orders and health | [API](docs/api.md) |
| Architecture | Layout, server construction, tests | [Architecture](docs/architecture.md) |
| Deployment | systemd, Tailscale, Telegram credentials | [DEPLOY.md](DEPLOY.md) |
| History | Authors and AI-assisted commits | [Project history](docs/project-history.md) |

## Features at a glance

- **Alerts that fire once.** Each level queues one alert in a durable outbox. The outbox retries delivery and never sends twice.
- **A journal that never invents P/L.** Deposits and withdrawals are tracked separately from trading results.
- **Orders you confirm.** Every order shows a preview and needs your confirmation. Orders go to paper unless you change the endpoint.
- **Figures that admit missing data.** Metrics show `N/A` or `Insufficient data` until enough history exists.

Screenshots of every screen are in [Features](docs/features.md).

## License

ISC. See [LICENSE](LICENSE).
