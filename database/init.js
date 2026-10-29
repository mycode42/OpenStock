const Database = require('better-sqlite3');
const paths = require('../lib/paths');
const migrations = [
  require('./migrations/001_initial'),
  require('./migrations/002_legacy_compatibility'),
  require('./migrations/003_indexes'),
  require('./migrations/004_alert_levels'),
  require('./migrations/005_alert_trigger_continuity'),
  require('./migrations/006_alert_log_foreign_keys'),
  require('./migrations/007_trade_merge_audit')
  ,require('./migrations/008_trade_republic')
  ,require('./migrations/009_trade_republic_order_audit')
  ,require('./migrations/010_verified_trade_republic_mappings')
  ,require('./migrations/011_price_source')
  ,require('./migrations/012_isin_first_trade_republic')
  ,require('./migrations/013_eur_history_nullable_usd')
  ,require('./migrations/014_trade_republic_execution_and_quotes')
  ,require('./migrations/015_broker_reconciliation_candidates')
  ,require('./migrations/016_broker_health_state')
  ,require('./migrations/017_broker_health_notification_status')
  ,require('./migrations/018_reliable_monitoring')
  ,require('./migrations/019_broker_health_debounce')
  ,require('./migrations/020_trade_republic_only_quotes')
  ,require('./migrations/021_crypto_execution_sync')
  ,require('./migrations/022_percentage_move_alerts')
  ,require('./migrations/023_alpaca_usd_profile')
  ,require('./migrations/024_alpaca_alert_outbox')
  ,require('./migrations/025_alpaca_orders')
];

function initDatabase({ silent = false, dbPath = paths.databasePath(), seedDefaults = true } = {}) {
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  db.exec("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (datetime('now')))" );
  for (const migration of migrations) {
    if (db.prepare('SELECT 1 FROM schema_migrations WHERE version = ?').get(migration.version)) continue;
    db.transaction(() => {
      migration.up(db);
      db.prepare('INSERT INTO schema_migrations (version, name) VALUES (?, ?)').run(migration.version, migration.name);
    })();
  }
  if (seedDefaults && db.prepare('SELECT COUNT(*) AS count FROM stocks').get().count === 0) {
    const seed = db.transaction(() => {
      const insertStock = db.prepare("INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur, price_source) VALUES (?, ?, ?, ?, ?, 'trade_republic')");
      const insertLevel = db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
      for (const stock of [['AAPL', 'Apple Inc.', 'USD', 150, 200], ['MSFT', 'Microsoft Corporation', 'USD', 250, 350], ['GOOGL', 'Alphabet Inc.', 'USD', 120, 180], ['TSLA', 'Tesla Inc.', 'USD', 200, 300], ['AMZN', 'Amazon.com Inc.', 'USD', 100, 150]]) {
        const result = insertStock.run(...stock);
        insertLevel.run(result.lastInsertRowid, 'BUY', stock[3]);
        insertLevel.run(result.lastInsertRowid, 'SELL', stock[4]);
      }
    });
    seed();
  }
  db.close();
  if (!silent) console.log('Database schema initialized at:', dbPath);
  return true;
}

if (require.main === module) initDatabase();
module.exports = { initDatabase, migrations };
