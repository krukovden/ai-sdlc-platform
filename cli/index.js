'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {createInterface} = require('node:readline/promises');
const ROOT = path.resolve(__dirname, '..');
const TARGETS = {claude: '.claude', copilot: '.copilot', orca: '.agents'};
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
const exists = file => { try { fs.lstatSync(file); return true; } catch { return false; } };
const fileHash = file => exists(file) ? hash(fs.readFileSync(file)) : null;
const commandOf = entry => typeof entry === 'string' ? entry.trim() : entry && typeof entry.command === 'string' ? entry.command.trim() : null;
function write(file, data) { fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, data); }
// Write through a temporary name and flush it, so a reader sees the old file or the whole new one.
function durable(file, text) {
  fs.mkdirSync(path.dirname(file), {recursive: true});
  const fd = fs.openSync(file + '.tmp', 'w');
  try { fs.writeSync(fd, text); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(file + '.tmp', file);
}
function prune(directory, stop) {
  while (directory !== stop && directory.startsWith(stop + path.sep)) {
    try { fs.rmdirSync(directory); } catch { break; }
    directory = path.dirname(directory);
  }
}
function removeBytecode(source, base) {
  if (!source.endsWith('.py')) return;
  const cache = path.join(path.dirname(source), '__pycache__');
  if (!exists(cache) || fs.lstatSync(cache).isSymbolicLink()) return;
  const stem = path.basename(source, '.py');
  for (const name of fs.readdirSync(cache)) {
    if (name.startsWith(stem + '.') && name.endsWith('.pyc')) {
      const file = path.join(cache, name);
      if (fs.lstatSync(file).isFile()) fs.unlinkSync(file);
    }
  }
  prune(cache, base);
}
function files(root, relative, result = {}) {
  if (path.isAbsolute(relative) || relative.split(/[\\/]/).includes('..')) throw new Error('Unsafe catalog path');
  const absolute = path.join(root, relative);
  const stat = fs.lstatSync(absolute);
  if (stat.isSymbolicLink()) throw new Error(`Payload symlink is unsupported: ${relative}`);
  if (stat.isDirectory()) {
    for (const name of fs.readdirSync(absolute).sort()) {
      if (name !== '__pycache__' && name !== '.DS_Store' && !name.endsWith('.pyc')) files(root, path.join(relative, name), result);
    }
  } else result[relative] = {data: fs.readFileSync(absolute), mode: stat.mode};
  return result;
}
function recoveryDecision(current, expected, options) {
  if (current === expected) return 'clean';
  if (options.overwrite) return 'overwrite';
  if (options['keep-local']) return 'keep-local';
  throw new Error('Local edits preserved; use --keep-local or --overwrite');
}
function normalizeJournal(journal, state) {
  const receiptFile = path.join(state, 'receipt.json');
  const baseline = exists(receiptFile) ? read(receiptFile).files || {} : {};
  const legacy = Object.keys(baseline).length ? baseline : journal.expected || {};
  const samePayload = journal.payload === journal.discardRoot;
  const staging = path.join(state, 'staging');
  const place = Array.isArray(journal.place) ? journal.place : [];
  const normalized = {...journal};
  normalized.sourceExpected = normalized.sourceExpected || legacy;
  normalized.targetExpected = normalized.targetExpected || Object.fromEntries(place.map(file => [file, samePayload ? (legacy[file] ?? null) : null]));
  normalized.placedHash = normalized.placedHash || Object.fromEntries(place.map(file => {
    const source = path.join(staging, file);
    if (exists(source)) return [file, fileHash(source)];
    return [file, null];
  }));
  return normalized;
}
function hooksDir(base) { return path.join(base, '.idp', 'hooks'); }
function stableHooks(state) {
  return {statusline: path.join(state, 'bin', 'statusline.py'),
    cliff: path.join(state, 'bin', 'cliff_handoff.py')};
}
function statuslineFiles(base) {
  const directory = path.join(base, '.claude');
  if (!exists(directory)) return [];
  return fs.readdirSync(directory).filter(name => /^settings.*\.json$/u.test(name))
    .map(name => path.join(directory, name));
}
function syncStatusLine(base, state, command) {
  const next = path.join(hooksDir(base), 'statusline-next.json');
  if (!exists(next)) return;
  let wrapped;
  try {
    const data = read(next);
    wrapped = data && data.statusLine;
  } catch { return; }
  const expected = `python3 "${stableHooks(state).statusline}"`;
  for (const file of statuslineFiles(base)) {
    let settings;
    try { settings = read(file); } catch { continue; }
    if (!(settings && typeof settings === 'object')) continue;
    const current = settings.statusLine;
    const currentCommand = commandOf(current);
    if (!(currentCommand?.includes('scripts/hooks/statusline.py')
      || currentCommand === `python3 "${stableHooks(state).statusline}"`)) continue;
    if (command === 'uninstall') {
      if (wrapped) settings.statusLine = wrapped;
      else delete settings.statusLine;
    } else {
      settings.statusLine = typeof current === 'object' && current ? {...current, command: expected} : {type: 'command', command: expected};
    }
    fs.writeFileSync(file, JSON.stringify(settings, null, 2) + '\n');
  }
  if (command === 'uninstall') fs.rmSync(next, {force: true});
}
function matchesCopilot(entry, event) {
  return entry && entry.type === 'command' && entry.timeoutSec === 120
    && typeof entry.bash === 'string'
    && new RegExp(`^python3 "(.+/scripts/hooks/cliff_handoff\\.py|.+/bin/cliff_handoff\\.py)" --host copilot --event ${event}$`).test(entry.bash);
}
function rewriteCopilotEntries(data, state, command) {
  if (!(data && data.version === 1 && data.hooks && typeof data.hooks === 'object')) return false;
  let changed = false;
  const cliff = stableHooks(state).cliff;
  for (const [event, name] of [['errorOccurred', 'errorOccurred'], ['sessionEnd', 'sessionEnd']]) {
    const entries = Array.isArray(data.hooks[event]) ? data.hooks[event] : null;
    if (!entries) continue;
    const kept = [];
    for (const entry of entries) {
      if (!matchesCopilot(entry, name)) {
        kept.push(entry);
        continue;
      }
      changed = true;
      if (command === 'uninstall') continue;
      kept.push({type: 'command', timeoutSec: 120, bash: `python3 "${cliff}" --host copilot --event ${name}`});
    }
    if (kept.length) data.hooks[event] = kept;
    else delete data.hooks[event];
  }
  return changed;
}
function syncCopilotHooks(base, state, command) {
  for (const directory of [path.join(base, '.copilot', 'hooks'), path.join(base, '.github', 'hooks')]) {
    if (!exists(directory)) continue;
    for (const name of fs.readdirSync(directory).filter(file => file.endsWith('.json'))) {
      const file = path.join(directory, name);
      let data;
      try { data = read(file); } catch { continue; }
      if (!rewriteCopilotEntries(data, state, command)) continue;
      if (command === 'uninstall' && !Object.keys(data.hooks).length) {
        fs.rmSync(file, {force: true});
        prune(path.dirname(file), directory);
        continue;
      }
      fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n');
    }
  }
}
function syncHostRegistrations(base, state, command) {
  syncStatusLine(base, state, command);
  syncCopilotHooks(base, state, command);
}
// An install, update or uninstall is a transaction: the new payload is staged, a journal naming every
// remaining step is made durable, and only then do live paths change. Each step is idempotent, so a
// run that finds a journal simply finishes it. The previous payload is deleted after the receipt commits.
function settle(state, base, journal, options = {}) {
  journal = normalizeJournal(journal, state);
  const staging = path.join(state, 'staging'), receiptFile = path.join(state, 'receipt.json');
  for (const file of journal.place) {
    const source = path.join(staging, file);
    if (!exists(source)) continue;
    const target = path.join(journal.payload, file);
    const decision = recoveryDecision(fileHash(target), journal.targetExpected[file] ?? null, options);
    if (decision === 'keep-local') {
      continue;
    }
    fs.mkdirSync(path.dirname(target), {recursive: true});
    fs.renameSync(source, target);
  }
  for (const file of journal.place) {
    const source = path.join(staging, file);
    if (exists(source)) continue;
    const target = path.join(journal.payload, file);
    const decision = recoveryDecision(fileHash(target), journal.receipt.files[file] ?? null, options);
    if (decision === 'keep-local') continue;
  }
  syncHostRegistrations(base, state, journal.command);
  for (const [link, oldTarget] of Object.entries(journal.fromLinks)) {
    if (link in journal.toLinks) continue;
    if (exists(link) && fs.lstatSync(link).isSymbolicLink() && fs.readlinkSync(link) === oldTarget) fs.unlinkSync(link);
    prune(path.dirname(link), base);
  }
  for (const [link, destination] of Object.entries(journal.toLinks)) {
    if (exists(link)) {
      const current = fs.lstatSync(link).isSymbolicLink() && fs.readlinkSync(link);
      if (current === destination) continue;
      if (current !== journal.fromLinks[link]) throw new Error(`Unmanaged path preserved: ${link}`);
      fs.unlinkSync(link);
    }
    fs.mkdirSync(path.dirname(link), {recursive: true}); fs.symlinkSync(destination, link);
  }
  if (journal.receipt) durable(receiptFile, JSON.stringify(journal.receipt, null, 2) + '\n');
  else { fs.rmSync(receiptFile, {force: true}); fs.rmSync(path.join(state, 'freshness.json'), {force: true}); }
  for (const file of journal.discard) {
    const absolute = path.join(journal.discardRoot, file);
    if (!exists(absolute)) continue;
    const decision = recoveryDecision(fileHash(absolute), journal.sourceExpected[file] ?? null, options);
    if (decision === 'keep-local') {
      if (!journal.receipt) continue;
      if (journal.payload === journal.discardRoot) continue;
      const target = path.join(journal.payload, file);
      const sourceHash = fileHash(absolute), targetHash = fileHash(target);
      if (targetHash === null || targetHash === journal.placedHash[file]) {
        write(target, fs.readFileSync(absolute));
        fs.chmodSync(target, fs.lstatSync(absolute).mode);
        removeBytecode(absolute, base);
        fs.rmSync(absolute, {force: true}); prune(path.dirname(absolute), base);
      }
      continue;
    }
    removeBytecode(absolute, base);
    fs.rmSync(absolute, {force: true}); prune(path.dirname(absolute), base);
  }
  fs.rmSync(staging, {recursive: true, force: true});
  fs.rmSync(path.join(state, 'journal.json'), {force: true});
  if (!journal.receipt) prune(state, base);
}
function compare(a, b) {
  const aa = a.split('.').map(Number), bb = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) if (aa[i] !== bb[i]) return aa[i] - bb[i];
  return 0;
}
function latest(source) {
  const output = execFileSync('git', ['-c', 'credential.interactive=false', 'ls-remote', '--tags', source], {
    encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...process.env, GIT_TERMINAL_PROMPT: '0', GCM_INTERACTIVE: 'never'}});
  return [...output.matchAll(/refs\/tags\/v(\d+\.\d+\.\d+)$/gm)].map(m => m[1]).sort(compare).pop();
}
function parse(argv) {
  const options = {command: argv[0] || '--help'};
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--target') options.target = argv[++i];
    else if (['--local', '--global', '--keep-local', '--overwrite', '--yes'].includes(arg)) options[arg.slice(2)] = true;
    else throw new Error(`Unknown option ${arg}`);
  }
  if (options.local && options.global || options['keep-local'] && options.overwrite) throw new Error('Contradictory options');
  if (options.target && options.target !== 'all' && !TARGETS[options.target]) throw new Error('Unknown target');
  return options;
}
async function run(argv, context = {}) {
  const {root = ROOT, home = os.homedir(), cwd = process.cwd(), out = console.log, getLatest = latest} = context;
  const args = parse(argv);
  if (['--help', '-h', 'help'].includes(args.command)) {
    out('idp-platform install|update|check|status|uninstall|list [--target claude|copilot|orca|all] [--global|--local] [--keep-local|--overwrite] [--yes]'); return;
  }
  const catalog = read(path.join(root, 'catalog.json')), pkg = read(path.join(root, 'package.json'));
  const base = args.local ? cwd : home, state = path.join(base, '.idp/platform');
  const receiptFile = path.join(state, 'receipt.json'), journalFile = path.join(state, 'journal.json');
  if (['install', 'update', 'uninstall'].includes(args.command) && exists(journalFile)) {
    const pending = normalizeJournal(read(journalFile), state);
    settle(state, base, pending, args);
    out(`Finished an interrupted ${pending.command}`);
  }
  const receipt = exists(receiptFile) ? read(receiptFile) : {targets: [], files: {}, links: {}};
  if (args.command === 'uninstall' && !receipt.version) { out('Uninstalled; any explicitly kept local edits remain'); return; }
  const targets = args.target ? args.target === 'all' ? Object.keys(TARGETS) : [args.target] : receipt.targets.length ? receipt.targets : Object.keys(TARGETS);
  if (args.command === 'list') { out(`${catalog.name} ${pkg.version}`); catalog.skills.forEach(s => out(`skill ${s.name}`)); return; }
  if (args.command === 'check') {
    const version = receipt.version || pkg.version, newest = getLatest(catalog.source);
    const scope = args.local ? ' --local' : '';
    out(newest && compare(newest, version) > 0 ? `${version} → ${newest} available. Update: npx git+${catalog.source} update${scope}` : `${version}: no newer release available`); return;
  }
  if (args.command === 'status') {
    const interrupted = exists(journalFile) && `! interrupted ${read(journalFile).command}: run the command again to finish it`;
    if (!receipt.version) { out(interrupted || 'Nothing installed'); return; }
    out(`${catalog.name} ${receipt.version}: ${receipt.targets.join(', ')}`);
    if (interrupted) out(interrupted);
    for (const [link, expected] of Object.entries(receipt.links)) {
      if (!exists(link)) out(`! ${link}: link missing`);
      else if (!fs.lstatSync(link).isSymbolicLink() || fs.readlinkSync(link) !== expected) out(`! ${link}: link replaced`);
      else if (!exists(expected)) out(`! ${link}: link target missing`);
    }
    for (const [file, expected] of Object.entries(receipt.files)) {
      const absolute = path.join(receipt.payload, file);
      if (!exists(absolute) || hash(fs.readFileSync(absolute)) !== expected) out(`! ${file}: missing or edited locally`);
    }
    return;
  }
  if (!['install', 'update', 'uninstall'].includes(args.command)) throw new Error('Unknown command');
  if (args.command === 'update' && !receipt.version) throw new Error('Nothing installed; run install first');
  const desired = {};
  if (args.command !== 'uninstall') {
    if (receipt.version && compare(pkg.version, receipt.version) < 0) throw new Error('Refusing downgrade; fetch the latest package');
    for (const entry of [...catalog.payload, ...catalog.skills.map(s => s.path),
      ...(catalog.commands || []).map(c => c.path), ...(catalog.agents || []).map(a => a.path)]) files(root, entry, desired);
  }
  const kept = new Set();
  for (const file of new Set([...Object.keys(receipt.files), ...Object.keys(desired)])) {
    const oldHash = receipt.files[file], nextHash = desired[file] && hash(desired[file].data);
    out(`${!oldHash ? '+' : !nextHash ? '-' : oldHash === nextHash ? '=' : '~'} ${file}`);
    const absolute = receipt.payload && path.join(receipt.payload, file);
    if (absolute && exists(absolute) && (!oldHash || hash(fs.readFileSync(absolute)) !== oldHash)) {
      out(`! ${file} edited locally`);
      if (args.overwrite) continue;
      if (args['keep-local']) { kept.add(file); continue; }
      if (!args.yes && process.stdin.isTTY) {
        const prompt = createInterface({input: process.stdin, output: process.stdout});
        let answer;
        try { answer = await prompt.question(`Overwrite/delete ${file}? [y/N] `); } finally { prompt.close(); }
        if (/^y(es)?$/i.test(answer)) continue;
      }
      throw new Error('Local edits preserved; use --keep-local or --overwrite');
    }
  }
  if (args.command === 'uninstall' && !receipt.targets.every(target => targets.includes(target))) throw new Error('Uninstall removes the shared payload; use --target all');
  const activeTargets = [...new Set([...receipt.targets, ...targets])];
  const payload = path.join(state, pkg.version);
  if (args.command !== 'uninstall' && receipt.payload !== payload && exists(payload)) throw new Error(`Unmanaged payload preserved: ${payload}`);
  const links = {};
  if (args.command !== 'uninstall') {
    for (const target of activeTargets) {
      const prefix = target === 'copilot' && args.local ? '.github' : TARGETS[target];
      if (target === 'copilot') links[path.join(base, prefix, 'instructions/platform-freshness.instructions.md')] = path.join(payload, 'skills/platform-freshness/platform-freshness.instructions.md');
      if (target === 'claude') {
        for (const command of catalog.commands || []) {
          links[path.join(base, prefix, 'commands', path.basename(command.path))] = path.join(payload, command.path);
        }
      }
      for (const skill of catalog.skills) links[path.join(base, prefix, 'skills', skill.name)] = path.join(payload, skill.path);
      // Copilot reads an agent only as `<name>.agent.md`; Claude as `<name>.md`. The Orca
      // target is a shared skills directory and has no agents.
      if (target !== 'orca') for (const agent of catalog.agents || []) {
        const file = target === 'copilot' ? `${agent.name}.agent.md` : path.basename(agent.path);
        links[path.join(base, prefix, 'agents', file)] = path.join(payload, agent.path);
      }
    }
    links[path.join(base, '.local/bin/idp')] = path.join(payload, 'scripts/board.py');
    links[stableHooks(state).statusline] = path.join(payload, 'scripts/hooks/statusline.py');
    links[stableHooks(state).cliff] = path.join(payload, 'scripts/hooks/cliff_handoff.py');
  }
  for (const link of new Set([...Object.keys(receipt.links), ...Object.keys(links)])) {
    if (exists(link) && (!fs.lstatSync(link).isSymbolicLink() || fs.readlinkSync(link) !== receipt.links[link])) throw new Error(`Unmanaged path preserved: ${link}`);
  }
  const samePayload = receipt.payload === payload, uninstalling = args.command === 'uninstall';
  const staging = path.join(state, 'staging');
  fs.rmSync(staging, {recursive: true, force: true});
  const place = [];
  const stage = (file, data, mode) => { write(path.join(staging, file), data); fs.chmodSync(path.join(staging, file), mode); place.push(file); };
  if (!uninstalling) {
    for (const [file, item] of Object.entries(desired)) {
      if (kept.has(file)) continue;
      stage(file, item.data, file === 'scripts/board.py' ? item.mode | 0o111 : item.mode);
    }
    // A kept file stays where it is in the same payload; a new payload gets a copy of it.
    if (!samePayload) for (const file of kept) {
      const old = path.join(receipt.payload, file);
      stage(file, fs.readFileSync(old), fs.lstatSync(old).mode);
    }
  }
  const hashes = Object.fromEntries(Object.entries(desired).map(([file, item]) => [file, kept.has(file) ? receipt.files[file] : hash(item.data)]));
  for (const file of kept) hashes[file] = receipt.files[file];
  const sourceExpected = {};
  for (const file of Object.keys(receipt.files)) sourceExpected[file] = fileHash(path.join(receipt.payload, file));
  const targetExpected = {};
  if (!uninstalling) for (const file of place) targetExpected[file] = fileHash(path.join(payload, file));
  const placedHash = Object.fromEntries(place.map(file => [file, fileHash(path.join(staging, file))]));
  const discard = receipt.payload ? Object.keys(receipt.files).filter(file => {
    if (kept.has(file) && (uninstalling || samePayload)) return false;
    return !(samePayload && !uninstalling && desired[file]);
  }) : [];
  const journal = {command: args.command, payload, place, sourceExpected, targetExpected, placedHash, fromLinks: receipt.links, toLinks: links, discard, discardRoot: receipt.payload,
    receipt: uninstalling ? null : {version: pkg.version, source: catalog.source, payload, targets: activeTargets, files: hashes, links}};
  durable(journalFile, JSON.stringify(journal, null, 2) + '\n');
  settle(state, base, journal, args);
  if (uninstalling) { out('Uninstalled; any explicitly kept local edits remain'); return; }
  out(`Installed ${pkg.version}: ${activeTargets.join(', ')}. Add ${path.join(base, '.local/bin')} to PATH.`);
}
module.exports = {run, latest, compare, ROOT};
