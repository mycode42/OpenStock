'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { HandelsrepublikQuoteClient } = require('../lib/brokers/handelsrepublik');

test('SDK adapter is enabled by default but does not import without a private session path', async () => {
  let imported = 0;
  const client = new HandelsrepublikQuoteClient({ env: {}, importer: async () => { imported += 1; } });
  assert.equal(client.isEnabled(), true);
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /session file is not configured/);
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

test('SDK adapter shares one session handshake across concurrent quotes while quote reads remain parallel', async () => {
  let imports = 0, created = 0, restored = 0, refreshed = 0, quoteCalls = 0, releaseRestore;
  const restoreGate = new Promise(resolve => { releaseRestore = resolve; });
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => {
      created += 1;
      return {
        auth: { restoreSession: async () => { restored += 1; await restoreGate; return { id: 'session' }; }, refreshSession: async () => { refreshed += 1; } },
        trading: { priceForOrder: async options => { quoteCalls += 1; return options.side === 'sell' ? { bid: 10, unit: 'EUR' } : { ask: 10.2, unit: 'EUR' }; } },
        close() {}
      };
    } }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true' }, sessionFile: '/private/session.json', importer: async () => { imports += 1; return module; } });
  const quotes = [
    client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }),
    client.getQuote({ isin: 'US0000000002', exchange: 'TIB' }),
    client.getQuote({ isin: 'US0000000003', exchange: 'TIB' })
  ];
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual({ imports, created, restored, refreshed, quoteCalls }, { imports: 1, created: 1, restored: 1, refreshed: 0, quoteCalls: 0 });
  releaseRestore();
  const values = await Promise.all(quotes);
  assert.equal(refreshed, 1);
  assert.equal(quoteCalls, 6);
  assert.deepEqual(values.map(value => value.source), Array(3).fill('trade_republic_sdk_order_price'));
});

test('SDK adapter reconnects once after concurrent expired-session quote failures', async () => {
  let created = 0, restored = 0, refreshed = 0, firstClientCalls = 0, replacementCalls = 0, closed = 0;
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => {
      created += 1;
      const expired = created === 1;
      return {
        auth: { restoreSession: async () => { restored += 1; return { id: `session-${created}` }; }, refreshSession: async () => { refreshed += 1; } },
        trading: { priceForOrder: async options => {
          if (expired) { firstClientCalls += 1; throw new Error('expired session'); }
          replacementCalls += 1;
          return options.side === 'sell' ? { bid: 10, unit: 'EUR' } : { ask: 10.2, unit: 'EUR' };
        } },
        close: () => { closed += 1; }
      };
    } }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true' }, sessionFile: '/private/session.json', importer: async () => module });
  const quotes = await Promise.all([
    client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }),
    client.getQuote({ isin: 'US0000000002', exchange: 'TIB' }),
    client.getQuote({ isin: 'US0000000003', exchange: 'TIB' })
  ]);
  assert.equal(created, 2);
  assert.equal(restored, 2);
  assert.equal(refreshed, 2);
  assert.equal(firstClientCalls, 6);
  assert.equal(replacementCalls, 6);
  assert.equal(closed, 1);
  assert.deepEqual(quotes.map(quote => quote.source), Array(3).fill('trade_republic_sdk_order_price'));
});

test('SDK adapter rejects a missing saved session before quote calls', async () => {
  let quoteCalls = 0;
  const module = { FileSessionStore: class {}, TradeRepublicClient: { create: () => ({ auth: { restoreSession: async () => undefined }, trading: { priceForOrder: async () => { quoteCalls += 1; } }, close() {} }) } };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true' }, sessionFile: '/private/session.json', importer: async () => module });
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /SDK quote source unavailable/);
  assert.equal(quoteCalls, 0);
});
