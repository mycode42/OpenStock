const fs = require('node:fs/promises');
const path = require('node:path');

// Read-only host load for the top-bar gauge. Everything comes from /proc and /sys; nothing is written.
const CLOCK_TICKS = 100;
const PAGE_BYTES = 4096;
const TOP_PROCESSES = 10;

function parseProcStat(text) {
    const cores = [];
    let overall = null;
    for (const line of String(text).split('\n')) {
        if (!line.startsWith('cpu')) continue;
        const parts = line.trim().split(/\s+/);
        const values = parts.slice(1, 9).map(Number);
        if (values.length < 4 || values.some(item => !Number.isFinite(item))) continue;
        const sample = { idle: values[3] + (values[4] || 0), total: values.reduce((sum, item) => sum + item, 0) };
        if (parts[0] === 'cpu') overall = sample;
        else cores.push(sample);
    }
    return { overall, cores };
}

function cpuPercent(previous, current) {
    const total = current.total - previous.total;
    const idle = current.idle - previous.idle;
    if (!(total > 0)) return 0;
    return Math.max(0, Math.min(100, 100 * (1 - idle / total)));
}

function parseMeminfo(text) {
    const values = {};
    for (const line of String(text).split('\n')) {
        const match = /^(\w+):\s+(\d+)\s+kB/.exec(line);
        if (match) values[match[1]] = Number(match[2]) / 1024;
    }
    const totalMb = values.MemTotal || 0;
    const availableMb = values.MemAvailable ?? 0;
    const usedMb = Math.max(0, totalMb - availableMb);
    const swapTotalMb = values.SwapTotal || 0;
    const swapUsedMb = Math.max(0, swapTotalMb - (values.SwapFree || 0));
    return {
        memory: { total_mb: round(totalMb), used_mb: round(usedMb), available_mb: round(availableMb), percent: percentOf(usedMb, totalMb) },
        swap: { total_mb: round(swapTotalMb), used_mb: round(swapUsedMb), percent: percentOf(swapUsedMb, swapTotalMb) },
    };
}

// /sys/block/zram0/mm_stat: orig_data_size, compr_data_size, mem_used_total, ... (bytes)
function parseZramStat(text) {
    const fields = String(text).trim().split(/\s+/).map(Number);
    if (fields.length < 3 || fields.some(item => !Number.isFinite(item))) return null;
    const [original, compressed, memUsed] = fields;
    return {
        data_mb: round(original / (1024 * 1024)),
        compressed_mb: round(compressed / (1024 * 1024)),
        ram_used_mb: round(memUsed / (1024 * 1024)),
        ratio: compressed > 0 ? round(original / compressed, 2) : null,
    };
}

// /proc/<pid>/stat: fields after the command name. Index 1 is ppid, 11 utime, 12 stime, 19 starttime.
function parseProcessStat(text) {
    const close = String(text).lastIndexOf(')');
    if (close < 0) return null;
    const name = String(text).slice(String(text).indexOf('(') + 1, close);
    const parts = String(text).slice(close + 2).split(' ');
    const ppid = Number(parts[1]);
    const ticks = Number(parts[11]) + Number(parts[12]);
    if (!Number.isFinite(ppid) || !Number.isFinite(ticks)) return null;
    return { name, ppid, ticks };
}

function cpuPercentOfProcess(previousTicks, currentTicks, seconds) {
    if (!(seconds > 0) || previousTicks === undefined) return 0;
    return Math.max(0, 100 * ((currentTicks - previousTicks) / CLOCK_TICKS) / seconds);
}

function round(value, digits = 1) {
    if (!Number.isFinite(value)) return null;
    const factor = 10 ** digits;
    return Math.round(value * factor) / factor;
}

function percentOf(part, whole) {
    return whole > 0 ? round(100 * part / whole) : 0;
}

async function readText(file) {
    try {
        return await fs.readFile(file, 'utf8');
    } catch (error) {
        if (error.code === 'ENOENT' || error.code === 'EACCES' || error.code === 'ESRCH') return null;
        throw error;
    }
}

async function readProcesses() {
    const processes = new Map();
    let entries = [];
    try { entries = await fs.readdir('/proc'); } catch { return processes; }
    await Promise.all(entries.filter(entry => /^\d+$/.test(entry)).map(async entry => {
        const stat = parseProcessStat(await readText(`/proc/${entry}/stat`) || '');
        if (!stat) return;
        const statm = (await readText(`/proc/${entry}/statm`) || '').split(' ');
        processes.set(Number(entry), { ...stat, rss_mb: round(Number(statm[1] || 0) * PAGE_BYTES / (1024 * 1024)) });
    }));
    return processes;
}

