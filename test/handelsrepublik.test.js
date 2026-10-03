'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
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

test('SDK adapter starts PIN reauthentication when no saved session exists', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'handelsrepublik-auth-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  let login;
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => ({
      auth: {
        restoreSession: async () => undefined,
        loginWithPin: async options => { login = options; return { id: 'new-session' }; }
      },
      trading: { priceForOrder: async options => options.side === 'sell' ? { bid: 10, unit: 'EUR' } : { ask: 10.2, unit: 'EUR' } },
      close() {}
    }) }
  };
  const client = new HandelsrepublikQuoteClient({
    env: { TRADE_REPUBLIC_SDK_ENABLED: 'true', TRADE_REPUBLIC_SDK_PHONE_NUMBER: '+49123456789', TRADE_REPUBLIC_SDK_PIN: '0123' },
    sessionFile: path.join(dir, 'session.json'), authTimeoutMs: 120000, importer: async () => module
  });
  const quote = await client.getQuote({ isin: 'US0000000001', exchange: 'TIB' });
  assert.deepEqual(login, { phoneNumber: '+49123456789', pin: '0123', timeoutMs: 60000 });
  assert.equal(quote.source, 'trade_republic_sdk_order_price');
});

test('SDK adapter clears a rejected session and authenticates with PIN', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'handelsrepublik-auth-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  let cleared = 0, login = 0;
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => ({
      auth: {
        restoreSession: async () => ({ id: 'expired-session' }),
        refreshSession: async () => { throw Object.assign(new Error('sensitive auth detail'), { status: 401 }); },
        clearSession: async () => { cleared += 1; },
        loginWithPin: async ({ phoneNumber, pin }) => { assert.equal(phoneNumber, '+49123456789'); assert.equal(pin, '0123'); login += 1; return { id: 'new-session' }; }
      },
      trading: { priceForOrder: async options => options.side === 'sell' ? { bid: 10, unit: 'EUR' } : { ask: 10.2, unit: 'EUR' } },
      close() {}
    }) }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true', TRADE_REPUBLIC_SDK_PHONE_NUMBER: '+49123456789', TRADE_REPUBLIC_SDK_PIN: '0123' }, sessionFile: path.join(dir, 'session.json'), importer: async () => module });
  await client.getQuote({ isin: 'US0000000001', exchange: 'TIB' });
  assert.equal(cleared, 1);
  assert.equal(login, 1);
});

test('SDK adapter does not start PIN login for a transient session refresh failure', async () => {
  let login = 0;
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => ({
      auth: {
        restoreSession: async () => ({ id: 'saved-session' }),
        refreshSession: async () => { throw Object.assign(new Error('temporary transport failure'), { status: 503 }); },
        clearSession: async () => assert.fail('transient failure must preserve the saved session'),
        loginWithPin: async () => { login += 1; }
      },
      trading: { priceForOrder: async () => assert.fail('failed refresh must not quote') },
      close() {}
    }) }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true', TRADE_REPUBLIC_SDK_PHONE_NUMBER: '+49123456789', TRADE_REPUBLIC_SDK_PIN: '0123' }, sessionFile: '/private/session.json', importer: async () => module });
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /saved SDK session refresh failed/);
  assert.equal(login, 0);
});

test('SDK adapter backs off failed PIN attempts across client instances', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'handelsrepublik-auth-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const sessionFile = path.join(dir, 'session.json');
  let login = 0;
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => ({
      auth: {
        restoreSession: async () => undefined,
        loginWithPin: async () => { login += 1; throw new Error('approval not completed'); }
      },
      trading: { priceForOrder: async () => assert.fail('failed login must not quote') },
      close() {}
    }) }
  };
  const options = { env: { TRADE_REPUBLIC_SDK_ENABLED: 'true', TRADE_REPUBLIC_SDK_PHONE_NUMBER: '+49123456789', TRADE_REPUBLIC_SDK_PIN: '0123' }, sessionFile, importer: async () => module };
  const client = new HandelsrepublikQuoteClient(options);
  const request = { isin: 'US0000000001', exchange: 'TIB' };
  await assert.rejects(client.getQuote(request), /SDK PIN reauthentication failed/);
  await assert.rejects(new HandelsrepublikQuoteClient(options).getQuote(request), /retry cooldown/);
  assert.equal(login, 1);
  await assert.rejects(client.getQuote({ ...request, forceAuthRetry: true }), /SDK PIN reauthentication failed/);
  assert.equal(login, 2);
});

test('SDK adapter caps the reauthentication cooldown at one hour', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'handelsrepublik-auth-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const sessionFile = path.join(dir, 'session.json');
  const retryFile = `${sessionFile}.reauth-state.json`;
  fs.writeFileSync(retryFile, JSON.stringify({ failures: 12, retryAfter: Date.now() - 1 }), { mode: 0o600 });
  const module = {
    FileSessionStore: class {},
    TradeRepublicClient: { create: () => ({
      auth: { restoreSession: async () => undefined, loginWithPin: async () => { throw new Error('approval not completed'); } },
      trading: { priceForOrder: async () => assert.fail('failed login must not quote') },
      close() {}
    }) }
  };
  const client = new HandelsrepublikQuoteClient({ env: { TRADE_REPUBLIC_SDK_ENABLED: 'true', TRADE_REPUBLIC_SDK_PHONE_NUMBER: '+49123456789', TRADE_REPUBLIC_SDK_PIN: '0123' }, sessionFile, importer: async () => module });
  const attemptedAt = Date.now();
  await assert.rejects(client.getQuote({ isin: 'US0000000001', exchange: 'TIB' }), /SDK PIN reauthentication failed/);
  const retry = JSON.parse(fs.readFileSync(retryFile, 'utf8'));
  assert.equal(retry.failures, 13);
  assert.ok(retry.retryAfter - attemptedAt >= 60 * 60_000 - 1000);
  assert.ok(retry.retryAfter - attemptedAt <= 60 * 60_000 + 1000);
});
