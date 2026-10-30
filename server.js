const path = require('node:path');
const paths = require('./lib/paths');
require('dotenv').config({ path: paths.configFilePath(), quiet: true });
const express = require('express');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');
const demoData = require('./server/demo-data');
const APP_VERSION = require('./package.json').version;

const {
    toISODateTime,
    berlinTradingDayStart,
    berlinTradingDayEnd,
    evenlySample,
    serializeStock,
    serializeBrokerStock,
    serializeAlertLevel,
    serializeTrade,
} = require('./server/serializers');
const { alertLevelInput, booleanField, toDatabaseDate, numberField, normaliseTrade } = require('./server/input');
const { dashboardData, captureEquitySnapshot, brokerError } = require('./server/services');
const { detectDemoSetup, hasAlpacaCredentials } = require('./server/demo-setup');
const { AlpacaClient } = require('./lib/brokers/alpaca');
const { createAlpacaQuoteFeed } = require('./server/alpaca-quote-feed');
const { createDemoBroker } = require('./server/demo-broker');
const { rawDatabase } = require('./server/alpaca-profile');
const { syncOpenOrders } = require('./lib/alpaca-orders');

function createApp({
    db: suppliedDb,
    dbPath = paths.databasePath(),
    fetchImpl = globalThis.fetch,
    now = () => new Date(),
    demoMode = null,
    algoTraderRoot = paths.algoTraderRoot(),
} = {}) {
    const db = suppliedDb || new OpenStockDB(dbPath);
    const app = express();
    // Behind a reverse proxy, the proxy sets X-Forwarded-Proto and X-Forwarded-Host. Those headers are
    // honoured only when OPENSTOCK_TRUST_PROXY=1 says a proxy is in front. Otherwise any client can set them.
    const trustProxy = process.env.OPENSTOCK_TRUST_PROXY === '1';
    app.set('trust proxy', trustProxy ? 1 : false);
    app.locals.db = db;
    app.locals.fetchImpl = fetchImpl;
    app.locals.alpacaClient = new AlpacaClient({ fetchImpl, timeoutMs: Number(process.env.ALPACA_TIMEOUT_MS || 10000) });
    const detectedDemoSetup = detectDemoSetup({ algoTraderRoot });
    app.locals.demoMode =
        demoMode === null ? (process.env.NODE_ENV === 'test' ? false : detectedDemoSetup.demo_mode) : Boolean(demoMode);
    app.locals.demoStatus =
        demoMode === null
            ? {
                  ...detectedDemoSetup,
                  demo_mode: app.locals.demoMode,
                  setup: app.locals.demoMode ? detectedDemoSetup.setup : [],
              }
            : {
                  demo_mode: app.locals.demoMode,
                  setup: demoMode
                      ? detectedDemoSetup.setup.length
                          ? detectedDemoSetup.setup
                          : ['Demo mode was enabled manually']
                      : [],
              };
    app.locals.demoData = demoData;
    // Demo mode runs the real Alpaca code paths against a simulated paper account. It never opens a broker connection.
    if (app.locals.demoMode) {
        // Orders the demo database already tracks are rebuilt in the broker, so a restart keeps positions and open orders.
        const storedOrders = rawDatabase(db).prepare(`SELECT broker_order_id, client_order_id, symbol, side, qty, limit_price_usd, status,
            filled_avg_price_usd, submitted_at, updated_at FROM alpaca_orders WHERE broker_order_id IS NOT NULL`).all();
        app.locals.alpacaClient = createDemoBroker({ now, ordersEnabled: process.env.OPENSTOCK_DEMO_DATABASE === '1', storedOrders });
        // The monitor normally moves orders on from cancel and fill. The demo has no monitor, so its orders are synced here.
        const timer = setInterval(() => syncOpenOrders({ db, client: app.locals.alpacaClient, now }).catch(() => {}), 5000);
        timer.unref();
    }
    const serializeConfiguredStock = (stock) => serializeBrokerStock(stock, db);
    app.use(express.json());
    app.use(express.urlencoded({ extended: false }));
    app.use('/api', (req, res, next) => {
        res.set({
            'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
            Pragma: 'no-cache',
            Expires: '0',
            'X-OpenStock-Version': APP_VERSION,
        });
        const forwardedHost = trustProxy ? req.get('x-forwarded-host')?.split(',')[0].trim() : undefined;
        const requestHost = forwardedHost || req.get('host');
        // Every write must come from this site. A missing Origin header is refused, not waved through.
        if (
            ['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method) &&
            req.get('origin') !== `${req.protocol}://${requestHost}`
        ) {
            return res.status(403).json({ error: 'Same-origin mutation required' });
        }
        next();
    });
    app.use(express.static(path.join(__dirname, 'www')));
    app.locals.alpacaQuoteFeed = createAlpacaQuoteFeed({ app, now });

    require('./server/routes/stocks')({
        app,
        serializeConfiguredStock,
        serializeAlertLevel,
        alertLevelInput,
        numberField,
        booleanField,
        now,
        berlinTradingDayStart,
        berlinTradingDayEnd,
        evenlySample,
        toISODateTime,
    });

    require('./server/routes/journal')({
        app,
        serializeTrade,
        normaliseTrade,
        numberField,
        toDatabaseDate,
        captureEquitySnapshot,
        toISODateTime,
    });

    require('./server/routes/alpaca')({ app });

    require('./server/routes/dashboard')({
        app,
        alpacaQuoteFeed: app.locals.alpacaQuoteFeed,
        now,
    });

    require('./server/routes/algo-trader')({ app, root: algoTraderRoot, now });
    require('./server/routes/system')({ app, root: algoTraderRoot });

    // Health check
    app.get('/health', (req, res) => {
        res.set({
            'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
            Pragma: 'no-cache',
            Expires: '0',
            'X-OpenStock-Version': APP_VERSION,
        });
        res.json({ status: 'ok', version: APP_VERSION, timestamp: new Date().toISOString() });
    });

    // Serve index.html for all other routes (SPA fallback)
    app.get('/', (req, res) => {
        res.sendFile(path.join(__dirname, 'www', 'index.html'));
    });

    return app;
}

if (require.main === module) {
    const dbPath = paths.databasePath();
    const port = process.env.PORT || 3000;
    initDatabase({ dbPath });
    const app = createApp({ dbPath });
    const server = app.listen(port, '127.0.0.1', () => console.log(`OpenStock server listening on 127.0.0.1:${port}`));
    const shutdown = () => {
        console.log('OpenStock shutting down...');
        server.close(() => {
            app.locals.db.db.close();
            process.exit(0);
        });
    };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
}

module.exports = {
    createApp,
    serializeStock,
    serializeTrade,
    berlinTradingDayStart,
    berlinTradingDayEnd,
    detectDemoSetup,
};
