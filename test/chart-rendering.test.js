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

test('dashboard history charts are native EUR, smoothed, and compress closed-market time', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    const formatting = fs.readFileSync(path.join(root, 'www/modules/formatting.js'), 'utf8');
    assert.match(source, /Number\(point\.price_eur\)/);
    assert.match(source, /primary: `€\$\{money\(point\.price_eur\)\}`/);
    assert.match(source, /native EUR price chart/);
    assert.match(source, /history-title">\$\{chartRangeLabel\(range\)\} · EUR/);
    assert.match(source, /\['1d', '1w', '1mo', '1y', '5y'\]/);
    assert.match(source, /async function loadCardHistory\(card, range = '1d'\)/);
    assert.match(source, /mouseenter', \(\) => loadCardHistory\(card, '1d'\)/);
    assert.match(source, /focusin', \(\) => loadCardHistory\(card, '1d'\)/);
    assert.match(source, /panel\.dataset\.requestedRange \|\| '1d'/);
    assert.match(formatting, /'1w': '1-week'/);
    assert.match(source, /function smoothCoordinatePath/);
    assert.match(source, /x: index \/ \(clean\.length - 1\) \* width/);
    assert.match(source, /smooth: true/);
    assert.match(source, /\.sort\(\(left, right\) => parseOpenStockDate\(left\.recorded_at\)\.getTime\(\) - parseOpenStockDate\(right\.recorded_at\)\.getTime\(\)\)/);
    assert.doesNotMatch(source, /history-gap-line|maxGapMs|coordinateSegments|gapSegments/);
    assert.doesNotMatch(source, /point\.price_usd|USD price chart|· USD/);
});

test('dashboard history percentage follows the inspected point and resets to the range return', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /data-history-change/);
    assert.match(source, /data-default-change/);
    assert.match(source, /change: \(Number\(point\.price_eur\) \/ Number\(clean\[0\]\.price_eur\) - 1\) \* 100/);
    assert.match(source, /updateChange\(point\)/);
    assert.match(source, /resetChange\(\)/);
});

test('trade entry prices use two-decimal input precision', () => {
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    assert.match(journal, /id="trade-entry-price" name="entry_price" type="number" min="\.01" step="\.01" required \$\{isClosing \? 'readonly' : ''\} value="\$\{escapeHtml\(journalInputNumber\(edit\?\.entry_price, 2\)\)\}"/);
});

test('journal close workflow accepts a sale quantity and preserves the open balance', () => {
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    assert.match(journal, /for="trade-close-quantity">Quantity to sell/);
    assert.match(journal, /name="close_quantity" type="number"/);
    assert.match(journal, /Open Quantity/);
    assert.match(journal, /unsold balance remains open/);
});

test('market closures are removed from chart spacing and render as one smooth path', async () => {
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
        ], '1mo');
        assert.equal((html.match(/class="history-line up"/g) || []).length, 1);
        assert.equal((html.match(/history-gap-line/g) || []).length, 0);
        assert.match(html, /<path class="history-line up" d="M0,/);
        assert.match(html, / C/);
        assert.match(html, /&quot;x&quot;:88/);
        assert.match(html, /&quot;x&quot;:176/);
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
    assert.match(journal, /function equityAxisBounds/);
    assert.match(journal, /class="equity-grid-line"/);
    assert.match(journal, /class="journal-equity-y-labels"/);
    assert.match(journal, /class="journal-equity-x-labels"/);
    assert.match(journal, /function renderDailyPerformance/);
    assert.match(journal, /function renderMonthlyPerformance/);
    assert.match(journal, /<th>Return<\/th>/);
    assert.match(journal, /Number\(row\.pnl_eur\) \/ capitalBase \* 100/);
    assert.match(journal, /Showing up to 15 entries per page/);
    assert.match(journal, /function journalChartControls/);
    assert.match(journal, /data-journal-chart-mode/);
    assert.match(journal, /function renderPerformanceCurve/);
    assert.match(journal, /Avg Daily/);
    assert.match(journal, /Avg Monthly/);
    assert.match(journal, /journal-performance-card/);
    assert.match(journal, /function renderPerformanceDetails/);
    assert.match(journal, /<details class="journal-collapsible journal-performance-details">/);
    assert.match(journal, /Monthly compound rate/);
    assert.match(journal, /1-year capital illustration/);
    assert.match(journal, /chartCard\.insertAdjacentElement\('afterend', performanceCard\)/);
});

test('PWA asset versions derive from the release version and include chart interaction assets', () => {
    const index = fs.readFileSync(path.join(root, 'www/index.html'), 'utf8');
    const worker = fs.readFileSync(path.join(root, 'www/sw.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(index, /styles\.css\?v=1\.1\.0/);
    assert.match(index, /app\.js\?v=1\.1\.0/);
    assert.match(worker, /openstock-v1\.1\.0/);
    assert.match(worker, /styles\.css\?v=1\.1\.0/);
    assert.match(worker, /app\.js\?v=1\.1\.0/);
    assert.match(worker, /modules\/charts\.js/);
    assert.match(styles, /\.history-chart \.chart-hover-primary\{font-size:8px\}/);
    assert.match(styles, /\.journal-performance-card \.journal-collapsible-content\{padding:1rem 1\.25rem 1\.25rem\}/);
    assert.match(styles, /\.journal-performance-card \.journal-performance-table\{padding:\.9rem 0 0\}/);
    assert.doesNotMatch(styles, /history-gap-line/);
});

test('journal chart modes use concise labels with no explanatory subtitle', () => {
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(journal, /\['equity', 'Equity'\], \['daily', 'Daily'\], \['monthly', 'Monthly'\]/);
    assert.match(journal, /<header><h1>📓 Trading Journal<\/h1><\/header>/);
    assert.doesNotMatch(journal, /Trades, performance and risk/);
    assert.doesNotMatch(journal, /Current capital and peak equity over time/);
    assert.doesNotMatch(journal, /Realized P\/L in euros, with percentage return on contributed capital/);
    assert.match(journal, /journalKpi\('Est\. Yearly Profit'/);
    assert.match(journal, /journalKpi\('Annualized Return'/);
    assert.doesNotMatch(journal, /<div class="journal-small">Estimated yearly profit/);
    assert.match(styles, /\.journal-chart-controls \.history-range\{min-width:0;min-height:0;padding:\.33rem \.38rem;font-size:\.65rem\}/);
    assert.match(journal, /function journalAxisMoney/);
    assert.match(journal, /€\$\{Math\.round\(amount \/ 1000\)\}k/);
    assert.match(journal, /left = 40, right = 12/);
    assert.match(styles, /\.journal-equity-y-labels span\{position:absolute;right:95\.5%;/);
    assert.match(styles, /\.journal-equity-curve\{height:140px\}/);
    assert.match(styles, /\.journal-equity-chart\{margin-bottom:10px\}\.journal-equity-x-labels\{bottom:-6px\}/);
    assert.doesNotMatch(journal, /journal-chart-scale|Hover or focus for/);
});
