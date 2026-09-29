const test = require('node:test');
const assert = require('node:assert/strict');
const express = require('express');
const { createOwnerAuth } = require('../lib/owner-auth');

async function fixture() {
    const auth = createOwnerAuth({ ownerToken: 'x'.repeat(32), now: () => Date.now() });
    const app = express();
    app.use(express.json());
    app.post('/login', (req, res) => auth.login(req, res));
    app.use('/protected', auth.requireOwner);
    app.get('/protected/read', (req, res) => res.json({ ok: true }));
    app.post('/protected/write', (req, res) => res.json({ ok: true }));
    app.post('/protected/logout', (req, res) => auth.logout(req, res));
    const server = await new Promise(resolve => { const value = app.listen(0, '127.0.0.1', () => resolve(value)); });
    const base = `http://127.0.0.1:${server.address().port}`;
    return { server, request: (path, options = {}) => fetch(base + path, options) };
}

test('owner login requires the configured credential and issues an HttpOnly strict cookie', async t => {
    const f = await fixture();
    t.after(() => f.server.close());
    const rejected = await f.request('/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: 'wrong' }) });
    assert.equal(rejected.status, 401);
    const accepted = await f.request('/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: 'x'.repeat(32) }) });
    assert.equal(accepted.status, 200);
    assert.match(accepted.headers.get('set-cookie'), /HttpOnly/);
    assert.match(accepted.headers.get('set-cookie'), /Secure/);
    assert.match(accepted.headers.get('set-cookie'), /SameSite=Strict/);
    assert.match(accepted.headers.get('set-cookie'), /Max-Age=86400/);
    assert.equal((await accepted.json()).csrfToken.length > 30, true);
});

test('owner session expires after the configured one-day default lifetime', async t => {
    let time = Date.parse('2026-09-29T08:00:00Z');
    const auth = createOwnerAuth({ ownerToken: 'x'.repeat(32), now: () => time });
    const app = express();
    app.use(express.json());
    app.post('/login', (req, res) => auth.login(req, res));
    app.use('/protected', auth.requireOwner);
    app.get('/protected/read', (req, res) => res.json({ ok: true }));
    const server = await new Promise(resolve => { const value = app.listen(0, '127.0.0.1', () => resolve(value)); });
    t.after(() => server.close());
    const base = `http://127.0.0.1:${server.address().port}`;
    const login = await fetch(`${base}/login`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: 'x'.repeat(32) }) });
    const cookie = login.headers.get('set-cookie').split(';')[0];
    time += 24 * 60 * 60 * 1000 - 1;
    assert.equal((await fetch(`${base}/protected/read`, { headers: { cookie } })).status, 200);
    time += 1;
    assert.equal((await fetch(`${base}/protected/read`, { headers: { cookie } })).status, 401);
});

test('protected reads require a session and writes require same origin plus the session CSRF token', async t => {
    const f = await fixture();
    t.after(() => f.server.close());
    assert.equal((await f.request('/protected/read')).status, 401);
    const login = await f.request('/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: 'x'.repeat(32) }) });
    const sessionCookie = login.headers.get('set-cookie').split(';')[0];
    const { csrfToken } = await login.json();
    assert.equal((await f.request('/protected/read', { headers: { cookie: sessionCookie } })).status, 200);
    assert.equal((await f.request('/protected/write', { method: 'POST', headers: { cookie: sessionCookie, origin: 'http://evil.invalid', 'x-csrf-token': csrfToken } })).status, 403);
    assert.equal((await f.request('/protected/write', { method: 'POST', headers: { cookie: sessionCookie, origin: 'http://127.0.0.1', 'x-csrf-token': 'wrong' } })).status, 403);
    assert.equal((await f.request('/protected/write', { method: 'POST', headers: { cookie: sessionCookie, origin: `http://127.0.0.1:${f.server.address().port}`, 'x-csrf-token': csrfToken } })).status, 200);
});

test('owner authentication is unavailable without a sufficiently long configured credential', () => {
    assert.equal(createOwnerAuth({ ownerToken: 'too-short' }), null);
});
