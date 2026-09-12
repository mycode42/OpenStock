module.exports = {
  version: 3,
  name: 'supporting_indexes',
  up(db) {
    db.exec(`CREATE INDEX IF NOT EXISTS idx_stocks_archived ON stocks(archived);
      CREATE INDEX IF NOT EXISTS idx_trades_stock_closed ON trades(stock_id, closed_at);`);
  }
};
