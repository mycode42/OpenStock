



# OpenStock

OpenStock is a self-hosted dashboard for monitoring stock prices, keeping a trading journal, and tracking performance and risk. It polls executable EUR bid and ask prices, alerts on thresholds through Telegram, and records position lots, capital events and equity history in SQLite.

You can try it without any broker account. Demo mode runs the full interface on simulated quotes.

## Quick start

Requirements: Node.js 22 or newer, and a C/C++ toolchain with Python 3 for the `better-sqlite3` native module.

```bash
git clone https://github.com/mycode42/openstock.git
cd openstock
npm ci
npm run demo
```

Then open http://127.0.0.1:3000. To use another port, run `PORT=3001 npm run demo`.

### Demo mode

`npm run demo` starts OpenStock with simulated quotes and charts. It:

- ignores any local `.env` file and every broker credential variable, so it never contacts a broker;
- stores its data in `data/demo/openstock.db`, separate from any real database;
- shows a banner in the interface listing the setup still missing.

Demo mode is also the default whenever Trade Republic credentials or Algo Trader data are not configured. The application reports what is missing through `GET /api/demo-status`.

## Features

- **Dashboard:** current prices, alert state, quick stock actions, and local hover price-history charts.
<img width="1690" height="967" alt="Screenshot 2026-10-28 at 13 20 13" src="https://github.com/user-attachments/assets/2d5c7e6e-a755-483a-98ff-c81dae24ae43" />


- **Monitoring:** a one-shot monitor cycle (run by `npm run monitor` or a scheduler) that checks every configured stock against its BUY and SELL thresholds. Alerts fire once per episode and are retried through 
<img width="1679" height="956" alt="Screenshot 2026-10-28 at 13 22 08" src="https://github.com/user-attachments/assets/76143c54-63b3-4117-8aba-419667b77c11" />

a durable outbox until Telegram confirms delivery.
- **Trading journal:** record position lots, then edit or close them with frozen EUR conversion rates and fees. Adding a purchase to an open position merges it using quantity-weighted entry prices, with an audit trail.


- **Capital events:** initial capital, deposits, withdrawals and financing are tracked separately, so they never count as trading P/L.


- **Analytics:** P/L, risk, equity curve, drawdown, monthly and per-stock performance. Figures show `N/A` or `Insufficient data` until enough history exists.


- **Chart history:** persisted quote history with a background backfill queue; provider failures do not affect API responses.


- **Algo Trader pages:** research, universe and performance views for a separate Algo Trader installation, with its strategy settings editable from the UI.

<img width="1195" height="613" alt="Screenshot 2026-10-28 at 13 21 27" src="https://github.com/user-attachments/assets/ae0d0fc7-2303-4786-ab2f-91b396c69b73" />
<img width="1207" height="810" alt="Screenshot 2026-10-28 at 13 20 51" src="https://github.com/user-attachments/assets/b2bdc6a4-3884-49e0-890b-bb00282a4274" />


OpenStock does not place, change or cancel orders. Broker endpoints are read-only: they retrieve quotes, accounts, portfolio, orders and transactions.

<img width="1168" height="901" alt="Screenshot 2026-10-28 at 13 22 44" src="https://github.com/user-attachments/assets/0d99c880-96bb-4782-bb4b-71c1e6569a4e" />


## Configuration

Copy the example file to configure a real installation:

```bash
cp .env.example .env
```

`.env` is read from the repository directory. Set `OPENSTOCK_CONFIG_FILE` to load a different file. Keep credentials out of source control.

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADE_REPUBLIC_API_KEY` | empty | Key for the loopback Trade Republic UAPI sidecar. Leaving it empty, together with no SDK session, starts demo mode. |
| `TRADE_REPUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Loopback UAPI sidecar URL. |
| `TRADE_REPUBLIC_TIMEOUT_MS` | `30000` | Broker request timeout. 30000 is recommended for portfolio synchronization. |
| `TRADE_REPUBLIC_SDK_ENABLED` | `true` | Uses the read-only Handelsrepublik SDK as the primary quote source. Failed or disabled SDK requests fall back to UAPI. |
| `TRADE_REPUBLIC_SDK_SESSION_FILE` | empty | Private, server-side SDK session file. Required when the SDK source is enabled. Keep it outside the repository. |
| `TRADE_REPUBLIC_SDK_MODULE_PATH` | `../handelsrepublik/dist/index.js` | Compiled SDK module. Relative paths resolve from the checkout, so a sibling clone works without configuration. |
| `TRADE_REPUBLIC_SDK_PHONE_NUMBER` | empty | Used only when the saved SDK session is missing or rejected. The broker may ask for approval in its mobile app. |
| `TRADE_REPUBLIC_SDK_PIN` | empty | Used with the phone number for the same fallback login. |
| `TRADE_REPUBLIC_SDK_AUTH_TIMEOUT_MS` | `45000` | Time allowed for SDK login. |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from @BotFather for alert delivery. |
| `TELEGRAM_CHAT_ID` | empty | Chat that receives alerts. |
| `DATABASE_PATH` | `database/openstock.db` | SQLite database path. Relative paths resolve from the checkout, not the current directory. |
| `PORT` | `3000` | Web server port. The server always binds to `127.0.0.1`. |
| `MONITOR_CONCURRENCY` | `3` | Maximum parallel quote requests per monitor cycle. |
| `MONITOR_CYCLE_DEADLINE_MS` | `45000` | Stop starting new stock work after this cycle budget. |
| `LIVE_PRICE_REFRESH_MS` | `1000` | Display-only Dashboard refresh cadence. Minimum and default 1000 ms. |
| `HISTORY_BACKFILL_DELAY_MS` | `1500` | Pause between chart-history backfill requests. |
| `ALGO_TRADER_ROOT` | `../algo-trader` | Data directory of the optional Algo Trader installation. |

