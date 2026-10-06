'use strict';

const crypto = require('node:crypto');
const { sanitizeQuote, sanitizeInstrument, sanitizeAccount, sanitizeCollection } = require('../../lib/brokers/trade-republic');
const { normalizedBrokerRows } = require('../../lib/broker-sync-service');
const { syncTradeRepublicExecutions } = require('../../lib/brokers/handelsrepublik-execution-sync');
const { normalizeOrderSizing, hasValidOrderSizingInput, failedGateNames, fetchLiveOrderAuthority } = require('../../lib/order-safety');

module.exports = function registerTradeRepublicRoutes({ app, resolveVerifiedInstrument, serializeConfiguredStock, brokerError, serializeTrade }) {
// ===== API: broker integration (sanitized, read-first) =====
app.get('/api/broker/trade-republic/health', async (req, res) => { try { res.json({ broker: 'trade_republic', ...(await req.app.locals.brokerClient.health()), live_submission_enabled: process.env.TRADE_REPUBLIC_LIVE_ENABLED === 'true' && process.env.TRADE_REPUBLIC_DRY_RUN === 'false' }); } catch (e) { brokerError(res, e); } });
app.get('/api/broker/trade-republic/accounts', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getAccounts()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/portfolio', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getPortfolio()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/orders', async (req, res) => { try { const account_ref_masked=String(req.query.account_ref||'').trim()||undefined; res.json(await req.app.locals.brokerClient.getOrders({account_ref_masked})); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/transactions', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getTransactions()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/interests', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getInterests()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/tickets', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getTickets()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/activity', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getAccountActivity()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/price-alarms', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getPriceAlarms()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/reconciliation-candidates', (req, res) => res.json(req.app.locals.db.getBrokerReconciliationCandidates()));
app.post('/api/broker/trade-republic/accounts-history', async (req, res) => { try { const range=String(req.body?.range||'1y'); if(!['1d','5d','1m','1y','max'].includes(range)) return res.status(400).json({error:'Invalid account history range'}); res.json(await req.app.locals.brokerClient.getAccountsHistory(range)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/instrument/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getInstrument(mapping.isin)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/tr-instrument/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getTradeRepublicInstrument(mapping.isin)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/instrument-history/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); const range=String(req.query.range||'1m'); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); if(!['1d','5d','1m','1y','max'].includes(range)) return res.status(400).json({error:'Invalid instrument history range'}); res.json(await req.app.locals.brokerClient.getInstrumentHistory({isin:mapping.isin,range})); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/schedule/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getExchangeSchedule(mapping.exchange)); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/cancel', (req, res) => res.status(501).json({ error:'Live cancellation is disabled; no broker call was made', code:'CANCELLATION_DISABLED' }));
app.get('/api/broker/trade-republic/mappings', (req, res) => res.json(req.app.locals.db.getBrokerMappings().map(({ broker_metadata_json, ...m }) => ({ ...m, broker_metadata: JSON.parse(broker_metadata_json || '{}') }))));
app.post('/api/broker/trade-republic/mappings', async (req, res) => { try { const b=req.body||{}; const stock=req.app.locals.db.getStockById(Number(b.stock_id)); if(!stock) return res.status(404).json({error:'Stock not found'}); const { instrument, home } = await resolveVerifiedInstrument(req.app.locals.brokerClient, b.isin); const existing=req.app.locals.db.getConfigStocks().find(item => item.isin === instrument.isin && item.id !== stock.id) || req.app.locals.db.getArchivedStocks().find(item => item.isin === instrument.isin && item.id !== stock.id); if (existing) return res.status(409).json({error: existing.archived ? 'ISIN belongs to an archived stock; restore that identity explicitly' : 'ISIN already exists', stock_id: existing.id}); req.app.locals.db.saveBrokerMapping({stock_id:stock.id,broker:'trade_republic',ticker:instrument.ticker,isin:instrument.isin,exchange:home.exchange,currency:home.currency,broker_metadata_json:JSON.stringify({instrument,home}),source:'broker_verified',verified_at:new Date().toISOString()}); req.app.locals.db.updateStock(stock.id,{isin:instrument.isin,name:instrument.name}); res.status(201).json({mapping:req.app.locals.db.getBrokerMapping(stock.id), stock:serializeConfiguredStock(req.app.locals.db.getStockById(stock.id))}); } catch(e) { if (e.status) return res.status(e.status).json({error:e.message,verified:false}); brokerError(res,e); } });
app.post('/api/stocks/:id/history/sync-tr', async (req, res) => { try { const stock=req.app.locals.db.getStockById(Number(req.params.id)); if(!stock) return res.status(404).json({error:'Stock not found'}); const result=await req.app.locals.historyBackfill.syncTradeRepublic(stock); res.json({message:'Trade Republic history synchronized', ...result}); } catch(e) { res.status(e.message === 'verified Trade Republic mapping unavailable' ? 409 : 502).json({error:e.message || 'Trade Republic history unavailable'}); } });
app.get('/api/broker/trade-republic/mapping-candidates', async (req, res) => { try { const portfolio=await req.app.locals.brokerClient.getPortfolio(); res.json({candidates:portfolio.filter(x=>x.instrument).map(x=>({ticker:null,isin:x.instrument,quantity:x.quantity,account_ref_masked:x.account_ref_masked,match:'unresolved',reason:'Ticker-to-ISIN mapping requires explicit verification'}))}); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/quote/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at || mapping.currency !== 'EUR') return res.status(409).json({error:'Verified EUR broker mapping required'}); const quote=await req.app.locals.brokerClient.getQuote({isin:mapping.isin,exchange:mapping.exchange}); const quoteId=req.app.locals.db.addBrokerQuote({broker:'trade_republic',isin:mapping.isin,exchange:mapping.exchange,bid_eur:quote.bid_eur,ask_eur:quote.ask_eur,last_eur:quote.last_eur,currency:quote.currency,quote_time:quote.quote_time,observed_at:quote.observed_at,source:quote.source}).lastInsertRowid; res.json({...quote,quote_id:Number(quoteId),fresh:Date.now()-Date.parse(quote.observed_at)<15000}); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/sync', async (req, res) => {
  try {
    const client=req.app.locals.brokerClient, observed_at=new Date().toISOString();
    const [accounts,portfolio,orders,transactions]=await Promise.all([client.getAccounts(),client.getPortfolio(),client.getOrders(),client.getTransactions()]);
    const accountRows=accounts.filter(Boolean).map(a=>({...a,broker:'trade_republic',observed_at,raw_hash:null}));
    const orderRows=normalizedBrokerRows(orders,'orders',sanitizeCollection,observed_at);
    const transactionRows=normalizedBrokerRows(transactions,'transactions',sanitizeCollection,observed_at);
    const counts={accounts:req.app.locals.db.syncBrokerRows('broker_accounts',accountRows),portfolio:req.app.locals.db.syncBrokerRows('broker_portfolio',normalizedBrokerRows(portfolio,'portfolio',sanitizeCollection,observed_at).filter(x=>x.quantity!==null)),orders:req.app.locals.db.syncBrokerRows('broker_orders',orderRows),transactions:req.app.locals.db.syncBrokerRows('broker_transactions',transactionRows)};
    let journal_sync = null;
    try {
      journal_sync = await syncTradeRepublicExecutions({ db:req.app.locals.db, sdkClient:req.app.locals.sdkClient });
    } catch { journal_sync = { skipped:false, error:'Trade Republic execution history could not be synchronized' }; }
    const candidates=(journal_sync.skipped || journal_sync.error ? [...orderRows,...transactionRows] : []).filter(row=>['FILLED','EXECUTED'].includes(String(row.status).toUpperCase()) && !(row.execution_identity && req.app.locals.db.getBrokerExecution(row.execution_identity))).map(row=>{
      const complete=Boolean(row.execution_identity && row.instrument && row.side && row.quantity>0 && row.execution_price_eur>0 && row.execution_fees_eur!==null && row.execution_fees_eur!==undefined && row.executed_at);
      const candidateKey=crypto.createHash('sha256').update(JSON.stringify([row.external_ref_masked||null,row.occurred_at||null,row.title||null,row.amount_eur??null])).digest('hex');
      return {broker:'trade_republic',candidate_key:candidateKey,external_ref_masked:row.external_ref_masked,status:row.status,title:row.title||null,subtitle:row.subtitle||null,amount_eur:row.amount_eur,currency:row.currency,occurred_at:row.occurred_at,reason:complete?'Execution is not linked to an OpenStock order intent; review before journaling':'Authoritative ISIN, side, quantity, execution price, fees, or execution identity is missing; complete manually before journaling'};
    });
    const newCandidates=candidates.reduce((sum,candidate)=>sum+req.app.locals.db.addBrokerReconciliationCandidate(candidate).changes,0);
    res.json({broker:'trade_republic',observed_at,counts,new_reconciliation_candidates:newCandidates,pending_reconciliation:req.app.locals.db.getBrokerReconciliationCandidates().length,journal_sync});
  } catch(e) { brokerError(res,e); }
});
app.post('/api/broker/trade-republic/order-preview', async (req, res) => {
    try {
        const b = req.body || {};
        const db = req.app.locals.db;
        const stock = db.getStockById(Number(b.stock_id));
        if (stock?.asset_type === 'crypto') return res.status(409).json({ error: 'Crypto is available for monitoring and Journal only; manual crypto orders are disabled' });
        const mapping = db.getBrokerMapping(Number(b.stock_id));
        if (!mapping?.verified_at || mapping.currency !== 'EUR') return res.status(409).json({ error: 'Verified EUR mapping required' });
        const mode = String(b.mode || 'market');
        const validity = String(b.validity || 'GFD').toUpperCase();
        const limit = b.limit === '' || b.limit === undefined ? null : Number(b.limit);
        const stop = b.stop === '' || b.stop === undefined ? null : Number(b.stop);
        const valid = ['buy', 'sell'].includes(b.type) && ['market', 'limit', 'stopMarket'].includes(mode) && ['GFD', 'GTD', 'GTC'].includes(validity) && (mode !== 'limit' || limit > 0) && (mode === 'limit' || limit === null) && (mode !== 'stopMarket' || stop > 0) && (mode === 'stopMarket' || stop === null);
        if (!valid) return res.status(400).json({ error: 'Invalid mode, validity, limit, or stop' });
        const quote = await req.app.locals.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
        const side = b.type === 'buy' ? 'ask_eur' : 'bid_eur';
        if (!quote[side]) return res.status(502).json({ error: 'Fresh side quote unavailable' });
        const referencePrice = Number(quote[side]);
        const sizingPrice = mode === 'limit' ? Math.max(referencePrice, limit) : referencePrice;
        const sizing = normalizeOrderSizing(b, sizingPrice);
        if (!sizing.valid) return res.status(400).json({ error: sizing.error });
        const quantity = sizing.quantity;
        db.addBrokerQuote({ broker: 'trade_republic', isin: mapping.isin, exchange: mapping.exchange, bid_eur: quote.bid_eur, ask_eur: quote.ask_eur, last_eur: quote.last_eur || null, currency: quote.currency, quote_time: quote.quote_time, observed_at: quote.observed_at, source: quote.source });
        const fee = await req.app.locals.brokerClient.previewFees({ account_ref_masked: b.account_ref, exchange: mapping.exchange, instrument: mapping.isin, mode: mode, quantity: quantity, type: b.type, limit: limit, stop: stop, validity });
        const feeEur = Number(fee.fee_eur);
        const estimatedNotional = quantity * sizingPrice;
        const totalEur = Number.isFinite(feeEur) ? estimatedNotional + (b.type === 'buy' ? feeEur : -feeEur) : null;
        res.json({ broker: 'trade_republic', instrument: mapping.isin, exchange: mapping.exchange, side: b.type, quantity, sizing_mode: sizing.sizing_mode, requested_amount_eur: sizing.requested_amount_eur, sizing_price_eur: sizing.sizing_price_eur, reference_price_eur: referencePrice, fee_eur: fee.fee_eur, total_eur: totalEur, quote });
    } catch (e) { brokerError(res, e); }
});

async function authoritativeExecution(result) {
    const direct = result.execution_details || (result.execution && typeof result.execution === 'object' ? result.execution : null);
    if (!direct || !direct.identity || direct.fees_eur === null || direct.fees_eur === undefined || !direct.executed_at) return null;
    if (!(Number(direct.price_eur) > 0 && Number(direct.quantity) > 0 && Number.isFinite(Number(direct.fees_eur)) && Number(direct.fees_eur) >= 0)) return null;
    return Number.isNaN(Date.parse(direct.executed_at)) ? null : direct;
}

app.post('/api/broker/trade-republic/orders', async (req,res) => {
    const b=req.body||{}, db=req.app.locals.db, client=req.app.locals.brokerClient;
    const key=String(b.idempotency_key||'');
    const existing=key ? db.getBrokerOrderIntent(key) : null;
    if(existing) return res.status(existing.status==='REJECTED'?409:200).json({order:{idempotency_key:key,status:existing.status,external_ref_masked:existing.external_ref_masked||null},duplicate:true});
    const stock=db.getStockById(Number(b.stock_id));
    const mapping=db.getBrokerMapping(Number(b.stock_id));
    const side=String(b.type||'').toLowerCase();
    const rawMode=String(b.mode||'market');
    const mode=({market:'market',limit:'limit',stopmarket:'stopMarket'})[rawMode.toLowerCase()] || rawMode;
    const validity=String(b.validity||'GFD').toUpperCase();
    const limit=b.limit === undefined || b.limit === null || b.limit === '' ? null : Number(b.limit);
    const stop=b.stop === undefined || b.stop === null || b.stop === '' ? null : Number(b.stop);
    const maxCap=Number(process.env.TRADE_REPUBLIC_MAX_ORDER_EUR||0);
    const accountRef=String(b.account_ref||'');
    const preliminaryGates={
        live_enabled:process.env.TRADE_REPUBLIC_LIVE_ENABLED==='true',
        dry_run_disabled:process.env.TRADE_REPUBLIC_DRY_RUN==='false',
        kill_switch_clear:process.env.TRADE_REPUBLIC_KILL_SWITCH!=='true',
        acknowledged:process.env.TRADE_REPUBLIC_LIVE_ACK==='true',
        valid_asset_type:stock?.asset_type!=='crypto',
        mapped:Boolean(mapping&&mapping.verified_at&&mapping.currency==='EUR'),
        account_selected:Boolean(accountRef),
        confirmed:[`PLACE ${key}`,'PLACE ORDER'].includes(String(b.confirmation||'').trim()),
        idempotency:/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(key),
        valid_side:['buy','sell'].includes(side),
        valid_sizing_input:hasValidOrderSizingInput(b),
        valid_mode:['market','limit','stopMarket'].includes(mode),
        valid_validity:['GFD','GTD','GTC'].includes(validity),
        valid_limit:mode==='limit' ? Number.isFinite(limit)&&limit>0 : limit===null,
        valid_stop:mode==='stopMarket' ? Number.isFinite(stop)&&stop>0 : stop===null
    };
    if(Object.values(preliminaryGates).some(x=>!x)) {
        if(preliminaryGates.idempotency) db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id)||null,type:side,quantity:Number.isInteger(Number(b.quantity))?Number(b.quantity):null,sizing_mode:b.amount_eur ? 'amount' : 'quantity',requested_amount_eur:Number.isFinite(Number(b.amount_eur))?Number(b.amount_eur):null}),quote_id:null});
        return res.status(409).json({error:'Live order rejected: safety gate not satisfied',code:'ORDER_SAFETY_GATE',failed_gates:failedGateNames(preliminaryGates)});
    }
    let authority;
    try { authority=await fetchLiveOrderAuthority(client,{accountRef,isin:mapping.isin,exchange:mapping.exchange}); }
    catch(e) { return res.status(503).json({error:'Fresh Trade Republic account, position, or quote data is unavailable; no order was submitted',code:'LIVE_AUTHORITY_UNAVAILABLE'}); }
    const account=authority.account;
    const position=authority.position;
    const availableCash=Number(account?.available_cash_eur);
    const quote=authority.quote;
    const executable=side === 'buy' ? quote?.ask_eur : quote?.bid_eur;
    const conservativePrice=mode==='limit' ? Math.max(Number(executable)||0, Number.isFinite(limit) ? limit : 0) : Number(executable);
    const sizing=normalizeOrderSizing(b,conservativePrice);
    const quantity=sizing.valid?sizing.quantity:null;
    const quoteReady=Boolean(quote?.currency&&quote?.observed_at&&quote?.source);
    const q={...quote,id:quoteReady?Number(db.addBrokerQuote({broker:'trade_republic',isin:mapping.isin,exchange:mapping.exchange,bid_eur:quote.bid_eur,ask_eur:quote.ask_eur,last_eur:quote.last_eur||null,currency:quote.currency,quote_time:quote.quote_time,observed_at:quote.observed_at,source:quote.source}).lastInsertRowid):null};
    const gates={...preliminaryGates,
        notional_cap_ok:Number.isFinite(quantity)&&quantity>0&&Number.isFinite(conservativePrice)&&conservativePrice>0&&maxCap>0&&quantity*conservativePrice<=maxCap,
        fresh_quote:Boolean(q.observed_at && Date.now()-Date.parse(q.observed_at) >= 0 && Date.now()-Date.parse(q.observed_at) <= 15000),
        executable_quote:Boolean(executable), quote_eur:q.currency==='EUR',
        account_authoritative:Boolean(account&&Number.isFinite(availableCash)),
        account_resolvable:typeof client.resolveAccount !== 'function' || Boolean(client.resolveAccount(accountRef)),
        valid_quantity:Boolean(sizing.valid&&Number.isInteger(quantity)&&quantity>0),
        notional_cash_ok:side!=='buy'||availableCash>=quantity*conservativePrice,
        sufficient_position:side!=='sell'||(Boolean(position)&&Number(position.quantity)>=quantity)
    };
    if(Object.values(gates).some(x=>!x)) {
        db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id)||null,type:side,quantity:Number.isInteger(quantity)?quantity:null,sizing_mode:sizing.sizing_mode||null,requested_amount_eur:sizing.requested_amount_eur||null}),quote_id:q.id});
        return res.status(409).json({error:sizing.valid?'Live order rejected: safety gate not satisfied':sizing.error,code:'ORDER_SAFETY_GATE',failed_gates:failedGateNames(gates)});
    }
    const order={account_ref_masked:accountRef,exchange:mapping.exchange,instrument:mapping.isin,mode,quantity,type:side,validity};
    if (mode==='limit') order.limit=limit;
    if (mode==='stopMarket') order.stop=stop;
    let authoritativeFee;
    try {
        const preview=await client.previewFees(order);
        authoritativeFee=Number(preview?.fee_eur);
        if (!(Number.isFinite(authoritativeFee)&&authoritativeFee>=0&&String(preview?.currency||'').toUpperCase()==='EUR')) throw new Error('invalid fee preview');
    } catch(e) {
        return res.status(503).json({error:'Authoritative EUR fee preview unavailable; no order was submitted',code:'FEE_PREVIEW_UNAVAILABLE'});
    }
    const finalGates={cap_ok:quantity*conservativePrice+authoritativeFee<=maxCap,sufficient_cash:side!=='buy'||availableCash>=quantity*conservativePrice+authoritativeFee};
    if(Object.values(finalGates).some(x=>!x)) {
        db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id),type:side,quantity,sizing_mode:sizing.sizing_mode,requested_amount_eur:sizing.requested_amount_eur}),quote_id:q.id});
        return res.status(409).json({error:'Live order rejected: fee-inclusive safety gate not satisfied',gates:{...gates,...finalGates}});
    }
    db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'PROPOSED',request_json:JSON.stringify({stock_id:Number(b.stock_id),type:side,quantity,sizing_mode:sizing.sizing_mode,requested_amount_eur:sizing.requested_amount_eur,sizing_price_eur:sizing.sizing_price_eur,mode,limit,stop,validity,account_ref_masked:accountRef,fee_eur:authoritativeFee}),quote_id:q.id});
    const intent=db.getBrokerOrderIntent(key);
    try {
        const result=await client.placeOrder(order);
        const terminalRejected=['REJECTED','CANCELLED'].includes(String(result.status||'').toUpperCase());
        const filled = ['FILLED','EXECUTED'].includes(String(result.status || '').toUpperCase());
        let reconciled = null;
        let reconciliationWarning = null;
        const executionDetails = filled ? await authoritativeExecution(result) : null;
        if (executionDetails) {
            try {
                reconciled = db.recordBrokerExecution({ broker:'trade_republic', execution_identity:executionDetails.identity, order_intent_id:intent.id, stock_id:Number(b.stock_id), account_ref_masked:accountRef, side, quantity:executionDetails.quantity, execution_price_eur:executionDetails.price_eur, fees_eur:executionDetails.fees_eur, executed_at:executionDetails.executed_at, currency:'EUR' });
            } catch {
                reconciliationWarning='Confirmed fill could not be reconciled with Journal lots; manual review required';
            }
        }
        const frozenReason=terminalRejected?'Broker reported terminal rejection':result.status==='UNKNOWN'?'Broker response was inconclusive':reconciliationWarning;
        const executionState=filled?'filled':typeof result.execution==='string'?result.execution:'none';
        db.updateBrokerOrderIntent(intent.id,{status:terminalRejected?'REJECTED':filled?'FILLED':result.status==='UNKNOWN'?'UNKNOWN':'SUBMITTED',response_json:JSON.stringify({status:result.status,execution:executionState,reconciled:Boolean(reconciled)}),external_ref_masked:result.external_ref_masked,reconciled_at:reconciled ? new Date().toISOString() : null,frozen_reason:frozenReason});
        if (terminalRejected) return res.status(422).json({error:'Broker rejected or cancelled order',code:'ORDER_REJECTED',idempotency_key:key});
        const warning=reconciliationWarning || (filled&&!reconciled?'Fill lacked authoritative identity, price, fees, quantity, or timestamp; Journal unchanged':result.execution==='filled'?null:'Order ID/status does not prove a fill');
        return res.status(201).json({order:{idempotency_key:key,status:result.status,external_ref_masked:result.external_ref_masked,execution:executionState,reconciled:Boolean(reconciled)},warning});
    } catch(e) {
        if (e.code==='REJECTED' || [400,409,422].includes(e.status)) {
            db.updateBrokerOrderIntent(intent.id,{status:'REJECTED',response_json:JSON.stringify({code:'REJECTED'}),external_ref_masked:null,reconciled_at:null,frozen_reason:'Broker deterministically rejected the order'});
            return res.status(e.status || 422).json({error:'Broker rejected order',code:'ORDER_REJECTED',idempotency_key:key});
        }
        let reconciliation='inconclusive';
        if(e.code==='TIMEOUT'||e.code==='UNAVAILABLE') {
            try { await client.getOrders(); reconciliation='checked_without_authoritative_match'; } catch { reconciliation='reconciliation_unavailable'; }
        }
        db.updateBrokerOrderIntent(intent.id,{status:'UNKNOWN',response_json:JSON.stringify({code:e.code||'BROKER_ERROR'}),external_ref_masked:null,reconciled_at:null,frozen_reason:`Submission ambiguous; ${reconciliation}. Manual reconciliation required.`});
        return res.status(503).json({error:'Order outcome is unknown and is frozen; reconcile before retrying',code:'ORDER_UNKNOWN',idempotency_key:key});
    }
});
};
