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
        const hostile = '<img src=x onerror="alert(1)">';
        assert.equal(escape.escapeHtml(hostile).includes('<img'), false);
        for (const module of ['charts', 'dashboard', 'config', 'trading', 'journal']) {
            await import(pathToFileURL(path.join(tempDir, 'modules', `${module}.js`)).href);
        }
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});
