'use strict';

const LedgerMethods = require('./ledger');
const crypto = require('node:crypto');
function executionKey(broker, identity) {
    return crypto.createHash('sha256').update(`${broker}:${identity}`).digest('hex');
}

class BrokersMethods extends LedgerMethods {
    getBrokerMappings() { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_instrument_mappings'").get()) return []; return this.db.prepare(`SELECT m.*, s.name FROM broker_instrument_mappings m JOIN stocks s ON s.id=m.stock_id ORDER BY m.ticker`).all(); }
    getBrokerMapping(stockId) { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_instrument_mappings'").get()) return null; return this.db.prepare('SELECT * FROM broker_instrument_mappings WHERE stock_id=?').get(stockId); }
    saveBrokerMapping(input) { return this.db.prepare(`INSERT INTO broker_instrument_mappings (stock_id,broker,ticker,isin,exchange,currency,broker_metadata_json,source,verified_at) VALUES (@stock_id,@broker,@ticker,@isin,@exchange,@currency,@broker_metadata_json,@source,@verified_at) ON CONFLICT(stock_id) DO UPDATE SET broker=excluded.broker,ticker=excluded.ticker,isin=excluded.isin,exchange=excluded.exchange,currency=excluded.currency,broker_metadata_json=excluded.broker_metadata_json,source=excluded.source,verified_at=excluded.verified_at,updated_at=datetime('now')`).run(input); }
    addBrokerQuote(input) { return this.db.prepare(`INSERT INTO broker_quotes (broker,isin,exchange,bid_eur,ask_eur,last_eur,currency,quote_time,observed_at,source) VALUES (@broker,@isin,@exchange,@bid_eur,@ask_eur,@last_eur,@currency,@quote_time,@observed_at,@source)`).run(input); }
    getLatestBrokerQuote(isin, exchange) { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_quotes'").get()) return null; return this.db.prepare('SELECT * FROM broker_quotes WHERE broker=? AND isin=? AND exchange=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', isin, exchange); }
    getLatestBrokerAccount(accountRefMasked, maxAgeMs = 300000) {
        const row = this.db.prepare('SELECT * FROM broker_accounts WHERE broker=? AND account_ref_masked=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', accountRefMasked);
        return row && Date.now() - Date.parse(row.observed_at) >= 0 && Date.now() - Date.parse(row.observed_at) <= maxAgeMs ? row : null;
    }
    getLatestBrokerPosition(accountRefMasked, isin, maxAgeMs = 300000) {
        const row = this.db.prepare('SELECT * FROM broker_portfolio WHERE broker=? AND account_ref_masked=? AND isin=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', accountRefMasked, isin);
        return row && Date.now() - Date.parse(row.observed_at) >= 0 && Date.now() - Date.parse(row.observed_at) <= maxAgeMs ? row : null;
    }
    syncBrokerRows(table, rows) {
        const definitions = {
            broker_accounts: { columns:['broker','account_ref_masked','currency','cash_eur','available_cash_eur','observed_at','raw_hash'], key:['broker','account_ref_masked'] },
            broker_portfolio: { columns:['broker','account_ref_masked','isin','quantity','currency','value_eur','observed_at','external_ref_masked'], key:['broker','account_ref_masked','isin','observed_at'] },
            broker_orders: { columns:['broker','external_ref_masked','status','isin','side','quantity','amount_eur','occurred_at','observed_at','raw_hash'], key:['broker','external_ref_masked'] },
            broker_transactions: { columns:['broker','external_ref_masked','status','isin','quantity','amount_eur','occurred_at','observed_at','raw_hash'], key:['broker','external_ref_masked'] }
        };
        const definition = definitions[table];
        if (!definition) throw new Error('Invalid broker table');
        if (!rows.length) return 0;
        const updates = definition.columns.filter(column => !definition.key.includes(column)).map(column => `${column}=excluded.${column}`).join(',');
        const stmt = this.db.prepare(`INSERT INTO ${table} (${definition.columns.join(',')}) VALUES (${definition.columns.map(key=>`@${key}`).join(',')}) ON CONFLICT(${definition.key.join(',')}) DO UPDATE SET ${updates}`);
        return this.db.transaction(() => rows.reduce((count, row) => count + stmt.run(row).changes, 0))();
    }
    addBrokerReconciliationCandidate(input) { return this.db.prepare(`INSERT OR IGNORE INTO broker_reconciliation_candidates (broker,candidate_key,external_ref_masked,status,title,subtitle,amount_eur,currency,occurred_at,reason) VALUES (@broker,@candidate_key,@external_ref_masked,@status,@title,@subtitle,@amount_eur,@currency,@occurred_at,@reason)`).run(input); }
    getBrokerReconciliationCandidates() { return this.db.prepare('SELECT id,broker,candidate_key,external_ref_masked,status,title,subtitle,amount_eur,currency,occurred_at,reason,created_at FROM broker_reconciliation_candidates WHERE resolved_at IS NULL ORDER BY occurred_at DESC, id DESC').all(); }
    createBrokerOrderIntent(input) { return this.db.prepare(`INSERT INTO broker_order_intents (broker,idempotency_key,status,request_json,quote_id) VALUES (@broker,@idempotency_key,@status,@request_json,@quote_id)`).run(input); }
    getBrokerOrderIntent(key) { return this.db.prepare('SELECT * FROM broker_order_intents WHERE idempotency_key=?').get(key); }
    updateBrokerOrderIntent(id, input) { return this.db.prepare(`UPDATE broker_order_intents SET status=@status, response_json=@response_json, external_ref_masked=@external_ref_masked, reconciled_at=@reconciled_at, frozen_reason=@frozen_reason, updated_at=datetime('now') WHERE id=@id`).run({ ...input, id }); }
    getBrokerExecution(identity, broker = 'trade_republic') {
        const hashedIdentity = executionKey(broker, identity);
        return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity IN (?, ?) ORDER BY CASE WHEN execution_identity=? THEN 0 ELSE 1 END LIMIT 1').get(hashedIdentity, identity, hashedIdentity);
    }
    recordBrokerExecution(input) {
        const identity = executionKey(input.broker, input.execution_identity);
        const existing = this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity IN (?, ?) ORDER BY CASE WHEN execution_identity=? THEN 0 ELSE 1 END LIMIT 1').get(identity, input.execution_identity, identity);
        if (existing) return { existing: true, execution: existing };
        const execution = this.db.transaction(() => {
            this.db.prepare(`INSERT INTO broker_executions (broker,execution_identity,order_intent_id,stock_id,account_ref_masked,side,quantity,execution_price_eur,fees_eur,executed_at,currency) VALUES (@broker,@execution_identity,@order_intent_id,@stock_id,@account_ref_masked,@side,@quantity,@execution_price_eur,@fees_eur,@executed_at,@currency)`).run({ ...input, execution_identity: identity });
            const executionLabel = identity.slice(0, 20);
            const note = 'Trade Republic confirmed execution';
            if (input.side === 'buy') {
                this.addTrade({ trade_id: `TR-EXEC-${executionLabel}`, stock_id: input.stock_id, opened_at: input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, ''), closed_at: null, quantity: input.quantity, currency: 'EUR', entry_price: input.execution_price_eur, entry_fx_rate: 1, entry_price_eur: input.execution_price_eur, entry_fee_eur: input.fees_eur, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: 'trade_republic_execution', notes: note });
            } else {
                let remaining = Number(input.quantity);
                let remainingFee = Number(input.fees_eur);
                const lots = this.db.prepare('SELECT * FROM trades WHERE stock_id=? AND closed_at IS NULL ORDER BY opened_at, id').all(input.stock_id);
                for (const lot of lots) {
                    if (remaining <= 0) break;
                    const lotQuantity = Number(lot.quantity);
                    const sold = Math.min(remaining, lotQuantity);
                    const feeShare = sold === remaining ? remainingFee : Number(input.fees_eur) * sold / Number(input.quantity);
                    const when = input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, '');
                    if (sold === lotQuantity) this.db.prepare("UPDATE trades SET closed_at=?, exit_price=?, exit_fx_rate=1, exit_price_eur=?, exit_fee_eur=?, notes=CASE WHEN trim(COALESCE(notes,''))='' THEN ? ELSE trim(notes) || ' | ' || ? END, updated_at=datetime('now') WHERE id=?").run(when, input.execution_price_eur, input.execution_price_eur, feeShare, note, note, lot.id);
                    else {
                        const closedEntryFee = Number(lot.entry_fee_eur) * sold / lotQuantity;
                        this.db.prepare("UPDATE trades SET quantity=quantity-?, entry_fee_eur=entry_fee_eur-?, updated_at=datetime('now') WHERE id=?").run(sold, closedEntryFee, lot.id);
                        this.db.prepare(`INSERT INTO trades (trade_id,stock_id,opened_at,closed_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur,entry_fee_eur,exit_price,exit_fx_rate,exit_price_eur,exit_fee_eur,dividend_eur,strategy,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(`TR-SELL-${executionLabel}-${lot.id}`, lot.stock_id, lot.opened_at, when, sold, lot.currency, lot.entry_price, lot.entry_fx_rate, lot.entry_price_eur, closedEntryFee, input.execution_price_eur, 1, input.execution_price_eur, feeShare, 0, 'trade_republic_execution', note);
                    }
                    remaining -= sold;
                    remainingFee -= feeShare;
                }
                if (remaining > 0) throw new Error('Execution would oversell Journal lots');
            }
            return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity=?').get(identity);
        })();
        return { existing: false, execution };
    }
}

module.exports = BrokersMethods;
