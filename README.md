# OpenStock - Stock Monitoring, Trading Journal & Performance Dashboard

A private trading workspace for monitoring stock prices, recording position lots, and calculating auditable performance and risk KPIs. It converts USD to EUR and sends Telegram alerts when configured buy/sell thresholds are reached.

> **Note:** Stock prices are fetched from **Yahoo Finance** — no API key required. The Twelve Data API key in `.env` is optional and kept for legacy compatibility only.

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
│  - Converts USD→EUR     │      │
│  - Evaluates triggers   │      │      ┌─────────────────────────┐
│  - Sends Telegram       │──────┘      │  Yahoo Finance          │
│  - Logs to alerts_log   │             │  (prices & exchange)    │
└─────────────────────────┘             └─────────────────────────┘
```

## Prerequisites

- Node.js v22.x or higher
- SQLite 3.x (with CLI for debugging)
- Telegram Bot token
- Tailscale (pre-configured)
- **No stock price API key needed** — Yahoo Finance is used for free

## Configuration

### Environment Variables

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `TWELVE_DATA_API_KEY` | **Optional** — legacy only; prices come from Yahoo Finance |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_PATH` | Path to SQLite database |
| `PORT` | Server port (default: 3000) |
| `EXCHANGE_RATE_CACHE_SECONDS` | How long to cache exchange rates (default: 3600) |
| `TELEGRAM_BOT_USERNAME` | Your bot's username (e.g., `OpenStock_TelegramBot`) |

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

The `scripts/monitor.js` process runs every minute via a systemd timer. The former `monitor.sh` remains available only as a rollback target. The Node monitor:

1. Reads enabled stocks from database
2. Fetches current USD price from Yahoo Finance
3. Gets USD→EUR exchange rate (cached for 1 hour)
4. Calculates EUR price
5. Stores price data in database
6. Evaluates buy/sell thresholds
7. Sends Telegram alert on threshold crossing
8. Logs alert to `alerts_log` table (prevents duplicates)
9. Updates trigger state in stocks table
10. Stores a local price observation and, when capital is configured, an equity snapshot
11. Uses prepared SQLite statements, per-stock failure isolation, validated responses, explicit exit codes, and a single-run lock

Run one **live** monitoring cycle manually as the `openclaw` user with `npm run monitor` (`npm run monitor -- --help` only prints usage). The one-time systemd cutover and rollback procedure is documented in `docs/monitor-systemd-cutover.md`.

### Trigger Debouncing

- Alerts only fire when price **crosses into** trigger zone
- System tracks `trigger_state` in database: `NONE`, `BUY_TRIGGERED`, `SELL_TRIGGERED`
- After alert fires, price must exit zone before new alert possible

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

`trades` records one position lot. An open trade has no close date; a completed trade retains entry/exit prices, fees, currency and EUR conversion rates. `capital_events` never count as P/L. `equity_snapshots` are captured with the monitor so equity curves and drawdown do not have to be reconstructed from today's prices.

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
| `/health` | GET | Health check |

## License

MIT - Built for the OpenClaw ecosystem