function descendants(processes, rootPid) {
    const children = new Map();
    for (const [pid, info] of processes) {
        if (!children.has(info.ppid)) children.set(info.ppid, []);
        children.get(info.ppid).push(pid);
    }
    const seen = new Set();
    const stack = [rootPid];
    while (stack.length) {
        const pid = stack.pop();
        if (seen.has(pid)) continue;
        seen.add(pid);
        stack.push(...(children.get(pid) || []));
    }
    return [...seen].filter(pid => processes.has(pid));
}

function processAlive(pid) {
    try {
        process.kill(pid, 0);
        return true;
    } catch (error) {
        return error.code === 'EPERM';
    }
}

// The running research job, if any: the pid recorded by the lab, checked against the live process table.
async function activeResearchPid(root) {
    const jobsDir = path.join(root, 'data', 'research-lab', 'jobs');
    let entries = [];
    try { entries = await fs.readdir(jobsDir); } catch { return null; }
    for (const entry of entries.sort().reverse()) {
        const job = JSON.parse(await readText(path.join(jobsDir, entry, 'job.json')) || 'null');
        if (job && (job.state === 'running' || job.state === 'cancelling') && job.pid && processAlive(job.pid)) {
            return { id: job.id, pid: job.pid, strategy: job.strategy };
        }
    }
    return null;
}

function createSystemLoad({ root, procRoot = '/proc', sysRoot = '/sys', sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
    let previous = null;

    async function sampleCpu() {
        const text = await readText(path.join(procRoot, 'stat'));
        return text ? parseProcStat(text) : { overall: null, cores: [] };
    }

    return async function snapshot() {
        let before = previous;
        if (!before || Date.now() - before.at > 10_000) {
            // With no recent sample, take one, wait briefly, then measure against it.
            const at = Date.now();
            const cpu = await sampleCpu();
            const processes = await readProcesses();
            await sleep(250);
            before = { at, cpu, processes };
        }
        const at = Date.now();
        const cpu = await sampleCpu();
        const processes = await readProcesses();
        const seconds = (at - before.at) / 1000;
        const overall = cpuPercent(before.cpu.overall || { idle: 0, total: 0 }, cpu.overall || { idle: 0, total: 0 });
        const cores = cpu.cores.map((core, index) => round(cpuPercent(before.cpu.cores[index] || { idle: 0, total: 0 }, core), 0));

        const tableRows = [...processes].map(([pid, info]) => {
            const earlier = before.processes.get(pid);
            return {
                pid, name: info.name, rss_mb: info.rss_mb,
                cpu_percent: round(cpuPercentOfProcess(earlier?.ticks, info.ticks, seconds), 1),
                ppid: info.ppid, ticks: info.ticks,
            };
        }).sort((a, b) => b.cpu_percent - a.cpu_percent || b.rss_mb - a.rss_mb);

        const research = await activeResearchPid(root);
        let researchSummary = null;
        if (research) {
            const pids = descendants(processes, research.pid);
            const members = pids.map(pid => ({ pid, info: processes.get(pid), earlier: before.processes.get(pid) }));
            researchSummary = {
                id: research.id,
                strategy: research.strategy,
                processes: pids.length,
                rss_mb: round(members.reduce((sum, item) => sum + (item.info?.rss_mb || 0), 0)),
                cpu_percent: round(members.reduce((sum, item) => sum + cpuPercentOfProcess(item.earlier?.ticks, item.info?.ticks, seconds), 0)),
            };
        }

        const meminfo = parseMeminfo(await readText(path.join(procRoot, 'meminfo')) || '');
        const zram = parseZramStat(await readText(path.join(sysRoot, 'block', 'zram0', 'mm_stat')) || '');
        const loadavg = (await readText(path.join(procRoot, 'loadavg')) || '').split(' ').slice(0, 3).map(Number);
        const uptime = Number((await readText(path.join(procRoot, 'uptime')) || '0').split(' ')[0]);

        previous = { at, cpu, processes };
        return {
            at: new Date(at).toISOString(),
            cpu: { overall: round(overall), cores },
            ...meminfo,
            zram,
            load: loadavg.map(value => round(value, 2)),
            uptime_seconds: Math.round(uptime),
            processes: tableRows.filter(row => row.pid !== process.pid).slice(0, TOP_PROCESSES)
                .map(({ pid, name, cpu_percent, rss_mb }) => ({ pid, name, cpu_percent, rss_mb })),
            research: researchSummary,
        };
    };
}

module.exports = {
    createSystemLoad,
    cpuPercent,
    parseMeminfo,
    parseProcStat,
    parseProcessStat,
    parseZramStat,
};
