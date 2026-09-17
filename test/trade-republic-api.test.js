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

function liveBroker(overrides = {}) {
  return {
    getAccounts: async () => [{ account_ref_masked: 'DE••••56', currency: 'EUR', cash_eur: 1000, available_cash_eur: 1000 }],
    getPortfolio: async () => [{ account_ref_masked: 'DE••••56', instrument: 'JE00BS44BN30', quantity: 5, currency: 'EUR' }],
    getQuote: async () => ({ bid_eur: 10, ask_eur: 11, last_eur: null, currency: 'EUR', quote_time: new Date().toISOString(), observed_at: new Date().toISOString(), source: 'test' }),
    resolveAccount: accountRef => accountRef === 'DE••••56' ? 'private-account' : null,
    ...overrides
  };
}

test('missing live gates make zero broker calls', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = { placeOrder: async () => { calls++; } };
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'false', TRADE_REPUBLIC_DRY_RUN: 'true', TRADE_REPUBLIC_LIVE_ACK: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' });
  t.after(restore);
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: 'missing-gate-1', confirmation: 'PLACE missing-gate-1' }) });
  const body = await response.json();
  assert.equal(response.status, 409); assert.equal(body.code, 'ORDER_SAFETY_GATE'); assert.equal(body.failed_gates.includes('live_enabled'), true); assert.equal(calls, 0);
});

