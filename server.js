require('dotenv').config();
const express = require('express');
const path = require('path');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');

// Ensure database exists before starting server
const dbPath = process.env.DATABASE_PATH || '/home/ubuntu/openstock/database/openstock.db';
initDatabase();

const db = new OpenStockDB(dbPath);
const app = express();
const PORT = process.env.PORT || 3000;

// Middleware
app.use(express.json());
app.use(express.urlencoded({ extended: false }));
app.use(express.static(path.join(__dirname, 'www')));

// ===== API: Stocks =====
app.get('/api/stocks', (req, res) => {
    try {
        const stocks = db.getAllStocks();
        res.json(stocks);
    } catch (e) {
        console.error('Error fetching stocks:', e.message);
        res.status(500).json({ error: 'Failed to fetch stocks' });
    }
});

app.get('/api/stocks/:id', (req, res) => {
    try {
        const stock = db.getStockById(req.params.id);
        if (!stock) {
            return res.status(404).json({ error: 'Stock not found' });
        }
        res.json(stock);
    } catch (e) {
        console.error('Error fetching stock:', e.message);
        res.status(500).json({ error: 'Failed to fetch stock' });
    }
});

app.post('/api/stocks', (req, res) => {
    try {
        const { symbol, name, currency = 'USD', buy_threshold_eur, sell_threshold_eur } = req.body;

        // Validation
        if (!symbol || typeof symbol !== 'string') {
            return res.status(400).json({ error: 'Symbol is required' });
        }
        // Validate stock symbol format (1-5 uppercase letters, common tickers)
        const symbolRegex = /^[A-Z]{1,5}$/;
        if (!symbolRegex.test(symbol.trim())) {
            return res.status(400).json({ error: 'Invalid stock symbol format. Use 1-5 uppercase letters (e.g., AAPL, TSLA, NVDA)' });
        }
        if (!name || typeof name !== 'string') {
            return res.status(400).json({ error: 'Name is required' });
        }
        if (buy_threshold_eur === undefined || sell_threshold_eur === undefined) {
            return res.status(400).json({ error: 'Buy and sell thresholds are required' });
        }

        const buyThreshold = parseFloat(buy_threshold_eur);
        const sellThreshold = parseFloat(sell_threshold_eur);

        if (isNaN(buyThreshold) || isNaN(sellThreshold)) {
            return res.status(400).json({ error: 'Thresholds must be valid numbers' });
        }
        if (buyThreshold <= 0 || sellThreshold <= 0) {
            return res.status(400).json({ error: 'Thresholds must be positive' });
        }
        if (buyThreshold >= sellThreshold) {
            return res.status(400).json({ error: 'Buy threshold must be less than sell threshold' });
        }

        // Check stock limit
        const count = db.getAllStocks().length;
        if (count >= 25) {
            return res.status(400).json({ error: 'Maximum 25 stocks allowed' });
        }

        db.addStock(symbol.toUpperCase(), name, currency, buyThreshold, sellThreshold);
        res.status(201).json({ message: 'Stock added successfully' });
    } catch (e) {
        if (e.code === 'SQLITE_CONSTRAINT') {
            return res.status(409).json({ error: 'Stock symbol already exists' });
        }
        console.error('Error adding stock:', e.message);
        res.status(500).json({ error: 'Failed to add stock' });
    }
});

app.put('/api/stocks/:id', (req, res) => {
    try {
        const { buy_threshold_eur, sell_threshold_eur, owned, enabled, name } = req.body;
        const updates = {};

        if (buy_threshold_eur !== undefined) {
            const val = parseFloat(buy_threshold_eur);
            if (isNaN(val) || val <= 0) {
                return res.status(400).json({ error: 'Invalid buy threshold' });
            }
            updates.buy_threshold_eur = val;
        }

        if (sell_threshold_eur !== undefined) {
            const val = parseFloat(sell_threshold_eur);
            if (isNaN(val) || val <= 0) {
                return res.status(400).json({ error: 'Invalid sell threshold' });
            }
            updates.sell_threshold_eur = val;
        }

        if (buy_threshold_eur !== undefined && sell_threshold_eur !== undefined) {
            if (updates.buy_threshold_eur >= updates.sell_threshold_eur) {
                return res.status(400).json({ error: 'Buy threshold must be less than sell threshold' });
            }
        } else if (buy_threshold_eur !== undefined) {
            const existing = db.getStockById(req.params.id);
            if (existing && updates.buy_threshold_eur >= existing.sell_threshold_eur) {
                return res.status(400).json({ error: 'Buy threshold must be less than sell threshold' });
            }
        } else if (sell_threshold_eur !== undefined) {
            const existing = db.getStockById(req.params.id);
            if (existing && updates.sell_threshold_eur <= existing.buy_threshold_eur) {
                return res.status(400).json({ error: 'Sell threshold must be greater than buy threshold' });
            }
        }

        if (owned !== undefined) {
            const stock = db.getStockById(req.params.id);
            const newOwned = owned === 1 || owned === '1' || owned === true || owned === 'true';
            if (!stock) {
                return res.status(404).json({ error: 'Stock not found' });
            }
            updates.owned = newOwned ? 1 : 0;
        }

        if (enabled !== undefined) {
            const stock = db.getStockById(req.params.id);
            if (!stock) {
                return res.status(404).json({ error: 'Stock not found' });
            }
            updates.enabled = enabled === 1 || enabled === '1' || enabled === true || enabled === 'true' ? 1 : 0;
        }

        if (name !== undefined) {
            if (!name || typeof name !== 'string' || name.trim() === '') {
                return res.status(400).json({ error: 'Invalid name' });
            }
            updates.name = name.trim();
        }

        if (Object.keys(updates).length === 0) {
            return res.status(400).json({ error: 'No valid fields to update' });
        }

        db.updateStock(req.params.id, updates);
        res.json({ message: 'Stock updated successfully' });
    } catch (e) {
        console.error('Error updating stock:', e.message);
        res.status(500).json({ error: 'Failed to update stock' });
    }
});

