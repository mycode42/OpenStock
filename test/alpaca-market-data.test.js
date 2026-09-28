'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { AlpacaMarketDataClient, parseAlpacaLatestQuote } = require('../lib/alpaca-market-data');

test('Alpaca parser preserves the quote timestamp and calculates the USD midpoint', () => {
    const quote = parseAlpacaLatestQuote({ quote: { bp: 100, ap: 102, t: '2026-09-28T10:00:00Z' } }, { symbol: 'AAPL', feed: 'iex' });
    assert.deepEqual(quote, { symbol: 'AAPL', priceUsd: 101, bidUsd: 100, askUsd: 102, quoteTime: '2026-09-28T10:00:00.000Z', source: 'alpaca_iex_quote' });
});

test('Alpaca client uses only the latest quote endpoint and keeps credentials in request headers', async () => {
    let request;
    const client = new AlpacaMarketDataClient({ keyId: 'key-id', secretKey: 'secret-value', baseUrl: 'https://data.example.test', feed: 'iex', fetchImpl: async (url, options) => {
        request = { url, options }; return { ok: true, json: async () => ({ quote: { bp: 10, ap: 12, t: '2026-09-28T10:00:00Z' } }) };
    } });
    const quote = await client.getLatestQuote('aapl');
    assert.match(request.url, /^https:\/\/data\.example\.test\/v2\/stocks\/AAPL\/quotes\/latest\?feed=iex$/);
    assert.equal(request.options.headers['APCA-API-KEY-ID'], 'key-id');
    assert.equal(request.options.headers['APCA-API-SECRET-KEY'], 'secret-value');
    assert.equal(quote.priceUsd, 11);
});

test('Alpaca client fails closed while no API key pair is configured', async () => {
    const client = new AlpacaMarketDataClient({ keyId: '', secretKey: '', fetchImpl: async () => { throw new Error('must not call'); } });
    await assert.rejects(client.getLatestQuote('AAPL'), /not configured/);
});

test('Alpaca client accepts Alpaca header-style environment key aliases', () => {
    const keyName = 'APCA-API-KEY-ID', secretName = 'APCA-API-SECRET-KEY';
    const originalKey = process.env[keyName], originalSecret = process.env[secretName];
    try {
        process.env[keyName] = 'key'; process.env[secretName] = 'secret';
        const client = new AlpacaMarketDataClient();
        assert.equal(client.configured(), true);
    } finally {
        if (originalKey === undefined) delete process.env[keyName]; else process.env[keyName] = originalKey;
        if (originalSecret === undefined) delete process.env[secretName]; else process.env[secretName] = originalSecret;
    }
});
