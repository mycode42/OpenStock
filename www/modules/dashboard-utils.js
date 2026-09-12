export function monitoredStocks(stocks) { return stocks.filter(stock => Number(stock.enabled) === 1 && Number(stock.archived) === 0); }
