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
function liveBroker(overrides={}) { return { getAccounts:async()=>[{account_ref_masked:'DE••••56',currency:'EUR',available_cash_eur:1000}],getPortfolio:async()=>[{account_ref_masked:'DE••••56',instrument:'JE00BS44BN30',quantity:5,currency:'EUR'}],getQuote:async()=>({bid_eur:10,ask_eur:11,currency:'EUR',quote_time:new Date().toISOString(),observed_at:new Date().toISOString(),source:'test'}),resolveAccount:ref=>ref==='DE••••56'?'private-account':null,...overrides}; }

test('confirmed execution reconciles Journal with actual fill and is idempotent', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  const restore=env({TRADE_REPUBLIC_LIVE_ENABLED:'true',TRADE_REPUBLIC_DRY_RUN:'false',TRADE_REPUBLIC_LIVE_ACK:'true',TRADE_REPUBLIC_KILL_SWITCH:'false',TRADE_REPUBLIC_MAX_ORDER_EUR:'1000'}); t.after(restore);
  let calls=0;
  f.app.locals.brokerClient=liveBroker({previewFees:async()=>({fee_eur:1.25,currency:'EUR'}),placeOrder:async()=>{calls++; return {external_ref_masked:'or••••01',status:'FILLED',execution:{identity:'exec-1',price_eur:12.34,quantity:2,fees_eur:1.25,executed_at:'2026-09-15T10:00:00.000Z'}}}});
  const response=await f.request('/api/broker/trade-republic/orders',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({stock_id:f.stockId,account_ref:'DE••••56',type:'buy',quantity:2,idempotency_key:'reconcile-1',confirmation:'PLACE reconcile-1'})});
  assert.equal(response.status,201); assert.equal(calls,1); const responseBody=await response.json(); assert.equal(JSON.stringify(responseBody).includes('exec-1'),false);
  assert.equal(f.db.getBrokerExecution('exec-1').execution_price_eur,12.34); assert.notEqual(f.db.getBrokerExecution('exec-1').execution_identity,'exec-1'); assert.equal(f.db.getAllTrades()[0].entry_price_eur,12.34); assert.equal(f.db.getAllTrades()[0].notes.includes('exec-1'),false);
  assert.equal(f.db.getBrokerOrderIntent('reconcile-1').response_json.includes('exec-1'),false);
  assert.equal(f.db.recordBrokerExecution({broker:'trade_republic',execution_identity:'exec-1',order_intent_id:null,stock_id:f.stockId,account_ref_masked:'DE••••56',side:'buy',quantity:2,execution_price_eur:12.34,fees_eur:1.25,executed_at:'2026-09-15T10:00:00.000Z',currency:'EUR'}).existing,true); assert.equal(calls,1);
});

test('legacy raw execution identities remain idempotent after identity hashing', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  f.db.db.prepare(`INSERT INTO broker_executions (broker,execution_identity,stock_id,account_ref_masked,side,quantity,execution_price_eur,fees_eur,executed_at,currency) VALUES (?,?,?,?,?,?,?,?,?,?)`).run('trade_republic','legacy-exec-1',f.stockId,'DE••••56','buy',1,12,1,'2026-09-14T10:00:00.000Z','EUR');
  const result=f.db.recordBrokerExecution({broker:'trade_republic',execution_identity:'legacy-exec-1',order_intent_id:null,stock_id:f.stockId,account_ref_masked:'DE••••56',side:'buy',quantity:1,execution_price_eur:12,fees_eur:1,executed_at:'2026-09-14T10:00:00.000Z',currency:'EUR'});
  assert.equal(result.existing,true); assert.equal(result.execution.execution_identity,'legacy-exec-1'); assert.equal(f.db.getBrokerExecution('legacy-exec-1').execution_identity,'legacy-exec-1'); assert.equal(f.db.getAllTrades().length,0);
});

test('read-only sanitizer removes account and card identifiers', () => {
  const safe=sanitizeReadOnly({ DE1234567890123456:{iban:'DE123',portfolio:4}, cardNumber:'4111111111111111', useful:'ok', nested:{opaque:'DE9999999999999999',id:'a1b2c3d4-e5f6-4890-abcd-ef1234567890'} }, 'portfolio');
  assert.equal(JSON.stringify(safe).includes('1234567890123456'), false); assert.equal(JSON.stringify(safe).includes('411111'), false); assert.equal(JSON.stringify(safe).includes('9999999999999999'), false); assert.equal(JSON.stringify(safe).includes('a1b2c3d4'), false); assert.equal(safe.data.useful,'ok'); assert.equal(safe.data['DE••••56'].portfolio,4);
});

