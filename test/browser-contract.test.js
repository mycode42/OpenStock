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
        for (const module of ['charts', 'dashboard', 'config', 'trading']) {
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
            globalThis.fetch = async () => ({ ok: false, json: async () => ({ error: 'Rejected', code: 'ORDER_SAFETY_GATE', failed_gates: ['account_selected'] }) });
            let received;
            assert.equal(await api.apiRequest('/test', {}, (message, details) => { received = { message, details }; }), null);
            assert.deepEqual(received, { message: 'Rejected', details: { error: 'Rejected', code: 'ORDER_SAFETY_GATE', failed_gates: ['account_selected'] } });
        } finally { globalThis.fetch = originalFetch; }
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('dashboard cards retain live bid and ask placeholders for broker quotes', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /data-live-bid/);
    assert.match(dashboard, /data-live-ask/);
    assert.match(dashboard, /value === null \|\| value === undefined \|\| value === ''/);
    assert.match(dashboard, /quoteMoney\(quote\.bid_eur\)/);
    assert.match(dashboard, /quoteMoney\(quote\.ask_eur\)/);
    assert.match(dashboard, /Handelsrepublik SDK/);
    assert.match(dashboard, /retrySdkAuthOnNextPoll/);
    assert.match(dashboard, /live-prices\?retrySdkAuth=1/);
});

test('dashboard cards show the Journal buy-in and a live current-price percentage', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /Buy-in:/);
    assert.match(dashboard, /data-buy-in-price/);
    assert.match(dashboard, /data-live-price-change/);
    assert.match(dashboard, /current \/ buyIn - 1/);
    assert.match(dashboard, /history-change \$\{formatted\.className\}/);
});

test('Configure polls display-only live prices only while its page is active', () => {
    const config = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'config.js'), 'utf8');
    const app = fs.readFileSync(path.join(__dirname, '..', 'www', 'app.js'), 'utf8');

    assert.match(config, /const livePollMs = 1000/);
    assert.match(config, /window\.location\.hash !== '#\/config'/);
    assert.match(config, /af\('\/live-prices'/);
    assert.match(config, /data-live-price/);
    assert.match(config, /stopLivePolling/);
    assert.match(app, /if\(page!=='config'\)config\.stopLivePolling\(\)/);
});

test('dashboard links to alert configuration and Journal marks broker rows read-only', () => {
    const root = path.join(__dirname, '..', 'www');
    const dashboard = fs.readFileSync(path.join(root, 'modules', 'dashboard.js'), 'utf8');
    const config = fs.readFileSync(path.join(root, 'modules', 'config.js'), 'utf8');
    const journal = fs.readFileSync(path.join(root, 'modules', 'journal.js'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');
    assert.match(dashboard, /data-stock-action="configureAlerts"/);
    assert.match(config, /function openAlerts\(stockId\)/);
    assert.match(app, /configureAlerts: config\.openAlerts/);
    assert.match(config, /data-alert-action="add-percent"/);
    assert.match(journal, /Broker confirmed · read only/);
    assert.match(journal, /external activity or history the broker cannot provide/);
    assert.match(styles, /\.history-popover\{[^}]*z-index:30;isolation:isolate;overflow:hidden;/);
    assert.match(styles, /\.dashboard-alert-link\{[^}]*z-index:0;/);
});

test('Dashboard refreshes only open history panels once per minute while visible', () => {
    const dashboard = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'dashboard.js'), 'utf8');
    assert.match(dashboard, /const chartRefreshMs = 60000/);
    assert.match(dashboard, /window\.setInterval\(refreshOpenCharts, chartRefreshMs\)/);
    assert.match(dashboard, /refreshOpenHistoryPanels\(document\.getElementById\('page-dashboard'\)\)/);
    assert.match(dashboard, /document\.hidden/);
    assert.match(dashboard, /window\.clearInterval\(chartRefreshTimer\)/);
});

test('broker Trading screen is not exposed while it is disabled', () => {
    const root = path.join(__dirname, '..', 'www');
    const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const serviceWorker = fs.readFileSync(path.join(root, 'sw.js'), 'utf8');

    assert.doesNotMatch(index, /href="#\/trading"/);
    assert.doesNotMatch(index, /id="page-trading"/);
    assert.match(app, /const ENABLE_TRADING = false/);
    assert.doesNotMatch(serviceWorker, /\.\/modules\/trading\.js/);
});

test('Journal requests fifteen trades per page', () => {
    const app = fs.readFileSync(path.join(__dirname, '..', 'www', 'app.js'), 'utf8');
    assert.match(app, /trades\?page=\$\{requestedPage\}&page_size=15/);
});

test('Journal displays instrument and quantity with trades without exposing trade IDs or a dividends column', () => {
    const journal = fs.readFileSync(path.join(__dirname, '..', 'www', 'modules', 'journal.js'), 'utf8');
    assert.match(journal, /<th>Trade No\.<\/th><th>Opened<\/th><th>Status<\/th><th>Instrument<\/th><th>Quantity<\/th><th>Buy-in Price<\/th><th>Sell Price<\/th><th>Net P\/L<\/th><th>Return<\/th><th><\/th>/);
    assert.doesNotMatch(journal, /<th>Trade ID<\/th>/);
    assert.doesNotMatch(journal, /dividendHeader|tradeHeaders|<th>Dividends<\/th>/);
    assert.match(journal, /trade\.trade_number/);
    assert.match(journal, /journalTradeOpenedDate\(trade\.opened_at\)/);
    assert.match(journal, /journalTradePrice\(trade\.entry_price, trade\.currency, trade\.asset_type\)/);
    assert.match(journal, /journalTradePrice\(trade\.exit_price, trade\.currency, trade\.asset_type\)/);
});
