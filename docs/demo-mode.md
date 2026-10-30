# Demo mode

Part of the [OpenStock README](../README.md).

`npm run demo` starts OpenStock with a simulated paper account. The interface, the Configure page and the order flow are the same code as a real installation. Only the broker is replaced:

- **Watchlist and alerts.** Six US stocks, three of them held, with price and percentage alert levels. One level has real orders switched on with a $500 budget, and one retired level shows the triggered section.
- **Quotes.** Prices move a little around fixed reference values. They are labelled as simulated market data.
- **Orders.** Limit orders are accepted, rest until the simulated price reaches them, and fill on the next status check. A marketable limit fills at once. Cancel works. Filled orders change the simulated positions and the journal.
- **Journal and history.** The Journal, the stock charts and the Algo Trader pages show simulated history in USD.

Nothing is sent to a broker. The simulator never opens a network connection, and it accepts orders only in the demo database, so a real database can never receive a simulated order.

Demo data lives in `data/demo/openstock.db`, separate from any real database. The first run seeds it. Delete `data/demo/` to start over.

## When demo mode starts

Demo mode is on whenever `ALPACA_MANUAL_API_KEY` or `ALPACA_MANUAL_API_SECRET` is empty. `npm run demo` also sets up the demo database. `npm start` without keys runs in demo mode too, but then it uses your own database and it refuses orders. Run `npm run demo` to try orders.

The application reports the missing setup through `GET /api/demo-status`.
