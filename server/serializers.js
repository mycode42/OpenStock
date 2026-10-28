const { calculateTrade, calculateDashboard, snapshotBucketAt } = require('../lib/analytics');

// Convert SQLite or already-ISO UTC timestamps to one valid ISO 8601 value.
function toISODateTime(value) {
    if (!value) return null;
    const normalized = String(value).trim().replace(' ', 'T');
    const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`;
    const parsed = new Date(zoned);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

const BERLIN_DATE_TIME = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
});

function berlinDateTimeParts(value) {
    return Object.fromEntries(
        BERLIN_DATE_TIME.formatToParts(value)
            .filter((part) => part.type !== 'literal')
            .map((part) => [part.type, Number(part.value)]),
    );
}

function berlinTradingDayStart(now = new Date()) {
    const local = berlinDateTimeParts(now);
    let dateAtMidnight = Date.UTC(local.year, local.month - 1, local.day);
    if (local.hour * 60 + local.minute < 7 * 60 + 30) dateAtMidnight -= 86400000;
    const tradingDate = new Date(dateAtMidnight);
    const desired = {
        year: tradingDate.getUTCFullYear(),
        month: tradingDate.getUTCMonth() + 1,
        day: tradingDate.getUTCDate(),
        hour: 7,
        minute: 30,
        second: 0,
    };
    let timestamp = Date.UTC(
        desired.year,
        desired.month - 1,
        desired.day,
        desired.hour,
        desired.minute,
        desired.second,
    );
    for (let pass = 0; pass < 2; pass += 1) {
        const actual = berlinDateTimeParts(new Date(timestamp));
        timestamp +=
            Date.UTC(desired.year, desired.month - 1, desired.day, desired.hour, desired.minute, desired.second) -
            Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute, actual.second);
    }
    return new Date(timestamp);
}

function berlinTradingDayEnd(now = new Date()) {
    // The 1-day chart uses a fixed Berlin wall-clock session: 07:30–22:00.
    // DST changes occur outside this interval, so the endpoint is exactly
    // fourteen and a half hours after the matching session start.
    return new Date(berlinTradingDayStart(now).getTime() + (14 * 60 + 30) * 60000);
}

function evenlySample(points, maximum) {
    if (!Number.isInteger(maximum) || maximum < 2 || points.length <= maximum) return points;
    const sampled = [points[0]];
    const step = (points.length - 1) / (maximum - 1);
    for (let index = 1; index < maximum - 1; index += 1) sampled.push(points[Math.round(index * step)]);
    sampled.push(points[points.length - 1]);
    return sampled;
}

function serializeStock(stock) {
    if (!stock) return stock;
    const { owned: _legacyOwned, ...publicStock } = stock;
    return {
        ...publicStock,
        last_price_update: toISODateTime(stock.last_price_update),
        created_at: toISODateTime(stock.created_at),
        updated_at: toISODateTime(stock.updated_at),
    };
}

function serializeBrokerStock(stock, db) {
    if (!stock) return stock;
    const mapping = db.getBrokerMapping(stock.id);
    const quote = mapping ? db.getLatestBrokerQuote(mapping.isin, mapping.exchange) : null;
    const observation = db.getLatestPriceObservation(stock.id);
    const isTradeRepublicPrice = (source) => String(source || '').startsWith('trade_republic');
    const brokerPriceIsCurrent = Boolean(quote && isTradeRepublicPrice(observation?.source));
    const safeQuote = brokerPriceIsCurrent
        ? {
              bid_eur: quote.bid_eur,
              ask_eur: quote.ask_eur,
              spread_eur: quote.bid_eur !== null && quote.ask_eur !== null ? quote.ask_eur - quote.bid_eur : null,
              quote_time: quote.quote_time,
              observed_at: quote.observed_at,
              source: quote.source,
          }
        : null;
    const fallback = observation?.source === 'trade_republic_order_price';
    const marketLabel = !observation
        ? 'No quote'
        : observation.source === 'trade_republic_sdk_order_price'
          ? 'Handelsrepublik SDK'
          : observation.source === 'trade_republic_order_price'
            ? 'Trade Republic UAPI sidecar'
            : 'Trade Republic history';
    return {
        ...serializeStock(stock),
        asset_type: stock.asset_type || 'stock',
        broker_mapping: mapping
            ? {
                  broker: mapping.broker,
                  ticker: mapping.ticker,
                  isin: mapping.isin,
                  exchange: mapping.exchange,
                  currency: mapping.currency,
                  asset_type: stock.asset_type || 'stock',
                  verified_at: mapping.verified_at,
                  source: mapping.source,
              }
            : null,
        broker_quote: safeQuote,
        market_data: {
            source: observation?.source || null,
            label: marketLabel,
            fallback,
            quote_time: toISODateTime(observation?.quote_time),
        },
        position: db.getPositionSummary(stock.id),
        alert_levels: db.getAlertLevels(stock.id).map(serializeAlertLevel),
    };
}

function serializeAlertLevel(level) {
    if (!level) return level;
    return {
        ...level,
        price_eur: Number(level.price_eur),
        triggered_at: toISODateTime(level.triggered_at),
        retired_at: toISODateTime(level.retired_at),
        created_at: toISODateTime(level.created_at),
        updated_at: toISODateTime(level.updated_at),
    };
}

function serializeTrade(trade) {
    const calculated = calculateTrade(trade);
    return {
        ...calculated,
        opened_at: toISODateTime(calculated.opened_at),
        closed_at: toISODateTime(calculated.closed_at),
        created_at: toISODateTime(calculated.created_at),
        updated_at: toISODateTime(calculated.updated_at),
    };
}

module.exports = {
    toISODateTime,
    BERLIN_DATE_TIME,
    berlinDateTimeParts,
    berlinTradingDayStart,
    berlinTradingDayEnd,
    evenlySample,
    serializeStock,
    serializeBrokerStock,
    serializeAlertLevel,
    serializeTrade,
};
