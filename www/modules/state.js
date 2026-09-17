export function createState() {
    return { stocks: [], stockMap: {}, trades: [], tradePagination: { page: 1, page_size: 20, total: 0, total_pages: 1 }, journalPage: 1, capitalEvents: [], analytics: null, historyCache: new Map() };
}

export function sortedStocks(stocks) {
    return [...stocks].sort((a, b) => String(a.symbol).localeCompare(String(b.symbol)));
}

export function indexStocks(stocks) {
    return Object.fromEntries(stocks.map(stock => [stock.symbol, stock]));
}
