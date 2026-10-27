# OpenStock - Stock Monitoring, Trading Journal & Performance Dashboard

A private workspace for monitoring stock prices, recording position lots, and calculating performance and risk KPIs. Quotes use the read-only Handelsrepublik SDK first and the authenticated loopback Trade Republic UAPI sidecar as backup. Both return EUR executable bid/ask values.

Every monitored instrument needs a verified Trade Republic EUR mapping. If both quote providers fail, OpenStock records a per-stock failure and continues the cycle.

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
│  - Sends Telegram       │──────┬──────│ Handelsrepublik SDK      │
│  - Durable alert outbox │      └──────│ Trade Republic UAPI     │
└─────────────────────────┘             └─────────────────────────┘
```

## Prerequisites

- Node.js 22 or higher
- Python 3, a C/C++ compiler toolchain, and `make` for native Node modules (for example, `build-essential` on Debian/Ubuntu)
- SQLite 3.x (with CLI for debugging)
- Telegram Bot token
- Tailscale (pre-configured)
- Trade Republic account access is provisioned outside the dashboard; no market-data API key is required

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
| `MONITOR_CONCURRENCY` | Maximum parallel quote requests per monitor cycle (default: 3) |
| `MONITOR_CYCLE_DEADLINE_MS` | Stop starting new stock work after this cycle budget (default: 45000) |
| `TELEGRAM_BOT_USERNAME` | Your bot's username (e.g., `OpenStock_TelegramBot`) |
| `TRADE_REPUBLIC_BASE_URL` | Loopback Trade Republic UAPI URL (default: `http://127.0.0.1:8000`) |
| `TRADE_REPUBLIC_TIMEOUT_MS` | Broker request timeout; 30000 is recommended for portfolio synchronization |
| `TRADE_REPUBLIC_SDK_ENABLED` | Uses the read-only Handelsrepublik SDK as the primary quote source; default `true` |
| `TRADE_REPUBLIC_SDK_SESSION_FILE` | Private, server-side SDK session path; required when the SDK source is enabled |
| `TRADE_REPUBLIC_SDK_MODULE_PATH` | Local compiled SDK module path (default `/opt/handelsrepublik/dist/index.js`) |
| `LIVE_PRICE_REFRESH_MS` | Display-only Dashboard/Configure refresh cadence; minimum/default 1000 ms |

### Trade Republic integration

The read-only Handelsrepublik SDK provides the primary executable EUR bid/ask. If it is disabled, unconfigured, or unavailable, OpenStock tries the loopback Trade Republic UAPI sidecar. The SDK adapter only reads executable quotes; broker account endpoints remain separate from market-data selection.

Prices, amounts, fees, and performance values are displayed rounded to two decimal places. Journal trade entry, close, and edit fields retain entered precision up to six decimal places; calculations and stored values use that full precision.

The Dashboard and Configure page poll `/api/live-prices` once per second while visible. Requests share the same SDK → UAPI selection path as the monitor, coalesce, and cache briefly. Live display requests never write stock state, history, alert state, or equity snapshots. Monitor and explicit refresh observations are stored in SQLite; BUY/SELL alerts use the executable ask/bid respectively.

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

The server loads `.env` from the repository directory by default. Set
`OPENSTOCK_CONFIG_FILE` only when you want to load a different environment
file. The default database path in `.env.example` is relative to the checkout,
so the setup also works locally at `http://localhost:3000`.

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

The `scripts/monitor.js` process runs every minute via a systemd timer. For each verified EUR mapping it tries the Handelsrepublik SDK first and the loopback UAPI sidecar second. If both fail, that stock fails for the cycle without stopping other symbols. The monitor:

1. Reads enabled stocks from database
2. Fetches an executable EUR quote through SDK → UAPI
3. Stores price and quote-side data in SQLite
4. Evaluates buy/sell thresholds against ask/bid
5. Sends Telegram alerts on threshold crossing
6. Creates a durable alert-outbox record, then retires a level only after confirmed delivery
7. Stores a local price observation and, when capital is configured, an equity snapshot
8. Uses bounded concurrency, a cycle deadline, per-stock failure isolation, validated responses, explicit exit codes, and a single-run lock

Run one monitoring cycle manually as the `openclaw` user with `npm run monitor` (`npm run monitor -- --help` only prints usage). Use the tracked systemd override in `ops/systemd/` when deploying monitor configuration changes.

### Trigger Debouncing

- Each alert level is independently one-shot and is retired only after Telegram confirms delivery.
- Rearm a retired level explicitly through the alert-level API/UI.
- `trigger_state` and the stock-level threshold columns are legacy compatibility data; `alert_levels` and open Journal trades are authoritative.

### Journal positions

Position summaries and SELL eligibility use the total quantity in open Journal
trades. Without an open position, SELL levels remain configured but do not fire.
Adding another open purchase merges it into the oldest open trade using
quantity-weighted entry prices while preserving the entered precision and a full
source snapshot in `trade_merge_events`. Closed trades never merge. Archiving a
stock preserves its history; restoring it re-enables non-retired alert levels,
while retired levels need explicit rearming.

### Historical chart backfill

The server queues missing history for active, non-archived stocks with verified
Trade Republic EUR mappings. It uses the sidecar multi-range history endpoint;
incomplete ranges remain eligible for retry. Stocks without a verified mapping
remain visible but have no quote/history until mapped. Backfill runs in the
background so provider failures do not affect API responses or server
availability.

The same prepared, idempotent queue can be run manually:

```bash
npm run backfill-history
# Optional: HISTORY_BACKFILL_DELAY_MS=2000 npm run backfill-history
```

Charts use persisted quote and Trade Republic history rows. Tests use mocked
provider calls and temporary SQLite databases.

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
```

## Troubleshooting

### Monitoring not running

```bash
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
journalctl -u openstock-monitor.service -n 50
```

### Prices not updating

Check `/api/broker/trade-republic/health`, then verify the SDK session and UAPI sidecar are reachable from the OpenStock service.

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
| `/api/broker/trade-republic/health` | GET | Sanitized broker connectivity status |
| `/api/broker/trade-republic/mappings` | GET/POST | List or broker-verify stock instrument mappings |
| `/api/broker/trade-republic/sync` | POST | Synchronize normalized account, portfolio, order, and transaction data |
| `/api/broker/trade-republic/quote/:stockId` | GET | Fetch and persist a sanitized executable quote |
| `/health` | GET | Health check |

## Architecture and migrations

The browser uses native ES modules: `www/app.js` is the bootstrap, while
`www/modules/` contains API access, state, chart, dashboard, journal, and
configuration behavior. The app supports pointer, touch, focus, and keyboard
chart interaction.

Server construction is separated from listening with `createApp()` exported by
`server.js`; route handlers live in `server/routes/`. This allows
route/integration tests to use temporary SQLite files and mocked providers.
Production startup remains `node server.js`.

Database startup applies ordered, transactional migrations from
`database/migrations/` and records versions in `schema_migrations`. Existing
databases are adopted in place: archived stock state, trade dividends, and
older journal entries are preserved. Database operations are grouped under
`database/domains/`, with `database/db.js` composing the public database class.
Tests use isolated temporary databases and do not open the live database.

Run verification with:

```bash
npm test
for f in $(find . -path './node_modules' -prune -o -name '*.js' -print); do node --check "$f"; done
git diff --check
```

## License

MIT - Built for the OpenClaw ecosystem
