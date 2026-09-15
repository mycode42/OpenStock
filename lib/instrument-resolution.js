const PREFERRED_EXCHANGES = new Set(['XNAS', 'XNYS', 'XLON']);

function normalized(value) { return String(value || '').trim().toUpperCase(); }

function chooseTicker(instrument) {
    const listings = Array.isArray(instrument?.listings) ? instrument.listings : [];
    const intl = normalized(instrument?.intlSymbol);
    if (intl) return intl;
    const primary = instrument?.primaryExchange;
    if (primary && PREFERRED_EXCHANGES.has(normalized(primary.exchange)) && normalized(primary.symbol)) return normalized(primary.symbol);
    const preferred = listings.find(item => PREFERRED_EXCHANGES.has(normalized(item.exchange)) && normalized(item.symbol));
    if (preferred) return normalized(preferred.symbol);
    const nonTib = listings.find(item => normalized(item.exchange) !== 'TIB' && normalized(item.symbol) && normalized(item.symbol) !== normalized(instrument?.isin));
    const home = normalized(instrument?.homeSymbol);
    const ticker = normalized(instrument?.ticker);
    return nonTib?.symbol ? normalized(nonTib.symbol) : (home && home !== normalized(instrument?.isin) ? home : (ticker && ticker !== normalized(instrument?.isin) ? ticker : normalized(instrument?.symbol || instrument?.isin) || null));
}

async function resolveVerifiedInstrument(client, inputIsin) {
    const isin = normalized(inputIsin);
    if (!/^[A-Z0-9]{12}$/.test(isin)) throw Object.assign(new Error('A valid 12-character ISIN is required'), { status: 400 });
    const [instrument, home] = await Promise.all([client.getInstrument(isin), client.getTradeRepublicInstrument(isin)]);
    const exchange = normalized(home.exchange || instrument.exchange);
    const currency = normalized(home.currency || instrument.currency);
    if (instrument.isin !== isin || home.isin !== isin) throw Object.assign(new Error('Broker returned a different ISIN'), { status: 422 });
    if (!instrument.name || instrument.tradable === false || exchange !== 'TIB' || currency !== 'EUR') throw Object.assign(new Error('Instrument is not a tradable EUR TIB instrument'), { status: 422 });
    return { instrument: { ...instrument, ticker: chooseTicker(instrument), exchange, currency }, home: { ...home, exchange, currency } };
}

module.exports = { PREFERRED_EXCHANGES, chooseTicker, resolveVerifiedInstrument };
