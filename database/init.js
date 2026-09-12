const Database = require('better-sqlite3');
const migrations = [
  require('./migrations/001_initial'),
  require('./migrations/002_legacy_compatibility'),
  require('./migrations/003_indexes')
];

function initDatabase({ silent = false, dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db', seedDefaults = true } = {}) {
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
    const insert = db.prepare('INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur) VALUES (?, ?, ?, ?, ?)');
    for (const stock of [['AAPL', 'Apple Inc.', 'USD', 150, 200], ['MSFT', 'Microsoft Corporation', 'USD', 250, 350], ['GOOGL', 'Alphabet Inc.', 'USD', 120, 180], ['TSLA', 'Tesla Inc.', 'USD', 200, 300], ['AMZN', 'Amazon.com Inc.', 'USD', 100, 150]]) insert.run(...stock);
  }
  db.close();
  if (!silent) console.log('Database schema initialized at:', dbPath);
  return true;
}

if (require.main === module) initDatabase();
module.exports = { initDatabase, migrations };
