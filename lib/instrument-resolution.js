const PREFERRED_EXCHANGES = new Set(['XNAS', 'XNYS', 'XLON']);

function normalized(value) { return String(value || '').trim().toUpperCase(); }

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
    const exchange = normalized(home.exchange || instrument.exchange);
    const currency = normalized(home.currency || instrument.currency);
    if (instrument.isin !== isin || home.isin !== isin) throw Object.assign(new Error('Broker returned a different ISIN'), { status: 422 });
    if (!instrument.name || instrument.tradable === false || exchange !== 'TIB' || currency !== 'EUR') throw Object.assign(new Error('Instrument is not a tradable EUR TIB instrument'), { status: 422 });
    return { instrument: { ...instrument, ticker: chooseTicker(instrument), exchange, currency }, home: { ...home, exchange, currency } };
}

function cryptoTicker(asset, query = '') {
    const raw = asset?.raw && typeof asset.raw === 'object' ? asset.raw : {};
    const direct = normalized(raw.intlSymbol || raw.cryptoSymbol || raw.ticker || raw.symbol).replace(/[^A-Z0-9.\-]/g, '');
    if (direct && direct.length <= 12) return direct;
    const idTicker = String(asset?.id || '').toUpperCase().match(/^XF000([A-Z]{2,8})\d+$/)?.[1];
    if (idTicker) return idTicker;
    const queryTicker = normalized(query).replace(/[^A-Z0-9.\-]/g, '');
    if (queryTicker && queryTicker.length <= 12) return queryTicker;
    return normalized(asset?.id).slice(0, 20) || 'CRYPTO';
}

function supportedCryptoDestinations(destinations) {
    return (Array.isArray(destinations) ? destinations : []).filter(destination => {
        const modes = (destination.orderModes || []).map(mode => String(mode).toLowerCase());
        const currency = String(destination.currencyId || '').toUpperCase();
        return modes.includes('market') && (!currency || currency === 'EUR');
    });
}

async function resolveCryptoCandidates(sessionClient, query) {
    const text = String(query || '').trim();
    if (!text || text.length > 64) throw Object.assign(new Error('Enter a crypto name or symbol'), { status: 400 });
    const client = await sessionClient.connect();
    if (!client?.assets?.search || !client?.trading?.orderDestinations) throw Object.assign(new Error('Handelsrepublik crypto discovery is unavailable'), { status: 503 });
    const matches = await client.assets.search(text, { type: 'crypto', limit: 10 });
    const candidates = [];
    for (const asset of Array.isArray(matches) ? matches : []) {
        const instrumentId = String(asset?.id || '').trim().toUpperCase();
        if (!instrumentId) continue;
        const type = String(asset?.type || asset?.raw?.type || 'crypto').toLowerCase();
        if (!type.includes('crypto')) continue;
        let destinations;
        try { destinations = supportedCryptoDestinations(await client.trading.orderDestinations(instrumentId)); }
        catch { continue; }
        if (!destinations.length) continue;
        candidates.push({
            instrument_id: instrumentId,
            symbol: cryptoTicker(asset, text),
            name: String(asset.name || asset.raw?.name || asset.raw?.shortName || cryptoTicker(asset, text)).slice(0, 200),
            asset_type: 'crypto',
            venues: destinations.map(destination => ({
                exchange: String(destination.id),
                name: String(destination.name || destination.id),
                currency: String(destination.currencyId || 'EUR').toUpperCase(),
                open: destination.open === true,
                order_modes: destination.orderModes || []
            }))
        });
    }
    return candidates;
}

async function resolveVerifiedCryptoInstrument(sessionClient, { instrumentId, exchange }) {
    const isin = String(instrumentId || '').trim().toUpperCase();
    const venue = String(exchange || '').trim().toUpperCase();
    if (!isin || !venue) throw Object.assign(new Error('Crypto instrument and market are required'), { status: 400 });
    const client = await sessionClient.connect();
    if (!client?.assets?.get || !client?.trading?.orderDestinations) throw Object.assign(new Error('Handelsrepublik crypto discovery is unavailable'), { status: 503 });
    const asset = await client.assets.get(isin);
    const type = String(asset?.type || asset?.raw?.type || '').toLowerCase();
    if (String(asset?.id || '').toUpperCase() !== isin || !type.includes('crypto')) {
        throw Object.assign(new Error('Trade Republic did not verify this crypto instrument'), { status: 422 });
    }
    const destination = supportedCryptoDestinations(await client.trading.orderDestinations(isin))
        .find(item => String(item.id).toUpperCase() === venue);
    if (!destination) throw Object.assign(new Error('Selected crypto market does not support EUR market orders'), { status: 422 });

    if (typeof sessionClient.getQuote !== 'function') throw Object.assign(new Error('Executable crypto quote validation is unavailable'), { status: 503 });
    const quote = await sessionClient.getQuote({ isin, exchange: venue });
    if (quote.currency !== 'EUR' || !(Number(quote.bid_eur) > 0 || Number(quote.ask_eur) > 0)) {
        throw Object.assign(new Error('Trade Republic did not return an executable EUR crypto quote'), { status: 422 });
    }
    const symbol = cryptoTicker(asset);
    const instrument = {
        isin,
        ticker: symbol,
        name: String(asset.name || asset.raw?.name || asset.raw?.shortName || symbol).slice(0, 200),
        type: 'CRYPTO',
        tradable: true
    };
    const home = { isin, exchange: venue, currency: 'EUR', name: destination.name || venue };
    return { instrument, home, asset, destination, quote };
}

module.exports = { PREFERRED_EXCHANGES, chooseTicker, resolveVerifiedInstrument, resolveCryptoCandidates, resolveVerifiedCryptoInstrument };
