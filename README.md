# OpenStock

OpenStock is a self-hosted stock dashboard and trading journal built around Alpaca accounts and market data in USD. It uses SIP quotes when available and falls back to IEX when the account cannot access SIP.

It is also the frontend for the algo-trader repository: https://github.com/mycode42/algo-trader.git

<img width="800" alt="Screenshot 2026-10-28 at 13 40 08" src="https://github.com/user-attachments/assets/51ec9bbc-aff4-44e4-ae12-9d2480047462" />


You can try it without any broker account. Demo mode runs the full interface on simulated quotes.

## Quick start

Requirements: Node.js 22 or newer, and a C/C++ toolchain with Python 3 for the `better-sqlite3` native module.

```bash
git clone https://github.com/mycode42/OpenStock.git
cd openstock
npm ci
cp .env.example .env
# Add ALPACA_LIVE_API_KEY and ALPACA_LIVE_API_SECRET to .env
npm start
```

Then open http://127.0.0.1:3000. The `.env` file is ignored by Git; never commit live credentials. To use another port, set `PORT=3001` before `npm start`.

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
| `ALPACA_LIVE_API_KEY` | empty | Alpaca Trading API key for the USD account. |
| `ALPACA_LIVE_API_SECRET` | empty | Alpaca Trading API secret. Keep it in the ignored `.env` file. |
| `ALPACA_DATA_FEED` | `sip` | Preferred Alpaca stock-data feed. OpenStock falls back between SIP and IEX when feed access is denied. |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from @BotFather for alert delivery. |
| `TELEGRAM_CHAT_ID` | empty | Chat that receives alerts. |
| `DATABASE_PATH` | `database/openstock.db` | SQLite database path. Relative paths resolve from the checkout, not the current directory. |
| `PORT` | `3000` | Web server port. The server always binds to `127.0.0.1`. |
| `MONITOR_CONCURRENCY` | `3` | Maximum parallel quote requests per monitor cycle. |
| `MONITOR_CYCLE_DEADLINE_MS` | `45000` | Stop starting new stock work after this cycle budget. |
| `LIVE_PRICE_REFRESH_MS` | `1000` | Display-only Dashboard refresh cadence. Minimum and default 1000 ms. |
| `HISTORY_BACKFILL_DELAY_MS` | `1500` | Pause between chart-history backfill requests. |
| `ALGO_TRADER_ROOT` | `./algo-trader` (set to `/opt/algo-trader` until cutover) | Data directory of the optional Algo Trader installation. |

### Price sources

Alpaca provides the quote and chart-history data. OpenStock requests the configured SIP or IEX feed and labels the feed returned by Alpaca.

BUY alerts compare against the executable ask; SELL alerts compare against the executable bid.

Prices, amounts, fees and performance values are displayed rounded to two decimals. Journal fields keep the precision you enter, up to six decimals; calculations and stored values use full precision.

The Dashboard polls `/api/live-prices` while visible. That endpoint is display-only: it never writes stock state, history, alert state or equity snapshots.

To copy an existing watchlist and its alert levels into the Alpaca USD watchlist, configure Alpaca credentials first, then run this once:

```bash
node scripts/migrate-alpaca-profile.js
```

The script verifies symbols with Alpaca and converts legacy EUR price levels using the current EUR/USD rate. It refuses to overwrite an already-populated Alpaca watchlist.

## Monitoring behavior

`scripts/monitor.js` runs one cycle per invocation:

1. Reads enabled tickers from the Alpaca watchlist.
2. Fetches executable ask and bid quotes from Alpaca.
3. Evaluates BUY against ask and SELL against bid; SELL alerts wait for an open Alpaca position.
4. Sends Telegram alerts through the durable alert outbox and retires levels after confirmed delivery.
5. Fetches chart history directly from Alpaca.

The cycle uses bounded concurrency, a deadline, per-stock failure isolation, validated responses, explicit exit codes, and a single-run lock.

```bash
npm run monitor            # run one cycle
npm run monitor -- --help  # print usage only
```

### Alert levels

- Each alert level is independently one-shot. It retires only after Telegram confirms delivery.
- Rearm a retired level explicitly through the alert-level API or the UI.
- `trigger_state` and the stock-level threshold columns are legacy compatibility data. `alert_levels` and open journal trades are authoritative.

### Journal positions

Position summaries and SELL eligibility come from the authenticated Alpaca account. Without an open position, SELL levels stay configured but do not fire.

### Chart history backfill

Alpaca chart ranges use Alpaca bars directly.

## Performance data model

- `trades` records one position lot. An open trade has no close date; a completed trade keeps entry and exit prices, fees, currency and EUR conversion rates.
- `capital_events` never count as P/L.
- `equity_snapshots` are captured by the monitor and by state-changing journal operations, so equity curves and drawdown do not depend on today's prices.
- Analytics GET requests are read-only.
- Annualized return and estimated yearly profit need at least 30 calendar days since initial capital. Volatility and Sharpe need 30 equity observations.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check and version |
| `/api/demo-status` | GET | Whether demo mode is active and what setup is missing |
| `/api/dashboard` | GET | Stock dashboard data |
| `/api/live-prices` | GET | Display-only Alpaca quotes |
| `/api/alpaca/journal` | GET | Read-only Alpaca account, positions, and fills |
| `/api/alpaca/alert-levels/:id/rearm` | POST | Rearm a copied Alpaca alert level |
| `/api/stocks/:id/history?broker=alpaca` | GET | Alpaca chart history |

Mutating requests must come from the same origin as the page.

## Architecture

```
server.js                 HTTP app setup (loopback only) and demo-mode detection
server/routes/            Route handlers
server/algo-trader/       Algo Trader data readers, settings, and payload builders
server/demo-data.js       Simulated data for demo mode
lib/                      Quote service, monitor, analytics, history backfill, broker clients
database/                 SQLite access; migrations/ is append-only once applied
scripts/                  monitor, history backfill, equity snapshot and rebuild tools
www/                      Browser UI as native ES modules; www/app.js is the bootstrap
test/                     node:test suites, one per module
```

Server construction is separate from listening: `createApp()` in `server.js` builds the app, so route tests run against temporary SQLite files and mocked providers. Database startup applies ordered, transactional migrations from `database/migrations/` and records them in `schema_migrations`.

## Development

```bash
npm test          # full test suite
npm run check     # syntax check and PWA asset consistency
npm run audit:prod
```

Tests use isolated temporary databases and mocked provider calls. They never open the live database.

## Project history

The commit history contains entries under several author names: `mycode42`, `Andreas Gerster`, `Andreas`, and `Stocky`. `Stocky` is the author identity of an OpenClaw AI agent used to develop this project, so those commits are AI-authored. Five commits on `main` carry a `Co-Authored-By: Claude Haiku 5.5` trailer, marking AI-assisted work; two of them are from the v2.0.1 release. List them with `git log --grep "Co-Authored-By"`.

## Deployment

The single-host setup with systemd, Tailscale and the OpenClaw Telegram credentials is documented in [DEPLOY.md](DEPLOY.md).

## License

ISC. See [LICENSE](LICENSE).
