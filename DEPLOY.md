# Deploying OpenStock

This guide describes the single-host deployment that OpenStock is built for: a Linux server under `/opt/openstock`, run by systemd, reachable through Tailscale. For a local try-out, see the quick start in the [README](README.md).

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
- SQLite 3.x with the CLI, for debugging
- Telegram bot token
- Tailscale, configured for the host
- Trade Republic account access provisioned outside the dashboard (see the README for the quote sources)

## Install

```bash
git clone git@github.com:mycode42/openstock.git /opt/openstock
cd /opt/openstock
npm install

cp .env.example .env
# Edit .env with your credentials. Keep it out of source control.

# Initialize the database (automatic on first run)
npm start
```

`server.js` loads `.env` from the repository directory by default. Set `OPENSTOCK_CONFIG_FILE` to load a different file. The default `DATABASE_PATH` in `.env.example` is relative to the checkout, so the setup also works at `http://localhost:3000`.

## Services

```bash
# Web application
sudo systemctl enable --now openstock.service

# Monitoring timer (one cycle per minute)
sudo systemctl enable --now openstock-monitor.timer

# Status
sudo systemctl status openstock.service
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
```

To run one monitoring cycle by hand as the service user:

```bash
npm run monitor            # run one cycle
npm run monitor -- --help  # print usage only
```

The tracked monitor drop-ins in `ops/systemd/` (`openstock-monitor-30s.conf`, `openstock-monitor-node.conf`) are the source of truth for monitor configuration changes.

## Logs

```bash
# Web server
journalctl -u openstock.service -f

# Monitor
tail -f /opt/openstock/memory/monitor.log
tail -f /opt/openstock/memory/monitor-error.log
```

## Database

```bash
# Location from configuration
grep DATABASE_PATH .env

# Query directly
sqlite3 /opt/openstock/database/openstock.db ".tables"
sqlite3 /opt/openstock/database/openstock.db "SELECT * FROM stocks;"
```

Backing up:

```bash
mkdir -p /var/backups/openstock
cp /opt/openstock/database/openstock.db /var/backups/openstock/openstock.db.$(date +%F-%H%M%S)

# Include the audit trail
tar -czf openstock-backup-$(date +%F).tar.gz \
    database/openstock.db \
    memory/*.log
```

The live database and `database/*.db.backup` files are not tracked in git. Keep them out of commits.

## Updating

```bash
git pull origin main
npm install
sudo systemctl restart openstock.service
```

Restart the service only in a deploy step. Editing files does not reload a running server.

## Telegram alerts

1. Chat with [@BotFather](https://t.me/BotFather) to create a bot and get its token.
2. Add the bot as a member of your Telegram channel or group.
3. Add your Telegram user ID to `telegram-default-allowFrom.json` in the OpenClaw credentials directory.

Check the bot and the chat:

```bash
curl "https://api.telegram.org/botYOUR_TOKEN/getMe"
cat /home/openclaw/.openclaw/credentials/telegram-default-allowFrom.json
cat /opt/openstock/memory/monitor-error.log
```

## Trade Republic sidecar and SDK

Quotes come from the read-only Handelsrepublik SDK first and the loopback Trade Republic UAPI sidecar second. The SDK session file and any PIN belong on the server only, outside the repository. The sidecar listens on `TRADE_REPUBLIC_BASE_URL` (default `http://127.0.0.1:8000`).

If prices stop updating, check `/api/broker/trade-republic/health`, then confirm the SDK session and the sidecar are reachable from the OpenStock service.

## Tailscale

The web app listens on `127.0.0.1:3000` only. Expose it through Tailscale Serve:

- Local: `http://localhost:3000`
- Tailscale: `https://openstock.tailbff3c4.ts.net` (configure Tailscale Serve for port 3000)

## Troubleshooting

### Monitor not running

```bash
sudo systemctl status openstock-monitor.timer
sudo systemctl list-timers | grep openstock
journalctl -u openstock-monitor.service -n 50
```

### Database locked or corrupted

```bash
sqlite3 /opt/openstock/database/openstock.db "PRAGMA integrity_check;"
# If there are issues, restart the web server to release locks
sudo systemctl restart openstock.service
```

### Web interface not reachable

```bash
ss -tlnp | grep 3000
ps aux | grep "node server.js"
journalctl -u openstock.service -n 50 --no-pager
```
