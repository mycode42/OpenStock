const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp } = require('../server');
const { sanitizeReadOnly } = require('../lib/brokers/trade-republic');

async function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-reconcile-'));
  const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true });
  const db = new OpenStockDB(file); const stock = db.addStock('BIRK', 'Birkenstock', 'EUR', 10, 20); const stockId = Number(stock.lastInsertRowid);
  db.saveBrokerMapping({ stock_id: stockId, broker:'trade_republic', ticker:'BIRK', isin:'JE00BS44BN30', exchange:'TIB', currency:'EUR', broker_metadata_json:'{}', source:'test', verified_at:new Date().toISOString() });
  db.syncBrokerRows('broker_accounts', [{ broker:'trade_republic', account_ref_masked:'DE••••56', currency:'EUR', cash_eur:1000, available_cash_eur:1000, observed_at:new Date().toISOString(), raw_hash:null }]);
  db.addBrokerQuote({ broker:'trade_republic', isin:'JE00BS44BN30', exchange:'TIB', bid_eur:10, ask_eur:11, last_eur:null, currency:'EUR', quote_time:new Date().toISOString(), observed_at:new Date().toISOString(), source:'test' });
  const app=createApp({db,backfill:{enqueue(){},enqueueMissing(){return 0}},fetchImpl:async()=>({ok:true,json:async()=>({})})});
  const server=await new Promise(resolve=>{const s=app.listen(0,'127.0.0.1',()=>resolve(s));});
  return {app,db,stockId,server,request:(url,options)=>fetch('http://127.0.0.1:'+server.address().port+url,options),dir};
}
function env(values){const old={}; for(const [k,v] of Object.entries(values)){old[k]=process.env[k];process.env[k]=v;} return ()=>{for(const [k,v] of Object.entries(old)){if(v===undefined)delete process.env[k];else process.env[k]=v;}};}

test('confirmed execution reconciles Journal with actual fill and is idempotent', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  const restore=env({TRADE_REPUBLIC_LIVE_ENABLED:'true',TRADE_REPUBLIC_DRY_RUN:'false',TRADE_REPUBLIC_LIVE_ACK:'true',TRADE_REPUBLIC_KILL_SWITCH:'false',TRADE_REPUBLIC_MAX_ORDER_EUR:'1000'}); t.after(restore);
  let calls=0;
  f.app.locals.brokerClient={placeOrder:async()=>{calls++; return {external_ref_masked:'or••••01',status:'FILLED',execution:{identity:'exec-1',price_eur:12.34,quantity:2,fees_eur:1.25,executed_at:'2026-09-15T10:00:00.000Z'}}}};
  const response=await f.request('/api/broker/trade-republic/orders',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({stock_id:f.stockId,account_ref:'DE••••56',type:'buy',quantity:2,idempotency_key:'reconcile-1',confirmation:'PLACE reconcile-1'})});
  assert.equal(response.status,201); assert.equal(calls,1);
  assert.equal(f.db.getBrokerExecution('exec-1').execution_price_eur,12.34); assert.equal(f.db.getAllTrades()[0].entry_price_eur,12.34);
  assert.equal(f.db.recordBrokerExecution({broker:'trade_republic',execution_identity:'exec-1',order_intent_id:null,stock_id:f.stockId,account_ref_masked:'DE••••56',side:'buy',quantity:2,execution_price_eur:12.34,fees_eur:1.25,executed_at:'2026-09-15T10:00:00.000Z',currency:'EUR'}).existing,true); assert.equal(calls,1);
});

test('read-only sanitizer removes account and card identifiers', () => {
  const safe=sanitizeReadOnly({ DE1234567890123456:{iban:'DE123',portfolio:4}, cardNumber:'4111111111111111', useful:'ok' }, 'portfolio');
  assert.equal(JSON.stringify(safe).includes('1234567890123456'), false); assert.equal(JSON.stringify(safe).includes('411111'), false); assert.equal(safe.data.useful,'ok');
});

test('read-only account maps mask IBAN-style and numeric account keys', () => {
  const safe=sanitizeReadOnly({ DE1234567890123456:{value:1}, 1234567890:{value:2}, useful:'ok' }, 'portfolio');
  assert.equal(safe.data['DE••••56'].value, 1); assert.equal(safe.data['12••••90'].value, 2); assert.equal(safe.data.useful, 'ok');
  assert.equal(JSON.stringify(safe).includes('1234567890123456'), false);
});

test('executed transaction without authoritative fields becomes a pending candidate', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  f.app.locals.brokerClient={getAccounts:async()=>[],getPortfolio:async()=>[],getOrders:async()=>({}),getTransactions:async()=>[{id:'tx-1',title:'Trade Republic',subtitle:'Achat',status:'EXECUTED',timestamp:'2026-09-15T10:00:00Z',amount:{currency:'EUR',value:-10001,fractionDigits:2},presentation:null}]};
  const response=await f.request('/api/broker/trade-republic/sync',{method:'POST'});
  assert.equal(response.status,200); const body=await response.json(); assert.equal(body.pending_reconciliation,1);
  const candidates=f.db.getBrokerReconciliationCandidates(); assert.equal(candidates[0].amount_eur,-100.01); assert.equal(candidates[0].reason.includes('missing'),true); assert.equal(f.db.getAllTrades().length,0);
});
