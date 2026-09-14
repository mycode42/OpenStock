const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');

test('responsive charts stretch their SVG interaction plane to the full visible width', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /viewBox="0 0 \$\{width\} \$\{height\}" preserveAspectRatio="none"/);
});

test('hover labels compensate for responsive SVG stretching and support three lines', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    assert.match(source, /const compensateStretch =/);
    assert.match(source, /text\.setAttribute\('transform', compensateStretch/);
    assert.match(source, /class="chart-hover-tertiary"/);
    assert.match(journal, /tertiary: `Cash \$\{journalMoney\(point\.cash_eur\)\} · Positions/);
});

test('PWA asset versions include the chart interaction fix', () => {
    const index = fs.readFileSync(path.join(root, 'www/index.html'), 'utf8');
    const worker = fs.readFileSync(path.join(root, 'www/sw.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(index, /styles\.css\?v=18/);
    assert.match(index, /app\.js\?v=18/);
    assert.match(worker, /openstock-v18/);
    assert.match(worker, /styles\.css\?v=18/);
    assert.match(worker, /app\.js\?v=18/);
    assert.match(worker, /modules\/charts\.js/);
    assert.match(styles, /\.history-chart \.chart-hover-primary\{font-size:8px\}/);
});
