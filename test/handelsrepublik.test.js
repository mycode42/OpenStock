'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { HandelsrepublikQuoteClient } = require('../lib/brokers/handelsrepublik');

test('SDK adapter is disabled by default and does not import the SDK', async () => {
  let imported = 0;
  const client = new HandelsrepublikQuoteClient({ env: {}, importer: async () => { imported += 1; } });
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /disabled/);
  assert.equal(imported, 0);
});

test('SDK adapter produces executable EUR bid/ask from separate sell and buy reads', async () => {
  let refreshed = 0, closed = 0, calls = [];
  const module = {
    FileSessionStore: class { constructor(file) { this.file = file; } },
    TradeRepublicClient: { create: ({ sessionStore }) => ({
      auth: { restoreSession: async () => ({ id: 'session' }), refreshSession: async () => { refreshed += 1; } },
      trading: { priceForOrder: async options => { calls.push(options); return options.side === 'sell' ? { bid: 9.9, unit: 'EUR', time: '2026-01-01T12:00:00Z' } : { ask: 10.1, unit: 'EUR', time: '2026-01-01T12:00:01Z' }; } },
      close: () => { closed += 1; }, sessionStore
    }) }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true' }, sessionFile: '/private/session.json', importer: async () => module });
  const quote = await client.getQuote({ isin: 'US0000000001', exchange: 'TIB' });
  assert.equal(refreshed, 1);
  assert.deepEqual(calls.map(call => call.side), ['sell', 'buy']);
  assert.deepEqual({ bid: quote.bid_eur, ask: quote.ask_eur, source: quote.source }, { bid: 9.9, ask: 10.1, source: 'trade_republic_sdk_order_price' });
  client.close();
  assert.equal(closed, 1);
});

test('SDK adapter rejects a missing saved session before quote calls', async () => {
  let quoteCalls = 0;
  const module = { FileSessionStore: class {}, TradeRepublicClient: { create: () => ({ auth: { restoreSession: async () => undefined }, trading: { priceForOrder: async () => { quoteCalls += 1; } }, close() {} }) } };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true' }, sessionFile: '/private/session.json', importer: async () => module });
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /SDK quote source unavailable/);
  assert.equal(quoteCalls, 0);
});
