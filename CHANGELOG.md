# OpenStock changelog

## 1.0.2 — 2026-09-17

### Journal usability

- Added server-side Journal pagination with 20 trades per page and Previous/Next navigation.
- Prevented duplicate Initial Capital entries in the Journal and returned clear `409 INITIAL_CAPITAL_EXISTS` API feedback.
- Made Deposit the default reconciliation option after initial capital has been recorded, with guidance that deposits and withdrawals do not change trading P/L.

## 1.0.1 — 2026-09-16

### Monitoring reliability

- Added a durable alert outbox with delivery state and retry handling; alert levels are retired only after confirmed Telegram delivery.
- Updated broker account, order, and transaction synchronization to refresh current state instead of retaining the first observation.
- Added per-range Trade Republic history-backfill state so incomplete history remains eligible for retry.
- Consolidated quote selection for manual refreshes and scheduled monitoring, with bounded monitor concurrency, a cycle deadline, and race-safe FX cache writes.
- Required a verified EUR broker mapping for direct quote reads and refreshed operational documentation.

## 1.0.0 — 2026-09-16

### Reliability baseline

- Trade Republic is the primary EUR quote source; Yahoo Finance plus cached/free FX is the read-only fallback for dashboard refreshes and monitor trigger evaluation.
- Missing quotes or FX fail closed without overwriting the last known dashboard value or creating alerts.
- Dashboard quote provenance is explicit; Yahoo fallback never renders as a Trade Republic bid/ask.
- Consolidated the v1 test baseline around financial, migration, broker, API, chart, and browser-security contracts; compact output is the default with a verbose option for debugging.
