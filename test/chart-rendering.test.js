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

test('dashboard history charts are native EUR, smoothed, and use a fixed 1-day session scale', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    const formatting = fs.readFileSync(path.join(root, 'www/modules/formatting.js'), 'utf8');
    assert.match(source, /Number\(point\.price_eur\)/);
    assert.match(source, /primary: \['1y', '5y'\]\.includes\(range\) \? formatChartDate\(point\.recorded_at\) : formatChartHoverTime\(point\.recorded_at\)/);
    assert.match(source, /price: `€\$\{money\(point\.price_eur\)\}`/);
    assert.match(source, /data-chart-readout/);
    assert.match(source, /data-chart-value/);
    assert.match(source, /updateValue\(point\)/);
    assert.match(source, /resetValue\(\)/);
    assert.match(source, /native EUR price chart/);
    assert.match(source, /history-title">\$\{chartRangeLabel\(range\)\} · EUR/);
    assert.match(source, /\['1d', '1w', '1mo', '1y', '5y'\]/);
    assert.match(source, /async function loadCardHistory\(card, range = '1d', \{ force = false, showLoading = true \} = \{\}\)/);
    assert.match(source, /async function refreshOpenHistoryPanels\(container = document\)/);
    assert.match(source, /loadCardHistory\(card, panel\.dataset\.requestedRange \|\| '1d', \{ force: true, showLoading: false \}\)/);
    assert.match(source, /mouseenter', \(\) => \{ if \(!usesMobileChartInteraction\(\)\) loadCardHistory\(card, '1d'\); \}/);
    assert.match(source, /focusin', \(\) => \{ if \(!usesMobileChartInteraction\(\)\) loadCardHistory\(card, '1d'\); \}/);
    assert.match(source, /panel\.dataset\.requestedRange \|\| '1d'/);
    assert.match(formatting, /'1w': '1-week'/);
    assert.match(source, /function smoothCoordinatePath/);
    assert.match(source, /const hasFixedDayScale/);
    assert.match(source, /formatChartTime\(startAt\)/);
    assert.match(source, /smooth: true/);
    assert.match(source, /\.sort\(\(left, right\) => parseOpenStockDate\(left\.recorded_at\)\.getTime\(\) - parseOpenStockDate\(right\.recorded_at\)\.getTime\(\)\)/);
    assert.doesNotMatch(source, /history-gap-line|maxGapMs|coordinateSegments|gapSegments/);
    assert.doesNotMatch(source, /point\.price_usd|USD price chart|· USD/);
});

test('one-day charts reserve future session time on a 07:30–22:00 wall-clock scale', async () => {
    const sourceDir = path.join(root, 'www/modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-day-chart-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });
    try {
        const { createCharts } = await import(pathToFileURL(path.join(tempDir, 'modules/charts.js')).href);
        const { renderHistoryChart } = createCharts({ request: async () => null, historyCache: new Map() });
        const html = renderHistoryChart([
            { recorded_at: '2026-09-16T05:30:00Z', price_eur: 100 },
            { recorded_at: '2026-09-16T12:45:00Z', price_eur: 101 }
        ], '1d', { start_at: '2026-09-16T05:30:00Z', end_at: '2026-09-16T20:00:00Z' });
        assert.match(html, /&quot;x&quot;:132/);
        assert.match(html, /&quot;primary&quot;:&quot;16\. Sept – 07:30&quot;,&quot;secondary&quot;:&quot;&quot;,&quot;price&quot;:&quot;€100,00&quot;/);
        assert.match(html, /class="history-price" data-chart-value data-default-chart-value="€100,00 – €101,00"/);
        assert.match(html, /<span style="left:0%">07:30<\/span><span style="left:25%">11:07<\/span><span style="left:50%">14:45<\/span><span style="left:75%">18:22<\/span><span style="left:100%">22:00<\/span>/);
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('empty 1d charts show Market closed on Berlin weekends', async () => {
    const sourceDir = path.join(root, 'www/modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-weekend-chart-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });
    try {
        const { createCharts } = await import(pathToFileURL(path.join(tempDir, 'modules/charts.js')).href);
        const { renderHistoryChart } = createCharts({ request: async () => null, historyCache: new Map() });
        const saturday = new Date('2026-10-03T12:00:00+02:00');
        const monday = new Date('2026-10-05T12:00:00+02:00');
        assert.match(renderHistoryChart([], '1d', { now: saturday }), /market closed/);
        assert.match(renderHistoryChart([{ recorded_at: '2026-10-02T12:00:00Z', price_eur: 100 }], '1d', { now: saturday }), /market closed/);
        assert.match(renderHistoryChart([], '1d', { now: monday }), /collecting price history/);
        assert.match(renderHistoryChart([], '1w', { now: saturday }), /collecting price history/);
        const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
        assert.match(source, /history-loading">loading price history/);
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('dashboard history percentage follows the inspected point and resets to the range return', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /data-chart-change/);
    assert.match(source, /data-default-chart-change/);
    assert.match(source, /change: \(Number\(point\.price_eur\) \/ Number\(clean\[0\]\.price_eur\) - 1\) \* 100/);
    assert.match(source, /updateChange\(point\)/);
    assert.match(source, /resetChange\(\)/);
});

test('dashboard chart popovers remain available briefly while crossing their placement gap', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    assert.match(source, /setTimeout\(\(\) => panel\.classList\.remove\('is-open'\), 300\)/);
    assert.match(source, /card\.addEventListener\('pointerleave', \(\) => \{ if \(!usesMobileChartInteraction\(\)\) delayPopoverClose\(\); \}\)/);
    assert.match(source, /panel\.addEventListener\('pointerenter', \(\) => \{ if \(!usesMobileChartInteraction\(\)\) cancelPopoverClose\(\); \}\)/);
    assert.match(source, /panel\.addEventListener\('pointerleave', \(\) => \{ if \(!usesMobileChartInteraction\(\)\) delayPopoverClose\(\); \}\)/);
});

test('mobile dashboard charts open on a card click and close when another surface is clicked', () => {
    const source = fs.readFileSync(path.join(root, 'www/modules/charts.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(source, /window\.matchMedia\?\.\('\(hover: none\), \(pointer: coarse\)'\)/);
    assert.match(source, /card\.addEventListener\('click', event =>/);
    assert.match(source, /closeMobilePopovers\(panel\)/);
    assert.match(source, /document\.addEventListener\('click', event =>/);
    assert.doesNotMatch(source, /touchstart/);
    assert.match(styles, /@media\(max-width:640px\).*\.history-popover\.is-open\{display:block\}/);
    assert.doesNotMatch(styles, /@media\(max-width:640px\).*\.stock-card:focus-within \.history-popover/);
});

test('desktop dashboard hover charts are double-sized without changing mobile dimensions', () => {
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(styles, /\.history-popover\{[^}]*width:576px/);
    assert.match(styles, /\.history-chart\{display:block;width:100%;height:164px/);
    assert.match(styles, /@media\(max-width:640px\)\{\.history-popover\{[^}]*width:100%[^}]*\}\.history-chart\{height:82px\}/);
    assert.match(styles, /\.history-price\{color:inherit;font-size:inherit;font-weight:inherit\}/);
    assert.match(styles, /\.history-chart \.chart-hover-primary\{font-size:10px\}/);
    assert.match(styles, /@media\(max-width:640px\).*\.history-chart \.chart-hover-primary\{font-size:8px\}.*\.history-chart \.chart-hover-secondary\{font-size:6px\}/);
    assert.match(styles, /\.history-dates span:not\(:first-child\):not\(:last-child\)\{display:none\}/);
});

test('active dashboard hover price keeps the original range typography', () => {
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(styles, /\.history-meta,\.history-dates\{color:#94a3b8;font-size:\.72rem\}/);
    assert.match(styles, /\.history-price\{color:inherit;font-size:inherit;font-weight:inherit\}/);
});

test('dashboard charts provide five position-matched x-axis labels on desktop', async () => {
    const sourceDir = path.join(root, 'www/modules');
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-axis-ticks-'));
    fs.writeFileSync(path.join(tempDir, 'package.json'), '{"type":"module"}\n');
    fs.cpSync(sourceDir, path.join(tempDir, 'modules'), { recursive: true });
    try {
        const { createCharts } = await import(pathToFileURL(path.join(tempDir, 'modules/charts.js')).href);
        const { renderHistoryChart } = createCharts({ request: async () => null, historyCache: new Map() });
        const html = renderHistoryChart([
            { recorded_at: '2026-09-16T05:30:00Z', price_eur: 100 },
            { recorded_at: '2026-09-16T09:07:30Z', price_eur: 101 }
        ], '1d', { start_at: '2026-09-16T05:30:00Z', end_at: '2026-09-16T20:00:00Z' });
        assert.match(html, /class="history-dates" aria-label="Chart time axis">(?:<span[^>]*>[^<]+<\/span>){5}<\/div>/);
        assert.match(html, /left:25%/);
        assert.match(html, /left:50%/);
        assert.match(html, /left:75%/);
    } finally {
        fs.rmSync(tempDir, { recursive: true, force: true });
    }
});

test('trade buy and sell prices use six-decimal input precision', () => {
    const journal = fs.readFileSync(path.join(root, 'www/modules/journal.js'), 'utf8');
    assert.match(journal, /id="trade-entry-price" name="entry_price" type="number" min="\.000001" step="\.000001" required \$\{isClosing \? 'readonly' : ''\} value="\$\{escapeHtml\(journalInputNumber\(edit\?\.entry_price, 6\)\)\}"/);
    assert.match(journal, /id="trade-exit-price" name="exit_price" type="number" min="\.000001" step="\.000001" \$\{isClosing \? 'required' : ''\} value="\$\{escapeHtml\(journalInputNumber\(edit\?\.exit_price, 6\)\)\}"/);
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
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(source, /const compensateStretch =/);
    assert.match(source, /text\.setAttribute\('transform', compensateStretch/);
    assert.match(source, /class="chart-hover-tertiary"/);
    assert.doesNotMatch(journal, /const equityDetail/);
    assert.match(journal, /const performanceDetail = point => `\$\{point\.trades\} \$\{point\.trades === 1 \? 'trade' : 'trades'\}`/);
    assert.match(journal, /primary: daily \? journalDayMonthLabel\(point\.at\) : journalMonthLabel\(point\.at\), secondary: '', tertiary: ''/);
    assert.match(journal, /const capitalBase = Number\(initialCapital\)/);
    assert.match(journal, /change: capitalBase > 0 \? \(value \/ capitalBase - 1\) \* 100 : null/);
    assert.match(journal, /renderJournalChart\(analytics, journalChartMode, journalPerformanceChartType, initialCapital\?\.amount_eur\)/);
    assert.match(styles, /\.journal-equity-curve \.chart-hover-text\{font-size:10px\}/);
    assert.match(journal, /function equityAxisBounds/);
    assert.match(journal, /return `\$\{number > 0 \? '\+' : number < 0 \? '-' : ''\}\$\{journalMoney\(Math\.abs\(number\)\)\}`/);
    assert.match(journal, /class="equity-grid-line"/);
    assert.match(journal, /class="journal-equity-y-labels"/);
    assert.match(journal, /class="journal-equity-x-labels(?:\s[^"]*)?"/);
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
    const version = require('../package.json').version.replaceAll('.', '\\.');
    const index = fs.readFileSync(path.join(root, 'www/index.html'), 'utf8');
    const worker = fs.readFileSync(path.join(root, 'www/sw.js'), 'utf8');
    const styles = fs.readFileSync(path.join(root, 'www/styles.css'), 'utf8');
    assert.match(index, new RegExp(`styles\\.css\\?v=${version}`));
    assert.match(index, new RegExp(`app\\.js\\?v=${version}`));
    assert.match(worker, new RegExp(`openstock-v${version}`));
    assert.match(worker, new RegExp(`styles\\.css\\?v=${version}`));
    assert.match(worker, new RegExp(`app\\.js\\?v=${version}`));
    assert.match(worker, /modules\/charts\.js/);
    assert.match(styles, /\.history-chart \.chart-hover-primary\{font-size:10px\}/);
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
