# OpenStock

**A self-hosted stock dashboard, trading journal, and paper-trading Algo engine, built on Alpaca.**

OpenStock watches your alert levels, journals your Alpaca account in USD, and runs the Algo Trader sleeves in [algo-trader/](algo-trader/). It uses SIP quotes when available and falls back to IEX when the account cannot access SIP.

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
