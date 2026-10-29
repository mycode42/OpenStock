const test = require('node:test');
const assert = require('node:assert/strict');
const { cpuPercent, parseMeminfo, parseProcStat, parseProcessStat, parseZramStat } = require('../server/system-load');

test('cpuPercent is the busy share of the time between two samples', () => {
    const before = { idle: 100, total: 200 };
    const after = { idle: 150, total: 300 };
    assert.equal(cpuPercent(before, after), 50);
    assert.equal(cpuPercent(before, before), 0);
});

test('parseProcStat reads the overall line and each core', () => {
    const text = [
        'cpu  100 0 50 800 50 0 0 0 0 0',
        'cpu0 50 0 25 400 25 0 0 0 0 0',
        'cpu1 50 0 25 400 25 0 0 0 0 0',
        'intr 12345',
    ].join('\n');
    const parsed = parseProcStat(text);
    assert.deepEqual(parsed.overall, { idle: 850, total: 1000 });
    assert.equal(parsed.cores.length, 2);
});

test('parseMeminfo converts kB to MB and computes used percentages', () => {
    const text = 'MemTotal: 16384 kB\nMemAvailable: 8192 kB\nSwapTotal: 4096 kB\nSwapFree: 1024 kB\n';
    const parsed = parseMeminfo(text);
    assert.equal(parsed.memory.total_mb, 16);
    assert.equal(parsed.memory.used_mb, 8);
    assert.equal(parsed.memory.percent, 50);
    assert.equal(parsed.swap.used_mb, 3);
    assert.equal(parsed.swap.percent, 75);
});

test('parseZramStat reports original, compressed, and RAM use with the ratio', () => {
    const mb = 1024 * 1024;
    const parsed = parseZramStat(`${8 * 1024 * mb / 2} ${1024 * mb} ${2 * 1024 * mb} 0 0 0 0 0`);
    assert.equal(parsed.data_mb, 4096);
    assert.equal(parsed.compressed_mb, 1024);
    assert.equal(parsed.ratio, 4);
    assert.equal(parseZramStat('not numbers'), null);
});

test('parseProcessStat handles command names with spaces and parentheses', () => {
    const fields = Array.from({ length: 50 }, (_, index) => String(index));
    fields[1] = '42';
    fields[11] = '100';
    fields[12] = '50';
    const parsed = parseProcessStat(`123 (my (odd) name) S ${fields.slice(1).join(' ')}`);
    assert.equal(parsed.name, 'my (odd) name');
    assert.equal(parsed.ppid, 42);
    assert.equal(parsed.ticks, 150);
});
