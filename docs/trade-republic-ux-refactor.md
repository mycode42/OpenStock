# OpenStock Trade Republic UX/trading refactor

## Architecture

- TradeRepublicClient is the broker-independent read/order adapter. It normalizes the local UAPI contract and never returns raw broker payloads or credentials.
- Verified ISIN + exchange + EUR mappings remain authoritative. Dashboard refresh uses the verified Trade Republic order-price quote first and only falls back to Yahoo after a tested TR failure. Direct bid, ask, midpoint, timestamp, and source are persisted in stocks, broker_quotes, and price_history.
- Trading is a top-level SPA route. Its identity fields are derived from the selected stock mapping; account, side, mode, validity, conditional prices, quantity, preview, confirmation, idempotency, and execution gates remain server authoritative. GTD is sent only as the sidecar-supported `expiry.type`; this integration has no GTD date field.
- Confirmed FILLED/EXECUTED responses are reconciled against sanitized orders/transactions and only when authoritative execution identity, quantity, EUR price, fees, and timestamp are present. Buys merge open Journal lots; sells consume FIFO lots and close or split them. SUBMITTED, order-ID-only, rejected, and UNKNOWN outcomes remain pending/manual and never create Journal trades.

## Sanitized broker endpoints

All /api responses are same-origin protected for mutations and Cache-Control: no-store:

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

Live `/api/transactions` rows can be `TRADING_TRADE_EXECUTED` with localized titles/subtitles and money shaped as `{currency,value,fractionDigits}`. The adapter normalizes minor units (`value / 10^fractionDigits`). These rows do not provide enough identity, ISIN, quantity, execution price, or fees to create Journal trades; incomplete executed rows are stored as sanitized pending reconciliation candidates for explicit completion. Numeric and IBAN-shaped account-map keys are masked.

Personal details, card data, account numbers, credentials, cookies, secret-like fields, and raw payloads are filtered. POST /api/broker/trade-republic/cancel is an explicit disabled response and makes no live call; cancellation has not been enabled.

## Migration and PWA

Migration 014_trade_republic_execution_and_quotes adds nullable authoritative quote columns and the unique broker_executions ledger. Migration 015_broker_reconciliation_candidates adds an idempotent pending-import ledger. Both are append-only after application. PWA assets advance to v25 and include modules/trading.js.

## Verification

NODE_PATH=/opt/openstock/node_modules node --test passes all 90 tests, including live-gate zero-call tests, parser fixtures, migration tests, browser module/escaping tests, execution reconciliation/idempotency, minor-unit transaction normalization, pending reconciliation, account-key masking, and read-only sanitization. No production database, cache, lock, service, place-order, or cancel endpoint was used.

## Contract source

Parser field choices were checked against the supplied ReDoc PDF and live http://127.0.0.1:8000/openapi.json. The local OpenAPI confirms Order.mode (market, limit, stopMarket), Order.validity (GFD, GTD, GTC), integer positive quantity, ISIN, exchange, and account fields. The local API was read only.
