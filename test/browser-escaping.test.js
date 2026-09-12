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
