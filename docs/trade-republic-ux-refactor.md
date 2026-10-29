# OpenStock Trade Republic read-only integration

OpenStock uses Trade Republic data read-only. The Dashboard and Journal consume it; no order placement, fee preview, or cancellation path exists.

## Architecture

- TradeRepublicClient is a read-only adapter for the local UAPI contract. It normalizes responses and never returns raw broker payloads or credentials.
- Verified ISIN + exchange + EUR mappings remain authoritative. Dashboard refresh and the monitor share one quote service: the Handelsrepublik SDK is primary and the Trade Republic UAPI sidecar is the backup. Direct bid, ask, midpoint, timestamp, and source are persisted in stocks, broker_quotes, and price_history. Provider failures stay isolated per stock.
- Broker-confirmed executions reach the Journal only through the read-only execution sync, and only when authoritative execution identity, quantity, EUR price, fees, and timestamp are present. Buys merge open Journal lots; sells consume FIFO lots and close or split them. Incomplete executions become pending reconciliation candidates for explicit review.

## Sanitized broker endpoints

All /api responses are same-origin protected and Cache-Control: no-store:

- GET /api/broker/trade-republic/accounts
- GET /api/broker/trade-republic/portfolio
- GET /api/broker/trade-republic/orders
- GET /api/broker/trade-republic/transactions
- POST /api/broker/trade-republic/accounts-history (1d|5d|1m|1y|max)
- GET /api/broker/trade-republic/price-alarms
- GET /api/broker/trade-republic/interests
- GET /api/broker/trade-republic/tickets
- GET /api/broker/trade-republic/activity
- GET /api/broker/trade-republic/reconciliation-candidates
- GET /api/broker/trade-republic/instrument/:stockId
- GET /api/broker/trade-republic/tr-instrument/:stockId
- GET /api/broker/trade-republic/instrument-history/:stockId
- GET /api/broker/trade-republic/schedule/:stockId

Live `/api/transactions` rows can be `TRADING_TRADE_EXECUTED` with localized titles/subtitles and money shaped as `{currency,value,fractionDigits}`. The adapter normalizes minor units (`value / 10^fractionDigits`). These rows do not provide enough identity, ISIN, quantity, execution price, or fees to create Journal trades; incomplete executed rows are stored as sanitized pending reconciliation candidates for explicit completion. Numeric and IBAN-shaped account-map keys are masked.

Personal details, card data, account numbers, credentials, cookies, secret-like fields, identifiers, and raw payloads are filtered or masked. Personal/card routes, price-alarm mutations, and order endpoints are intentionally not exposed.

## Migration

Migration 014_trade_republic_execution_and_quotes adds nullable authoritative quote columns and the unique broker_executions ledger. Migration 015_broker_reconciliation_candidates adds an idempotent pending-import ledger. Migration 009_trade_republic_order_audit and the broker_order_intents table are historical: they remain in place because applied migrations are append-only, but no code writes to them anymore.

## Verification

Run `NODE_PATH=/opt/OpenStock/node_modules node --test` before deployment. The suite includes migration tests, browser module/escaping tests, execution reconciliation/idempotency, minor-unit transaction normalization, pending reconciliation, account-key masking, and read-only sanitization. Tests use isolated databases and mocks; no production database, cache, lock, service, or broker write endpoint is used.
