'use strict';

// The demo keeps the market open every day, so weekend charts and fills follow the same clock as the quotes.
const test = require('node:test');
const assert = require('node:assert/strict');
const { replayBars } = require('../server/demo-replay');

test('demo replay produces intraday bars on a Saturday, like on a weekday', () => {
    const saturdayNoon = new Date('2026-11-07T12:00:00Z').getTime();
    const bars = replayBars({
        symbol: 'TSLA',
        timeframe: '5Min',
        start: new Date(saturdayNoon - 86_400_000).toISOString(),
        end: new Date(saturdayNoon).toISOString(),
        limit: 1000,
        nowTime: saturdayNoon,
    });
    assert.ok(bars && bars.length > 0, 'expected bars on a Saturday');
    const saturdayBars = bars.filter(bar => new Date(bar.t).getUTCDate() === 7);
    assert.ok(saturdayBars.length > 0, 'expected at least one bar dated Saturday');
});

test('the weekly chart ends on the live demo quote', () => {
    const { priceAt } = require('../server/demo-seed');
    const now = new Date('2026-11-07T12:17:00Z').getTime();
    const bars = replayBars({
        symbol: 'TSLA', timeframe: '30Min',
        start: new Date(now - 7 * 86_400_000).toISOString(), end: new Date(now).toISOString(),
        limit: 1000, nowTime: now,
    });
    assert.equal(bars.at(-1).c, Number(priceAt('TSLA', now).toFixed(4)));
});
