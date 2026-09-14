const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

test('shared browser escaping prevents hostile text and quote breakout', async () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'www/modules/escape.js'), 'utf8');
    const { escapeHtml, escapeAttribute } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    const hostile = '<img src=x onerror="alert(1)">\' quote';
    const encoded = escapeHtml(hostile);
    assert.equal(encoded.includes('<img'), false);
    assert.equal(encoded.includes('onerror="'), false);
    assert.equal(escapeAttribute(hostile), encoded);
    assert.match(encoded, /&lt;img/);
    assert.match(encoded, /&quot;alert/);
});

test('dynamic browser templates route text through the shared escaping module', () => {
    const root = path.join(__dirname, '..', 'www/modules');
    for (const file of ['dashboard.js', 'config.js', 'journal.js']) {
        const source = fs.readFileSync(path.join(root, file), 'utf8');
        assert.match(source, /escapeHtml/);
    }
});

test('dashboard leaves stock management controls in Configure', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'www/modules/dashboard.js'), 'utf8');
    assert.doesNotMatch(source, /data-stock-action="toggle"/);
    assert.doesNotMatch(source, /data-stock-action="remove"/);
    assert.doesNotMatch(source, />Disable<|>Remove</);
});

test('dashboard only shows position value for held stocks and nests USD beside EUR', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'www/modules/dashboard.js'), 'utf8');
    assert.match(source, /const positionValue = quantity \* Number\(stock\.current_price_eur \|\| 0\)/);
    assert.match(source, /position\.owned[\s\S]*Position value:/);
    assert.match(source, /stock-price-usd">\(\$\$\{money\(usdPrice\)\}\)<\/span>/);
    assert.doesNotMatch(source, /Position: \$\{money\(position\.quantity\)\}/);
});

test('browser modules link without missing named exports', async () => {
    const sourceDir = path.join(__dirname, '..', 'www/modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-browser-modules-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });

    try {
        await import(pathToFileURL(path.join(tempDir, 'modules/charts.js')).href);
        await import(pathToFileURL(path.join(tempDir, 'modules/dashboard.js')).href);
        await import(pathToFileURL(path.join(tempDir, 'modules/config.js')).href);
        await import(pathToFileURL(path.join(tempDir, 'modules/journal.js')).href);
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('journal edit workflow normalizes numeric data IDs and uses visible accessible controls', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'www/modules/journal.js'), 'utf8');
    assert.match(source, /String\(trade\.id\) === String\(editingTradeId\)/);
    assert.match(source, /scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\)/);
    assert.match(source, /data-trade-action="cancel"/);
    assert.match(source, /id="trade-form"/);
    assert.match(source, /editingIntent = 'close'/);
    assert.match(source, /`\/trades\/\$\{editingTradeId\}\/close`/);
    assert.match(source, /step="1"/);
    assert.match(source, /merge into an existing open position.*weighted average/);
    assert.doesNotMatch(source, /\bonclick=|\bonsubmit=/);
});

test('configuration uses a compact single-stock alert editor without legacy ownership controls', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'www/modules/config.js'), 'utf8');
    assert.match(source, /expandedStockId/);
    assert.match(source, /Manage alerts/);
    assert.match(source, /Triggered · ready to rearm/);
    assert.match(source, /open one stock at a time/);
    assert.doesNotMatch(source, /Mark Owned|Mark Sold|\btos\b|data-stock-action="owned"/);
});

test('browser pages do not emit inline event handlers', () => {
    const root = path.join(__dirname, '..', 'www');
    for (const file of ['app.js', 'modules/dashboard.js', 'modules/config.js', 'modules/journal.js']) {
        assert.doesNotMatch(fs.readFileSync(path.join(root, file), 'utf8'), /\bonclick=|\bonsubmit=/, file);
    }
    const dashboard = fs.readFileSync(path.join(root, 'modules/dashboard.js'), 'utf8');
    assert.match(dashboard, /const \{[^}]*\bnav\b[^}]*\} = context;/s);
});