Demo mode is automatic while either a Trade Republic credential or the Algo Trader data is missing.

### Price sources

Quotes come from the Handelsrepublik SDK first and the loopback Trade Republic UAPI sidecar second. Every monitored instrument needs a verified Trade Republic EUR mapping. If both providers fail for a symbol, OpenStock records a per-stock failure and continues the cycle.

BUY alerts compare against the executable ask; SELL alerts compare against the executable bid.

Prices, amounts, fees and performance values are displayed rounded to two decimals. Journal fields keep the precision you enter, up to six decimals; calculations and stored values use full precision.

The Dashboard polls `/api/live-prices` once per second while visible. That endpoint is display-only: it never writes stock state, history, alert state or equity snapshots.

## Monitoring behavior

`scripts/monitor.js` runs one cycle per invocation:

1. Reads enabled stocks from the database.
2. Fetches an executable EUR quote for each stock, SDK first, UAPI second.
3. Stores price and quote-side data in SQLite.
4. Evaluates buy and sell thresholds against ask and bid.
5. Sends Telegram alerts on threshold crossing.
6. Writes a durable alert-outbox record, and retires an alert level only after confirmed delivery.
7. Stores a local price observation and, when capital is configured, an equity snapshot.

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

Position summaries and SELL eligibility use the total quantity of open journal trades. Without an open position, SELL levels stay configured but do not fire. Archiving a stock preserves its history; restoring it re-enables non-retired alert levels.

### Chart history backfill

The server queues missing history for active, non-archived stocks with verified Trade Republic EUR mappings. Incomplete ranges stay eligible for retry. Run the queue manually with:

```bash
npm run backfill-history
# Optional: HISTORY_BACKFILL_DELAY_MS=2000 npm run backfill-history
```

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
| `/api/live-prices` | GET | Display-only executable prices |
| `/api/stocks` | GET, POST | List or add stocks |
| `/api/stocks/archived` | GET | Archived stocks kept for journal and history |
| `/api/stocks/:id` | GET, PUT, DELETE | Read, update or remove a stock |
| `/api/stocks/:id/restore` | POST | Restore an archived stock |
| `/api/stocks/:id/history` | GET | Chart history for a stock |
| `/api/stocks/:id/alert-levels` | GET, POST | List or add alert levels |
| `/api/alert-levels/:id` | PUT, DELETE | Update or remove an alert level |
| `/api/alert-levels/:id/rearm` | POST | Rearm a retired alert level |
| `/api/trades` | GET, POST | List or record position lots |
| `/api/trades/:id` | PUT | Edit a trade |
| `/api/trades/:id/close` | POST | Close an open trade |
| `/api/capital-events` | GET, POST | List or record capital movements |
| `/api/analytics/dashboard` | GET | Performance, risk and equity-curve data |
| `/api/broker/trade-republic/health` | GET | Sanitized broker connectivity status |
| `/api/broker/trade-republic/mappings` | GET, POST | List or verify instrument mappings |
| `/api/broker/trade-republic/quote/:stockId` | GET | Fetch and persist a sanitized executable quote |
| `/api/broker/trade-republic/sync` | POST | Synchronize account, portfolio, order and transaction data |

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

The commit history contains entries under several author names: `mycode42`, `Andreas Gerster`, `Andreas`, and `Stocky`. `Stocky` is the author identity of an OpenClaw AI agent used to develop this project, so those commits are AI-authored. Three further commits carry a `Co-Authored-By: Claude Haiku 5.5` trailer, marking AI-assisted work. List them with `git log --grep "Co-Authored-By"`.

## Deployment

The single-host setup with systemd, Tailscale and the OpenClaw Telegram credentials is documented in [DEPLOY.md](DEPLOY.md).

## License

ISC. See [LICENSE](LICENSE).
