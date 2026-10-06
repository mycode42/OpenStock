module.exports = {
  version: 21,
  name: 'crypto_instruments_and_broker_execution_sync',
  up(db) {
    const stocksExist = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stocks'").get();
    if (stocksExist) {
      const stockColumns = db.prepare('PRAGMA table_info(stocks)').all().map(column => column.name);
      if (!stockColumns.includes('asset_type')) db.exec("ALTER TABLE stocks ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'stock'");
    }
    db.exec(`CREATE TABLE IF NOT EXISTS broker_sync_state (
      sync_key TEXT PRIMARY KEY,
      updated_at TEXT NOT NULL,
      changed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )`);
  }
};
