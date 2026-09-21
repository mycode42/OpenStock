'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { QuoteService } = require('../lib/quote-service');

const stock = { id: 1, symbol: 'TEST', price_source: 'trade_republic', mapping: { isin: 'US0000000001', exchange: 'TIB', currency: 'EUR', verified_at: '2026-01-01T00:00:00Z' } };

test('sidecar quotes are primary and preserve executable bid and ask without starting pytr', async () => {
    let pytrCalls = 0;
    const service = new QuoteService({ db: {}, brokerClient: { getQuote: async () => ({ bid_eur: 10, ask_eur: 12, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR', bid_time: '2026-01-01T12:00:00Z', ask_time: '2026-01-01T12:00:00Z', observed_at: '2026-01-01T12:00:00Z' }) }, pytrClient: { getQuotes: async () => { pytrCalls += 1; return new Map(); } }, now: () => new Date('2026-01-01T12:00:00Z') });
    const [result] = await service.fetchMany([stock], { pytrFallback: true });
    assert.equal(pytrCalls, 0);
    assert.equal(result.quote.source, 'trade_republic_order_price');
    assert.equal(result.quote.bidEur, 10);
    assert.equal(result.quote.askEur, 12);
    assert.equal(result.quote.priceEur, 11);
});

test('pytr is queried only after a sidecar quote failure and before Yahoo', async () => {
    let pytrCalls = 0;
    const service = new QuoteService({ db: {}, brokerClient: { getQuote: async () => { throw new Error('offline'); } }, pytrClient: { getQuotes: async isins => { pytrCalls += 1; assert.deepEqual(isins, [stock.mapping.isin]); return new Map([[stock.mapping.isin, { lastEur: 11, askEur: 11.2, quoteTime: '2026-01-01T12:00:00Z', observedAt: '2026-01-01T12:00:00Z', source: 'pytr_rates' }]]); } }, fetchImpl: async () => { throw new Error('Yahoo must not be called'); }, config: { fx: {} }, now: () => new Date('2026-01-01T12:00:00Z') });
    const [result] = await service.fetchMany([stock], { pytrFallback: true });
    assert.equal(pytrCalls, 1);
    assert.equal(result.quote.source, 'pytr_rates');
    assert.equal(result.quote.bidEur, null);
    assert.equal(result.quote.askEur, 11.2);
});
