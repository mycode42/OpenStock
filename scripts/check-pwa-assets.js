#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const version = require(path.join(__dirname, '..', 'package.json')).version;
const root = path.join(__dirname, '..', 'www');
const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
for (const marker of [`styles.css?v=${version}`, `app.js?v=${version}`]) {
  if (!index.includes(marker)) throw new Error(`Entry asset version is missing from index.html: ${marker}`);
}

process.stdout.write(`PWA asset version ${version} is consistent.\n`);
