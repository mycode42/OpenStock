'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EXIT, acquireLock, safeError, sendTelegram } = require('../lib/monitor');

test('shared monitor exit codes and error text stay stable for the Alpaca monitor', () => {
    assert.deepEqual(EXIT, { OK: 0, PARTIAL: 1, CONFIG: 2, LOCKED: 4 });
    assert.equal(safeError(new Error('request to bot123:SECRET failed\nretry')), 'request to bot[redacted] failed retry');
});

test('Telegram request encodes its message and returns the delivery id', async () => {
    let request;
    const id = await sendTelegram('hello', { token: 'test-token', chatId: '42', fetchImpl: async (url, options) => {
        request = { url, options };
        return { ok: true, json: async () => ({ ok: true, result: { message_id: 7 } }) };
    } });
    assert.equal(id, 7);
    assert.match(request.url, /sendMessage$/);
    assert.match(request.options.body.toString(), /chat_id=42/);
});

test('single-run lock is exclusive and releasable', t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-lock-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const lock = path.join(dir, 'lock');
    const release = acquireLock(lock);
    assert.ok(release);
    assert.equal(acquireLock(lock), null);
    release();
    assert.ok(acquireLock(lock));
});
