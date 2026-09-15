const mappings = {
  BIRK: 'JE00BS44BN30', CRSP: 'CH0334081137', DKS: 'US2533931026',
  HIMS: 'US4330001060', IONQ: 'US46222L1089', JOBY: 'KYG651631007',
  SPCX: 'US84615Q1031', SUNB: 'US8669661048', TEM: 'US88023B1035', TSLA: 'US88160R1014'
};

module.exports = {
  version: 12,
  name: 'isin_first_trade_republic_catalogue',
  up(db) {
    if (!db.prepare('PRAGMA table_info(stocks)').all().some(column => column.name === 'isin')) db.exec('ALTER TABLE stocks ADD COLUMN isin TEXT');
    db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_stocks_isin_unique ON stocks(isin) WHERE isin IS NOT NULL');
    const find = db.prepare("SELECT id, symbol FROM stocks WHERE symbol = ? AND archived = 0");
    const setStock = db.prepare("UPDATE stocks SET isin = ?, currency = 'EUR', price_source = 'trade_republic', current_price_usd = NULL, last_exchange_rate = NULL, updated_at = datetime('now') WHERE id = ? AND (isin IS NULL OR isin = ?)");
    const mapping = db.prepare(`INSERT INTO broker_instrument_mappings
      (stock_id, broker, ticker, isin, exchange, currency, broker_metadata_json, source, verified_at)
      VALUES (?, 'trade_republic', ?, ?, 'TIB', 'EUR', ?, 'broker_verified_backfill', datetime('now'))
      ON CONFLICT(stock_id) DO UPDATE SET ticker=excluded.ticker, isin=excluded.isin,
      exchange=excluded.exchange, currency=excluded.currency, broker_metadata_json=excluded.broker_metadata_json,
      source=excluded.source, verified_at=excluded.verified_at, updated_at=datetime('now')`);
    for (const [symbol, isin] of Object.entries(mappings)) {
      const stock = find.get(symbol);
      if (!stock) continue;
      const result = setStock.run(isin, stock.id, isin);
      if (result.changes) mapping.run(stock.id, symbol, isin, JSON.stringify({ home_exchange: 'TIB', currency: 'EUR', verification: 'instrument_and_tr_instrument' }));
    }
  }
};
