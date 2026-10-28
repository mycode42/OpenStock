const path = require('node:path');
const paths = require('./lib/paths');
require('dotenv').config({ path: paths.configFilePath(), quiet: true });
const express = require('express');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');
const { HistoryBackfillQueue } = require('./lib/history-backfill');
const { TradeRepublicClient } = require('./lib/brokers/trade-republic');
const {
    resolveVerifiedInstrument,
    resolveCryptoCandidates,
    resolveVerifiedCryptoInstrument,
} = require('./lib/instrument-resolution');
const { HandelsrepublikQuoteClient } = require('./lib/brokers/handelsrepublik');
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
const { dashboardData, captureEquitySnapshot, queueStockHistory, brokerError } = require('./server/services');
const { quoteServiceFor, createLiveQuoteFeed } = require('./server/quote-feed');
const { detectDemoSetup } = require('./server/demo-setup');

function createApp({
    db: suppliedDb,
    dbPath = paths.databasePath(),
    fetchImpl = globalThis.fetch,
    backfill = null,
    sdkClient = null,
    now = () => new Date(),
    demoMode = null,
    algoTraderRoot = paths.algoTraderRoot(),
} = {}) {
    const db = suppliedDb || new OpenStockDB(dbPath);
    const historyBackfill =
        backfill ||
        new HistoryBackfillQueue({
            db,
            delayMs: process.env.HISTORY_BACKFILL_DELAY_MS || 1500,
        });
    const app = express();
    // OpenStock is published behind the OpenClaw reverse proxy.  Express sees
    // the final hop as HTTP unless it trusts that proxy, while browsers send
    // their HTTPS Origin header.  Trusting one proxy hop lets req.protocol use
    // X-Forwarded-Proto for the same-origin mutation check below.
    app.set('trust proxy', 1);
    app.locals.db = db;
    app.locals.historyBackfill = historyBackfill;
    app.locals.fetchImpl = fetchImpl;
    app.locals.brokerClient = new TradeRepublicClient({
        fetchImpl,
        timeoutMs: Number(process.env.TRADE_REPUBLIC_TIMEOUT_MS || 30000),
    });
    app.locals.sdkClient = sdkClient || new HandelsrepublikQuoteClient();
    const detectedDemoSetup = detectDemoSetup({ algoTraderRoot, sdkClient: app.locals.sdkClient });
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
    historyBackfill.brokerClient = app.locals.brokerClient;
    historyBackfill.sdkClient = app.locals.sdkClient;
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
        const forwardedHost = req.get('x-forwarded-host')?.split(',')[0].trim();
        const requestHost = forwardedHost || req.get('host');
        if (
            ['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method) &&
            req.get('origin') &&
            req.get('origin') !== `${req.protocol}://${requestHost}`
        ) {
            return res.status(403).json({ error: 'Same-origin mutation required' });
        }
        next();
    });
    app.use(express.static(path.join(__dirname, 'www')));
    app.locals.liveQuoteFeed = createLiveQuoteFeed({ app, now });

    require('./server/routes/stocks')({
        app,
        serializeConfiguredStock,
        serializeAlertLevel,
        alertLevelInput,
        numberField,
        booleanField,
        queueStockHistory,
        resolveVerifiedInstrument,
        resolveCryptoCandidates,
        resolveVerifiedCryptoInstrument,
        brokerError,
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
        dashboardData,
        captureEquitySnapshot,
        toISODateTime,
    });

    require('./server/routes/dashboard')({
        app,
        dashboardData,
        captureEquitySnapshot,
        serializeConfiguredStock,
        quoteServiceFor,
        now,
    });

    require('./server/routes/algo-trader')({ app, root: algoTraderRoot, now });

    require('./server/routes/trade-republic')({
        app,
        resolveVerifiedInstrument,
        serializeConfiguredStock,
        brokerError,
        serializeTrade,
    });

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

    // Seed real charts only after broker setup is present. The demo feed is synthetic
    // and must not trigger writes to the user's stock history.
    if (!app.locals.demoMode) {
        try {
            historyBackfill.enqueueMissing();
        } catch (error) {
            console.error('Unable to queue startup history backfill:', error.message);
        }
    }
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
    createLiveQuoteFeed,
    detectDemoSetup,
};
