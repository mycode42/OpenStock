const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const root = path.resolve(__dirname, '..');

test('responsive charts stretch their SVG interaction plane to the full visible width', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /viewBox="0 0 \$\{width\} \$\{height\}" preserveAspectRatio="none"/);
});

test('dashboard history charts are native EUR with dashed market-closure connectors', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /Number\(point\.price_eur\)/);
    assert.match(source, /primary: `€\$\{money\(point\.price_eur\)\}`/);
    assert.match(source, /native EUR price chart/);
    assert.match(source, /history-title">\$\{chartRangeLabel\(range\)\} · EUR/);
    assert.match(source, /maxGapMs/);
    assert.match(source, /times\[index\] - times\[index - 1\] > maxGapMs/);
    assert.match(source, /coordinateSegments\.push\(\[\]\)/);
    assert.match(source, /const gapSegments = coordinateSegments\.slice\(0, -1\)/);
    assert.match(source, /history-gap-line/);
    assert.match(source, /\.sort\(\(left, right\) => parseOpenStockDate\(left\.recorded_at\)\.getTime\(\) - parseOpenStockDate\(right\.recorded_at\)\.getTime\(\)\)/);
    assert.doesNotMatch(source, /liveSources/);
    assert.doesNotMatch(source, /point\.price_usd|USD price chart|· USD/);
});

test('market closures render as dashed connectors rather than blank chart spans', async () => {
    const sourceDir = path.join(root, 'www/modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-chart-render-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });
    try {
        const { createCharts } = await import(pathToFileURL(path.join(tempDir, 'modules/charts.js')).href);
        const { renderHistoryChart } = createCharts({ request: async () => null, historyCache: new Map() });
        const html = renderHistoryChart([
            { recorded_at: '2026-09-14T08:00:00Z', price_eur: 100 },
            { recorded_at: '2026-09-14T09:00:00Z', price_eur: 101 },
            { recorded_at: '2026-09-15T08:00:00Z', price_eur: 102 },
            { recorded_at: '2026-09-15T09:00:00Z', price_eur: 103 }
        ], '1mo', 7200);
        assert.equal((html.match(/history-gap-line/g) || []).length, 1);
        assert.equal((html.match(/<polyline/g) || []).length, 3);
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('hover labels compensate for responsive SVG stretching and support three lines', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    assert.match(source, /const compensateStretch =/);
    assert.match(source, /text\.setAttribute\('transform', compensateStretch/);
    assert.match(source, /class="chart-hover-tertiary"/);
    assert.match(journal, /tertiary: `Cash \$\{journalMoney\(point\.cash_eur\)\} · Positions/);
});

test('PWA asset versions are consistent and include the chart interaction fix', () => {
    const index = fs.readFileSync(path.join(root, 'www/index.html'), 'utf8');
    const worker = fs.readFileSync(path.join(root, 'www/sw.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(index, /styles\.css\?v=35/);
    assert.match(index, /app\.js\?v=35/);
    assert.match(worker, /openstock-v35/);
    assert.match(worker, /styles\.css\?v=35/);
    assert.match(worker, /app\.js\?v=35/);
    assert.match(worker, /modules\/charts\.js/);
    assert.match(styles, /\.history-chart \.chart-hover-primary\{font-size:8px\}/);
    assert.match(styles, /\.history-gap-line\{stroke-dasharray:4 4;stroke-width:1\.35;stroke-opacity:\.48;filter:none\}/);
});
