#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('node:path').join(__dirname, '..', '.env'), quiet: true });
const path = require('node:path');
const { runPytrLoginRecovery } = require('../lib/pytr-login-recovery');

const memoryDir = process.env.MONITOR_MEMORY_DIR || path.join(__dirname, '..', 'memory');
const statePath = process.env.PYTR_LOGIN_RECOVERY_STATE || path.join(memoryDir, 'pytr-login-recovery.json');
function log(message) { process.stdout.write(`[${new Date().toISOString()}] ${message}\n`); }

runPytrLoginRecovery({ statePath, log }).then(result => {
    if (!result.started) log('No PyTR login recovery is pending.');
    process.exitCode = 0;
}).catch(error => {
    log(`PyTR login recovery failed safely: ${String(error?.message || error).replace(/[\r\n]+/g, ' ').slice(0, 160)}`);
    process.exitCode = 1;
});
