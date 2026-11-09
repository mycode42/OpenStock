const test = require('node:test');
const assert = require('node:assert/strict');
const { createRateLimiter } = require('../server/rate-limit');

function fakeResponse() {
    const headers = {};
    return {
        headers,
        statusCode: 200,
        body: null,
        set(name, value) { headers[name] = value; return this; },
        status(code) { this.statusCode = code; return this; },
        json(body) { this.body = body; return this; },
    };
}

test('the limiter lets a client through up to the limit, refuses the rest, and resets after the window', () => {
    let clock = 0;
    const limit = createRateLimiter({ windowMs: 1000, max: 3, now: () => clock });
    const req = { ip: '203.0.113.7' };
    const results = [];
    for (let i = 0; i < 5; i += 1) {
        const res = fakeResponse();
        let passed = false;
        limit(req, res, () => { passed = true; });
        results.push(passed ? 'next' : res.statusCode);
    }
    assert.deepEqual(results, ['next', 'next', 'next', 429, 429]);
    clock = 1500;
    const later = fakeResponse();
    let passed = false;
    limit(req, later, () => { passed = true; });
    assert.equal(passed, true);
});

test('each client has its own window', () => {
    const limit = createRateLimiter({ windowMs: 1000, max: 1, now: () => 0 });
    let first = false;
    limit({ ip: 'a' }, fakeResponse(), () => { first = true; });
    let second = false;
    limit({ ip: 'b' }, fakeResponse(), () => { second = true; });
    assert.equal(first && second, true);
});

test('a client that changes its forwarded address still counts against its socket address', () => {
    const socketKey = req => req.socket.remoteAddress;
    const limit = createRateLimiter({ windowMs: 1000, max: 2, now: () => 0, keyOf: socketKey });
    const results = [];
    for (const forwarded of ['1.1.1.1', '2.2.2.2', '3.3.3.3']) {
        const res = fakeResponse();
        let passed = false;
        limit({ ip: forwarded, socket: { remoteAddress: '127.0.0.1' } }, res, () => { passed = true; });
        results.push(passed ? 'next' : res.statusCode);
    }
    assert.deepEqual(results, ['next', 'next', 429]);
});

test('visitors behind the tunnel are told apart by their forwarded visitor address', () => {
    const visitorOf = req => req.get('cf-connecting-ip') || req.ip;
    const limit = createRateLimiter({ windowMs: 1000, max: 1, now: () => 0, keyOf: visitorOf });
    const seen = [];
    for (const visitor of ['203.0.113.1', '203.0.113.2']) {
        const req = { ip: '127.0.0.1', get: name => (name === 'cf-connecting-ip' ? visitor : undefined) };
        let passed = false;
        limit(req, fakeResponse(), () => { passed = true; });
        seen.push(passed);
    }
    assert.deepEqual(seen, [true, true]);
});
