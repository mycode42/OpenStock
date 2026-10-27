const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp } = require('../server');

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

