const Database = require('better-sqlite3');
const fs = require('fs');
const path = require('path');

function initDatabase() {
    const dbPath = process.env.DATABASE_PATH || '/home/ubuntu/openstock/database/openstock.db';
    const db = new Database(dbPath);

    // Read schema
    const schemaPath = path.join(__dirname, 'schema.sql');
    const schema = fs.readFileSync(schemaPath, 'utf8');
    
    // Execute schema
    db.exec(schema);
    console.log('Database schema initialized at:', dbPath);

    // Insert default stocks if empty
    const stockCount = db.prepare('SELECT COUNT(*) as count FROM stocks').get().count;
    if (stockCount === 0) {
        const insertStock = db.prepare(`
            INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur)
            VALUES (?, ?, ?, ?, ?)
        `);
        
        const defaults = [
            ['AAPL', 'Apple Inc.', 'USD', 150.0, 200.0],
            ['MSFT', 'Microsoft Corporation', 'USD', 250.0, 350.0],
            ['GOOGL', 'Alphabet Inc.', 'USD', 120.0, 180.0],
            ['TSLA', 'Tesla Inc.', 'USD', 200.0, 300.0],
            ['AMZN', 'Amazon.com Inc.', 'USD', 100.0, 150.0]
        ];

        const inserted = defaults.map(stock => {
            try {
                insertStock.run(...stock);
                return true;
            } catch (e) {
                console.warn(`Failed to insert stock ${stock[0]}:`, e.message);
                return false;
            }
        }).filter(Boolean).length;

        console.log(`Inserted ${inserted} default stocks`);
    }

    db.close();
    return true;
}

if (require.main === module) {
    initDatabase();
}

module.exports = { initDatabase };