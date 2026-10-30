'use strict';

// Real market history for the demo. Intraday sessions and daily bars are exported from our market data by
// scripts/market/export_demo_sessions.py. The simulated broker (dashboard charts) and the Algo Universe both read
// them here, so a name has the same chart on both pages. A name without fixture data returns null, and the caller
// falls back to the simulated bars.
const { basePrice, priceAt } = require('./demo-seed');

const DAY_MS = 86_400_000;
const INTRADAY_MINUTES = { '1Min': 1, '5Min': 5, '30Min': 30 };
// Ranges used by the Algo Universe page, with the same bar sizes as the dashboard history.
const UNIVERSE_RANGES = {
    '1d': { timeframe: '5Min', days: 1 },
    '1w': { timeframe: '30Min', days: 7 },
    '1mo': { timeframe: '1Day', days: 31 },
    '1y': { timeframe: '1Day', days: 366 },
    '5y': { timeframe: '1Week', days: 1827 },
};

let sessionCache = null;
let dailyCache = null;

function loadFixture(name) {
    try { return require(`./${name}`); }
    catch (error) { if (error.code === 'MODULE_NOT_FOUND') return null; throw error; }
}

function sessionList() {
    if (sessionCache === null) sessionCache = (loadFixture('demo-sessions.json') || { sessions: [] }).sessions;
    return sessionCache;
}

function dailySeries() {
    if (dailyCache === null) dailyCache = (loadFixture('demo-daily.json') || { symbols: {} }).symbols;
    return dailyCache;
}

const easternClock = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
// Eastern date and 5-minute slot since 04:00, the same window the sessions cover.
function easternSlot(time) {
    const parts = Object.fromEntries(easternClock.formatToParts(new Date(time)).map(part => [part.type, part.value]));
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    return { day: `${parts.year}-${parts.month}-${parts.day}`, index: Math.floor((minutes - 240) / 5) };
}

function isWeekend(day) {
    const weekday = new Date(`${day}T12:00:00Z`).getUTCDay();
    return weekday === 0 || weekday === 6;
}

// One session per symbol and day, chosen by a hash so the same day always draws the same shape.
function sessionFor(symbol, day) {
    const sessions = sessionList();
    if (!sessions.length) return null;
    const key = `${symbol}|${day}`;
    return sessions[[...key].reduce((sum, character) => (sum * 31 + character.charCodeAt(0)) >>> 0, 7) % sessions.length];
}

function round(value, digits = 4) {
    return Number(value.toFixed(digits));
}

function bpsPrice(anchor, bps) {
    return anchor * (1 + bps / 10000);
}

// Today's session ends on the live quote. Earlier days start from the reference price.
function anchorFor(symbol, day, nowTime) {
    const now = easternSlot(nowTime);
    if (day === now.day) {
        const session = sessionFor(symbol, day);
        const index = Math.min(191, Math.max(0, now.index));
        return priceAt(symbol, nowTime) / (1 + session.bars_bps[index][3] / 10000);
    }
    return basePrice(symbol);
}

function intradayBars(symbol, minutes, from, to, nowTime) {
    const span = Math.max(1, minutes / 5);
    const bars = [];
    for (let time = from; time <= to; time += minutes * 60_000) {
        const slot = easternSlot(time);
        if (slot.index < 0 || slot.index >= 192 || isWeekend(slot.day)) continue;
        const session = sessionFor(symbol, slot.day);
        if (!session) return null;
        const series = session.bars_bps;
        const last = Math.min(191, slot.index + span - 1);
        let high = -Infinity;
        let low = Infinity;
        for (let index = slot.index; index <= last; index += 1) {
            high = Math.max(high, series[index][1]);
            low = Math.min(low, series[index][2]);
        }
        const anchor = anchorFor(symbol, slot.day, nowTime);
        bars.push({
            t: new Date(time).toISOString(),
            o: round(bpsPrice(anchor, series[slot.index][0])),
            h: round(bpsPrice(anchor, high)),
            l: round(bpsPrice(anchor, low)),
            c: round(bpsPrice(anchor, series[last][3])),
            v: 1000 + Math.abs(series[last][3] - series[slot.index][0]) * 40,
        });
    }
    return bars;
}

// Daily bars carry the exported dates; the last exported close is the live quote.
function dailyBars(symbol, from, to, nowTime) {
    const series = dailySeries()[symbol];
    if (!series) return null;
    const anchor = priceAt(symbol, nowTime);
    return series
        .filter(([day]) => {
            const time = Date.parse(`${day}T20:00:00Z`);
            return time >= from && time <= to;
        })
        .map(([day, open, high, low, close]) => ({
            t: `${day}T20:00:00.000Z`,
            o: round(bpsPrice(anchor, open)), h: round(bpsPrice(anchor, high)),
            l: round(bpsPrice(anchor, low)), c: round(bpsPrice(anchor, close)),
            v: 1_000_000,
        }));
}

// Weeks start on Monday, the way the dashboard's weekly bars are read.
function weeklyBars(daily) {
    const weeks = new Map();
    for (const bar of daily) {
        const date = new Date(bar.t);
        const monday = new Date(date.getTime() - ((date.getUTCDay() + 6) % 7) * DAY_MS).toISOString().slice(0, 10);
        const week = weeks.get(monday);
        if (!week) weeks.set(monday, { t: bar.t, o: bar.o, h: bar.h, l: bar.l, c: bar.c, v: bar.v });
        else Object.assign(week, { t: bar.t, h: Math.max(week.h, bar.h), l: Math.min(week.l, bar.l), c: bar.c, v: week.v + bar.v });
    }
    return [...weeks.values()];
}

// Bars for a timeframe and window, or null when the symbol has no real history in the demo.
function replayBars({ symbol, timeframe, start, end, limit = 1000, nowTime }) {
    if (basePrice(symbol) === null) return null;
    const from = new Date(start).getTime();
    const to = new Date(end).getTime();
    const minutes = INTRADAY_MINUTES[timeframe];
    let bars = null;
    if (minutes) {
        if (!sessionList().length) return null;
        bars = intradayBars(symbol, minutes, from, to, nowTime);
    } else if (timeframe === '1Day' || timeframe === '1Week') {
        const daily = dailyBars(symbol, from, to, nowTime);
        if (!daily) return null;
        bars = timeframe === '1Day' ? daily : weeklyBars(daily);
    }
    return bars ? bars.slice(-limit) : null;
}

// The Algo Universe chart for a range: intraday ranges show the latest session only, like the dashboard.
function universeHistory(symbol, range, nowTime) {
    const config = UNIVERSE_RANGES[range];
    if (!config) return null;
    const to = nowTime;
    const from = to - config.days * DAY_MS;
    const bars = replayBars({ symbol, timeframe: config.timeframe, start: from, end: to, limit: 500, nowTime });
    if (!bars || !bars.length) return null;
    let shown = bars;
    if (INTRADAY_MINUTES[config.timeframe]) {
        const latest = easternSlot(Date.parse(bars[bars.length - 1].t)).day;
        shown = bars.filter(bar => easternSlot(Date.parse(bar.t)).day === latest);
    }
    return shown.map(bar => ({ price: bar.c, recorded_at: bar.t }));
}

module.exports = { replayBars, universeHistory, UNIVERSE_RANGES, easternSlot };
