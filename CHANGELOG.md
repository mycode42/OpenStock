# Changelog

## [1.5.0] - 2026-10-08

V1.5 consolidates the separate paper-trading routes and adds a continuously running Algo strategy portfolio with persisted coordinator state and research tooling.

### Added

- Dedicated Swing, Day, and Algo Alpaca account routing with broker-side account ID verification.
- Persisted Algo coordinator and per-strategy lifecycle state, startup reconciliation, health reporting, and execution safeguards.
- Feed-aware market data for Basic and Algo Trader Plus, including BOATS routing for eligible overnight scans.
- Local Algo portfolio replay, bounded signal-parameter profiles, chronological walk-forward evaluation, and slippage sensitivity reports.
- Focused GitHub Actions test workflow.

### Changed

- OpenClaw prompts and operating notes document separate modes, explicit feed handling, and service health checks.
- Algo research replay accepts regular-session IEX or SIP only; BOATS requires a separate overnight replay and fill model.
- Package metadata uses the V1.5 version and PEP 621 project fields.

### Configuration notes

- Copy `.env.example` and configure separate paper-account credentials and account IDs for each enabled trading route.
- The Algo service uses the fixed Alpaca paper endpoint. Live-account execution is not supported by that service.
- Set `ALPACA_DATA_TIER=basic` until the Plus subscription is active; regular-session history then uses IEX and overnight bars use BOATS.
- This release is paper-trading software. Simulator reports and autoresearch candidates do not update live strategy settings.