test('all live gates submit once and duplicate idempotency does not resubmit', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; let feeCalls = 0; f.app.locals.brokerClient = liveBroker({ previewFees: async order => { feeCalls++; assert.equal(order.validity, 'GFD'); return { fee_eur: 1, currency: 'EUR' }; }, placeOrder: async order => { calls++; assert.equal(order.account_ref_masked, 'DE••••56'); assert.equal(order.validity, 'GFD'); return { external_ref_masked: 'ab••••yz', status: 'SUBMITTED', execution: 'none' }; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const body = { stock_id: f.stockId, type: 'buy', quantity: 2, account_ref: 'DE••••56', idempotency_key: 'all-gates-1', confirmation: 'PLACE all-gates-1' };
  let response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal(response.status, 201); assert.equal(calls, 1); assert.equal(feeCalls, 1);
  response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal(response.status, 200); assert.equal(calls, 1);
  assert.equal(f.db.db.prepare('SELECT status FROM broker_order_intents WHERE idempotency_key=?').get('all-gates-1').status, 'SUBMITTED');
});

test('euro-value preview floors to whole units and includes fees', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let feeOrder;
  f.app.locals.brokerClient = liveBroker({
    getQuote: async () => ({ bid_eur: 10, ask_eur: 11, last_eur: null, currency: 'EUR', quote_time: new Date().toISOString(), observed_at: new Date().toISOString(), source: 'test' }),
    previewFees: async order => { feeOrder = order; return { fee_eur: 1, currency: 'EUR' }; }
  });
  const response = await f.request('/api/broker/trade-republic/order-preview', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', amount_eur: 25, account_ref: 'DE••••56' }) });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.quantity, 2);
  assert.equal(body.sizing_mode, 'amount');
  assert.equal(body.requested_amount_eur, 25);
  assert.equal(body.sizing_price_eur, 11);
  assert.equal(body.total_eur, 23);
  assert.equal(feeOrder.quantity, 2);
});

test('euro-value live order uses a hidden key contract and generic confirmation', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let submitted;
  f.app.locals.brokerClient = liveBroker({
    previewFees: async () => ({ fee_eur: 1, currency: 'EUR' }),
    placeOrder: async order => { submitted = order; return { external_ref_masked: 'ab••••yz', status: 'SUBMITTED', execution: 'none' }; }
  });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const key = 'ui-amount-order-1';
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', amount_eur: 25, account_ref: 'DE••••56', idempotency_key: key, confirmation: 'PLACE ORDER' }) });
  assert.equal(response.status, 201);
  assert.equal(submitted.quantity, 2);
  assert.equal('amount_eur' in submitted, false);
  const persisted = JSON.parse(f.db.db.prepare('SELECT request_json FROM broker_order_intents WHERE idempotency_key=?').get(key).request_json);
  assert.equal(persisted.quantity, 2);
  assert.equal(persisted.sizing_mode, 'amount');
  assert.equal(persisted.requested_amount_eur, 25);
});

test('ambiguous or undersized sizing never reaches the broker', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0;
  f.app.locals.brokerClient = liveBroker({ previewFees: async () => { calls++; }, placeOrder: async () => { calls++; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  for (const [index, sizing] of [{ quantity: 1, amount_eur: 25 }, { amount_eur: 5 }].entries()) {
    const key = `invalid-sizing-${index}`;
    const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', account_ref: 'DE••••56', idempotency_key: key, confirmation: 'PLACE ORDER', ...sizing }) });
    assert.equal(response.status, 409);
  }
  assert.equal(calls, 0);
});

test('ambiguous submission is frozen after reconciliation check', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let orderCalls = 0; let reconcileCalls = 0; f.app.locals.brokerClient = liveBroker({ previewFees: async () => ({ fee_eur: 1, currency: 'EUR' }), placeOrder: async () => { orderCalls++; throw new BrokerError('timeout', 'TIMEOUT', 503); }, getOrders: async () => { reconcileCalls++; return []; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const key = 'ambiguous-1'; const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}` }) });
  assert.equal(response.status, 503); assert.equal(orderCalls, 1); assert.equal(reconcileCalls, 1);
  assert.equal(f.db.db.prepare('SELECT status FROM broker_order_intents WHERE idempotency_key=?').get(key).status, 'UNKNOWN');
});

test('live order gates use fresh Trade Republic authority instead of stale local snapshots', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let accountsCalls = 0; let portfolioCalls = 0; let orderCalls = 0;
  f.app.locals.brokerClient = liveBroker({
    getAccounts: async () => { accountsCalls++; return [{ account_ref_masked: 'DE••••56', currency: 'EUR', available_cash_eur: 1000 }]; },
    getPortfolio: async () => { portfolioCalls++; return [{ account_ref_masked: 'DE••••56', instrument: 'JE00BS44BN30', quantity: 2, currency: 'EUR' }]; },
    previewFees: async () => ({ fee_eur: 1, currency: 'EUR' }),
    placeOrder: async () => { orderCalls++; return { external_ref_masked: 'or••••01', status: 'SUBMITTED', execution: 'none' }; }
  });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'sell', quantity: 1, account_ref: 'DE••••56', idempotency_key: 'fresh-authority-1', confirmation: 'PLACE fresh-authority-1' }) });
  assert.equal(response.status, 201); assert.equal(accountsCalls, 1); assert.equal(portfolioCalls, 1); assert.equal(orderCalls, 1);
});

test('stock-derived read-only routes use the verified mapping and validated ranges', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  const calls=[]; f.app.locals.brokerClient={
    getInstrument:async isin=>{calls.push(['instrument',isin]);return {isin};},
    getTradeRepublicInstrument:async isin=>{calls.push(['tr-instrument',isin]);return {isin,exchange:'TIB'};},
    getInstrumentHistory:async input=>{calls.push(['history',input]);return {isin:input.isin,range:input.range,points:[]};},
    getExchangeSchedule:async exchange=>{calls.push(['schedule',exchange]);return {exchange};}
  };
  for (const url of [`/api/broker/trade-republic/instrument/${f.stockId}`,`/api/broker/trade-republic/tr-instrument/${f.stockId}`,`/api/broker/trade-republic/instrument-history/${f.stockId}?range=1m`,`/api/broker/trade-republic/schedule/${f.stockId}`]) assert.equal((await f.request(url)).status,200);
  assert.deepEqual(calls,[['instrument','JE00BS44BN30'],['tr-instrument','JE00BS44BN30'],['history',{isin:'JE00BS44BN30',range:'1m'}],['schedule','TIB']]);
  assert.equal((await f.request(`/api/broker/trade-republic/instrument-history/${f.stockId}?range=5y`)).status,400);
});

test('mapping verification requires both identity and home exchange', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  f.app.locals.brokerClient = { getInstrument: async isin => ({ isin, name: 'BIRK' }), getTradeRepublicInstrument: async isin => ({ isin, exchange: 'WRONG', currency: 'EUR' }) };
  const response = await f.request('/api/broker/trade-republic/mappings', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, isin: 'JE00BS44BN30', exchange: 'TIB', currency: 'EUR' }) });
  assert.equal(response.status, 422);
  assert.equal(f.db.getBrokerMapping(f.stockId).verified_at !== null, true); // existing verified mapping remains untouched
});

test('invalid mode, validity, and limit are rejected before place-order', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = { placeOrder: async () => { calls++; } };
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  for (const body of [
    { mode: 'stopMarket', validity: 'GFD' },
    { mode: 'market', validity: 'DAY' },
    { mode: 'limit', validity: 'GFD', limit: 0 }
  ]) {
    const key = `invalid-${body.mode}-${body.validity}-${body.limit || 'none'}`;
    const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}`, ...body }) });
    assert.equal(response.status, 409);
  }
  assert.equal(calls, 0);
});

