module.exports = {
  version: 20,
  name: 'trade_republic_only_quotes',
  up(db) {
    if (db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stocks'").get()) {
      db.exec("UPDATE stocks SET price_source='trade_republic'");
    }
  }
};
