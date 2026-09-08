# Task 4 — Paper trading and operational risk controls

Use the system contract and complete Tasks 1–3 first. Add paper operation,
never live broker execution.

Implement an event-sourced paper ledger and simulated order/fill engine with
market-session validation, conservative fill/slippage rules, partial fills,
fees, cash/reserved buying power, idempotency keys, and reconciliation against
the market-data snapshot used for each decision. A signal may create a paper
order intent only after deterministic risk approval.

Risk approval must enforce configurable hard limits: mode guard, instrument
allow-list, no unsupported assets, max position/notional, max sector and total
exposure, available cash/margin, daily loss, drawdown, order rate, duplicate
order prevention, stale-data threshold, event/market-closed halt, and a global
kill switch. Each rejection must be explicit and auditable. The kill switch
must be tested to stop new paper orders immediately.

Add operations: health/readiness checks, structured logs/metrics, alert
deduplication, backup and restore drill documentation, retention policy,
configuration change audit, and a paper-vs-reference reconciliation report.
Define the promotion rubric: a minimum sustained paper period, no unresolved
reconciliation discrepancy, no risk-gate bypass, documented incident drills,
and an owner review. Do not implement a live-mode UI action.
