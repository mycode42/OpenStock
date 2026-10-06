'use strict';

const DatabaseCore = require('./core');

class StocksMethods extends DatabaseCore {
    getAllStocks() {
        return this.db.prepare('SELECT * FROM stocks ORDER BY symbol ASC').all();
    }

    getConfigStocks() {
        return this.db.prepare('SELECT * FROM stocks WHERE archived = 0 ORDER BY symbol ASC').all();
    }

    getActiveStocks() {
        return this.db.prepare('SELECT * FROM stocks WHERE archived = 0 AND enabled = 1 ORDER BY symbol ASC').all();
    }

    getJournalStocks() {
        return this.db.prepare(`
            SELECT s.*, EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id) AS has_journal_history
            FROM stocks s WHERE s.archived = 0 OR EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id)
            ORDER BY s.symbol ASC
        `).all();
    }

    getArchivedStocks() {
        return this.db.prepare(`
            SELECT s.*, EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id) AS has_journal_history
            FROM stocks s WHERE s.archived = 1 ORDER BY s.symbol ASC
        `).all();
    }

    getEnabledStocks() {
        return this.db.prepare(`
            SELECT id, symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur,
                   enabled, archived, trigger_state, price_source
            FROM stocks
            WHERE enabled = 1 AND archived = 0
            ORDER BY symbol ASC
        `).all();
    }

    getStockById(id) {
        return this.db.prepare('SELECT * FROM stocks WHERE id = ?').get(id);
    }

    getStockByBrokerInstrument(instrumentId) {
        return this.db.prepare(`SELECT s.* FROM stocks s
            LEFT JOIN broker_instrument_mappings m ON m.stock_id=s.id AND m.broker='trade_republic'
            WHERE s.isin=? OR m.isin=? ORDER BY s.archived, s.id LIMIT 1`).get(instrumentId, instrumentId) || null;
    }

    ensureBrokerJournalStock({ instrumentId, symbol, name, currency = 'EUR', assetType = 'stock' }) {
        const ensure = this.db.transaction(() => {
            const existing = this.getStockByBrokerInstrument(instrumentId);
            if (existing) return existing;
            const base = String(symbol || instrumentId).trim().toUpperCase().replace(/[^A-Z0-9.\-]/g, '').slice(0, 16) || 'ASSET';
            let uniqueSymbol = base;
            if (this.db.prepare('SELECT 1 FROM stocks WHERE symbol=?').get(uniqueSymbol)) {
                const suffix = String(instrumentId).replace(/[^A-Z0-9]/gi, '').slice(-5).toUpperCase() || 'TR';
                uniqueSymbol = `${base.slice(0, 14)}-${suffix}`;
                let collision = 2;
                while (this.db.prepare('SELECT 1 FROM stocks WHERE symbol=?').get(uniqueSymbol)) {
                    uniqueSymbol = `${base.slice(0, 12)}-${suffix}-${collision++}`;
                }
            }
            const insert = this.db.prepare(`INSERT INTO stocks
                (symbol,isin,name,currency,buy_threshold_eur,sell_threshold_eur,enabled,archived,price_source,asset_type)
                VALUES (?,?,?,?,0,0,0,0,'trade_republic',?)`);
            const row = insert.run(uniqueSymbol, instrumentId, name || uniqueSymbol, currency, assetType);
            return this.getStockById(row.lastInsertRowid);
        });
        return ensure.immediate();
    }

    getStockBySymbol(symbol) {
        return this.db.prepare('SELECT * FROM stocks WHERE symbol = ?').get(symbol);
    }

    getLatestPriceObservation(stockId) {
        return this.db.prepare('SELECT source, quote_time, recorded_at FROM price_history WHERE stock_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1').get(stockId) || null;
    }

