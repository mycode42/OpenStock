module.exports = {
  version: 13,
  name: 'eur_history_nullable_usd',
  up(db) {
    const column = db.prepare('PRAGMA table_info(price_history)').all().find(item => item.name === 'price_usd');
    if (!column || Number(column.notnull) === 0) return;
    db.pragma('foreign_keys = OFF');
    db.exec(`ALTER TABLE price_history RENAME TO price_history_legacy;
      CREATE TABLE price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, stock_id INTEGER NOT NULL, price_usd REAL,
        price_eur REAL, exchange_rate REAL, source TEXT NOT NULL, quote_time TEXT,
        recorded_at TEXT DEFAULT (datetime('now')) NOT NULL,
        FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE,
        UNIQUE (stock_id, recorded_at)
      );
      INSERT INTO price_history (id, stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
        SELECT id, stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at FROM price_history_legacy;
      DROP TABLE price_history_legacy;
      CREATE INDEX IF NOT EXISTS idx_price_history_stock_recorded_at ON price_history(stock_id, recorded_at);`);
    db.pragma('foreign_keys = ON');
  }
};
