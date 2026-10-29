'use strict';

module.exports = {
  version: 27,
  name: 'alpaca_order_budgets',
  up(db) {
    // USD budget a BUY level spends when it fires with real orders on. SELL levels exit the whole position.
    db.exec(`
      ALTER TABLE alpaca_alert_levels ADD COLUMN order_budget_usd REAL CHECK (order_budget_usd IS NULL OR order_budget_usd > 0);
    `);
  }
};
