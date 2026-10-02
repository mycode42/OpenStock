'use strict';

function safeError(error) {
    return String(error?.message || error || 'unknown error')
        .replace(/[\r\n\t]/g, ' ')
        .replace(/(bot)[^/\s]*/gi, '$1[redacted]')
        .slice(0, 300);
}

function finitePositive(value, label) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) throw new Error(`Invalid ${label}`);
    return number;
}

function parseTradeRepublicMonitorQuote(quote, now = new Date()) {
    if (!quote || typeof quote !== 'object') throw new Error('Trade Republic quote unavailable');
    const currency = String(quote.currency || '').toUpperCase();
    const bidUnit = String(quote.bid_unit || currency).toUpperCase();
    const askUnit = String(quote.ask_unit || currency).toUpperCase();
    if (currency !== 'EUR' || bidUnit !== 'EUR' || askUnit !== 'EUR') throw new Error('Trade Republic quote is not EUR');
    const bid = Number(quote.bid_eur), ask = Number(quote.ask_eur);
    const validBid = Number.isFinite(bid) && bid > 0;
    const validAsk = Number.isFinite(ask) && ask > 0;
    if (!validBid && !validAsk) throw new Error('Trade Republic quote has no valid executable side');
    const quoteTime = quote.bid_time || quote.ask_time || quote.quote_time || now.toISOString();
    const parsedTime = new Date(quoteTime);
    return {
        priceEur: finitePositive(validBid && validAsk ? (bid + ask) / 2 : (validBid ? bid : ask), 'Trade Republic EUR price'),
        bidEur: validBid ? bid : null,
        askEur: validAsk ? ask : null,
        side: validBid && validAsk ? 'midpoint' : (validAsk ? 'ask' : 'bid'),
        quoteTime: Number.isNaN(parsedTime.getTime()) ? now.toISOString() : parsedTime.toISOString(),
        source: 'trade_republic_order_price'
    };
}

module.exports = { safeError, parseTradeRepublicMonitorQuote };
