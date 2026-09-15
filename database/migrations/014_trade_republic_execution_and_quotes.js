module.exports = {
  version: 14,
  name: 'trade_republic_execution_and_authoritative_quotes',
  up(db) {
    const columns = db.prepare('PRAGMA table_info(stocks)').all().map(c => c.name);
    if (!columns.includes('current_bid_eur')) db.exec('ALTER TABLE stocks ADD COLUMN current_bid_eur REAL');
    if (!columns.includes('current_ask_eur')) db.exec('ALTER TABLE stocks ADD COLUMN current_ask_eur REAL');
    if (!columns.includes('current_midpoint_eur')) db.exec('ALTER TABLE stocks ADD COLUMN current_midpoint_eur REAL');
    db.exec(`CREATE TABLE IF NOT EXISTS broker_executions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT NOT NULL, execution_identity TEXT NOT NULL UNIQUE,
      order_intent_id INTEGER, stock_id INTEGER NOT NULL, account_ref_masked TEXT NOT NULL,
      side TEXT NOT NULL CHECK (side IN ('buy','sell')), quantity REAL NOT NULL CHECK (quantity > 0),
      execution_price_eur REAL NOT NULL CHECK (execution_price_eur > 0), fees_eur REAL NOT NULL DEFAULT 0 CHECK (fees_eur >= 0),
      executed_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR', created_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY (order_intent_id) REFERENCES broker_order_intents(id) ON DELETE SET NULL,
      FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE RESTRICT
    );
    CREATE INDEX IF NOT EXISTS idx_broker_executions_stock ON broker_executions(stock_id, executed_at);`);
  }
};
