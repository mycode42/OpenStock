function hasTable(db, name) {
  return Boolean(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name));
}

function hasColumn(db, table, column) {
  return hasTable(db, table) && db.prepare(`PRAGMA table_info(${table})`).all().some(item => item.name === column);
}

function migrateLegacyJournal(db) {
  if (!hasTable(db, 'journal_entries') || !hasTable(db, 'trades')) return;
  if (db.prepare('SELECT COUNT(*) AS count FROM trades').get().count > 0) return;
  const entries = db.prepare('SELECT * FROM journal_entries ORDER BY created_at ASC, id ASC').all();
  if (!entries.length) return;
  const insert = db.prepare(`INSERT INTO trades (
    trade_id, stock_id, opened_at, closed_at, quantity, currency, entry_price,
    entry_fx_rate, entry_price_eur, entry_fee_eur, strategy, notes
  ) VALUES (?, ?, ?, NULL, ?, 'EUR', ?, 1, ?, 0, 'legacy import', ?)`);
  const close = db.prepare('UPDATE trades SET closed_at=?, exit_price=?, exit_fx_rate=1, exit_price_eur=?, notes=? WHERE id=?');
  const openLots = new Map();
  for (const entry of entries) {
    const quantity = Number(entry.quantity);
    const price = Number(entry.price_eur);
    if (!(quantity > 0 && price > 0)) continue;
    if (entry.action === 'BUY') {
      const result = insert.run(`LEGACY-BUY-${entry.id}`, entry.stock_id, entry.created_at, quantity, price, price, entry.notes || 'Imported from legacy journal');
      const lots = openLots.get(entry.stock_id) || [];
      lots.push({ id: Number(result.lastInsertRowid), quantity, price, openedAt: entry.created_at });
      openLots.set(entry.stock_id, lots);
    } else if (entry.action === 'SELL') {
      let remaining = quantity;
      const lots = openLots.get(entry.stock_id) || [];
      while (remaining > 0 && lots.length) {
        const lot = lots[0];
        const matched = Math.min(remaining, lot.quantity);
        const notes = `${entry.notes || ''}${entry.notes ? ' · ' : ''}Imported legacy sell #${entry.id}`;
        if (matched < lot.quantity) {
          db.prepare('UPDATE trades SET quantity=? WHERE id=?').run(matched, lot.id);
          const split = insert.run(`LEGACY-SPLIT-${entry.id}-${remaining}`, entry.stock_id, lot.openedAt, lot.quantity - matched, lot.price, lot.price, 'Legacy lot split during FIFO import');
          close.run(entry.created_at, price, price, notes, lot.id);
          lots[0] = { id: Number(split.lastInsertRowid), quantity: lot.quantity - matched, price: lot.price, openedAt: lot.openedAt };
        } else {
          close.run(entry.created_at, price, price, notes, lot.id);
          lots.shift();
        }
        remaining -= matched;
      }
    }
  }
}

module.exports = {
  version: 2,
  name: 'legacy_compatibility',
  up(db) {
    if (hasTable(db, 'stocks') && !hasColumn(db, 'stocks', 'archived')) db.exec('ALTER TABLE stocks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0');
    if (hasTable(db, 'trades') && !hasColumn(db, 'trades', 'dividend_eur')) db.exec('ALTER TABLE trades ADD COLUMN dividend_eur REAL NOT NULL DEFAULT 0 CHECK (dividend_eur >= 0)');
    migrateLegacyJournal(db);
  }
};
