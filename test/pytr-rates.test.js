'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { parseRatesCsv, PytrRatesClient } = require('../lib/pytr-rates');

test('pytr CSV parser keeps EUR last and ask values by ISIN', () => {
    const quotes = parseRatesCsv('noise\nName;ISIN;price;ask\nTesla;US88160R1014;321.25;321.30\nNo ask;DE0005140008;11.5;\n');
    assert.deepEqual(quotes.get('US88160R1014').lastEur, 321.25);
    assert.deepEqual(quotes.get('US88160R1014').askEur, 321.3);
    assert.equal(quotes.get('DE0005140008').askEur, null);
});

test('pytr client uses one bounded CSV batch and never supplies credentials as arguments', async () => {
    let call;
    const client = new PytrRatesClient({ now: () => new Date('2026-09-17T12:00:00Z'), exec: async (...args) => { call = args; return { stdout: 'Name;ISIN;price;ask\nTesla;US88160R1014;321.25;321.30\n' }; } });
    const quotes = await client.getQuotes(['US88160R1014', 'invalid', 'US88160R1014']);
    assert.equal(quotes.size, 1);
    assert.deepEqual(call[1], ['rates', '--no-waf-token', '--no-decimal-localization', '--sort-by-column', 'ISIN', '-o', '/dev/stdout', 'US88160R1014']);
});