app.delete('/api/stocks/:id', (req, res) => {
    try {
        const result = db.removeStock(req.params.id);
        if (result.changes === 0) {
            return res.status(404).json({ error: 'Stock not found' });
        }
        res.json({ message: 'Stock deleted successfully' });
    } catch (e) {
        console.error('Error deleting stock:', e.message);
        res.status(500).json({ error: 'Failed to delete stock' });
    }
});

// ===== API: Journal =====
app.get('/api/journal', (req, res) => {
    try {
        const entries = db.getAllJournalEntries();
        res.json(entries);
    } catch (e) {
        console.error('Error fetching journal:', e.message);
        res.status(500).json({ error: 'Failed to fetch journal entries' });
    }
});

app.post('/api/journal', (req, res) => {
    try {
        const { stock_id, action, price_eur, quantity, notes = '' } = req.body;

        if (!stock_id) {
            return res.status(400).json({ error: 'Stock ID is required' });
        }
        if (!action || (action !== 'BUY' && action !== 'SELL')) {
            return res.status(400).json({ error: 'Action must be BUY or SELL' });
        }
        if (!price_eur) {
            return res.status(400).json({ error: 'Price is required' });
        }
        if (!quantity) {
            return res.status(400).json({ error: 'Quantity is required' });
        }

        const priceEur = parseFloat(price_eur);
        const qty = parseFloat(quantity);

        if (isNaN(priceEur) || isNaN(qty) || priceEur <= 0 || qty <= 0) {
            return res.status(400).json({ error: 'Invalid price or quantity' });
        }

        // Verify stock exists
        const stock = db.getStockById(stock_id);
        if (!stock) {
            return res.status(404).json({ error: 'Stock not found' });
        }

        db.addJournalEntry(stock_id, action.toUpperCase(), priceEur, qty, notes);
        res.status(201).json({ message: 'Journal entry added successfully' });
    } catch (e) {
        console.error('Error adding journal entry:', e.message);
        res.status(500).json({ error: 'Failed to add journal entry' });
    }
});

app.put('/api/journal/:id', (req, res) => {
    try {
        const { action, price_eur, quantity, notes = '' } = req.body;

        if (!action || (action !== 'BUY' && action !== 'SELL')) {
            return res.status(400).json({ error: 'Action must be BUY or SELL' });
        }

        const priceEur = parseFloat(price_eur);
        const qty = parseFloat(quantity);

        if (isNaN(priceEur) || isNaN(qty) || priceEur <= 0 || qty <= 0) {
            return res.status(400).json({ error: 'Invalid price or quantity' });
        }

        db.updateJournalEntry(req.params.id, action.toUpperCase(), priceEur, qty, notes);
        res.json({ message: 'Journal entry updated successfully' });
    } catch (e) {
        console.error('Error updating journal entry:', e.message);
        res.status(500).json({ error: 'Failed to update journal entry' });
    }
});

app.delete('/api/journal/:id', (req, res) => {
    try {
        const result = db.deleteJournalEntry(req.params.id);
        if (result.changes === 0) {
            return res.status(404).json({ error: 'Journal entry not found' });
        }
        res.json({ message: 'Journal entry deleted successfully' });
    } catch (e) {
        console.error('Error deleting journal entry:', e.message);
        res.status(500).json({ error: 'Failed to delete journal entry' });
    }
});

// ===== API: Dashboard data =====
app.get('/api/dashboard', (req, res) => {
    try {
        const stocks = db.getAllStocks();
        const entries = db.getAllJournalEntries();

        // Calculate P/L summary
        let realizedPl = 0;
        const buyPositions = {};
        
        for (const entry of entries) {
            if (entry.action === 'BUY') {
                if (!buyPositions[entry.stock_id]) buyPositions[entry.stock_id] = [];
                buyPositions[entry.stock_id].push(entry);
            } else if (entry.action === 'SELL') {
                const buys = buyPositions[entry.stock_id] || [];
                if (buys.length > 0) {
                    const buy = buys.shift(); // FIFO
                    realizedPl += (entry.price_eur - buy.price_eur) * entry.quantity;
                }
            }
        }

        res.json({
            stocks,
            journal_count: entries.length,
            realized_pl: realizedPl.toFixed(2)
        });
    } catch (e) {
        console.error('Error fetching dashboard:', e.message);
        res.status(500).json({ error: 'Failed to fetch dashboard data' });
    }
});

// Health check
app.get('/health', (req, res) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Serve index.html for all other routes (SPA fallback)
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'www', 'index.html'));
});

app.get('/config', (req, res) => {
    res.sendFile(path.join(__dirname, 'www', 'config', 'index.html'));
});

app.get('/journal', (req, res) => {
    res.sendFile(path.join(__dirname, 'www', 'journal', 'index.html'));
});

app.listen(PORT, '127.0.0.1', () => {
    console.log(`OpenStock server listening on 127.0.0.1:${PORT}`);
});

// Keep references for graceful shutdown
let dbRef = db;
process.on('SIGTERM', () => {
    console.log('SIGTERM received, shutting down...');
    dbRef.db.close();
    process.exit(0);
});

process.on('SIGINT', () => {
    console.log('SIGINT received, shutting down...');
    dbRef.db.close();
    process.exit(0);
});