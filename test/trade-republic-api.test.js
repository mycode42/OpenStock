'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp } = require('../server');

test('public app exposes Alpaca only and has no broker profile switch', async t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-public-brokers-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    const app = createApp({ db, demoMode: true });
    const server = await new Promise(resolve => {
        const listener = app.listen(0, '127.0.0.1', () => resolve(listener));
    });
    const request = url => fetch(`http://127.0.0.1:${server.address().port}${url}`);
    t.after(() => { server.close(); db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });

    for (const url of [
        '/api/broker-profile',
        '/api/broker/trade-republic/health',
        '/api/broker/trade-republic/accounts',
        '/api/broker/trade-republic/portfolio',
    ]) {
        assert.equal((await request(url)).status, 404, url);
    }
});
