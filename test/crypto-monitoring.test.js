const test = require('node:test');
const assert = require('node:assert/strict');
const { instrumentAssetClass, resolveVerifiedInstrument } = require('../lib/instrument-resolution');
const { sanitizeCollection } = require('../lib/brokers/trade-republic');
const { QuoteService } = require('../lib/quote-service');

test('Trade Republic crypto instruments are verified as EUR TIB instruments', async () => {
  const isin = 'XF000ETH0019';
  const client = {
    getInstrument: async () => ({ isin, name: 'Ethereum', intlSymbol: 'ETH', tradable: true, type: 'CRYPTO' }),
    getTradeRepublicInstrument: async () => ({ isin, exchange: 'TIB', currency: 'EUR', tradable: true, type: 'CRYPTO' })
  };
  const result = await resolveVerifiedInstrument(client, isin);
  assert.equal(result.instrument.ticker, 'ETH');
  assert.equal(instrumentAssetClass(result.instrument), 'crypto');
  assert.equal(result.home.exchange, 'TIB');
  assert.equal(result.home.currency, 'EUR');
});

test('crypto uses the documented Trade Republic TIB/EUR quote contract when home metadata omits venue fields', async () => {
  const isin = 'XF000ETH0019';
  const client = {
    getInstrument: async () => ({ isin, name: 'Ethereum', intlSymbol: 'ETH', tradable: true, type: 'CRYPTO' }),
    getTradeRepublicInstrument: async () => ({ isin, type: 'CRYPTO' })
  };
  const result = await resolveVerifiedInstrument(client, isin);
  assert.equal(result.instrument.exchange, 'TIB');
  assert.equal(result.instrument.currency, 'EUR');
  assert.equal(result.home.exchange, 'TIB');
  assert.equal(result.home.currency, 'EUR');
});

test('portfolio sanitization retains a fractional crypto quantity and type', () => {
  const [position] = sanitizeCollection({ DE1234567890123456: { categories: [{ positions: [{ isin: 'XF000ETH0019', netSize: 0.125, type: 'CRYPTO', currentValue: 250 }] }] } }, 'portfolio');
  assert.equal(position.instrument, 'XF000ETH0019');
  assert.equal(position.quantity, 0.125);
  assert.equal(position.instrument_type, 'CRYPTO');
});

test('crypto monitoring does not substitute an unverified Yahoo symbol', async () => {
  let yahooCalls = 0;
  const stock = {
    id: 1, symbol: 'ETH', price_source: 'trade_republic',
    mapping: { isin: 'XF000ETH0019', exchange: 'TIB', currency: 'EUR', verified_at: '2026-09-30T00:00:00Z', broker_metadata_json: JSON.stringify({ asset_class: 'crypto' }) }
  };
  const service = new QuoteService({
    db: { getBrokerMapping: () => stock.mapping },
    brokerClient: { getQuote: async () => { throw new Error('broker offline'); } },
    pytrClient: { getQuotes: async () => new Map() },
    fetchImpl: async () => { yahooCalls += 1; throw new Error('Yahoo must not be called for crypto'); },
    config: { fx: {} }
  });
  const [result] = await service.fetchMany([stock], { pytrFallback: true });
  assert.equal(result.quote, undefined);
  assert.match(result.error.message, /broker offline/);
  assert.equal(yahooCalls, 0);
});
