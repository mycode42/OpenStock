module.exports = {
  version: 9,
  name: 'trade_republic_order_audit',
  up(db) {
    db.exec(`ALTER TABLE broker_order_intents ADD COLUMN response_json TEXT;
      ALTER TABLE broker_order_intents ADD COLUMN reconciled_at TEXT;
      ALTER TABLE broker_order_intents ADD COLUMN frozen_reason TEXT;`);
  }
};
