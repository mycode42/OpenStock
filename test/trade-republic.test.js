const test = require('node:test');
const assert = require('node:assert/strict');
const { TradeRepublicClient, sanitizeQuote, sanitizeAccount } = require('../lib/brokers/trade-republic');

test('Trade Republic client sends X-API-Key server-side and never returns it', async () => {
  let seen;
  const client = new TradeRepublicClient({ env: { TRADE_REPUBLIC_API_KEY: 'secret-test-key' }, fetchImpl: async (url, options) => { seen = { url, headers: options.headers }; return { ok: true, json: async () => ({ bid: 10, ask: 11, currency: 'EUR' }) }; } });
  const quote = await client.getQuote({ isin: 'DE0000000001', exchange: 'XETR' });
  assert.equal(seen.headers['X-API-Key'], 'secret-test-key');
  assert.equal(quote.ask_eur, 11);
  assert.equal(JSON.stringify(quote).includes('secret-test-key'), false);
});
test('sanitizers mask account references and preserve EUR bid/ask semantics', () => {
  const account = sanitizeAccount({ securitiesAccountNumber: 'DE1234567890123456', cashAmount: 42 });
  assert.equal(account.account_ref_masked, 'DE••••56');
  assert.deepEqual(sanitizeQuote({ bid: 1, ask: 2, currency: 'USD' }, { isin: 'X', exchange: 'XETR' }).bid_eur, null);
});
test('client timeout fails closed without exposing request details', async () => {
  const client = new TradeRepublicClient({ env: { TRADE_REPUBLIC_API_KEY: 'secret' }, timeoutMs: 1000, fetchImpl: (_u, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })))) });
  await assert.rejects(() => client.getAccounts(), error => error.code === 'TIMEOUT' && !String(error).includes('secret'));
});
