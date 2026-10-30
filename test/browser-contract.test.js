const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

test('v1 browser modules link and shared escaping blocks markup injection', async () => {
    const sourceDir = path.join(__dirname, '..', 'www', 'modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-browser-v1-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });
    try {
        const escape = await import(pathToFileURL(path.join(tempDir, 'modules', 'escape.js')).href);
        const api = await import(pathToFileURL(path.join(tempDir, 'modules', 'api.js')).href);
        const hostile = '<img src=x onerror="alert(1)">';
        assert.equal(escape.escapeHtml(hostile).includes('<img'), false);
        for (const module of ['charts', 'dashboard']) {
            await import(pathToFileURL(path.join(tempDir, 'modules', `${module}.js`)).href);
        }
        const journal = await import(pathToFileURL(path.join(tempDir, 'modules', 'journal.js')).href);
        const journalView = journal.createJournal({
            getStocks: () => [], getTrades: () => [], getTradePagination: () => ({}), getCapitalEvents: () => [], getAnalytics: () => null,
            request: async () => null, showA() {}, setJournalPage() {}, reloadJournal() {}, charts: { attachInteractiveLineCharts() {}, interactiveChartSvg() {} }
        });
        const shortPagination = journalView.renderTradePagination({ total: 45, page: 2, page_size: 15, total_pages: 3 });
        assert.match(shortPagination, /data-trade-page="1"[^>]*aria-label="Page 1"/);
        assert.match(shortPagination, /data-trade-page="2"[^>]*aria-label="Page 2"[^>]*aria-current="page"/);
        assert.match(shortPagination, /data-trade-page="3"[^>]*aria-label="Page 3"/);
        const longPagination = journalView.renderTradePagination({ total: 210, page: 8, page_size: 15, total_pages: 14 });
        assert.equal((longPagination.match(/class="journal-page-ellipsis"/g) || []).length, 2);
        assert.match(longPagination, /aria-label="Page 14"/);
        const originalFetch = globalThis.fetch;
        try {
            globalThis.fetch = async () => ({ ok: false, json: async () => ({ error: 'Rejected', code: 'REQUEST_REJECTED', failed_gates: ['example_gate'] }) });
            let received;
            assert.equal(await api.apiRequest('/test', {}, (message, details) => { received = { message, details }; }), null);
            assert.deepEqual(received, { message: 'Rejected', details: { error: 'Rejected', code: 'REQUEST_REJECTED', failed_gates: ['example_gate'] } });
        } finally { globalThis.fetch = originalFetch; }
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('dashboard cards render Alpaca bid and ask values in USD', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /data-live-bid/);
    assert.match(dashboard, /data-live-ask/);
    assert.match(dashboard, /value === null \|\| value === undefined \|\| value === ''/);
    assert.match(dashboard, /formatMoney\(quote\.bid_usd/);
    assert.match(dashboard, /formatMoney\(quote\.ask_usd/);
    assert.match(dashboard, /Alpaca market data/);
    assert.doesNotMatch(dashboard, /Handelsrepublik|retrySdkAuth|Trade Republic/);
});

test('dashboard cards show the Journal buy-in and a live current-price percentage', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /Buy-in:/);
    assert.match(dashboard, /data-buy-in-price/);
    assert.match(dashboard, /data-live-price-change/);
    assert.match(dashboard, /current \/ buyIn - 1/);
    assert.match(dashboard, /history-change \$\{formatted\.className\}/);
});

test('Configure page manages Alpaca alert levels without a broker configuration path', () => {
    const root = path.join(__dirname, '..', 'www');
    const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const config = fs.readFileSync(path.join(root, 'modules', 'config.js'), 'utf8');

    assert.match(index, /data-page="config"[\s\S]*Configure/);
    assert.match(index, /id="page-config"/);
    assert.match(app, /modules\/config\.js/);
    assert.doesNotMatch(app, /broker-profile|trade_republic/);
    assert.doesNotMatch(config, /trade_republic|\/stocks|instruments\/resolve/);
    assert.match(config, /\/alpaca\/alert-levels\/\$\{encodeURIComponent\(levelId\)\}\/order/);
});

test('dashboard exposes Alpaca alert rearming and Journal trade rows remain read-only', () => {
    const root = path.join(__dirname, '..', 'www');
    const dashboard = fs.readFileSync(path.join(root, 'modules', 'dashboard.js'), 'utf8');
    const journal = fs.readFileSync(path.join(root, 'modules', 'journal.js'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');
    assert.doesNotMatch(dashboard, /data-alpaca-rearm|Rearm<\/button>/);
    assert.match(dashboard, /class="threshold disabled alert-link" href="#\/config\?stock=\$\{escapeHtml\(stock\.id\)\}&level=\$\{escapeHtml\(level\.id\)\}"/);
    assert.match(app, /af\('\/alpaca\/journal'\)/);
    assert.doesNotMatch(app, /broker-profile/);
    assert.doesNotMatch(dashboard, /data-stock-action="configureAlerts"/);
    assert.doesNotMatch(journal, /Broker confirmed · read only/);
    assert.doesNotMatch(journal, /external activity or history the broker cannot provide/);
    assert.doesNotMatch(journal, /data-trade-action|trade-form|journal-trade-editor/);
    assert.doesNotMatch(journal, /journal-definitions|Definitions: Net P\/L/);
    assert.match(journal, /collapseJournalCardByTitle\(container, 'Trades'/);
    assert.match(journal, /collapseJournalCardByTitle\(container, 'Per-Instrument Analytics'/);
    assert.match(styles, /\.history-popover\{[^}]*z-index:30;isolation:isolate;overflow:hidden;/);
});

test('Dashboard refreshes only open history panels once per minute while visible', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /const chartRefreshMs = 60000/);
    assert.match(dashboard, /window\.setInterval\(refreshOpenCharts, chartRefreshMs\)/);
    assert.match(dashboard, /refreshOpenHistoryPanels\(document\.getElementById\('page-dashboard'\)\)/);
    assert.match(dashboard, /document\.hidden/);
    assert.match(dashboard, /window\.clearInterval\(chartRefreshTimer\)/);
});

test('broker Trading screen and order UI are removed', () => {
    const root = path.join(__dirname, '..', 'www');
    const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

    assert.doesNotMatch(index, /href="#\/trading"/);
    assert.doesNotMatch(index, /id="page-trading"/);
    assert.doesNotMatch(app, /ENABLE_TRADING|modules\/trading/);
    assert.equal(fs.existsSync(path.join(root, 'modules', 'trading.js')), false);
});

test('Journal loads read-only Alpaca account activity', () => {
    const app = fs.readFileSync(path.join(__dirname, '..', 'www', 'app.js'), 'utf8');
    assert.match(app, /af\('\/alpaca\/journal'\)/);
});

test('Journal displays instrument and quantity with trades without exposing trade IDs or a dividends column', () => {
    const journal = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'journal.js'), 'utf8');
    assert.match(journal, /<th>Trade No\.<\/th><th>Opened<\/th><th>Status<\/th><th>Instrument<\/th><th>Quantity<\/th><th>Buy-in Price<\/th><th>Sell Price<\/th><th>Net P\/L<\/th><th>Return<\/th>/);
    assert.doesNotMatch(journal, /<th>Trade ID<\/th>/);
    assert.doesNotMatch(journal, /dividendHeader|tradeHeaders|<th>Dividends<\/th>/);
    assert.match(journal, /trade\.trade_number/);
    assert.match(journal, /journalTradeOpenedDate\(trade\.opened_at\)/);
    assert.match(journal, /journalTradePrice\(trade\.entry_price_usd, trade\.currency, trade\.asset_type\)/);
    assert.match(journal, /journalTradePrice\(trade\.exit_price_usd, trade\.currency, trade\.asset_type\)/);
});

test('notifications use a fixed toast layer so they never move the page', () => {
    const root = path.join(__dirname, '..', 'www');
    const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const config = fs.readFileSync(path.join(root, 'modules', 'config.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');
    assert.match(index, /<div id="toast-layer" class="toast-layer"/);
    assert.match(app, /document\.getElementById\('toast-layer'\)/);
    assert.doesNotMatch(app, /config-messages|containerId/);
    assert.doesNotMatch(config, /config-messages/);
    assert.match(styles, /\.toast-layer\{position:fixed;/);
});

test('equity and options charts share the 04:00–20:00 ET cut, time-based x and local-time labels', () => {
    const chart = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'algo-trader', 'equity-chart.js'), 'utf8');
    const journal = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'journal.js'), 'utf8');
    assert.match(chart, /const RANGE_SPAN_MS = \{ '1W': 7 \* DAY_MS/);
    assert.match(chart, /function fraction\(time, start, span\)/);
    assert.match(chart, /marketDay = new Intl\.DateTimeFormat\('en-CA', \{ timeZone: 'America\/New_York'/);
    assert.match(chart, /const session = sessionWindow\(new Date\(time\)\);/);
    assert.match(chart, /lineSegments/);
    assert.doesNotMatch(chart, /timeZone: '(?!America\/New_York)/);
    assert.doesNotMatch(journal.match(/function journalDayMonthLabel[^}]*}/)[0], /Europe\/Berlin/);
});

test('standalone PWA supports pull-to-refresh and refreshes its cached entry assets', () => {
    const root = path.join(__dirname, '..', 'www');
    const version = require('../package.json').version.replaceAll('.', '\\.');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');
    assert.match(app, /display-mode: standalone/);
    assert.match(app, /touchstart/);
    assert.match(app, /Release to refresh/);
    assert.match(app, /window\.location\.reload\(\)/);
    assert.match(index, /id="pull-refresh-indicator"/);
    assert.match(styles, /\.pwa-standalone\{overscroll-behavior-y:contain\}/);
});
