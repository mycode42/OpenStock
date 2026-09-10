const Database = require('better-sqlite3');
const fs = require('fs');
const path = require('path');

function hasTable(db, tableName) {
    return Boolean(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(tableName));
}

function hasColumn(db, tableName, columnName) {
    return db.prepare(`PRAGMA table_info(${tableName})`).all().some(column => column.name === columnName);
}

function migrateTrades(db) {
    if (hasTable(db, 'trades') && !hasColumn(db, 'trades', 'dividend_eur')) {
        db.exec('ALTER TABLE trades ADD COLUMN dividend_eur REAL NOT NULL DEFAULT 0 CHECK (dividend_eur >= 0)');
    }
}

// The older journal stored individual BUY/SELL executions. Preserve the source
// table and import what can be paired unambiguously into the lot-based model.
// Any unmatched legacy execution remains safely in journal_entries for audit.
function migrateLegacyJournal(db) {
    if (!hasTable(db, 'journal_entries')) return;
    if (db.prepare('SELECT COUNT(*) AS count FROM trades').get().count > 0) return;
    const entries = db.prepare('SELECT * FROM journal_entries ORDER BY created_at ASC, id ASC').all();
    if (entries.length === 0) return;
    const insert = db.prepare(`INSERT INTO trades (
        trade_id, stock_id, opened_at, closed_at, quantity, currency, entry_price,
        entry_fx_rate, entry_price_eur, entry_fee_eur, exit_price, exit_fx_rate,
        exit_price_eur, exit_fee_eur, strategy, notes
    ) VALUES (?, ?, ?, NULL, ?, 'EUR', ?, 1, ?, 0, NULL, NULL, NULL, 0, 'legacy import', ?)`);
    const close = db.prepare(`UPDATE trades SET closed_at=?, exit_price=?, exit_fx_rate=1, exit_price_eur=?, notes=? WHERE id=?`);
    const openLots = new Map();
    const run = db.transaction(() => {
        for (const entry of entries) {
            const quantity = Number(entry.quantity);
            const price = Number(entry.price_eur);
            if (!(quantity > 0 && price > 0)) continue;
            if (entry.action === 'BUY') {
                const result = insert.run(`LEGACY-BUY-${entry.id}`, entry.stock_id, entry.created_at, quantity, price, price, entry.notes || 'Imported from legacy journal');
                const list = openLots.get(entry.stock_id) || [];
                list.push({ id: Number(result.lastInsertRowid), quantity, price, opened_at: entry.created_at });
                openLots.set(entry.stock_id, list);
                continue;
            }
            if (entry.action !== 'SELL') continue;
            let remaining = quantity;
            const lots = openLots.get(entry.stock_id) || [];
            while (remaining > 0 && lots.length) {
                const lot = lots[0];
                const matched = Math.min(remaining, lot.quantity);
                if (matched < lot.quantity) {
                    db.prepare('UPDATE trades SET quantity=? WHERE id=?').run(matched, lot.id);
                    const split = insert.run(`LEGACY-SPLIT-${entry.id}-${remaining}`, entry.stock_id, lot.opened_at, lot.quantity - matched, lot.price, lot.price, 'Legacy lot split during FIFO import');
                    close.run(entry.created_at, price, price, `${entry.notes || ''}${entry.notes ? ' · ' : ''}Imported legacy sell #${entry.id}`, lot.id);
                    lots[0] = { id: Number(split.lastInsertRowid), quantity: lot.quantity - matched, price: lot.price, opened_at: lot.opened_at };
                } else {
                    close.run(entry.created_at, price, price, `${entry.notes || ''}${entry.notes ? ' · ' : ''}Imported legacy sell #${entry.id}`, lot.id);
                    lots.shift();
                }
                remaining -= matched;
            }
        }
    });
    run();
}

function initDatabase({ silent = false } = {}) {
    const dbPath = process.env.DATABASE_PATH || '/home/ubuntu/openstock/database/openstock.db';
    const db = new Database(dbPath);

    // Read schema
    const schemaPath = path.join(__dirname, 'schema.sql');
    const schema = fs.readFileSync(schemaPath, 'utf8');
    
    // Execute schema
    db.exec(schema);
    migrateTrades(db);
    migrateLegacyJournal(db);
    if (!silent) console.log('Database schema initialized at:', dbPath);

    // Insert default stocks if empty
    const stockCount = db.prepare('SELECT COUNT(*) as count FROM stocks').get().count;
    if (stockCount === 0) {
        const insertStock = db.prepare(`
            INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur)
            VALUES (?, ?, ?, ?, ?)
        `);
        
        const defaults = [
            ['AAPL', 'Apple Inc.', 'USD', 150.0, 200.0],
            ['MSFT', 'Microsoft Corporation', 'USD', 250.0, 350.0],
            ['GOOGL', 'Alphabet Inc.', 'USD', 120.0, 180.0],
            ['TSLA', 'Tesla Inc.', 'USD', 200.0, 300.0],
            ['AMZN', 'Amazon.com Inc.', 'USD', 100.0, 150.0]
        ];

        const inserted = defaults.map(stock => {
            try {
                insertStock.run(...stock);
                return true;
            } catch (e) {
                console.warn(`Failed to insert stock ${stock[0]}:`, e.message);
                return false;
            }
        }).filter(Boolean).length;

        if (!silent) console.log(`Inserted ${inserted} default stocks`);
    }

    db.close();
    return true;
}

if (require.main === module) {
    initDatabase();
}

module.exports = { initDatabase };
