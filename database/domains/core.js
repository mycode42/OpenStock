'use strict';

const Database = require('better-sqlite3');

class DatabaseCore {
    constructor(dbPath) {
        this.db = new Database(dbPath);
        this.db.pragma('journal_mode = WAL');
        this.db.pragma('synchronous = NORMAL');
        this.db.pragma('foreign_keys = ON');
    }
}

module.exports = DatabaseCore;
