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
