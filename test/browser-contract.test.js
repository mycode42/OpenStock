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
        for (const module of ['charts', 'dashboard', 'config', 'trading', 'journal']) {
            await import(pathToFileURL(path.join(tempDir, 'modules', `${module}.js`)).href);
        }
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
