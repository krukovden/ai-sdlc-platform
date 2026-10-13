'use strict';
// The committed plugin and marketplace manifests must be exactly what cli/version.js
// generates from package.json and catalog.json. A skill added to the catalog without
// regenerating them installs through npx but is invisible to Claude's plugin path.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {sync} = require('../cli/version');

const ROOT = path.resolve(__dirname, '..');
const MANIFESTS = ['package.json', '.claude-plugin/plugin.json', '.claude-plugin/marketplace.json', '.github/plugin/marketplace.json'];

test('committed manifests match what cli/version.js generates', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'idp-manifests-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  for (const file of [...MANIFESTS, 'catalog.json']) {
    fs.mkdirSync(path.dirname(path.join(dir, file)), {recursive: true});
    fs.copyFileSync(path.join(ROOT, file), path.join(dir, file));
  }
  sync(undefined, dir);
  for (const file of MANIFESTS) {
    assert.equal(fs.readFileSync(path.join(ROOT, file), 'utf8'), fs.readFileSync(path.join(dir, file), 'utf8'),
      `${file} is stale: run node cli/version.js and commit the result`);
  }
});
