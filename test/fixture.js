'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {ROOT} = require('../cli');
const json = file => JSON.parse(fs.readFileSync(file));
let packed;
// The source tree every fixture installs from is what `npm pack` would publish, not the working tree:
// a file missing from package.json "files" must be missing here too.
function packedRoot() {
  if (packed) return packed;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'idp-pack-'));
  process.on('exit', () => fs.rmSync(dir, {recursive: true, force: true}));
  const tarball = execFileSync('npm', ['pack', '--silent', '--ignore-scripts', '--pack-destination', dir], {cwd: ROOT, encoding: 'utf8'}).trim().split('\n').pop();
  execFileSync('tar', ['-xzf', path.join(dir, tarball), '-C', dir]);
  return packed = path.join(dir, 'package');
}
function fixture(t) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'idp-install-'));
  t.after(() => fs.rmSync(temp, {recursive: true, force: true}));
  const root = path.join(temp, 'source'), home = path.join(temp, 'home'), cwd = path.join(temp, 'project');
  fs.mkdirSync(home); fs.mkdirSync(cwd);
  fs.cpSync(packedRoot(), root, {recursive: true});
  const output = [];
  return {temp, root, home, cwd, output, out: line => output.push(line)};
}
// Versions relative to the package, so a release never breaks the suite:
// CURRENT is what is installed, NEXT and AFTER are the simulated releases.
const CURRENT = require('../package.json').version;
const bump = (v, n) => { const [a, b] = v.split('.').map(Number); return `${a}.${b + n}.0`; };
const NEXT = bump(CURRENT, 1), AFTER = bump(CURRENT, 2);
module.exports = {CURRENT, NEXT, AFTER, fixture, json, packedRoot};
