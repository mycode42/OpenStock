const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp } = require('../server');
const { BrokerError } = require('../lib/brokers/trade-republic');

async function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-tr-api-'));
  const file = path.join(dir, 'test.db');
  initDatabase({ dbPath: file, seedDefaults: false, silent: true });
  const db = new OpenStockDB(file);
  const stock = db.addStock('BIRK', 'Birkenstock', 'EUR', 10, 20);
  const stockId = Number(stock.lastInsertRowid);
  db.saveBrokerMapping({ stock_id: stockId, broker: 'trade_republic', ticker: 'BIRK', isin: 'JE00BS44BN30', exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'test', verified_at: new Date().toISOString() });
  db.syncBrokerRows('broker_accounts', [{ broker: 'trade_republic', account_ref_masked: 'DE••••56', currency: 'EUR', cash_eur: 1000, available_cash_eur: 1000, observed_at: new Date().toISOString(), raw_hash: null }]);
  db.addBrokerQuote({ broker: 'trade_republic', isin: 'JE00BS44BN30', exchange: 'TIB', bid_eur: 10, ask_eur: 11, last_eur: null, currency: 'EUR', quote_time: new Date().toISOString(), observed_at: new Date().toISOString(), source: 'test' });
  const app = createApp({ db, backfill: { enqueue() {}, enqueueMissing() { return 0; } }, fetchImpl: async () => ({ ok: true, json: async () => ({}) }) });
  const server = await new Promise(resolve => { const listener = app.listen(0, '127.0.0.1', () => resolve(listener)); });
  const request = (url, options) => fetch(`http://127.0.0.1:${server.address().port}${url}`, options);
  return { app, db, stockId, request, server, dir };
}

function env(values) {
  const old = {};
  for (const [key, value] of Object.entries(values)) { old[key] = process.env[key]; process.env[key] = value; }
  return () => { for (const [key, value] of Object.entries(old)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } };
}

test('missing live gates make zero broker calls', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = { placeOrder: async () => { calls++; } };
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'false', TRADE_REPUBLIC_DRY_RUN: 'true', TRADE_REPUBLIC_LIVE_ACK: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' });
  t.after(restore);
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: 'missing-gate-1', confirmation: 'PLACE missing-gate-1' }) });
  assert.equal(response.status, 409); assert.equal(calls, 0);
});

test('all live gates submit once and duplicate idempotency does not resubmit', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = { placeOrder: async order => { calls++; assert.equal(order.account_ref_masked, 'DE••••56'); return { external_ref_masked: 'ab••••yz', status: 'SUBMITTED', execution: 'none' }; } };
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const body = { stock_id: f.stockId, type: 'buy', quantity: 2, account_ref: 'DE••••56', idempotency_key: 'all-gates-1', confirmation: 'PLACE all-gates-1' };
  let response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal(response.status, 201); assert.equal(calls, 1);
  response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal(response.status, 200); assert.equal(calls, 1);
  assert.equal(f.db.db.prepare('SELECT status FROM broker_order_intents WHERE idempotency_key=?').get('all-gates-1').status, 'SUBMITTED');
});

test('ambiguous submission is frozen after reconciliation check', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let orderCalls = 0; let reconcileCalls = 0; f.app.locals.brokerClient = { placeOrder: async () => { orderCalls++; throw new BrokerError('timeout', 'TIMEOUT', 503); }, getOrders: async () => { reconcileCalls++; return []; } };
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const key = 'ambiguous-1'; const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}` }) });
  assert.equal(response.status, 503); assert.equal(orderCalls, 1); assert.equal(reconcileCalls, 1);
  assert.equal(f.db.db.prepare('SELECT status FROM broker_order_intents WHERE idempotency_key=?').get(key).status, 'UNKNOWN');
});

test('mapping verification requires both identity and home exchange', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  f.app.locals.brokerClient = { getInstrument: async isin => ({ isin, name: 'BIRK' }), getTradeRepublicInstrument: async isin => ({ isin, exchange: 'WRONG', currency: 'EUR' }) };
  const response = await f.request('/api/broker/trade-republic/mappings', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, isin: 'JE00BS44BN30', exchange: 'TIB', currency: 'EUR' }) });
  assert.equal(response.status, 422);
  assert.equal(f.db.getBrokerMapping(f.stockId).verified_at !== null, true); // existing verified mapping remains untouched
});