test('read-only account maps mask IBAN-style and numeric account keys', () => {
  const safe=sanitizeReadOnly({ DE1234567890123456:{value:1}, 1234567890:{value:2}, useful:'ok' }, 'portfolio');
  assert.equal(safe.data['DE••••56'].value, 1); assert.equal(safe.data['12••••90'].value, 2); assert.equal(safe.data.useful, 'ok');
  assert.equal(JSON.stringify(safe).includes('1234567890123456'), false);
});

test('executed transaction without authoritative fields becomes a pending candidate', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  f.app.locals.brokerClient={getAccounts:async()=>[],getPortfolio:async()=>[],getOrders:async()=>({}),getTransactions:async()=>[{id:'tx-1',title:'Trade Republic',subtitle:'Achat',status:'EXECUTED',timestamp:'2026-09-15T10:00:00Z',amount:{currency:'EUR',value:-10001,fractionDigits:2},presentation:null}]};
  let response=await f.request('/api/broker/trade-republic/sync',{method:'POST'});
  assert.equal(response.status,200); let body=await response.json(); assert.equal(body.new_reconciliation_candidates,1); assert.equal(body.pending_reconciliation,1);
  response=await f.request('/api/broker/trade-republic/sync',{method:'POST'}); body=await response.json(); assert.equal(body.new_reconciliation_candidates,0); assert.equal(body.pending_reconciliation,1);
  const candidates=f.db.getBrokerReconciliationCandidates(); assert.equal(candidates[0].amount_eur,-100.01); assert.equal(candidates[0].reason.includes('missing'),true); assert.equal(f.db.getAllTrades().length,0);
});


test('sell execution closes FIFO quantity with proportional entry and exit fees', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  const insert=f.db.db.prepare(`INSERT INTO trades (trade_id,stock_id,opened_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur,entry_fee_eur,exit_fee_eur,dividend_eur,strategy,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  insert.run('lot-1',f.stockId,'2026-09-14 09:00:00',2,'EUR',10,1,10,2,0,0,'test','');
  insert.run('lot-2',f.stockId,'2026-09-14 10:00:00',2,'EUR',20,1,20,4,0,0,'test','');
  f.db.recordBrokerExecution({broker:'trade_republic',execution_identity:'sell-1',order_intent_id:null,stock_id:f.stockId,account_ref_masked:'DE••••56',side:'sell',quantity:3,execution_price_eur:30,fees_eur:3,executed_at:'2026-09-15T11:00:00.000Z',currency:'EUR'});
  const closed=f.db.getAllTrades({status:'closed'}), open=f.db.getAllTrades({status:'open'});
  assert.equal(closed.reduce((sum,row)=>sum+row.quantity,0),3); assert.equal(closed.reduce((sum,row)=>sum+row.exit_fee_eur,0),3); assert.equal(open[0].quantity,1); assert.equal(open[0].entry_fee_eur,2);
});

test('oversell reconciliation rolls back execution and Journal changes', async t => {
  const f=await fixture(); t.after(()=>{f.server.close();f.db.db.close();fs.rmSync(f.dir,{recursive:true,force:true});});
  f.db.addTrade({trade_id:'lot-only',stock_id:f.stockId,opened_at:'2026-09-14 09:00:00',closed_at:null,quantity:1,currency:'EUR',entry_price:10,entry_fx_rate:1,entry_price_eur:10,entry_fee_eur:1,exit_price:null,exit_fx_rate:null,exit_price_eur:null,exit_fee_eur:0,dividend_eur:0,strategy:'test',notes:''});
  assert.throws(()=>f.db.recordBrokerExecution({broker:'trade_republic',execution_identity:'oversell-1',order_intent_id:null,stock_id:f.stockId,account_ref_masked:'DE••••56',side:'sell',quantity:2,execution_price_eur:30,fees_eur:1,executed_at:'2026-09-15T11:00:00.000Z',currency:'EUR'}),/oversell/);
  assert.equal(f.db.getBrokerExecution('oversell-1'),undefined); assert.equal(f.db.getAllTrades({status:'open'})[0].quantity,1);
});
