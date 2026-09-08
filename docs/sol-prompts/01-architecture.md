# Task 1 — Architecture and safety baseline

Use the system contract. First inspect the current OpenStock repository,
including package scripts, database schema/migrations, services, monitor,
systemd units, client API usage, tests, deployment/readme, and git status. Do
not overwrite current uncommitted work.

Create a concise architecture decision record and implementation backlog for a
signal-only -> backtest -> paper -> limited-live -> live platform. The design
must cover:

1. bounded contexts: data ingestion, instrument master/corporate actions,
   research/signals, portfolio & risk, execution adapters, ledger/audit,
   notifications, and control-plane UI;
2. typed contracts and database ownership for raw observations, normalized
   bars/quotes, fundamentals, dividends, news events, signal versions,
   target allocations, order intents, risk decisions, fills, positions,
   cash, reconciliation results, and audit events;
3. a mode/state diagram that proves no signal can accidentally become a live
   order;
4. a deployment topology that keeps the web server, scheduler, data workers,
   paper engine, and any future broker connector isolated;
5. threat model: secrets, prompt injection in news, malicious market payloads,
   duplicated orders, stale data, clock drift, partial fills, broker outages,
   and database corruption;
6. an explicit evaluation spike for LEAN versus a thin in-house interface.

Implement only safe scaffolding/documentation and test hooks agreed by the
existing code style. Do not add a broker SDK or order endpoint. Success is a
reviewable ADR, prioritized backlog, clear service boundaries, and a testable
mode guard.
