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
    assert.deepEqual(db.prepare('SELECT version FROM schema_migrations ORDER BY version').all().map(row => row.version), migrations.map(migration => migration.version));
    assert.equal(db.prepare('PRAGMA table_info(stocks)').all().some(row => row.name === 'archived'), true);
    assert.deepEqual(db.prepare('PRAGMA table_info(broker_health_state)').all().map(row => row.name), ['broker', 'status', 'checked_at', 'changed_at', 'last_notified_at', 'last_error', 'last_notified_status', 'failure_streak', 'success_streak']);
    db.close();
});

test('migration 017 upgrades an already-applied broker health table', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.exec(`CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
      CREATE TABLE broker_health_state (broker TEXT PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,changed_at TEXT NOT NULL,last_notified_at TEXT,last_error TEXT);`);
    const insert = db.prepare('INSERT INTO schema_migrations(version,name) VALUES (?,?)');
    for (const migration of migrations.filter(item => item.version <= 16)) insert.run(migration.version, migration.name);
    db.close();
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const upgraded = new Database(file);
    assert.equal(upgraded.prepare('PRAGMA table_info(broker_health_state)').all().some(row => row.name === 'last_notified_status'), true);
    assert.equal(upgraded.prepare('SELECT 1 FROM schema_migrations WHERE version=17').get()['1'], 1);
    assert.equal(upgraded.prepare('PRAGMA table_info(broker_health_state)').all().some(row => row.name === 'failure_streak'), true);
    assert.equal(upgraded.prepare('SELECT 1 FROM schema_migrations WHERE version=20').get()['1'], 1);
    upgraded.close();
});

test('fresh seeded initialization creates one BUY and SELL level per stock', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    initDatabase({ dbPath: file, seedDefaults: true, silent: true });
    const db = new Database(file);
    assert.equal(db.prepare('SELECT COUNT(*) count FROM stocks').get().count, 5);
    assert.equal(db.prepare("SELECT COUNT(*) count FROM stocks WHERE price_source='trade_republic'").get().count, 5);
    assert.equal(db.prepare('SELECT COUNT(*) count FROM alert_levels').get().count, 10);
    assert.equal(db.prepare(`SELECT COUNT(*) count FROM stocks s
      WHERE (SELECT COUNT(*) FROM alert_levels al WHERE al.stock_id=s.id AND al.side='BUY')=1
        AND (SELECT COUNT(*) FROM alert_levels al WHERE al.stock_id=s.id AND al.side='SELL')=1`).get().count, 5);
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
    assert.equal(migrated.prepare('SELECT price_source FROM stocks WHERE id=7').get().price_source, 'trade_republic');
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
    assert.deepEqual(adopted.prepare('SELECT version FROM schema_migrations ORDER BY version').all().map(row => row.version), migrations.map(migration => migration.version));
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

test('migration adopts legacy trigger debounce state and links existing history', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.exec(fs.readFileSync(path.join(__dirname, '..', 'database', 'schema.sql'), 'utf8'));
    db.prepare("INSERT INTO stocks(id,symbol,name,buy_threshold_eur,sell_threshold_eur,trigger_state) VALUES (1,'IONQ','IonQ',10,20,'BUY_TRIGGERED'),(2,'TEM','Tempus',11,22,'SELL_TRIGGERED'),(3,'TSLA','Tesla',12,24,'NONE')").run();
    db.prepare("INSERT INTO alerts_log(stock_id,alert_type,price_eur,threshold_eur,sent_at) VALUES (1,'BUY',9,10,'2026-09-14 10:00:00'),(2,'SELL',23,22,'2026-09-14 11:00:00')").run();
    db.close();
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const migrated = new Database(file);
    assert.deepEqual(migrated.prepare("SELECT stocks.symbol,alert_levels.side,alert_levels.triggered,alert_levels.retired,alert_levels.active,alert_levels.enabled FROM alert_levels JOIN stocks ON stocks.id=alert_levels.stock_id WHERE stocks.symbol IN ('IONQ','TEM','TSLA') AND (alert_levels.triggered=1 OR stocks.symbol='TSLA') ORDER BY stocks.symbol").all(), [
        { symbol: 'IONQ', side: 'BUY', triggered: 1, retired: 1, active: 0, enabled: 0 },
        { symbol: 'TEM', side: 'SELL', triggered: 1, retired: 1, active: 0, enabled: 0 },
        { symbol: 'TSLA', side: 'BUY', triggered: 0, retired: 0, active: 1, enabled: 1 },
        { symbol: 'TSLA', side: 'SELL', triggered: 0, retired: 0, active: 1, enabled: 1 }
    ]);
    assert.equal(migrated.prepare("SELECT COUNT(*) count FROM alerts_log WHERE alert_level_id IS NOT NULL").get().count, 2);
    migrated.close();
});

test('migration 006 repairs earlier alert-log foreign keys without losing history', t => {
    const { dir, file } = tempFile();
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new Database(file);
    db.prepare("INSERT INTO stocks(id,symbol,name,buy_threshold_eur,sell_threshold_eur) VALUES (1,'FK','Foreign key',10,20)").run();
    db.prepare("INSERT INTO alert_levels(id,stock_id,side,price_eur,enabled,active,triggered,retired) VALUES (1,1,'BUY',10,0,0,1,1)").run();
    db.prepare("INSERT INTO alerts_log(id,stock_id,alert_level_id,alert_type,price_eur,threshold_eur) VALUES (1,1,1,'BUY',9,10)").run();
    db.exec(`DELETE FROM schema_migrations WHERE version=6;
      ALTER TABLE alerts_log RENAME TO alerts_log_current;
      CREATE TABLE alerts_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, stock_id INTEGER NOT NULL, alert_type TEXT NOT NULL,
        price_eur REAL NOT NULL, threshold_eur REAL NOT NULL, sent_at TEXT DEFAULT (datetime('now')) NOT NULL,
        telegram_message_id INTEGER, alert_level_id INTEGER,
        FOREIGN KEY(stock_id) REFERENCES stocks(id) ON DELETE CASCADE,
        FOREIGN KEY(alert_level_id) REFERENCES alert_levels(id)
      );
      INSERT INTO alerts_log SELECT * FROM alerts_log_current;
      DROP TABLE alerts_log_current;`);
    db.close();

    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const repaired = new Database(file);
    repaired.pragma('foreign_keys = ON');
    assert.equal(repaired.prepare('SELECT COUNT(*) count FROM alerts_log').get().count, 1);
    repaired.prepare('DELETE FROM alert_levels WHERE id=1').run();
    assert.equal(repaired.prepare('SELECT alert_level_id FROM alerts_log WHERE id=1').get().alert_level_id, null);
    repaired.close();
});
