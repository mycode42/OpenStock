module.exports = {
  version: 7,
  name: 'trade_merge_audit',
  up(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS trade_merge_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_trade_id INTEGER NOT NULL,
        source_trade_id TEXT NOT NULL UNIQUE,
        source_trade_json TEXT NOT NULL,
        merged_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (target_trade_id) REFERENCES trades(id) ON DELETE RESTRICT
      );
      CREATE INDEX IF NOT EXISTS idx_trade_merge_events_target ON trade_merge_events(target_trade_id, merged_at);
    `);
  }
};
