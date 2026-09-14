module.exports = {
  version: 4,
  name: 'normalized_alert_levels',
  up(db) {
    db.exec(`CREATE TABLE IF NOT EXISTS alert_levels (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      stock_id INTEGER NOT NULL,
      side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
      price_eur REAL NOT NULL CHECK (price_eur > 0),
      enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
      active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
      triggered INTEGER NOT NULL DEFAULT 0 CHECK (triggered IN (0, 1)),
      retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
      triggered_at TEXT,
      retired_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE,
      UNIQUE (stock_id, side, price_eur)
    );
    CREATE INDEX IF NOT EXISTS idx_alert_levels_stock_state ON alert_levels(stock_id, enabled, active, triggered, retired);
    CREATE INDEX IF NOT EXISTS idx_alert_levels_side ON alert_levels(stock_id, side);
    CREATE TRIGGER IF NOT EXISTS update_alert_levels_updated_at
      AFTER UPDATE ON alert_levels
    BEGIN
      UPDATE alert_levels SET updated_at = datetime('now') WHERE id = NEW.id;
    END;`);
    const insert = db.prepare(`INSERT OR IGNORE INTO alert_levels (stock_id, side, price_eur)
      SELECT id, ?, ? FROM stocks WHERE id = ?`);
    for (const stock of db.prepare('SELECT id, buy_threshold_eur, sell_threshold_eur FROM stocks').all()) {
      insert.run('BUY', stock.buy_threshold_eur, stock.id);
      insert.run('SELL', stock.sell_threshold_eur, stock.id);
    }
    if (db.prepare("SELECT 1 FROM pragma_table_info('alerts_log') WHERE name = 'alert_level_id'").get() === undefined) {
      db.exec('ALTER TABLE alerts_log ADD COLUMN alert_level_id INTEGER REFERENCES alert_levels(id) ON DELETE SET NULL');
    }
    db.exec(`UPDATE alerts_log SET alert_level_id = (
      SELECT al.id FROM alert_levels al
      WHERE al.stock_id = alerts_log.stock_id AND al.side = alerts_log.alert_type
        AND al.price_eur = alerts_log.threshold_eur
      LIMIT 1
    ) WHERE alert_level_id IS NULL`);

    // Carry the legacy debounce state into the corresponding normalized level.
    // This prevents a deployment from replaying an alert that was already
    // delivered under the binary trigger model. The latest matching history
    // timestamp is retained for auditability; no alert rows are deleted.
    db.exec(`UPDATE alert_levels
      SET triggered = 1, retired = 1, active = 0, enabled = 0,
          triggered_at = COALESCE((SELECT MAX(sent_at) FROM alerts_log l WHERE l.alert_level_id = alert_levels.id), datetime('now')),
          retired_at = COALESCE((SELECT MAX(sent_at) FROM alerts_log l WHERE l.alert_level_id = alert_levels.id), datetime('now'))
      WHERE (side = 'BUY' AND EXISTS (SELECT 1 FROM stocks s WHERE s.id = alert_levels.stock_id AND s.trigger_state = 'BUY_TRIGGERED'))
         OR (side = 'SELL' AND EXISTS (SELECT 1 FROM stocks s WHERE s.id = alert_levels.stock_id AND s.trigger_state = 'SELL_TRIGGERED'))`);
  }
};
