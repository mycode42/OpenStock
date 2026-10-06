'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { QuoteService } = require('../lib/quote-service');

const stock = { id: 1, symbol: 'TEST', mapping: { isin: 'US0000000001', exchange: 'TIB', currency: 'EUR', verified_at: '2026-01-01T00:00:00Z' } };
const quote = { bid_eur: 1.234567, ask_eur: 1.234568, bid_unit: 'EUR', ask_unit: 'EUR', currency: 'EUR', observed_at: '2026-01-01T12:00:00Z' };

test('SDK is primary and retains six-decimal executable prices', async () => {
    let sidecarCalls = 0;
    const service = new QuoteService({
        db: {},
        sdkClient: { isEnabled: () => true, getQuote: async () => quote },
        brokerClient: { getQuote: async () => { sidecarCalls += 1; throw new Error('must not be called'); } },
        now: () => new Date('2026-01-01T12:00:00Z')
    });

    const [row] = await service.fetchMany([stock]);
    assert.equal(row.quote.source, 'trade_republic_sdk_order_price');
    assert.equal(row.quote.bidEur, 1.234567);
    assert.equal(row.quote.askEur, 1.234568);
    assert.equal(row.quote.priceEur, 1.2345675);
    assert.equal(sidecarCalls, 0);
});

test('verified EUR crypto mappings use the SDK executable quote path', async () => {
    let request;
    const service = new QuoteService({
        db: {},
        sdkClient: { isEnabled: () => true, getQuote: async value => { request = value; return quote; } },
        now: () => new Date('2026-01-01T12:00:00Z')
    });
    const [row] = await service.fetchMany([{
        id: 2, symbol: 'ETH', asset_type: 'crypto',
        mapping: { isin: 'XF000ETH0019', exchange: 'BHS', currency: 'EUR', verified_at: '2026-01-01T00:00:00Z' }
    }]);
    assert.equal(request.isin, 'XF000ETH0019');
    assert.equal(request.exchange, 'BHS');
    assert.equal(row.quote.source, 'trade_republic_sdk_order_price');
    assert.equal(row.quote.priceEur, 1.2345675);
});

test('explicit refresh requests bypass an SDK authentication cooldown', async () => {
    let forceAuthRetry;
    const service = new QuoteService({ db: {}, sdkClient: { isEnabled: () => true, getQuote: async request => { forceAuthRetry = request.forceAuthRetry; return quote; } } });
    const [row] = await service.fetchMany([stock], { forceSdkAuthRetry: true });
    assert.equal(forceAuthRetry, true);
    assert.equal(row.quote.source, 'trade_republic_sdk_order_price');
});

test('UAPI sidecar is used when SDK is disabled or fails', async t => {
    const events = [];
    const service = new QuoteService({
        db: {},
        sdkClient: { isEnabled: () => true, getQuote: async () => { events.push('sdk'); throw new Error('SDK unavailable'); } },
        brokerClient: { getQuote: async request => { events.push('uapi'); assert.ok(request.timeoutMs > 0); return { ...quote, observed_at: '2026-01-01T12:00:01Z' }; } },
        now: () => new Date('2026-01-01T12:00:00Z'),
        log() {}
    });

    const [row] = await service.fetchMany([stock], { deadlineAt: Date.now() + 1000 });
    assert.deepEqual(events, ['sdk', 'uapi']);
    assert.equal(row.quote.source, 'trade_republic_order_price');
    assert.equal(row.quote.bidEur, 1.234567);
    assert.equal(row.quote.askEur, 1.234568);
});

test('a stale sidecar health failure does not suppress the SDK fallback path', async () => {
    const events = [];
    const service = new QuoteService({
        db: {},
        sdkClient: { isEnabled: () => true, getQuote: async () => { events.push('sdk'); throw new Error('SDK unavailable'); } },
        brokerClient: { getQuote: async () => { events.push('uapi'); return quote; } },
        log() {}
    });
    const [row] = await service.fetchMany([stock], { brokerOnline: false });
    assert.deepEqual(events, ['sdk', 'uapi']);
    assert.equal(row.quote.source, 'trade_republic_order_price');
});

test('provider failures stay isolated per stock and missing mappings fail closed', async () => {
    let calls = 0;
    const service = new QuoteService({
        db: {},
        sdkClient: { isEnabled: () => false },
        brokerClient: { getQuote: async request => {
            calls += 1;
            if (request.isin === 'US0000000002') throw new Error('sidecar offline');
            return quote;
        } },
        log() {}
    });
    const rows = await service.fetchMany([stock, { ...stock, id: 2, mapping: { ...stock.mapping, isin: 'US0000000002' } }, { id: 3, symbol: 'UNMAPPED' }]);
    assert.equal(rows[0].quote.bidEur, 1.234567);
    assert.match(rows[1].error.message, /sidecar offline/);
    assert.match(rows[2].error.message, /verified EUR Trade Republic mapping unavailable/);
    assert.equal(calls, 2);
});

test('quote workers honor the configured concurrency and cycle deadline', async () => {
    const service = new QuoteService({ db: {} });
    let active = 0, maximum = 0;
    service.fetch = async item => {
        active += 1;
        maximum = Math.max(maximum, active);
        await new Promise(resolve => setTimeout(resolve, 5));
        active -= 1;
        return { priceEur: item.id };
    };
    const rows = await service.fetchMany(Array.from({ length: 6 }, (_, id) => ({ id })), { concurrency: 2 });
    assert.equal(maximum, 2);
    assert.deepEqual(rows.map(row => row.quote.priceEur), [0, 1, 2, 3, 4, 5]);

    let calls = 0;
    service.fetch = async () => { calls += 1; };
    const [expired] = await service.fetchMany([{ id: 7 }], { deadlineAt: Date.now() - 1 });
    assert.equal(calls, 0);
    assert.match(expired.error.message, /deadline/);
});
