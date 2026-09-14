function foreignKeyUses(db, from, parent, onDelete) {
  return db.prepare('PRAGMA foreign_key_list(alerts_log)').all()
    .some(key => key.from === from && key.table === parent && key.on_delete === onDelete);
}

module.exports = {
  version: 6,
  name: 'alert_log_foreign_keys',
  up(db) {
    const stockKeyIsSafe = foreignKeyUses(db, 'stock_id', 'stocks', 'SET NULL');
    const levelKeyIsSafe = foreignKeyUses(db, 'alert_level_id', 'alert_levels', 'SET NULL');
    if (stockKeyIsSafe && levelKeyIsSafe) return;

    db.exec(`
      ALTER TABLE alerts_log RENAME TO alerts_log_before_fk_repair;
      CREATE TABLE alerts_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_id INTEGER,
        alert_type TEXT NOT NULL,
        price_eur REAL NOT NULL,
        threshold_eur REAL NOT NULL,
        sent_at TEXT DEFAULT (datetime('now')) NOT NULL,
        telegram_message_id INTEGER,
        alert_level_id INTEGER,
        FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE SET NULL,
        FOREIGN KEY (alert_level_id) REFERENCES alert_levels(id) ON DELETE SET NULL
      );
      INSERT INTO alerts_log (id, stock_id, alert_type, price_eur, threshold_eur, sent_at, telegram_message_id, alert_level_id)
        SELECT old.id,
          CASE WHEN EXISTS (SELECT 1 FROM stocks s WHERE s.id = old.stock_id) THEN old.stock_id ELSE NULL END,
          old.alert_type, old.price_eur, old.threshold_eur, old.sent_at, old.telegram_message_id,
          CASE WHEN EXISTS (SELECT 1 FROM alert_levels al WHERE al.id = old.alert_level_id) THEN old.alert_level_id ELSE NULL END
        FROM alerts_log_before_fk_repair old;
      DROP TABLE alerts_log_before_fk_repair;
      CREATE INDEX IF NOT EXISTS idx_alerts_log_stock_id ON alerts_log(stock_id);
      CREATE INDEX IF NOT EXISTS idx_alerts_log_sent_at ON alerts_log(sent_at);
    `);
  }
};
