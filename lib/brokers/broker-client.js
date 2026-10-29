class BrokerError extends Error {
  constructor(message, code = 'BROKER_ERROR', status = 502) { super(message); this.code = code; this.status = status; }
}

class BrokerClient {
  async health() { throw new Error('Not implemented'); }
  async getPortfolio() { throw new Error('Not implemented'); }
  async getAccounts() { throw new Error('Not implemented'); }
  async getOrders() { throw new Error('Not implemented'); }
  async getTransactions() { throw new Error('Not implemented'); }
  async getQuote() { throw new Error('Not implemented'); }
  async getInstrument() { throw new Error('Not implemented'); }
}

module.exports = { BrokerClient, BrokerError };
