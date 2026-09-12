const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase, migrations } = require('../database/init');

function tempFile() { const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-migration-')); return { dir, file: path.join(dir, 'legacy.db') }; }

test('fresh initialization is versioned and idempotent', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new Database(file);
    assert.deepEqual(db.prepare('SELECT version FROM schema_migrations ORDER BY version').all().map(row => row.version), [1, 2, 3]);
    assert.equal(db.prepare('PRAGMA table_info(stocks)').all().some(row => row.name === 'archived'), true);
    db.close();
});

test('representative legacy stocks, trades, and journal tables are adopted without losing rows', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.exec(`CREATE TABLE stocks (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, name TEXT, currency TEXT DEFAULT 'USD', current_price_usd REAL, current_price_eur REAL, buy_threshold_eur REAL NOT NULL, sell_threshold_eur REAL NOT NULL, owned INTEGER DEFAULT 0 NOT NULL, enabled INTEGER DEFAULT 1 NOT NULL, last_price_update TEXT, last_exchange_rate REAL, trigger_state TEXT DEFAULT 'NONE', created_at TEXT DEFAULT (datetime('now')) NOT NULL, updated_at TEXT DEFAULT (datetime('now')) NOT NULL);
      CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT UNIQUE NOT NULL, stock_id INTEGER NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, quantity REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR', entry_price REAL NOT NULL, entry_fx_rate REAL NOT NULL DEFAULT 1, entry_price_eur REAL NOT NULL, entry_fee_eur REAL NOT NULL DEFAULT 0, exit_price REAL, exit_fx_rate REAL, exit_price_eur REAL, exit_fee_eur REAL NOT NULL DEFAULT 0, strategy TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT (datetime('now')) NOT NULL, updated_at TEXT DEFAULT (datetime('now')) NOT NULL);
      CREATE TABLE journal_entries (id INTEGER PRIMARY KEY, stock_id INTEGER, action TEXT, quantity REAL, price_eur REAL, notes TEXT, created_at TEXT);
      INSERT INTO stocks (id, symbol, name, buy_threshold_eur, sell_threshold_eur) VALUES (7, 'OLD', 'Legacy', 1, 2);
      INSERT INTO journal_entries VALUES (1, 7, 'BUY', 2, 10, 'legacy note', '2026-01-01 00:00:00');`);
    db.close();
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const migrated = new Database(file);
    assert.equal(migrated.prepare('SELECT archived FROM stocks WHERE id=7').get().archived, 0);
    assert.equal(migrated.prepare('SELECT dividend_eur FROM trades WHERE trade_id=?').get('LEGACY-BUY-1').dividend_eur, 0);
    assert.equal(migrated.prepare('SELECT notes FROM trades WHERE trade_id=?').get('LEGACY-BUY-1').notes, 'legacy note');
    migrated.close();
});

test('legacy partial sells preserve the closed and remaining lot quantities', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.exec(`CREATE TABLE stocks (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, name TEXT, currency TEXT DEFAULT 'USD', current_price_usd REAL, current_price_eur REAL, buy_threshold_eur REAL NOT NULL, sell_threshold_eur REAL NOT NULL, owned INTEGER DEFAULT 0 NOT NULL, enabled INTEGER DEFAULT 1 NOT NULL, last_price_update TEXT, last_exchange_rate REAL, trigger_state TEXT DEFAULT 'NONE', created_at TEXT DEFAULT (datetime('now')) NOT NULL, updated_at TEXT DEFAULT (datetime('now')) NOT NULL);
      CREATE TABLE journal_entries (id INTEGER PRIMARY KEY, stock_id INTEGER, action TEXT, quantity REAL, price_eur REAL, notes TEXT, created_at TEXT);
      INSERT INTO stocks (id, symbol, name, buy_threshold_eur, sell_threshold_eur) VALUES (7, 'OLD', 'Legacy', 1, 2);
      INSERT INTO journal_entries VALUES (1, 7, 'BUY', 5, 10, 'buy', '2026-01-01 00:00:00');
      INSERT INTO journal_entries VALUES (2, 7, 'SELL', 2, 12, 'sell', '2026-01-02 00:00:00');`);
    db.close();
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const migrated = new Database(file);
    const lots = migrated.prepare('SELECT quantity, closed_at, entry_price_eur, exit_price_eur FROM trades ORDER BY id').all();
    assert.deepEqual(lots, [
        { quantity: 2, closed_at: '2026-01-02 00:00:00', entry_price_eur: 10, exit_price_eur: 12 },
        { quantity: 3, closed_at: null, entry_price_eur: 10, exit_price_eur: null }
    ]);
    migrated.close();
});

test('an existing current schema is adopted without duplicate objects', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.exec(fs.readFileSync(path.join(__dirname, '..', 'database', 'schema.sql'), 'utf8'));
    db.close();
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const adopted = new Database(file);
    assert.deepEqual(adopted.prepare('SELECT version FROM schema_migrations ORDER BY version').all().map(row => row.version), [1, 2, 3]);
    adopted.close();
});

test('failed migration is transactional and is not recorded', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const failing = { version: 99, name: 'intentional_failure', up(db) { db.exec('CREATE TABLE should_rollback (id INTEGER)'); throw new Error('boom'); } };
    migrations.push(failing);
    try {
        assert.throws(() => initDatabase({ dbPath: file, seedDefaults: false, silent: true }), /boom/);
    } finally {
        migrations.pop();
    }
    const db = new Database(file);
    assert.equal(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'").get(), undefined);
    assert.equal(db.prepare('SELECT 1 FROM schema_migrations WHERE version=99').get(), undefined);
    db.close();
});
