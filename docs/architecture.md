# Architecture and development

Part of the [OpenStock README](../README.md). Production setup is in [DEPLOY.md](../DEPLOY.md).

## Layout

```
server.js                 HTTP app setup (loopback only) and demo-mode detection
server/routes/            Route handlers
server/algo-trader/       Algo Trader data readers, settings, and payload builders
server/demo-data.js       Simulated data for demo mode
lib/                      Quote service, monitor, analytics, history backfill, broker clients
database/                 SQLite access; migrations/ is append-only once applied
scripts/                  monitor, history backfill, equity snapshot and rebuild tools
www/                      Browser UI as native ES modules; www/app.js is the bootstrap
test/                     node:test suites, one per module
algo-trader/              Algo Trader subtree (Python); see its README
```

Server construction is separate from listening: `createApp()` in `server.js` builds the app, so route tests run against temporary SQLite files and mocked providers. Database startup applies ordered, transactional migrations from `database/migrations/` and records them in `schema_migrations`.

## Development

```bash
npm test          # full test suite
npm run check     # syntax check and PWA asset consistency
npm run audit:prod
```

Tests use isolated temporary databases and mocked provider calls. They never open the live database.
