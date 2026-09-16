# OpenStock changelog

## 1.0.0 — 2026-09-16

### Reliability baseline

- Trade Republic is the primary EUR quote source; Yahoo Finance plus cached/free FX is the read-only fallback for dashboard refreshes and monitor trigger evaluation.
- Missing quotes or FX fail closed without overwriting the last known dashboard value or creating alerts.
- Dashboard quote provenance is explicit; Yahoo fallback never renders as a Trade Republic bid/ask.
- Consolidated the v1 test baseline around financial, migration, broker, API, chart, and browser-security contracts; compact output is the default with a verbose option for debugging.
