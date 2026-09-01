# OpenStock - Stock Price Monitoring & Trading Journal

A production-ready MVP for monitoring stock prices, converting USD to EUR, and sending Telegram alerts when buy/sell thresholds are reached. Includes a trading journal for recording actual trades.

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
│  │ Dashboard │  │ Stocks    │  │ Trading      │       │
│  │           │  │ Config    │  │ Journal      │       │
│  └───────────┘  └───────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────┘
                                 │
                                 │
┌────────────────────────────────▼─────────────────────────┐
│                 SQLite Database (openstock.db)           │
│  ┌───────────┐  ┌───────────┐  ┌──────────────┐        │
│  │ stocks    │  │ alerts_log│  │ journal_     │        │
│  │           │  │           │  │ entries      │        │
│  └───────────┘  └───────────┘  └──────────────┘        │
└─────────────────────────────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  │                  ▼
┌─────────────────────────┐      │      ┌─────────────────────────┐
│   Monitoring Script     │      │      │  Exchange Rate Cache    │
│      (monitor.sh)       │      │      │   (JSON cache)          │
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
sudo systemctl status openstock-web.service
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
```

## Viewing Logs

```bash
# Web server logs
tail -f /home/ubuntu/openstock/memory/web.log

# Monitor logs
tail -f /home/ubuntu/openstock/memory/monitor.log

# Error logs
tail -f /home/ubuntu/openstock/memory/monitor-error.log
```

## Database Location

```bash
# View database location
cat .env | grep DATABASE_PATH

# Query directly
sqlite3 /home/ubuntu/openstock/database/openstock.db "SELECT * FROM stocks;"
sqlite3 /home/ubuntu/openstock/database/openstock.db ".tables"
```

## Monitoring Behavior

The `monitor.sh` script runs every minute via systemd timer and:

1. Reads enabled stocks from database
2. Fetches current USD price from Yahoo Finance
3. Gets USD→EUR exchange rate (cached for 1 hour)
4. Calculates EUR price
5. Stores price data in database
6. Evaluates buy/sell thresholds
7. Sends Telegram alert on threshold crossing
8. Logs alert to `alerts_log` table (prevents duplicates)
9. Updates trigger state in stocks table

### Trigger Debouncing

- Alerts only fire when price **crosses into** trigger zone
- System tracks `trigger_state` in database: `NONE`, `BUY_TRIGGERED`, `SELL_TRIGGERED`
- After alert fires, price must exit zone before new alert possible

## Web Interface

Access the web app:

- **Local:** `http://localhost:3000`
- **Tailscale:** `https://openstock.tailbff3c4.ts.net` (configure Tailscale Serve)

### Features

- **Dashboard:** View all stocks with current prices and trigger status
- **Configure Stocks:** Add/remove stocks, set thresholds, toggle ownership
- **Trading Journal:** Record buy/sell transactions with notes

## Updating the Application

```bash
# Pull changes
git pull origin main

# Install updates
npm install

# Restart service
sudo systemctl restart openstock-web.service
```

## Backup Considerations

```bash
# Backup database
cp /home/ubuntu/openstock/database/openstock.db /path/to/backup/

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
curl "https://api.twelvedata.com/quote?symbol=AAPL&apikey=YOUR_KEY"
```

### Telegram alerts not sending

```bash
# Check bot token
curl "https://api.telegram.org/botYOUR_TOKEN/getMe"

# Check chat ID
cat /home/ubuntu/.openclaw/credentials/telegram-default-allowFrom.json

# Check monitor error log
cat /home/ubuntu/openstock/memory/monitor-error.log
```

### Database locked or corrupted

```bash
# Check integrity
sqlite3 /home/ubuntu/openstock/database/openstock.db "PRAGMA integrity_check;"

# If issues, restart the web server to release locks
sudo systemctl restart openstock-web.service
```

### Web interface not accessible

```bash
# Check if server is running
ss -tlnp | grep 3000

# Check process
ps aux | grep "node server.js"

# Check logs
tail /home/ubuntu/openstock/memory/web.log
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stocks` | GET | List all stocks |
| `/api/stocks/:id` | GET | Get specific stock |
| `/api/stocks` | POST | Add new stock |
| `/api/stocks/:id` | PUT | Update stock |
| `/api/stocks/:id` | DELETE | Remove stock |
| `/api/journal` | GET | List journal entries |
| `/api/journal` | POST | Add journal entry |
| `/api/journal/:id` | PUT | Edit entry |
| `/api/journal/:id` | DELETE | Delete entry |
| `/api/dashboard` | GET | Dashboard data |
| `/health` | GET | Health check |

## License

MIT - Built for the OpenClaw ecosystem
