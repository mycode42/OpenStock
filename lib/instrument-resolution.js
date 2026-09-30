const PREFERRED_EXCHANGES = new Set(['XNAS', 'XNYS', 'XLON']);

function normalized(value) { return String(value || '').trim().toUpperCase(); }

// Trade Republic uses values such as CRYPTO and CRYPTOCURRENCY for crypto
// instruments. Keep this deliberately narrow: an unrecognised broker type is
// still a normal security, not a guess that could change its quote policy.
function instrumentAssetClass(instrument) {
    const type = normalized(instrument?.type || instrument?.instrumentType);
    if (/(?:^|_)(?:CRYPTO|CRYPTOCURRENC(?:Y|IES))(?:_|$)/.test(type)) return 'crypto';
    // The authenticated UAPI currently omits `type` for crypto. Its active
    // spot listing is still part of the verified instrument response, and is
    // a broker-specific signal (for example ETHEUR.SPOT), not a guessed ISIN.
    return (Array.isArray(instrument?.listings) && instrument.listings.some(listing => listing?.active !== false && /(?:^|[._-])SPOT$/.test(normalized(listing?.symbol)))) ? 'crypto' : 'security';
}

function hasActiveSpotListing(instrument) {
    return Array.isArray(instrument?.listings) && instrument.listings.some(listing => listing?.active !== false && /(?:^|[._-])SPOT$/.test(normalized(listing?.symbol)));
}

function chooseTicker(instrument) {
    const listings = Array.isArray(instrument?.listings) ? instrument.listings : [];
    const intl = normalized(instrument?.intlSymbol);
    if (intl) return intl;
    const primary = instrument?.primaryExchange;
    if (primary && PREFERRED_EXCHANGES.has(normalized(primary.exchange)) && normalized(primary.symbol)) return normalized(primary.symbol);
    const preferred = listings.find(item => item.active !== false && PREFERRED_EXCHANGES.has(normalized(item.exchange)) && normalized(item.symbol))
        || listings.find(item => PREFERRED_EXCHANGES.has(normalized(item.exchange)) && normalized(item.symbol));
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
    const crypto = instrumentAssetClass(instrument) === 'crypto';
    const exchange = normalized(home.exchange || instrument.exchange);
    const currency = normalized(home.currency || instrument.currency);
    const activeHomeListing = Array.isArray(instrument.listings) && instrument.listings.some(listing => listing?.active !== false && normalized(listing.exchange) === exchange);
    if (instrument.isin !== isin || home.isin !== isin) throw Object.assign(new Error('Broker returned a different ISIN'), { status: 422 });
    // Securities use Trade Republic's TIB/EUR contract. Crypto is verified
    // against its active broker spot listing and the returned EUR home venue
    // (currently BHS for Ethereum), rather than incorrectly forcing TIB.
    const validVenue = crypto ? currency === 'EUR' && (exchange === 'TIB' || (hasActiveSpotListing(instrument) && activeHomeListing)) : exchange === 'TIB' && currency === 'EUR';
    if (!instrument.name || instrument.tradable === false || !validVenue) throw Object.assign(new Error('Instrument is not a tradable EUR Trade Republic instrument'), { status: 422 });
    return { instrument: { ...instrument, ticker: chooseTicker(instrument), exchange, currency }, home: { ...home, exchange, currency } };
}

module.exports = { PREFERRED_EXCHANGES, chooseTicker, instrumentAssetClass, resolveVerifiedInstrument };
