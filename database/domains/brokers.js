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
    getBrokerSyncState(syncKey) {
        return this.db.prepare('SELECT sync_key,updated_at,changed_at FROM broker_sync_state WHERE sync_key=?').get(syncKey) || null;
    }
    setBrokerSyncState(syncKey, updatedAt) {
        return this.db.prepare(`INSERT INTO broker_sync_state (sync_key,updated_at) VALUES (?,?)
            ON CONFLICT(sync_key) DO UPDATE SET updated_at=MAX(broker_sync_state.updated_at,excluded.updated_at),changed_at=datetime('now')`).run(syncKey, updatedAt);
    }
    resolveBrokerReconciliationCandidate(candidateKey) {
        return this.db.prepare("UPDATE broker_reconciliation_candidates SET resolved_at=datetime('now') WHERE candidate_key=? AND resolved_at IS NULL").run(candidateKey);
    }
    recordBrokerExecutionAgainstManualTrade(input) {
        if (input.side !== 'sell') return null;
        const identity = executionKey(input.broker, input.execution_identity);
        const existing = this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity IN (?, ?) ORDER BY CASE WHEN execution_identity=? THEN 0 ELSE 1 END LIMIT 1').get(identity, input.execution_identity, identity);
        if (existing) return { existing: true, execution: existing };
        const executedAt = input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, '');
        const fills = this.db.prepare(`SELECT * FROM trades WHERE stock_id=? AND closed_at IS NOT NULL AND trade_id NOT LIKE 'TR-%'
            AND date(closed_at)=date(?) AND ABS(quantity-?)<=0.000001`).all(input.stock_id, executedAt, input.quantity);
        const priceTolerance = Math.max(0.005, Math.abs(Number(input.execution_price_eur)) * 0.00002);
        const candidates = fills.filter(trade => Math.abs(Number(trade.exit_price_eur) - Number(input.execution_price_eur)) <= priceTolerance
            && Math.abs(Date.parse(`${trade.closed_at.replace(' ', 'T')}Z`) - Date.parse(`${executedAt.replace(' ', 'T')}Z`)) <= 6 * 60 * 60 * 1000);
        if (candidates.length !== 1) return null;
        const matchedTrade = candidates[0];
        const recordedAt = executedAt >= matchedTrade.opened_at ? executedAt : matchedTrade.closed_at;
        const execution = this.db.transaction(() => {
            this.db.prepare(`INSERT INTO broker_executions (broker,execution_identity,order_intent_id,stock_id,account_ref_masked,side,quantity,execution_price_eur,fees_eur,executed_at,currency)
                VALUES (@broker,@execution_identity,@order_intent_id,@stock_id,@account_ref_masked,@side,@quantity,@execution_price_eur,@fees_eur,@executed_at,@currency)`)
                .run({ ...input, execution_identity: identity });
            this.db.prepare(`UPDATE trades SET closed_at=?,exit_price=?,exit_fx_rate=1,exit_price_eur=?,exit_fee_eur=?,
                notes=CASE WHEN instr(COALESCE(notes,''),'Trade Republic confirmed execution')>0 THEN notes
                    WHEN trim(COALESCE(notes,''))='' THEN 'Trade Republic confirmed execution'
                    ELSE trim(notes) || ' | Trade Republic confirmed execution' END,updated_at=datetime('now') WHERE id=?`)
                .run(recordedAt, input.execution_price_eur, input.execution_price_eur, input.fees_eur, matchedTrade.id);
            const sourceId = `BROKER-EXEC-${identity}`;
            const source = JSON.stringify({ kind: 'broker_execution', side: 'sell', quantity: input.quantity, execution_price_eur: input.execution_price_eur, fees_eur: input.fees_eur, executed_at: input.executed_at });
            this.db.prepare('INSERT OR IGNORE INTO trade_merge_events (target_trade_id,source_trade_id,source_trade_json) VALUES (?,?,?)').run(matchedTrade.id, sourceId, source);

            // When an exact manual closing row covers the sale and the remaining
            // open rows are broker-created rebuys within that position, retain
            // their source legs under the manual main trade and clear the false
            // open balance. Any earlier, unrecorded opening quantity stays in
            // the manual main trade and its original cost basis.
            const openRows = this.db.prepare('SELECT * FROM trades WHERE stock_id=? AND closed_at IS NULL ORDER BY opened_at,id').all(input.stock_id);
            const brokerRows = openRows.filter(row => String(row.trade_id).startsWith('TR-EXEC-'));
            const hasOtherOpenRows = openRows.some(row => !String(row.trade_id).startsWith('TR-EXEC-'));
            const brokerQuantity = brokerRows.reduce((sum, row) => sum + Number(row.quantity), 0);
            const quantityTolerance = Math.max(1e-9, Math.abs(Number(input.quantity)) * 1e-12);
            if (brokerRows.length && !hasOtherOpenRows && brokerQuantity <= Number(input.quantity) + quantityTolerance) {
                for (const row of brokerRows) {
                    this.db.prepare('UPDATE trade_merge_events SET target_trade_id=? WHERE target_trade_id=?').run(matchedTrade.id, row.id);
                    this.db.prepare('INSERT OR IGNORE INTO trade_merge_events (target_trade_id,source_trade_id,source_trade_json) VALUES (?,?,?)')
                        .run(matchedTrade.id, row.trade_id, JSON.stringify(row));
                    this.db.prepare('DELETE FROM trades WHERE id=?').run(row.id);
                }
                const missingOpeningQuantity = Number(input.quantity) - brokerQuantity;
                if (missingOpeningQuantity > quantityTolerance) {
                    const note = `Broker buy history is short by ${missingOpeningQuantity.toFixed(6)} shares; the opening basis remains from the existing Journal row.`;
                    this.db.prepare("UPDATE trades SET notes=CASE WHEN instr(COALESCE(notes,''),?)>0 THEN notes WHEN trim(COALESCE(notes,''))='' THEN ? ELSE trim(notes) || ' | ' || ? END WHERE id=?")
                        .run(note, note, note, matchedTrade.id);
                }
            }
            return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity=?').get(identity);
        })();
        return { existing: false, execution, matchedTrade: this.db.prepare('SELECT * FROM trades WHERE id=?').get(matchedTrade.id) };
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
                const quantityTolerance = Math.max(1e-9, Math.abs(Number(input.quantity)) * 1e-12);
                const lots = this.db.prepare('SELECT * FROM trades WHERE stock_id=? AND closed_at IS NULL ORDER BY opened_at, id').all(input.stock_id);
                for (const lot of lots) {
                    if (remaining <= quantityTolerance) break;
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
                if (remaining > quantityTolerance) throw new Error('Execution would oversell Journal lots');
                const roundingResidue = 'Sub-1e-12 share remainder closed as broker sale rounding residue';
                this.db.prepare(`UPDATE trades SET closed_at=?,exit_price=?,exit_fx_rate=1,exit_price_eur=?,exit_fee_eur=0,
                    notes=CASE WHEN instr(COALESCE(notes,''),?)>0 THEN notes WHEN trim(COALESCE(notes,''))='' THEN ? ELSE trim(notes) || ' | ' || ? END,
                    updated_at=datetime('now') WHERE stock_id=? AND closed_at IS NULL AND quantity>0 AND quantity<1e-12`)
                    .run(input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, ''), input.execution_price_eur, input.execution_price_eur, roundingResidue, roundingResidue, roundingResidue, input.stock_id);
            }
            return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity=?').get(identity);
        })();
        return { existing: false, execution };
    }
    rebuildBrokerExecutionJournal(stockId) {
        const stock = this.getStockById(stockId);
        if (!stock) throw new Error('Stock not found');
        const currentTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id=? ORDER BY opened_at,id').all(stock.id);
        if (currentTrades.some(trade => !String(trade.trade_id || '').startsWith('TR-EXEC-') && !String(trade.trade_id || '').startsWith('TR-SELL-'))) {
            throw new Error('Cannot rebuild broker journal while external or manual trades exist for this instrument');
        }
        const executions = this.db.prepare('SELECT * FROM broker_executions WHERE stock_id=? ORDER BY executed_at,id').all(stock.id);
        if (!executions.length) throw new Error('No broker executions exist for this instrument');
        const currentIds = currentTrades.map(trade => Number(trade.id));
        const placeholders = currentIds.map(() => '?').join(',');
        const auditEvents = currentIds.length
            ? this.db.prepare(`SELECT * FROM trade_merge_events WHERE target_trade_id IN (${placeholders}) AND source_trade_id LIKE 'AUDIT-DUPLICATE-MANUAL-%' ORDER BY id`).all(...currentIds)
            : [];
        const rebuild = this.db.transaction(() => {
            if (currentIds.length) {
                this.db.prepare(`DELETE FROM trade_merge_events WHERE target_trade_id IN (${placeholders})`).run(...currentIds);
                this.db.prepare(`DELETE FROM trades WHERE id IN (${placeholders})`).run(...currentIds);
            }
            const targetByBuyIdentity = new Map();
            for (const execution of executions) {
                const identity = String(execution.execution_identity);
                const label = identity.slice(0, 20);
                const when = String(execution.executed_at).replace('T', ' ').replace(/\.\d{3}Z$/, '');
                const quantity = Number(execution.quantity);
                const price = Number(execution.execution_price_eur);
                const fees = Number(execution.fees_eur || 0);
                if (!(quantity > 0) || !(price > 0) || !Number.isFinite(fees) || fees < 0 || !['buy', 'sell'].includes(execution.side)) {
                    throw new Error(`Invalid saved broker execution ${label}`);
                }
                if (execution.side === 'buy') {
                    const result = this.addTrade({
                        trade_id: `TR-EXEC-${label}`, stock_id: Number(stock.id), opened_at: when, closed_at: null,
                        quantity, currency: 'EUR', entry_price: price, entry_fx_rate: 1, entry_price_eur: price,
                        entry_fee_eur: fees, exit_price: null, exit_fx_rate: null, exit_price_eur: null,
                        exit_fee_eur: 0, dividend_eur: 0, strategy: 'trade_republic_execution', notes: 'Trade Republic confirmed execution'
                    });
                    targetByBuyIdentity.set(label, Number(result.lastInsertRowid));
                    continue;
                }

                let remaining = quantity;
                let remainingFee = fees;
                const quantityTolerance = Math.max(1e-9, Math.abs(quantity) * 1e-12);
                const lots = this.db.prepare('SELECT * FROM trades WHERE stock_id=? AND closed_at IS NULL ORDER BY opened_at,id').all(stock.id);
                for (const lot of lots) {
                    if (remaining <= quantityTolerance) break;
                    const lotQuantity = Number(lot.quantity);
                    const sold = Math.min(remaining, lotQuantity);
                    const feeShare = sold === remaining ? remainingFee : fees * sold / quantity;
                    if (sold === lotQuantity) {
                        this.db.prepare(`UPDATE trades SET closed_at=?,exit_price=?,exit_fx_rate=1,exit_price_eur=?,exit_fee_eur=?,
                            notes=CASE WHEN instr(COALESCE(notes,''),'Trade Republic confirmed execution')>0 THEN notes
                                WHEN trim(COALESCE(notes,''))='' THEN 'Trade Republic confirmed execution'
                                ELSE trim(notes) || ' | Trade Republic confirmed execution' END,
                            updated_at=datetime('now') WHERE id=?`).run(when, price, price, feeShare, lot.id);
                    } else {
                        const closedEntryFee = Number(lot.entry_fee_eur) * sold / lotQuantity;
                        this.db.prepare('UPDATE trades SET quantity=quantity-?,entry_fee_eur=entry_fee_eur-?,updated_at=datetime(\'now\') WHERE id=?')
                            .run(sold, closedEntryFee, lot.id);
                        this.db.prepare(`INSERT INTO trades (trade_id,stock_id,opened_at,closed_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur,entry_fee_eur,exit_price,exit_fx_rate,exit_price_eur,exit_fee_eur,dividend_eur,strategy,notes)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
                            `TR-SELL-${label}-${lot.id}`, lot.stock_id, lot.opened_at, when, sold, lot.currency,
                            lot.entry_price, lot.entry_fx_rate, lot.entry_price_eur, closedEntryFee,
                            price, 1, price, feeShare, 0, 'trade_republic_execution', 'Trade Republic confirmed execution'
                        );
                    }
                    remaining -= sold;
                    remainingFee -= feeShare;
                }
                if (remaining > quantityTolerance) throw new Error(`Execution would oversell Journal lots for ${stock.symbol} by ${remaining}`);
                const roundingResidue = 'Sub-1e-12 share remainder closed as broker sale rounding residue';
                this.db.prepare(`UPDATE trades SET closed_at=?,exit_price=?,exit_fx_rate=1,exit_price_eur=?,exit_fee_eur=0,
                    notes=CASE WHEN instr(COALESCE(notes,''),?)>0 THEN notes WHEN trim(COALESCE(notes,''))='' THEN ? ELSE trim(notes) || ' | ' || ? END,
                    updated_at=datetime('now') WHERE stock_id=? AND closed_at IS NULL AND quantity>0 AND quantity<1e-12`)
                    .run(when, price, price, roundingResidue, roundingResidue, roundingResidue, stock.id);
            }

            for (const event of auditEvents) {
                let source;
                try { source = JSON.parse(event.source_trade_json); }
                catch { throw new Error(`Cannot restore malformed manual duplicate audit ${event.source_trade_id}`); }
                const targets = [...new Set((source.broker_buy_execution_ids || []).map(identity => targetByBuyIdentity.get(String(identity).slice(0, 20))).filter(Boolean))];
                if (targets.length !== 1) throw new Error(`Cannot map manual duplicate audit ${event.source_trade_id} to one rebuilt position`);
                this.db.prepare('INSERT INTO trade_merge_events (target_trade_id,source_trade_id,source_trade_json,merged_at) VALUES (?,?,?,?)')
                    .run(targets[0], event.source_trade_id, event.source_trade_json, event.merged_at);
            }
            const rebuiltTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id=?').all(stock.id);
            return {
                executions: executions.length,
                trades: rebuiltTrades.length,
                closed_trades: rebuiltTrades.filter(trade => trade.closed_at).length,
                open_trades: rebuiltTrades.filter(trade => !trade.closed_at).length,
                audit_records_restored: auditEvents.length
            };
        });
        return rebuild();
    }
}

module.exports = BrokersMethods;