test('buy cap uses the more conservative limit price', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = liveBroker({ placeOrder: async () => { calls++; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '50' }); t.after(restore);
  const key = 'limit-cap-1';
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, mode: 'limit', limit: 100, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}` }) });
  assert.equal(response.status, 409); assert.equal(calls, 0);
});

test('fresh Trade Republic account and position authority rejects buys and sells without broker calls', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let calls = 0; f.app.locals.brokerClient = liveBroker({ getAccounts: async () => [{ account_ref_masked: 'DE••••56', currency: 'EUR', available_cash_eur: 5 }], getPortfolio: async () => [], placeOrder: async () => { calls++; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  for (const [type, key] of [['buy', 'cash-gate-1'], ['sell', 'oversell-gate-1']]) {
    const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type, quantity: 1, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}` }) });
    const body = await response.json();
    assert.equal(response.status, 409); assert.equal(body.code, 'ORDER_SAFETY_GATE'); assert.equal(body.failed_gates.includes(type === 'buy' ? 'notional_cash_ok' : 'sufficient_position'), true);
  }
  assert.equal(calls, 0);
});

test('deterministic broker rejection is terminal and does not reconcile', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let orderCalls = 0; let reconcileCalls = 0;
  f.app.locals.brokerClient = liveBroker({ previewFees: async () => ({ fee_eur: 1, currency: 'EUR' }), placeOrder: async () => { orderCalls++; throw new BrokerError('rejected', 'REJECTED', 422); }, getOrders: async () => { reconcileCalls++; return []; } });
  const restore = env({ TRADE_REPUBLIC_LIVE_ENABLED: 'true', TRADE_REPUBLIC_DRY_RUN: 'false', TRADE_REPUBLIC_LIVE_ACK: 'true', TRADE_REPUBLIC_KILL_SWITCH: 'false', TRADE_REPUBLIC_MAX_ORDER_EUR: '1000' }); t.after(restore);
  const key = 'deterministic-reject-1';
  const response = await f.request('/api/broker/trade-republic/orders', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ stock_id: f.stockId, type: 'buy', quantity: 1, account_ref: 'DE••••56', idempotency_key: key, confirmation: `PLACE ${key}` }) });
  assert.equal(response.status, 422); assert.equal(orderCalls, 1); assert.equal(reconcileCalls, 0);
  assert.equal(f.db.db.prepare('SELECT status FROM broker_order_intents WHERE idempotency_key=?').get(key).status, 'REJECTED');
});


test('authoritative fees are required and included in cash safety', async t => {
  const f = await fixture(); t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
  let orderCalls=0;
  f.app.locals.brokerClient=liveBroker({getAccounts:async()=>[{account_ref_masked:'DE••••56',currency:'EUR',available_cash_eur:22}],previewFees:async()=>({fee_eur:1,currency:'EUR'}),placeOrder:async()=>{orderCalls++;}});
  const restore=env({TRADE_REPUBLIC_LIVE_ENABLED:'true',TRADE_REPUBLIC_DRY_RUN:'false',TRADE_REPUBLIC_LIVE_ACK:'true',TRADE_REPUBLIC_KILL_SWITCH:'false',TRADE_REPUBLIC_MAX_ORDER_EUR:'1000'}); t.after(restore);
  const key='fee-cash-1'; const response=await f.request('/api/broker/trade-republic/orders',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({stock_id:f.stockId,type:'buy',quantity:2,account_ref:'DE••••56',idempotency_key:key,confirmation:`PLACE ${key}`,fee_eur:0})});
  assert.equal(response.status,409); assert.equal(orderCalls,0);
});

test('stopMarket and GTD use the documented sidecar fields', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  let submitted; f.app.locals.brokerClient=liveBroker({previewFees:async()=>({fee_eur:1,currency:'EUR'}),placeOrder:async order=>{submitted=order;return {external_ref_masked:'or••••01',status:'SUBMITTED',execution:'none'};}});
  const restore=env({TRADE_REPUBLIC_LIVE_ENABLED:'true',TRADE_REPUBLIC_DRY_RUN:'false',TRADE_REPUBLIC_LIVE_ACK:'true',TRADE_REPUBLIC_KILL_SWITCH:'false',TRADE_REPUBLIC_MAX_ORDER_EUR:'1000'}); t.after(restore);
  const key='stop-gtd-1'; const response=await f.request('/api/broker/trade-republic/orders',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({stock_id:f.stockId,type:'buy',quantity:1,mode:'stopMarket',stop:12,validity:'GTD',account_ref:'DE••••56',idempotency_key:key,confirmation:`PLACE ${key}`})});
  assert.equal(response.status,201); assert.equal(submitted.mode,'stopMarket'); assert.equal(submitted.stop,12); assert.equal(submitted.validity,'GTD'); assert.equal('expiry' in submitted,false);
});
