'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {latest, compare} = require('../../cli');
function check({home = os.homedir(), cwd = process.cwd(), now = Date.now(), getLatest = latest, out = console.log} = {}) {
  let base = cwd;
  while (!fs.existsSync(path.join(base, '.idp/platform/receipt.json')) && path.dirname(base) !== base) base = path.dirname(base);
  let state = path.join(base, '.idp/platform');
  if (!fs.existsSync(path.join(state, 'receipt.json'))) state = path.join(home, '.idp/platform');
  // `update` without --local reads the home receipt, so a project-local installation has to say so.
  const scope = path.resolve(state) === path.resolve(home, '.idp/platform') ? '' : ' --local';
  let receipt, isPlugin = false;
  if (fs.existsSync(path.join(state, 'receipt.json'))) receipt = JSON.parse(fs.readFileSync(path.join(state, 'receipt.json')));
  else {
    // Plugin managers retain the repository layout but do not create CLI receipts.
    isPlugin = true;
    const root = path.resolve(__dirname, '../..');
    receipt = {version: JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).version,
      source: JSON.parse(fs.readFileSync(path.join(root, 'catalog.json'))).source};
    state = path.join(home, '.cache/idp-platform');
    fs.mkdirSync(state, {recursive: true});
  }
  const cache = path.join(state, 'freshness.json');
  try {
    const previous = JSON.parse(fs.readFileSync(cache));
    if (now - previous.checked < 7 * 24 * 60 * 60 * 1000) return;
  } catch {}
  // Record attempts too: an unreachable remote must not be retried every session.
  fs.writeFileSync(cache, JSON.stringify({checked: now}));
  try {
    const version = getLatest(receipt.source);
    if (version && compare(version, receipt.version) > 0) {
      const hint = isPlugin ? "Update via your host's plugin manager or marketplace" : `Update: npx git+${receipt.source} update${scope}`;
      out(`AI SDLC Platform ${receipt.version} → ${version} available. ${hint}`);
    }
  } catch {}
}
if (require.main === module) { try { check(); } catch {} }
module.exports = {check};
