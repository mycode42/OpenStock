const fs = require('node:fs');
const path = require('node:path');

module.exports = {
  version: 1,
  name: 'initial_schema',
  up(db) {
    const schema = fs.readFileSync(path.join(__dirname, '..', 'schema.sql'), 'utf8')
      .replace(/^PRAGMA[^;]+;\s*/gim, '');
    db.exec(schema);
  }
};
