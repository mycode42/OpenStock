# Configuration

Part of the [OpenStock README](../README.md).

Copy the example file to configure a real installation:

```bash
cp .env.example .env
```

`.env` is read from the repository directory. Set `OPENSTOCK_CONFIG_FILE` to load a different file. Keep credentials out of source control.

Add the Alpaca Trading API key and secret to `.env`. `ALPACA_MANUAL_BASE_URL` defaults to the paper endpoint, so a fresh install cannot reach the live account until you set it. The account is resolved from those credentials; OpenStock does not need an account ID. `ALPACA_DATA_FEED` defaults to `sip` and falls back to `iex` if SIP access is denied.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_MANUAL_API_KEY` | empty | Alpaca API key for the manual account. Use a paper key while the URL is paper. |
| `ALPACA_MANUAL_API_SECRET` | empty | Alpaca API secret for the manual account. Keep it in the ignored `.env` file. |
| `ALPACA_DATA_FEED` | `sip` | Preferred Alpaca stock-data feed. OpenStock falls back between SIP and IEX when feed access is denied. |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from @BotFather for alert delivery. |
| `TELEGRAM_CHAT_ID` | empty | Chat that receives alerts. |
| `DATABASE_PATH` | `database/openstock.db` | SQLite database path. Relative paths resolve from the checkout, not the current directory. |
| `PORT` | `3000` | Web server port. The server always binds to `127.0.0.1`. |
| `MONITOR_CONCURRENCY` | `3` | Maximum parallel quote requests per monitor cycle. |
| `MONITOR_CYCLE_DEADLINE_MS` | `45000` | Stop starting new stock work after this cycle budget. |
| `LIVE_PRICE_REFRESH_MS` | `1000` | Display-only Dashboard refresh cadence. Minimum and default 1000 ms. |
| `HISTORY_BACKFILL_DELAY_MS` | `1500` | Pause between chart-history backfill requests. |
| `ALPACA_MANUAL_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Trading endpoint for the manual account (dashboard and manual orders). Set to `https://api.alpaca.markets` only for the live account. |
| `ALPACA_EQUITY_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Algo Trader trading endpoint. Refused unless it is the paper endpoint. |
| `ALPACA_OPTIONS_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Options paper endpoint. Refused unless it is the paper endpoint. |
| `ORDER_NOTIONAL_USD` | `250` | Default USD amount for a BUY limit order. The dashboard asks each time and can use another amount. |
| `ALGO_TRADER_ROOT` | `./algo-trader` | Data directory of the optional Algo Trader installation. |

## Price sources

Alpaca provides the quote and chart-history data. OpenStock requests the configured SIP or IEX feed and labels the feed returned by Alpaca.

BUY alerts compare against the executable ask; SELL alerts compare against the executable bid.

Prices, amounts, fees and performance values are displayed rounded to two decimals. Journal fields keep the precision you enter, up to six decimals; calculations and stored values use full precision.

The Dashboard polls `/api/live-prices` while visible. That endpoint is display-only: it never writes stock state, history, alert state or equity snapshots.

## Migrating a watchlist

To copy an existing watchlist and its alert levels into the Alpaca USD watchlist, configure Alpaca credentials first, then run this once:

```bash
node scripts/migrate-alpaca-profile.js
```

The script verifies symbols with Alpaca and converts legacy EUR price levels using the current EUR/USD rate. It refuses to overwrite an already-populated Alpaca watchlist.
