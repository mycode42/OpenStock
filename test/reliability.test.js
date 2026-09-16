const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { HistoryBackfillQueue } = require('../lib/history-backfill');
const { EXIT, runMonitor } = require('../lib/monitor');

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-reliable-'));
  const file = path.join(dir, 'test.db');
  initDatabase({ dbPath: file, seedDefaults: false, silent: true });
  return { dir, file, db: new OpenStockDB(file) };
}

test('broker current-state rows are updated instead of silently retained', t => {
  const { dir, db } = fixture(); t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  db.syncBrokerRows('broker_accounts', [{ broker: 'trade_republic', account_ref_masked: 'DE••••56', currency: 'EUR', cash_eur: 10, available_cash_eur: 9, observed_at: '2026-01-01T10:00:00Z', raw_hash: null }]);
  db.syncBrokerRows('broker_accounts', [{ broker: 'trade_republic', account_ref_masked: 'DE••••56', currency: 'EUR', cash_eur: 20, available_cash_eur: 19, observed_at: '2026-01-01T11:00:00Z', raw_hash: null }]);
  assert.deepEqual(db.db.prepare('SELECT cash_eur,available_cash_eur,observed_at FROM broker_accounts').get(), { cash_eur: 20, available_cash_eur: 19, observed_at: '2026-01-01T11:00:00Z' });
});

test('incomplete Trade Republic history remains eligible and records failed range', async t => {
  const { dir, db } = fixture(); t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const id = Number(db.addStock('RETRY', 'Retry', 'EUR', 1, 2, 'US0000000001').lastInsertRowid);
  db.saveBrokerMapping({ stock_id: id, broker: 'trade_republic', ticker: 'RETRY', isin: 'US0000000001', exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'test', verified_at: '2026-01-01T00:00:00Z' });
  let fail = true;
  const queue = new HistoryBackfillQueue({ db, delayMs: 0, tradeRepublicRanges: ['5y', '1y'], brokerClient: { getInstrumentHistory: async ({ range }) => { if (range === '1y' && fail) throw new Error('temporary outage'); return { range, resolution: 1, points: [{ recordedAt: '2026-01-01 00:00:00', quoteTime: '2026-01-01T00:00:00Z', priceEur: 10 }] }; } }, logger: { log() {}, error() {} } });
  assert.equal(queue.enqueue(db.getStockById(id)), true); await queue.run();
  assert.equal(db.hasCompletedHistoryBackfill(id, 'trade_republic', ['5y', '1y']), false);
  assert.equal(db.db.prepare("SELECT status FROM history_backfill_runs WHERE stock_id=? AND range='1y'").get(id).status, 'FAILED');
  fail = false;
  assert.equal(queue.enqueue(db.getStockById(id)), true); await queue.run();
  assert.equal(db.hasCompletedHistoryBackfill(id, 'trade_republic', ['5y', '1y']), true);
});

test('failed alert delivery remains in outbox and is retried before retiring the level', async t => {
  const { dir, file, db } = fixture(); db.db.close();
  const raw = new Database(file);
  raw.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur) VALUES ('OUTBOX','Outbox',100,200)").run();
  const id = raw.prepare("SELECT id FROM stocks WHERE symbol='OUTBOX'").get().id;
  raw.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?,'BUY',100)").run(id);
  const config = { lockPath: path.join(dir, 'lock'), timeoutMs: 100, concurrency: 1, cycleDeadlineMs: 1000, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: { token: 'token', chatId: '42' } };
  const quote = { chart: { result: [{ timestamp: [10], indicators: { quote: [{ close: [90] }] }, meta: {} }] } };
  const failed = await runMonitor({ db: raw, now: new Date('2026-01-01T12:00:00Z'), config, fetchImpl: async url => url === 'fx' ? ({ ok: true, json: async () => ({ rates: { EUR: 1 } }) }) : url.includes('telegram') ? ({ ok: true, json: async () => ({ ok: false }) }) : ({ ok: true, json: async () => quote }), error() {}, log() {} });
  assert.equal(failed, EXIT.PARTIAL);
  assert.equal(raw.prepare('SELECT status FROM alert_outbox').get().status, 'FAILED');
  assert.equal(raw.prepare('SELECT triggered FROM alert_levels').get().triggered, 0);
  const succeeded = await runMonitor({ db: raw, now: new Date('2026-01-01T12:01:00Z'), config, fetchImpl: async url => url === 'fx' ? ({ ok: true, json: async () => ({ rates: { EUR: 1 } }) }) : url.includes('telegram') ? ({ ok: true, json: async () => ({ ok: true, result: { message_id: 77 } }) }) : ({ ok: true, json: async () => quote }), error: assert.fail, log() {} });
  assert.equal(succeeded, EXIT.OK);
  assert.deepEqual(raw.prepare('SELECT status,attempt_count,telegram_message_id FROM alert_outbox').get(), { status: 'SENT', attempt_count: 2, telegram_message_id: '77' });
  assert.equal(raw.prepare('SELECT triggered FROM alert_levels').get().triggered, 1);
  raw.close(); fs.rmSync(dir, { recursive: true, force: true });
});
