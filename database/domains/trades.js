'use strict';

const QuotesMethods = require('./quotes');
const crypto = require('node:crypto');

class TradesMethods extends QuotesMethods {
    tradeQueryParts(filters = {}) {
        const clauses = [];
        const values = [];
        if (filters.status === 'open') clauses.push('t.closed_at IS NULL');
        if (filters.status === 'closed') clauses.push('t.closed_at IS NOT NULL');
        if (filters.symbol) { clauses.push('s.symbol = ?'); values.push(filters.symbol); }
        const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
        return { where, values };
    }

    getAllTrades(filters = {}) {
        const { where, values } = this.tradeQueryParts(filters);
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur,
                   (SELECT COUNT(*) FROM trades numbered WHERE numbered.id <= t.id) AS trade_number
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            ${where}
            ORDER BY COALESCE(t.closed_at, t.opened_at) DESC, t.id DESC
        `).all(...values);
    }

    getTradesPage(filters = {}, { page = 1, pageSize = 20 } = {}) {
        const { where, values } = this.tradeQueryParts(filters);
        const safePageSize = Math.min(100, Math.max(1, Math.floor(Number(pageSize) || 20)));
        const requestedPage = Math.max(1, Math.floor(Number(page) || 1));
        const total = this.db.prepare(`SELECT COUNT(*) AS total FROM trades t JOIN stocks s ON s.id = t.stock_id ${where}`).get(...values).total;
        const totalPages = Math.max(1, Math.ceil(total / safePageSize));
        const safePage = Math.min(requestedPage, totalPages);
        const items = this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur,
                   (SELECT COUNT(*) FROM trades numbered WHERE numbered.id <= t.id) AS trade_number
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            ${where}
            ORDER BY COALESCE(t.closed_at, t.opened_at) DESC, t.id DESC
            LIMIT ? OFFSET ?
        `).all(...values, safePageSize, (safePage - 1) * safePageSize);
        return { items, page: safePage, page_size: safePageSize, total, total_pages: totalPages };
    }

    getTradeById(id) {
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur,
                   (SELECT COUNT(*) FROM trades numbered WHERE numbered.id <= t.id) AS trade_number
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            WHERE t.id = ?
        `).get(id);
    }

    addTrade(trade) {
        const insert = this.db.prepare(`
            INSERT INTO trades (
                trade_id, stock_id, opened_at, closed_at, quantity, currency,
                entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
                exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
            ) VALUES (
                @trade_id, @stock_id, @opened_at, @closed_at, @quantity, @currency,
                @entry_price, @entry_fx_rate, @entry_price_eur, @entry_fee_eur,
                @exit_price, @exit_fx_rate, @exit_price_eur, @exit_fee_eur, @dividend_eur, @strategy, @notes
            )
        `);
        return this.db.transaction(() => {
            if (this.db.prepare('SELECT 1 FROM trades WHERE trade_id = ? UNION ALL SELECT 1 FROM trade_merge_events WHERE source_trade_id = ? LIMIT 1').get(trade.trade_id, trade.trade_id)) {
                const error = new Error('Trade ID already exists');
                error.code = 'SQLITE_CONSTRAINT_UNIQUE';
                throw error;
            }
            if (trade.closed_at) return { ...insert.run(trade), merged: false, mergedCount: 0 };
            const openTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id = ? AND closed_at IS NULL ORDER BY opened_at, id').all(trade.stock_id);
            if (!openTrades.length) return { ...insert.run(trade), merged: false, mergedCount: 0 };
            return this.mergeOpenTradeRows(openTrades, trade);
        })();
    }

    mergeOpenTradeRows(openTrades, incoming = null) {
        if (!openTrades.length) throw new Error('No open trades to merge');
        const target = openTrades[0];
        const sources = [...openTrades.slice(1), ...(incoming ? [incoming] : [])];
        if (!sources.length) return { lastInsertRowid: target.id, merged: false, mergedCount: 0 };
        const rows = [target, ...sources];
        for (const row of rows) {
            if (!(Number.isFinite(Number(row.quantity)) && Number(row.quantity) > 0 && Number.isFinite(Number(row.entry_price)) && Number(row.entry_price) > 0 && Number.isFinite(Number(row.entry_price_eur)) && Number(row.entry_price_eur) > 0 && Number.isFinite(Number(row.entry_fee_eur)) && Number(row.entry_fee_eur) >= 0)) throw new Error('Cannot merge an invalid open trade');
        }
        const totalQuantity = rows.reduce((sum, row) => sum + Number(row.quantity), 0);
        const entryPriceEur = Number((rows.reduce((sum, row) => sum + Number(row.quantity) * Number(row.entry_price_eur), 0) / totalQuantity).toFixed(4));
        const currencies = [...new Set(rows.map(row => String(row.currency).toUpperCase()))];
        const currency = currencies.length === 1 ? currencies[0] : 'EUR';
        const entryPrice = currency === 'EUR'
            ? entryPriceEur
            : Number((rows.reduce((sum, row) => sum + Number(row.quantity) * Number(row.entry_price), 0) / totalQuantity).toFixed(4));
        const entryFxRate = currency === 'EUR' ? 1 : entryPriceEur / entryPrice;
        const strategies = [...new Set(rows.map(row => String(row.strategy || '').trim()).filter(Boolean))];
        const notes = [...new Set(rows.map(row => String(row.notes || '').trim()).filter(Boolean))];
        const mergeNote = `Merged ${sources.length} additional purchase${sources.length === 1 ? '' : 's'}; source details retained in trade_merge_events.`;
        if (!notes.includes(mergeNote)) notes.push(mergeNote);
        const audit = this.db.prepare('INSERT INTO trade_merge_events (target_trade_id, source_trade_id, source_trade_json) VALUES (?, ?, ?)');
        for (const source of sources) audit.run(target.id, source.trade_id, JSON.stringify(source));
        const existingSourceIds = sources.filter(source => Number.isInteger(Number(source.id))).map(source => Number(source.id));
        if (existingSourceIds.length) this.db.prepare(`DELETE FROM trades WHERE id IN (${existingSourceIds.map(() => '?').join(',')})`).run(...existingSourceIds);
        this.db.prepare(`UPDATE trades SET opened_at=?, quantity=?, currency=?, entry_price=?, entry_fx_rate=?, entry_price_eur=?, entry_fee_eur=?, strategy=?, notes=?, updated_at=datetime('now') WHERE id=?`).run(
            rows.map(row => row.opened_at).sort()[0], totalQuantity, currency, entryPrice, entryFxRate, entryPriceEur,
            rows.reduce((sum, row) => sum + Number(row.entry_fee_eur || 0), 0), strategies.join(' | '), notes.join(' | '), target.id
        );
        return { lastInsertRowid: target.id, merged: true, mergedCount: sources.length };
    }

    consolidateOpenTrades(stockId) {
        return this.db.transaction(() => {
            const openTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id = ? AND closed_at IS NULL ORDER BY opened_at, id').all(stockId);
            return this.mergeOpenTradeRows(openTrades);
        })();
    }

    getTradeMergeEvents(targetTradeId) {
        return this.db.prepare('SELECT * FROM trade_merge_events WHERE target_trade_id = ? ORDER BY id').all(targetTradeId);
    }

    updateTrade(id, trade) {
        return this.db.prepare(`
            UPDATE trades SET
                stock_id=@stock_id, opened_at=@opened_at, closed_at=@closed_at, quantity=@quantity, currency=@currency,
                entry_price=@entry_price, entry_fx_rate=@entry_fx_rate, entry_price_eur=@entry_price_eur, entry_fee_eur=@entry_fee_eur,
                exit_price=@exit_price, exit_fx_rate=@exit_fx_rate, exit_price_eur=@exit_price_eur, exit_fee_eur=@exit_fee_eur, dividend_eur=@dividend_eur,
                strategy=@strategy, notes=@notes, updated_at=datetime('now')
            WHERE id=@id
        `).run({ ...trade, id });
    }

    closeTradeQuantity(id, trade, closeQuantity) {
        const quantity = Number(closeQuantity);
        if (!Number.isFinite(quantity) || quantity <= 0) throw new Error('Sale quantity must be greater than zero');
        return this.db.transaction(() => {
            const existing = this.db.prepare('SELECT * FROM trades WHERE id = ?').get(id);
            if (!existing) throw new Error('Trade not found');
            if (existing.closed_at) throw new Error('Trade is already closed; use explicit edit to correct it');
            const openQuantity = Number(existing.quantity);
            if (quantity > openQuantity) throw new Error(`Sale quantity cannot exceed the open quantity (${openQuantity})`);
            if (quantity === openQuantity) {
                this.updateTrade(id, { ...trade, quantity: openQuantity });
                return { id, partial: false };
            }

            const closedEntryFee = Number(existing.entry_fee_eur || 0) * quantity / openQuantity;
            const remainingQuantity = openQuantity - quantity;
            const remainingEntryFee = Number(existing.entry_fee_eur || 0) - closedEntryFee;
            const suffix = `-SELL-${Date.now()}-${crypto.randomBytes(3).toString('hex')}`;
            const closedTradeId = `${existing.trade_id}${suffix}`;
            const sourceNote = `Partial sale of ${quantity} from open trade ${existing.trade_id}.`;
            const notes = [String(trade.notes || '').trim(), sourceNote].filter(Boolean).join(' | ');
            this.db.prepare(`
                INSERT INTO trades (
                    trade_id, stock_id, opened_at, closed_at, quantity, currency,
                    entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
                    exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
                ) VALUES (
                    @trade_id, @stock_id, @opened_at, @closed_at, @quantity, @currency,
                    @entry_price, @entry_fx_rate, @entry_price_eur, @entry_fee_eur,
                    @exit_price, @exit_fx_rate, @exit_price_eur, @exit_fee_eur, @dividend_eur, @strategy, @notes
                )
            `).run({ ...trade, trade_id: closedTradeId, quantity, entry_fee_eur: closedEntryFee, notes });
            this.db.prepare(`UPDATE trades SET quantity=?, entry_fee_eur=?, updated_at=datetime('now') WHERE id=?`)
                .run(remainingQuantity, remainingEntryFee, id);
            return { id, partial: true, closedTradeId };
        })();
    }

    // ===== CAPITAL & EQUITY =====
}

module.exports = TradesMethods;
