export function createState() {
    return { stocks: [], stockMap: {}, trades: [], capitalEvents: [], analytics: null, historyCache: new Map() };
}

export function sortedStocks(stocks) {
    return [...stocks].sort((a, b) => String(a.symbol).localeCompare(String(b.symbol)));
}

export function indexStocks(stocks) {
    return Object.fromEntries(stocks.map(stock => [stock.symbol, stock]));
}
