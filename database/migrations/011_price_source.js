module.exports = {
  version: 11,
  name: 'price_source_selection',
  up(db) { db.exec("ALTER TABLE stocks ADD COLUMN price_source TEXT NOT NULL DEFAULT 'yahoo'"); }
};
