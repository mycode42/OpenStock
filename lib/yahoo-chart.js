const YAHOO_CHART_URL = 'https://query1.finance.yahoo.com/v8/finance/chart';

function sqliteTimestamp(unixSeconds) {
    const date = new Date(Number(unixSeconds) * 1000);
    if (!Number.isFinite(date.getTime())) return null;
    return date.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '');
}

function parseDailyCloses(chart) {
    const timestamps = chart?.timestamp || [];
    const closes = chart?.indicators?.quote?.[0]?.close || [];
    const points = [];
    for (let index = 0; index < timestamps.length; index++) {
        const price = Number(closes[index]);
        const recordedAt = sqliteTimestamp(timestamps[index]);
        if (recordedAt && Number.isFinite(price) && price > 0) points.push({ priceUsd: price, recordedAt });
    }
    return points;
}

function yahooChartUrl(symbol, { range = '5y', interval = '1d' } = {}) {
    return `${YAHOO_CHART_URL}/${encodeURIComponent(symbol)}?range=${encodeURIComponent(range)}&interval=${encodeURIComponent(interval)}&events=history`;
}

async function fetchYahooChart(symbol, { fetchImpl = globalThis.fetch, range = '5y', interval = '1d', timeoutMs = 30000 } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('Fetch is not available');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetchImpl(yahooChartUrl(symbol, { range, interval }), {
            signal: controller.signal,
            headers: { 'User-Agent': 'Mozilla/5.0 OpenStock history', Accept: 'application/json', 'Cache-Control': 'no-cache' }
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (payload?.chart?.error) throw new Error(payload.chart.error.description || 'Yahoo chart error');
        return payload?.chart?.result?.[0] || null;
    } finally {
        clearTimeout(timeout);
    }
}

module.exports = { YAHOO_CHART_URL, sqliteTimestamp, parseDailyCloses, yahooChartUrl, fetchYahooChart };