    getPositionSummary(stockId) {
        const open = this.db.prepare(`
            SELECT
                COALESCE(SUM(quantity), 0) AS quantity,
                CASE WHEN SUM(quantity) > 0
                    THEN SUM(quantity * entry_price_eur) / SUM(quantity)
                    ELSE NULL
                END AS buy_in_price_eur
            FROM trades
            WHERE stock_id = ? AND closed_at IS NULL
        `).get(stockId);
        const quantity = Number(open.quantity || 0);
        return {
            quantity,
            owned: quantity > 0,
            source: 'journal',
            buy_in_price_eur: quantity > 0 ? Number(open.buy_in_price_eur) : null,
            warning: quantity > 0 ? null : 'No open Journal position; SELL alerts are suppressed.'
        };
    }

    getAlertLevels(stockId) { return this.db.prepare('SELECT al.*, s.symbol FROM alert_levels al JOIN stocks s ON s.id = al.stock_id WHERE al.stock_id = ? ORDER BY al.side, al.price_eur, al.id').all(stockId); }
    getActiveAlertLevels(stockId) { return this.db.prepare('SELECT * FROM alert_levels WHERE stock_id = ? AND enabled = 1 AND active = 1 AND triggered = 0 AND retired = 0 ORDER BY side, price_eur, id').all(stockId); }
    getAlertLevelById(id) { return this.db.prepare('SELECT al.*, s.symbol FROM alert_levels al JOIN stocks s ON s.id = al.stock_id WHERE al.id = ?').get(id); }
    addAlertLevel(stockId, side, priceEur) {
        if (!this.getStockById(stockId)) throw new Error('Stock not found');
        if (!['BUY', 'SELL'].includes(String(side).toUpperCase())) throw new Error('Side must be BUY or SELL');
        if (!Number.isFinite(Number(priceEur)) || Number(priceEur) <= 0) throw new Error('Alert price must be positive');
        const result = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)').run(stockId, String(side).toUpperCase(), Number(priceEur)); return this.getAlertLevelById(result.lastInsertRowid);
    }
    addPercentAlertLevel(stockId, direction, changePct) {
        const stock = this.getStockById(stockId);
        if (!stock) throw new Error('Stock not found');
        const move = String(direction).toUpperCase();
        if (!['DROP', 'GAIN'].includes(move)) throw new Error('Direction must be DROP or GAIN');
        const pct = Number(changePct);
        if (!Number.isFinite(pct) || pct <= 0 || pct >= (move === 'DROP' ? 100 : 10000)) throw new Error('Percentage must be positive and below the maximum for that direction');
        const reference = Number(stock.current_price_eur);
        if (!Number.isFinite(reference) || reference <= 0) throw new Error('A saved EUR quote is required before adding a percentage alert');
        const side = move === 'DROP' ? 'BUY' : 'SELL';
        const moveAmount = reference * pct / 100;
        const target = move === 'DROP' ? reference - moveAmount : reference + moveAmount;
        const result = this.db.prepare(`INSERT INTO alert_levels (stock_id, side, price_eur, trigger_mode, change_pct, reference_price_eur)
            VALUES (?, ?, ?, 'PERCENT', ?, ?)`).run(stockId, side, target, pct, reference);
        return this.getAlertLevelById(result.lastInsertRowid);
    }
    updateAlertLevel(id, updates) {
        const existing = this.getAlertLevelById(id);
        if (!existing) throw new Error('Alert level not found');
        if (updates.side !== undefined && !['BUY', 'SELL'].includes(String(updates.side).toUpperCase())) throw new Error('Side must be BUY or SELL');
        if (existing.trigger_mode === 'PERCENT' && updates.price_eur !== undefined) throw new Error('The target price for a percentage alert is calculated from its reference price');
        if (updates.price_eur !== undefined && (!Number.isFinite(Number(updates.price_eur)) || Number(updates.price_eur) <= 0)) throw new Error('Alert price must be positive');
        if (updates.change_pct !== undefined && (existing.trigger_mode !== 'PERCENT' || !Number.isFinite(Number(updates.change_pct)) || Number(updates.change_pct) <= 0 || Number(updates.change_pct) >= (existing.side === 'BUY' ? 100 : 10000))) throw new Error('Percentage alert value is invalid');
        for (const key of ['enabled', 'active']) if (updates[key] !== undefined && ![0, 1, true, false].includes(updates[key])) throw new Error(`${key} must be boolean`);
        if ((existing.triggered || existing.retired) && (updates.enabled === 1 || updates.enabled === true || updates.active === 1 || updates.active === true)) throw new Error('Retired alert levels must be rearmed explicitly');

        const normalized = {};
        if (updates.side !== undefined) normalized.side = String(updates.side).toUpperCase();
        if (updates.price_eur !== undefined) normalized.price_eur = Number(updates.price_eur);
        if (updates.change_pct !== undefined) {
            normalized.change_pct = Number(updates.change_pct);
            const moveAmount = Number(existing.reference_price_eur) * normalized.change_pct / 100;
            normalized.price_eur = existing.side === 'BUY' ? Number(existing.reference_price_eur) - moveAmount : Number(existing.reference_price_eur) + moveAmount;
        }
        if (updates.enabled !== undefined) normalized.enabled = updates.enabled ? 1 : 0;
        if (updates.active !== undefined) normalized.active = updates.active ? 1 : 0;
        if (normalized.enabled === 0) normalized.active = 0;
        if (normalized.enabled === 1 && updates.active === undefined) normalized.active = 1;
        const effectiveEnabled = normalized.enabled ?? Number(existing.enabled);
        if (normalized.active === 1 && effectiveEnabled !== 1) throw new Error('An active alert level must be enabled');

        const entries = Object.entries(normalized);
        if (!entries.length) throw new Error('No valid alert level fields to update');
        const result = this.db.prepare(`UPDATE alert_levels SET ${entries.map(([key]) => `${key} = ?`).join(', ')} WHERE id = ?`).run(...entries.map(([, value]) => value), id);
        if (!result.changes) throw new Error('Alert level not found');
        return this.getAlertLevelById(id);
    }
    deleteAlertLevel(id) { return this.db.prepare('DELETE FROM alert_levels WHERE id = ?').run(id); }
    rearmAlertLevel(id) {
        this.db.transaction(() => {
            // A level's outbox row represents one notification occurrence. Retaining
            // a prior SENT row would let the monitor retire a rearmed level without
            // issuing a fresh Telegram request.
            this.db.prepare('DELETE FROM alert_outbox WHERE alert_level_id=?').run(id);
            this.db.prepare("UPDATE alert_levels SET enabled=1, active=1, triggered=0, retired=0, triggered_at=NULL, retired_at=NULL WHERE id=?").run(id);
        })();
        return this.getAlertLevelById(id);
    }
    retireAlertLevel(id) { return this.db.prepare("UPDATE alert_levels SET enabled=0, active=0, triggered=1, retired=1, triggered_at=datetime('now'), retired_at=datetime('now') WHERE id=?").run(id); }

    addStock(symbol, name, currency, buyThreshold, sellThreshold, isin = null) {
        const stmt = this.db.prepare(`
            INSERT INTO stocks (symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur)
            VALUES (?, ?, ?, ?, ?, ?)
        `);
        return this.db.transaction(() => {
            const result = stmt.run(symbol, name, isin, currency, buyThreshold, sellThreshold);
            if (this.db.prepare("SELECT 1 FROM pragma_table_info('stocks') WHERE name='price_source'").get()) {
                this.db.prepare("UPDATE stocks SET price_source='trade_republic' WHERE id=?").run(result.lastInsertRowid);
            }
            const add = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
            add.run(result.lastInsertRowid, 'BUY', buyThreshold);
            add.run(result.lastInsertRowid, 'SELL', sellThreshold);
            return result;
        })();
    }

    addStockWithMapping({ symbol, name, isin, currency = 'EUR', buyThreshold, sellThreshold, assetType = 'stock', mapping }) {
        return this.db.transaction(() => {
            const result = this.db.prepare(`INSERT INTO stocks (symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur, price_source, current_price_usd, last_exchange_rate, asset_type)
                VALUES (?, ?, ?, ?, ?, ?, 'trade_republic', NULL, NULL, ?)`).run(symbol, name, isin, currency, buyThreshold, sellThreshold, assetType);
            const stockId = Number(result.lastInsertRowid);
            const add = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
            add.run(stockId, 'BUY', buyThreshold); add.run(stockId, 'SELL', sellThreshold);
            this.saveBrokerMapping({ ...mapping, stock_id: stockId });
            return { lastInsertRowid: stockId };
        })();
    }

    configureJournalStockWithMapping({ stockId, symbol, name, isin, currency = 'EUR', assetType = 'stock', buyThreshold, sellThreshold, mapping }) {
        return this.db.transaction(() => {
            const stock = this.getStockById(stockId);
            if (!stock) throw new Error('Journal instrument is unavailable');
            this.db.prepare(`UPDATE stocks SET symbol=?,name=?,isin=?,currency=?,asset_type=?,archived=0,enabled=1,owned=0,trigger_state='NONE',price_source='trade_republic',
                buy_threshold_eur=?,sell_threshold_eur=?,updated_at=datetime('now') WHERE id=?`)
                .run(symbol, name, isin, currency, assetType, buyThreshold, sellThreshold, stockId);
            if (Number(stock.archived)) this.db.prepare('UPDATE alert_levels SET enabled=CASE WHEN retired=0 THEN 1 ELSE 0 END,active=CASE WHEN retired=0 THEN 1 ELSE 0 END WHERE stock_id=?').run(stockId);
            this.saveBrokerMapping({ ...mapping, stock_id: stockId });
            const addLevel = this.db.prepare('INSERT OR IGNORE INTO alert_levels (stock_id,side,price_eur) VALUES (?,?,?)');
            addLevel.run(stockId, 'BUY', buyThreshold);
            addLevel.run(stockId, 'SELL', sellThreshold);
            return this.getStockById(stockId);
        })();
    }

    // Alert levels are canonical for monitoring. Legacy threshold columns are
    // retained as compatibility metadata for direct database callers only.
    restoreArchivedStock(id, { name, currency, buyThreshold = null, sellThreshold = null }) {
        const result = this.db.prepare(`
            UPDATE stocks SET name = ?, currency = ?, buy_threshold_eur = COALESCE(?, buy_threshold_eur), sell_threshold_eur = COALESCE(?, sell_threshold_eur),
                archived = 0, enabled = 1, owned = 0, trigger_state = 'NONE', updated_at = datetime('now')
            WHERE id = ? AND archived = 1
        `).run(name, currency, buyThreshold, sellThreshold, id);
        if (result.changes) this.db.prepare('UPDATE alert_levels SET enabled = CASE WHEN retired = 0 THEN 1 ELSE 0 END, active = CASE WHEN retired = 0 THEN 1 ELSE 0 END WHERE stock_id = ?').run(id);
        return result;
    }

    updateStock(id, updates) {
        const allowedFields = ['buy_threshold_eur', 'sell_threshold_eur', 'enabled', 'name', 'archived'];
        const setClauses = [];
        const values = [];

        for (const [key, value] of Object.entries(updates)) {
            if (allowedFields.includes(key)) {
                setClauses.push(`${key} = ?`);
                values.push(value);
            }
        }

        if (setClauses.length === 0) {
            throw new Error('No valid fields to update');
        }

        values.push(id);
        const stmt = this.db.prepare(`
            UPDATE stocks SET ${setClauses.join(', ')}, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(...values);
    }

    updateStockPrice(id, priceUsd, priceEur, exchangeRate) {
        const stmt = this.db.prepare(`
            UPDATE stocks
            SET current_price_usd = ?, current_price_eur = ?, last_price_update = datetime('now'),
                last_exchange_rate = ?, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(priceUsd, priceEur, exchangeRate, id);
    }
}

module.exports = StocksMethods;
