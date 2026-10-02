# OpenStock - Stock Monitoring, Trading Journal & Performance Dashboard

A private trading workspace for monitoring stock prices, recording position lots, and calculating auditable performance and risk KPIs. For verified instruments, the authenticated loopback Trade Republic UAPI sidecar is the primary EUR quote source; pytr is the display-only fallback, followed by Yahoo USD quotes converted to EUR.

> **Note:** Verified EUR instruments use Trade Republic quotes; **Yahoo Finance** remains the free fallback. No stock-price API key is required.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Tailscale Access                      │
│                    (https://openstock.tailbff3c4.ts.net)  │
└────────────────────────────────────────┬────────────────┘
                                         │
┌────────────────────────────────────────▼────────────────┐
│                 Web Application                        │
│                 Port 3000 (127.0.0.1)                  │
│  ┌───────────┐  ┌───────────┐  ┌──────────────┐       │
│  │ Dashboard │  │ Journal   │  │ Config       │       │
│  └───────────┘  └───────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────┘
                                 │
                                 │
┌────────────────────────────────▼─────────────────────────┐
│                 SQLite Database (openstock.db)           │
│  ┌───────────┐  ┌───────────┐  ┌──────────────┐        │
│  │ stocks    │  │ trades    │  │ capital_     │        │
│  │           │  │           │  │ events       │        │
│  └───────────┘  └───────────┘  └──────────────┘        │
└─────────────────────────────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  │                  ▼
┌─────────────────────────┐      │      ┌─────────────────────────┐
│   Node.js Monitor       │      │      │  Exchange Rate Cache    │
│      (monitor.js)       │      │      │   (JSON cache)          │
│  - Runs every minute    │      │      │                         │
│  - Fetches prices       │      │      └─────────────────────────┘
│  - Uses bounded workers │      │
│  - Evaluates triggers   │      │      ┌─────────────────────────┐
│  - Sends Telegram       │──────┘      │  Yahoo Finance          │
│  - Durable alert outbox │             │  (fallback prices/FX)   │
└─────────────────────────┘             └─────────────────────────┘
```

## Prerequisites

- Node.js v22.x or higher
- SQLite 3.x (with CLI for debugging)
- Telegram Bot token
- Tailscale (pre-configured)
- **No stock price API key needed** — Trade Republic uses the local sidecar; Yahoo Finance is free fallback

## Configuration

### Environment Variables

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_PATH` | Path to SQLite database |
| `PORT` | Server port (default: 3000) |
| `EXCHANGE_RATE_CACHE_SECONDS` | How long to cache exchange rates (default: 3600) |
| `MONITOR_CONCURRENCY` | Maximum parallel quote requests per monitor cycle (default: 3) |
| `MONITOR_CYCLE_DEADLINE_MS` | Stop starting new stock work after this cycle budget (default: 45000) |
| `TELEGRAM_BOT_USERNAME` | Your bot's username (e.g., `OpenStock_TelegramBot`) |
| `TRADE_REPUBLIC_BASE_URL` | Loopback Trade Republic UAPI URL (default: `http://127.0.0.1:8000`) |
| `TRADE_REPUBLIC_TIMEOUT_MS` | Broker request timeout; 30000 is recommended for portfolio synchronization |
| `TRADE_REPUBLIC_SDK_ENABLED` | Enables the read-only Handelsrepublik SDK as the primary executable quote source; default `false` |
| `TRADE_REPUBLIC_SDK_SESSION_FILE` | Private, server-side SDK session path; required when the SDK source is enabled |
| `TRADE_REPUBLIC_SDK_MODULE_PATH` | Local compiled SDK module path (default `/opt/handelsrepublik/dist/index.js`) |
| `PYTR_COMMAND` | pytr executable used as a display-only fallback price source (default `/opt/pytr-venv/bin/pytr`) |
| `PYTR_TIMEOUT_MS` | Bounded pytr batch timeout (default 8000) |
| `LIVE_PRICE_REFRESH_MS` | Display-only Dashboard/Configure refresh cadence; minimum/default 1000 ms |
| `TRADE_REPUBLIC_LIVE_ENABLED` | Environment-only live-order gate; default `false` |
| `TRADE_REPUBLIC_DRY_RUN` | Environment-only dry-run gate; default `true` |
| `TRADE_REPUBLIC_LIVE_ACK` | Environment-only acknowledgement gate; default `false` |
| `TRADE_REPUBLIC_KILL_SWITCH` | Immediate order kill switch; default `true` |
| `TRADE_REPUBLIC_MAX_ORDER_EUR` | Hard maximum value per manual order; default `0` |

### Trade Republic integration

OpenStock uses pytr only through its `rates` command for a batched, display-only current-price feed. pytr reads its own authentication material from the `openclaw` service account's `~/.pytr` directory; OpenStock does not store phone numbers, PINs, cookies, or pytr credentials. Trade Republic sidecar authentication is provisioned server-side and is not a dashboard or user-facing configuration setting.

The optional Handelsrepublik SDK is a read-only primary quote feed. When explicitly enabled with a private session file, it requests executable buy/sell order prices before the loopback sidecar. The fallback order is **SDK → sidecar → pytr → Yahoo Finance**. The adapter exposes no account, portfolio, order, or mutation methods.

The Dashboard and Configure page each poll `/api/live-prices` once per second only while that page is visible. Requests are coalesced and cached for one second, and never write stock state, history, alert state, or equity snapshots. Persistent price observations and charts continue to use the minute monitor. pytr exposes last/ask but not an executable bid, so the monitor retains UAPI bid/ask quotes (with Yahoo fallback) for configured BUY/SELL alert semantics.

Manual order submission is fail-closed. It additionally requires a verified mapping, fresh executable quote, synchronized cash or position data, selected account, typed confirmation, idempotency key, and all environment gates above. A submitted broker order never creates a Journal execution unless a later broker reconciliation proves it filled.

### Telegram Setup

1. Chat with [@BotFather](https://t.me/BotFather) to create a bot
2. Get your bot token
3. Add your bot as a member to your Telegram channel/group
4. Add your Telegram user ID to `telegram-default-allowFrom.json` in OpenClaw credentials

## Installation

```bash
# Clone the repository
git clone git@github.com:mycode42/openstock.git
cd openstock

# Install dependencies
npm install

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Initialize database (automatic on first run)
npm start
```

## Running

### Start the web application

```bash
npm start
# Or directly:
node server.js
```

### Start the monitoring timer

```bash
sudo systemctl enable --now openstock-monitor.timer
```

### Check service status

```bash
sudo systemctl status openstock.service
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
```

## Viewing Logs

```bash
# Web server logs
journalctl -u openstock.service -f

# Monitor logs
tail -f /opt/openstock/memory/monitor.log

# Error logs
tail -f /opt/openstock/memory/monitor-error.log
```

## Database Location

```bash
# View database location
cat .env | grep DATABASE_PATH

# Query directly
sqlite3 /opt/openstock/database/openstock.db "SELECT * FROM stocks;"
sqlite3 /opt/openstock/database/openstock.db ".tables"
```

## Monitoring Behavior

The `scripts/monitor.js` process runs every minute via a systemd timer. It uses Handelsrepublik SDK EUR order quotes for stocks with verified EUR mappings, then the loopback Trade Republic sidecar, pytr, and finally Yahoo Finance plus cached USD→EUR conversion when direct pricing is unavailable. If both the primary sources and pytr fail, it creates a rate-limited pytr v2-login recovery request; the separate recovery service starts `pytr login --v2` and waits for confirmation in the Trade Republic app while the monitor continues on Yahoo for that cycle. The monitor:

1. Reads enabled stocks from database
2. Fetches a verified Trade Republic EUR quote when configured, otherwise Yahoo Finance
3. Gets USD→EUR exchange rate for Yahoo quotes (cached for 1 hour)
4. Calculates EUR price only for Yahoo fallback quotes
5. Stores price data in database
6. Evaluates buy/sell thresholds
7. Sends Telegram alert on threshold crossing
8. Creates a durable alert-outbox record, sends Telegram, then atomically records/retire the delivered alert level
9. Stores a local price observation and, when capital is configured, an equity snapshot
10. Uses bounded concurrency, a per-cycle deadline, prepared SQLite statements, per-stock failure isolation, validated responses, explicit exit codes, and a single-run lock

Run one monitoring cycle manually as the `openclaw` user with `npm run monitor` (`npm run monitor -- --help` only prints usage). Use the tracked systemd override in `ops/systemd/` when deploying monitor configuration changes.

### PyTR v2-login recovery

Install the tracked recovery unit files after deploying the application code:

```bash
sudo install -m 0644 ops/systemd/openstock-pytr-login-recovery.service /etc/systemd/system/
sudo install -m 0644 ops/systemd/openstock-pytr-login-recovery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openstock-pytr-login-recovery.timer
```

The monitor writes only a private request marker under `/opt/openstock/memory/`; it never supplies or logs a phone number, PIN, cookies, or confirmation token. A failed or unconfirmed login leaves the Yahoo fallback in place. `PYTR_LOGIN_RECOVERY_COOLDOWN_SECONDS` defaults to 900 seconds to avoid repeated app prompts during an outage.

### Trigger Debouncing

- Each alert level is independently one-shot and is retired only after Telegram confirms delivery.
- Rearm a retired level explicitly through the alert-level API/UI.
- `trigger_state` and the stock-level threshold columns are legacy compatibility data; `alert_levels` and open Journal trades are authoritative.

### Historical chart backfill

The server queues missing history for active, non-archived stocks at startup and
after a stock is added or restored. Verified Trade Republic mappings use tracked
multi-range broker history; incomplete ranges remain eligible for retry. Other
stocks use one free Yahoo Finance chart request per stock for up to five years of
daily closes. Existing monitor/manual
history is preserved; a stock with any `yahoo_backfill` row is not requested
again. Backfill runs in the background, so Yahoo failures are logged without
affecting API responses or server availability.

The same prepared, idempotent queue can be run manually:

```bash
npm run backfill-history
# Optional: HISTORY_BACKFILL_DELAY_MS=2000 HISTORY_RANGE=5y npm run backfill-history
```

No Yahoo request is made by chart rendering. Tests use mocked HTTP and temporary
SQLite databases.

## Web Interface

Access the web app:

- **Local:** `http://localhost:3000`
- **Tailscale:** `https://openstock.tailbff3c4.ts.net` (configure Tailscale Serve)

### Features

- **Dashboard:** The original stock-monitoring overview with current prices, trigger state, quick stock actions, and local hover price-history charts.
- **Trading Journal:** Record position lots, then explicitly edit or close them with frozen EUR conversion rates and fees. Its analytics section contains capital, P/L, risk, equity curve, monthly, and per-stock performance data.
- **Capital events:** Record initial capital, deposits, withdrawals, and financing separately from trading performance.
- **Configure Stocks:** Add stocks and manage monitoring thresholds.

### Performance data model

`trades` records one position lot. An open trade has no close date; a completed trade retains entry/exit prices, fees, currency and EUR conversion rates. `capital_events` never count as P/L. `equity_snapshots` are captured by the monitor and state-changing Journal operations so equity curves and drawdown do not have to be reconstructed from today's prices. Analytics GET requests are read-only.

The dashboard deliberately shows `N/A` or `Insufficient data` until the relevant history exists. Annualized return and estimated yearly profit require at least 30 calendar days since initial capital; volatility and Sharpe require 30 equity observations.

## Updating the Application

```bash
# Pull changes
git pull origin main

# Install updates
npm install

# Restart service
sudo systemctl restart openstock.service
```

## Backup Considerations

```bash
# Backup database
mkdir -p /var/backups/openstock
cp /opt/openstock/database/openstock.db /var/backups/openstock/openstock.db.$(date +%F-%H%M%S)

# Include memory for audit trail
tar -czf openstock-backup-$(date +%F).tar.gz \
    database/openstock.db \
    memory/*.log \
    memory/exchange_rate_cache.json
```

## Troubleshooting

### Monitoring not running

```bash
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
journalctl -u openstock-monitor.service -n 50
```

### Prices not updating

```bash
curl -s -A "Mozilla/5.0" "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1m" | head -c 200
```

### Telegram alerts not sending

```bash
# Check bot token
curl "https://api.telegram.org/botYOUR_TOKEN/getMe"

# Check chat ID
cat /home/openclaw/.openclaw/credentials/telegram-default-allowFrom.json

# Check monitor error log
cat /opt/openstock/memory/monitor-error.log
```

### Database locked or corrupted

```bash
# Check integrity
sqlite3 /opt/openstock/database/openstock.db "PRAGMA integrity_check;"

# If issues, restart the web server to release locks
sudo systemctl restart openstock.service
```

### Web interface not accessible

```bash
# Check if server is running
ss -tlnp | grep 3000

# Check process
ps aux | grep "node server.js"

# Check logs
journalctl -u openstock.service -n 50 --no-pager
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stocks` | GET | List all stocks |
| `/api/stocks/:id` | GET | Get specific stock |
| `/api/stocks` | POST | Add new stock |
| `/api/stocks/:id` | PUT | Update stock |
| `/api/stocks/:id` | DELETE | Remove stock |
| `/api/trades` | GET/POST | List or record position lots |
| `/api/trades/:id` | PUT | Explicitly edit a trade |
| `/api/trades/:id/close` | POST | Close an open trade |
| `/api/capital-events` | GET/POST | List or record capital movements |
| `/api/analytics/dashboard` | GET | Performance, risk and equity-curve data |
| `/api/dashboard` | GET | Backward-compatible stock dashboard data |
| `/api/stocks/archived` | GET | Archived stocks retained for journal/history management |
| `/api/stocks/:id/restore` | POST | Restore an archived stock by identity |
| `/api/broker/trade-republic/health` | GET | Sanitized broker connectivity and live-gate status |
| `/api/broker/trade-republic/mappings` | GET/POST | List or broker-verify stock instrument mappings |
| `/api/broker/trade-republic/sync` | POST | Synchronize normalized account, portfolio, order, and transaction data |
| `/api/broker/trade-republic/quote/:stockId` | GET | Fetch and persist a sanitized executable quote |
| `/api/broker/trade-republic/order-preview` | POST | Fetch quote and fee preview without placing an order |
| `/api/broker/trade-republic/orders` | POST | Fail-closed manual order submission |
| `/health` | GET | Health check |

## Architecture and migrations

The browser uses native ES modules: `www/app.js` is the bootstrap, while
`www/modules/` contains API access, state, chart, dashboard, journal, and
configuration helpers. The app keeps the existing pointer, touch, focus, and
keyboard chart interaction and exposes a small compatibility bridge for legacy
inline markup.

Server construction is separated from listening with `createApp()` exported by
`server.js`. This allows route/integration tests to use temporary SQLite files
and mocked quote/backfill fetches. Production startup remains `node server.js`
and retains the systemd listener behavior.

Database startup applies ordered, transactional migrations from
`database/migrations/` and records versions in `schema_migrations`. Existing
databases are adopted in place: archived stock state, trade dividends, and
legacy journal entries are preserved. Tests never use
`database/openstock.db`.

Run verification with:

```bash
npm test
for f in $(find . -path './node_modules' -prune -o -name '*.js' -print); do node --check "$f"; done
git diff --check
```

## License

MIT - Built for the OpenClaw ecosystem
# Alert levels and Journal positions

OpenStock stores independent one-shot BUY and SELL alert levels in migration 004. Existing stock thresholds are copied once into one BUY and one SELL level; the legacy threshold columns remain for compatibility, but monitoring uses `alert_levels`. Levels can be added, deleted, disabled, and rearmed through the API or Configure view. A level is marked triggered/retired only after Telegram confirms delivery. Alerts are notifications only and never execute trades.

Position summaries and SELL eligibility are derived exclusively from open Journal lots (`position.source: journal`, with summed `position.quantity`). Manual ownership updates are rejected. Without an open Journal position, SELL levels stay configured but are suppressed until a position is recorded.

Recording another open purchase for a stock that already has an open Journal trade consolidates it into the oldest open trade. Quantity and fees are summed, EUR and native entry prices use quantity-weighted averages, and the earliest opening time is retained. Every absorbed trade is preserved verbatim in `trade_merge_events` before its duplicate open row is removed. Closed historical trades never merge.

Archiving disables alert levels while preserving their history. Explicit restore re-enables non-retired levels; retired levels require explicit rearm. Migration and verification must be run against a copied database, never the production file.
