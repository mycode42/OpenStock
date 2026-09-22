#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const version = require(path.join(__dirname, '..', 'package.json')).version;
const root = path.join(__dirname, '..', 'www');
const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const worker = fs.readFileSync(path.join(root, 'sw.js'), 'utf8');
const required = [
  `styles.css?v=${version}`,
  `app.js?v=${version}`,
  `openstock-v${version}`
];

for (const marker of required) {
  if (!index.includes(marker) && !worker.includes(marker)) {
    throw new Error(`PWA version marker is missing: ${marker}`);
  }
}
for (const marker of [`styles.css?v=${version}`, `app.js?v=${version}`]) {
  if (!index.includes(marker) || !worker.includes(marker)) {
    throw new Error(`PWA entry asset is inconsistent: ${marker}`);
  }
}
if (!worker.includes(`openstock-v${version}`)) throw new Error('Service-worker cache name does not match package version');

process.stdout.write(`PWA asset version ${version} is consistent.\n`);
