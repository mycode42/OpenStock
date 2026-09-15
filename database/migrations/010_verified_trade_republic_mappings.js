module.exports = {
  version: 10,
  name: 'verified_trade_republic_mappings',
  up(db) {
    const rows = [
      ['BIRK', 'JE00BS44BN30'],
      ['DKS', 'US2533931026'],
      ['SPCX', 'US84615Q1031']
    ];
    const find = db.prepare('SELECT id FROM stocks WHERE symbol = ? AND archived = 0');
    const upsert = db.prepare(`INSERT INTO broker_instrument_mappings
      (stock_id, broker, ticker, isin, exchange, currency, broker_metadata_json, source, verified_at)
      VALUES (?, 'trade_republic', ?, ?, 'TIB', 'EUR', ?, 'broker_portfolio_verified', datetime('now'))
      ON CONFLICT(stock_id) DO UPDATE SET isin=excluded.isin, exchange=excluded.exchange,
      currency=excluded.currency, broker_metadata_json=excluded.broker_metadata_json,
      source=excluded.source, verified_at=excluded.verified_at, updated_at=datetime('now')`);
    for (const [ticker, isin] of rows) {
      const stock = find.get(ticker);
      if (stock) upsert.run(stock.id, ticker, isin, JSON.stringify({ home_exchange: 'TIB', currency: 'EUR', verification: 'instrument_and_tr_instrument' }));
    }
  }
};
