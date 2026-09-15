const test = require('node:test');
const assert = require('node:assert/strict');
const { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeInstrumentHistory, sanitizeAccount, sanitizeCollection, parseOrderResponse } = require('../lib/brokers/trade-republic');

test('Trade Republic client sends X-API-Key server-side and never returns it', async () => {
  let seen;
  const client = new TradeRepublicClient({ env: { TRADE_REPUBLIC_API_KEY: 'secret-test-key' }, fetchImpl: async (url, options) => { seen = { url, headers: options.headers }; return { ok: true, json: async () => ({ bid: 10, ask: 11, currency: 'EUR' }) }; } });
  const quote = await client.getQuote({ isin: 'DE0000000001', exchange: 'XETR' });
  assert.equal(seen.headers['X-API-Key'], 'secret-test-key');
  assert.equal(quote.ask_eur, 11);
  assert.equal(JSON.stringify(quote).includes('secret-test-key'), false);
});
test('sanitizers mask account references and preserve EUR bid/ask semantics', () => {
  const account = sanitizeAccount({ securitiesAccountNumber: 'DE1234567890123456', cashAmount: 42, availableCashAmount: 17 });
  assert.equal(account.account_ref_masked, 'DE••••56');
  assert.equal(account.cash_eur, 42);
  assert.equal(account.available_cash_eur, 17);
  assert.deepEqual(sanitizeQuote({ bid: 1, ask: 2, currency: 'USD' }, { isin: 'X', exchange: 'XETR' }).bid_eur, null);
});
test('deterministic broker HTTP rejection is classified terminal', async () => {
  const client = new TradeRepublicClient({ env: { TRADE_REPUBLIC_API_KEY: 'secret' }, fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({ secret: 'do-not-return' }) }) });
  await assert.rejects(() => client.getAccounts(), error => error.code === 'REJECTED' && error.status === 422 && !String(error).includes('do-not-return'));
});
test('client timeout fails closed without exposing request details', async () => {
  const client = new TradeRepublicClient({ env: { TRADE_REPUBLIC_API_KEY: 'secret' }, timeoutMs: 1000, fetchImpl: (_u, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })))) });
  await assert.rejects(() => client.getAccounts(), error => error.code === 'TIMEOUT' && !String(error).includes('secret'));
});

test('nested order-price maps sell price to executable bid and buy price to executable ask', () => {
  const quote = sanitizeQuote({ sell: { time: '2026-09-15T10:00:00Z', price: 101, bidPrice: 100, unit: 'EUR', size: 2 }, buy: { time: '2026-09-15T10:00:01Z', price: 103, askPrice: 104, unit: 'EUR', size: 3 } }, { isin: 'US0000000001', exchange: 'TIB' });
  assert.equal(quote.bid_eur, 101);
  assert.equal(quote.ask_eur, 103);
  assert.equal(quote.bid_time, '2026-09-15T10:00:00.000Z');
  assert.equal(quote.ask_time, '2026-09-15T10:00:01.000Z');
  assert.equal(quote.bid_unit, 'EUR');
});

test('instrument and history sanitizers normalize broker identity without raw payloads', () => {
  const instrument = sanitizeInstrument({ data: { id: 'US0000000001', name: 'Example Corp', intlSymbol: 'EXM', tradable: true, type: 'STOCK' } }, 'US0000000001');
  assert.deepEqual({ isin: instrument.isin, name: instrument.name, ticker: instrument.intlSymbol, tradable: instrument.tradable }, { isin: 'US0000000001', name: 'Example Corp', ticker: 'EXM', tradable: true });
  const history = sanitizeInstrumentHistory({ aggregates: [{ time: 1760529600, open: 10, high: 12, low: 9, close: 11, volume: 4, adjValue: 11 }], resolution: '1d' }, { isin: 'US0000000001', range: '1m' });
  assert.equal(history.points[0].priceEur, 11); assert.equal(history.points[0].source, 'trade_republic_history'); assert.equal(JSON.stringify(history).includes('aggregates'), false);
});

test('portfolio keyed accounts and category positions are flattened and masked', () => {
  const rows = sanitizeCollection({ DE1234567890123456: { categories: [{ positions: [{ isin: 'JE00BS44BN30', averageBuyIn: { amount: 100, currency: 'EUR' }, netSize: 4, virtualSize: 4, status: 'ACTIVE', currentValue: 500, id: 'position-secret' }] }] } }, 'portfolio');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].instrument, 'JE00BS44BN30');
  assert.equal(rows[0].quantity, 4);
  assert.equal(rows[0].account_ref_masked, 'DE••••56');
  assert.equal(JSON.stringify(rows).includes('1234567890123456'), false);
});

test('portfolio normalization falls back to virtualSize when netSize is absent', () => {
  const rows = sanitizeCollection({ DE1234567890123456: { categories: [{ positions: [{ isin: 'US2533931026', virtualSize: 2 }] }] } }, 'portfolio');
  assert.equal(rows[0].quantity, 2);
});

test('transaction normalization reads nested amount values without exposing account data', () => {
  const rows = sanitizeCollection([{ id: 'transaction-secret', timestamp: '2026-09-15T10:00:00Z', amount: { currency: 'EUR', value: 12.34, fractionDigits: 2 }, status: 'EXECUTED', cashAccountNumber: 'DE1234567890123456' }], 'transactions');
  assert.equal(rows[0].amount_eur, 12.34);
  assert.equal(rows[0].currency, 'EUR');
  assert.equal(JSON.stringify(rows).includes('1234567890123456'), false);
});

test('order parser never treats an order id alone as a fill', () => {
  assert.deepEqual(parseOrderResponse({ orderId: 'secret-id' }), { external_ref_masked: 'se••••id', status: 'UNKNOWN', execution: 'none' });
  assert.equal(parseOrderResponse({ status: 'FILLED', orderId: 'secret-id' }).execution, 'filled');
});
