'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync, spawnSync} = require('node:child_process');
const {run} = require('../cli');
const {sync} = require('../cli/version');
const {check} = require('../skills/platform-freshness/check');
const {fixture, json} = require('./fixture');
const {CURRENT, NEXT, AFTER} = require('./fixture');

const receiptOf = ctx => json(path.join(ctx.home, '.idp/platform/receipt.json'));
const dropCatalogSkill = (ctx, name) => {
  const file = path.join(ctx.root, 'catalog.json'), catalog = json(file);
  catalog.skills = catalog.skills.filter(s => s.name !== name);
  fs.writeFileSync(file, JSON.stringify(catalog));
};
const statusOf = async ctx => { ctx.output.length = 0; await run(['status'], ctx); return ctx.output.join('\n'); };
const commandOf = entry => typeof entry === 'string' ? entry : entry && entry.command;
const stateDirOf = ctx => path.join(ctx.home, '.idp/platform');
const hookStateOf = ctx => path.join(ctx.home, '.idp/hooks');

// Everything a finished installation promises: receipt hashes match the payload, every link
// resolves to its recorded target, nothing of an earlier payload or a transaction is left behind.
async function assertConsistent(ctx, version) {
  const state = stateDirOf(ctx);
  const receipt = receiptOf(ctx);
  assert.equal(receipt.version, version);
  assert.equal(fs.existsSync(path.join(state, 'journal.json')), false, 'journal removed');
  assert.equal(fs.existsSync(path.join(state, 'staging')), false, 'staging removed');
  assert.deepEqual(fs.readdirSync(state).filter(n => n !== 'receipt.json' && n !== version && n !== 'freshness.json' && n !== 'bin'), [], 'only the current payload remains');
  for (const [link, destination] of Object.entries(receipt.links)) {
    assert.equal(fs.readlinkSync(link), destination);
    assert.ok(fs.existsSync(destination), `${link} resolves`);
  }
  assert.doesNotMatch(await statusOf(ctx), /^!/m);
}

test('keep-local survives a same-version update that removes the file from the catalog', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  const edited = path.join(receiptOf(ctx).payload, 'skills/planning/SKILL.md');
  fs.appendFileSync(edited, '\nmine\n');
  dropCatalogSkill(ctx, 'planning');
  for (let round = 0; round < 2; round++) {
    await run(['update', '--keep-local'], ctx);
    assert.match(fs.readFileSync(edited, 'utf8'), /mine\n$/, `edited bytes survive update ${round + 1}`);
    assert.ok(receiptOf(ctx).files['skills/planning/SKILL.md'], 'receipt keeps the original hash');
    assert.match(await statusOf(ctx), /skills\/planning\/SKILL.md: missing or edited locally/);
  }
});

test('keep-local carries a removed edited file into a new version payload', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  const edited = path.join(receiptOf(ctx).payload, 'skills/planning/SKILL.md');
  fs.appendFileSync(edited, '\nmine\n');
  dropCatalogSkill(ctx, 'planning'); sync(NEXT, ctx.root);
  await run(['update', '--keep-local'], ctx);
  assert.match(fs.readFileSync(path.join(receiptOf(ctx).payload, 'skills/planning/SKILL.md'), 'utf8'), /mine\n$/);
  assert.equal(fs.existsSync(path.join(ctx.home, `.idp/platform/${CURRENT}`)), false);
});

test('uninstall --target all removes a single-target installation', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  await run(['uninstall', '--target', 'all'], ctx);
  assert.deepEqual(fs.readdirSync(ctx.home), []);
});

test('uninstall of one target out of several is still refused', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'all'], ctx);
  await assert.rejects(run(['uninstall', '--target', 'orca'], ctx), /use --target all/);
  assert.ok(fs.existsSync(path.join(ctx.home, '.idp/platform/receipt.json')));
});

test('status reports every managed link that is missing or replaced', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'copilot'], ctx);
  assert.doesNotMatch(await statusOf(ctx), /^!/m);
  const skill = path.join(ctx.home, '.copilot/skills/design');
  const instructions = path.join(ctx.home, '.copilot/instructions/platform-freshness.instructions.md');
  fs.unlinkSync(skill);
  assert.ok(fs.existsSync(path.join(ctx.home, '.local/bin/idp')));
  fs.unlinkSync(instructions); fs.writeFileSync(instructions, 'mine');
  const status = await statusOf(ctx);
  assert.ok(status.includes(`! ${skill}: link missing`), status);
  assert.ok(status.includes(`! ${instructions}: link replaced`), status);
});

test('status reports an interrupted first install instead of nothing installed', async t => {
  const ctx = fixture(t);
  const state = path.join(ctx.home, '.idp/platform');
  fs.mkdirSync(state, {recursive: true});
  fs.writeFileSync(path.join(state, 'journal.json'), JSON.stringify({command: 'install'}));
  const status = await statusOf(ctx);
  assert.match(status, /^! interrupted install: run the command again to finish it$/m);
  assert.doesNotMatch(status, /Nothing installed/);
});

test('the shipped package carries the hooks directory into the payload', async t => {
  const ctx = fixture(t);
  assert.ok(fs.existsSync(path.join(ctx.root, 'hooks/hooks.json')), 'npm pack includes hooks/hooks.json');
  await run(['install', '--target', 'claude'], ctx);
  const payload = receiptOf(ctx).payload;
  for (const file of ['hooks/hooks.json', 'hooks/platform-freshness.json', 'hooks/cliff-handoff.json', 'hooks/context-watch.json', 'hooks/copilot-hooks.json', 'scripts/hooks/cliff_handoff.py', 'scripts/hooks/context_watch.py', 'scripts/hooks/statusline.py']) assert.ok(fs.existsSync(path.join(payload, file)), file);
});

test('Claude gets a real SessionStart hook and Copilot gets no second one', async t => {
  const ctx = fixture(t);
  const hooks = json(path.join(ctx.root, 'hooks/hooks.json')).hooks;
  // The handoff layers add PostToolUse and StopFailure; SessionStart stays the one freshness hook.
  assert.deepEqual(Object.keys(hooks), ['SessionStart', 'PostToolUse', 'StopFailure']);
  const commands = hooks.SessionStart.flatMap(group => group.hooks);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].type, 'command');
  assert.match(commands[0].command, /^node "\$\{CLAUDE_PLUGIN_ROOT\}\/skills\/platform-freshness\/check\.js"$/);
  assert.equal(json(path.join(ctx.root, '.claude-plugin/plugin.json')).hooks, './hooks/hooks.json');
  for (const copilot of ['.copilot-plugin/hooks.json', 'plugin.json']) assert.equal(fs.existsSync(path.join(ctx.root, copilot)), false, `${copilot} would register a Copilot hook`);
  assert.deepEqual(json(path.join(ctx.root, 'hooks/platform-freshness.json')).platforms, ['claude']);
  // Copilot's handoff hooks never include a SessionStart hook, so it never gets a second one.
  const copilotHooks = json(path.join(ctx.root, 'hooks/copilot-hooks.json'));
  assert.equal(copilotHooks.version, 1);
  assert.deepEqual(Object.keys(copilotHooks.hooks), ['errorOccurred', 'sessionEnd']);
  // The registered command, as a plugin manager would run it: one reminder line on stdout.
  const remote = path.join(ctx.temp, 'remote');
  fs.mkdirSync(remote);
  execFileSync('git', ['init', '-q', remote]);
  execFileSync('git', ['-C', remote, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'release']);
  execFileSync('git', ['-C', remote, 'tag', 'v9.0.0']);
  const file = path.join(ctx.root, 'catalog.json'), catalog = json(file); catalog.source = remote; fs.writeFileSync(file, JSON.stringify(catalog));
  const command = commands[0].command.replace('${CLAUDE_PLUGIN_ROOT}', ctx.root);
  const stdout = execFileSync('sh', ['-c', command], {cwd: ctx.cwd, env: {...process.env, HOME: ctx.home}, encoding: 'utf8'});
  assert.match(stdout, new RegExp(`^AI SDLC Platform ${CURRENT.replace(/\./g, '\\.')} → 9\\.0\\.0 available\\. Update via your host's plugin manager or marketplace\n$`));
});

test('the freshness reminder names --local only for a project-local installation', async t => {
  const ctx = fixture(t);
  const options = {...ctx, now: 1000000000, getLatest: () => '99.0.0'};
  await run(['install', '--local', '--target', 'orca'], ctx); ctx.output.length = 0;
  check(options);
  assert.match(ctx.output[0], /update --local$/);
  await run(['check', '--local'], {...options, getLatest: () => '99.0.0'});
  assert.match(ctx.output.at(-1), /update --local$/);
  fs.rmSync(path.join(ctx.cwd, '.idp'), {recursive: true});
  await run(['install', '--target', 'orca'], ctx); ctx.output.length = 0;
  check({...options, now: options.now + 8 * 86400000});
  assert.doesNotMatch(ctx.output[0], /--local/);
  await run(['check'], {...options, getLatest: () => '99.0.0'});
  assert.doesNotMatch(ctx.output.at(-1), /--local/);
});

test('update into existing version directory protects unmanaged files', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  sync(NEXT, ctx.root);
  const dest = path.join(stateDirOf(ctx), NEXT);
  fs.mkdirSync(dest, {recursive: true});
  fs.writeFileSync(path.join(dest, 'foreign.txt'), 'do not touch');
  await assert.rejects(run(['update', '--yes'], ctx), new RegExp(`Unmanaged payload preserved: ${dest.replace(/\./g, '\\.')}`));
});

test('interrupted recovery preserves a user-created collision for a newly added file', async t => {
  const ctx = fixture(t);
  await run(['install'], ctx);
  const newFile = 'skills/design/recovery-new.txt';
  fs.writeFileSync(path.join(ctx.root, newFile), 'release bytes\n');
  const installed = path.join(receiptOf(ctx).payload, newFile);
  const staged = path.join(stateDirOf(ctx), 'staging', newFile);
  const original = fs.renameSync;
  let fired = false;
  fs.renameSync = function (from, to) {
    if (from === staged && to === installed) { fired = true; throw new Error('injected new-file place'); }
    return original.call(this, from, to);
  };
  try { await assert.rejects(run(['update', '--yes'], ctx), /injected new-file place/); } finally { fs.renameSync = original; }
  assert.ok(fired);
  fs.mkdirSync(path.dirname(installed), {recursive: true});
  fs.writeFileSync(installed, 'user bytes\n');
  await run(['update', '--keep-local'], ctx);
  assert.equal(fs.readFileSync(installed, 'utf8'), 'user bytes\n');
  assert.match(await statusOf(ctx), /skills\/design\/recovery-new.txt: missing or edited locally/);
});

test('interrupted recovery revalidates edited files before replacing or deleting them', async t => {
  await t.test('same-version update keeps a post-interruption edit unless overwrite is explicit', async () => {
    const ctx = fixture(t);
    await run(['install'], ctx);
    const receipt = receiptOf(ctx);
    const installed = path.join(receipt.payload, 'skills/design/SKILL.md');
    fs.appendFileSync(path.join(ctx.root, 'skills/design/SKILL.md'), '\nrelease change\n');
    const staged = path.join(ctx.home, '.idp/platform/staging/skills/design/SKILL.md');
    const original = fs.renameSync;
    let fired = false;
    fs.renameSync = function (from, to) {
      if (from === staged && to === installed) { fired = true; throw new Error('injected place'); }
      return original.call(this, from, to);
    };
    try { await assert.rejects(run(['update', '--yes'], ctx), /injected place/); } finally { fs.renameSync = original; }
    assert.ok(fired);
    fs.appendFileSync(installed, '\nintervening edit\n');
    await assert.rejects(run(['update', '--yes'], ctx), /Local edits preserved/);
    await run(['update', '--keep-local'], ctx);
    assert.match(fs.readFileSync(installed, 'utf8'), /intervening edit\n$/);
    assert.match(await statusOf(ctx), /skills\/design\/SKILL.md: missing or edited locally/);
  });
  await t.test('uninstall keeps a post-interruption edit unless overwrite is explicit', async () => {
    const ctx = fixture(t);
    await run(['install'], ctx);
    const receipt = receiptOf(ctx);
    const installed = path.join(receipt.payload, 'skills/design/SKILL.md');
    const original = fs.rmSync;
    let fired = false;
    fs.rmSync = function (target, options) {
      if (target === installed) { fired = true; throw new Error('injected delete'); }
      return original.call(this, target, options);
    };
    try { await assert.rejects(run(['uninstall', '--yes'], ctx), /injected delete/); } finally { fs.rmSync = original; }
    assert.ok(fired);
    fs.appendFileSync(installed, '\nintervening edit\n');
    await assert.rejects(run(['uninstall', '--yes'], ctx), /Local edits preserved/);
    await run(['uninstall', '--keep-local'], ctx);
    assert.match(fs.readFileSync(installed, 'utf8'), /intervening edit\n$/);
    assert.equal(fs.existsSync(path.join(ctx.home, '.claude')), false);
    assert.equal(fs.existsSync(path.join(ctx.home, '.idp/platform/receipt.json')), false);
  });
});

test('keep-local recovery does not overwrite a post-link edit in the new payload', async t => {
  const ctx = fixture(t);
  await run(['install'], ctx);
  const installed = path.join(receiptOf(ctx).payload, 'skills/design/SKILL.md');
  fs.appendFileSync(installed, '\nkeep me\n');
  sync(NEXT, ctx.root);
  const receiptFile = path.join(stateDirOf(ctx), 'receipt.json');
  const original = fs.renameSync;
  let fired = false;
  fs.renameSync = function (from, to) {
    if (to === receiptFile) { fired = true; throw new Error('injected receipt commit'); }
    return original.call(this, from, to);
  };
  try { await assert.rejects(run(['update', '--keep-local'], ctx), /injected receipt commit/); } finally { fs.renameSync = original; }
  assert.ok(fired);
  const linked = path.join(ctx.home, '.claude/skills/design/SKILL.md');
  fs.appendFileSync(linked, '\nnew payload edit\n');
  await run(['update', '--keep-local'], ctx);
  assert.match(fs.readFileSync(linked, 'utf8'), /new payload edit\n$/);
  assert.match(await statusOf(ctx), /skills\/design\/SKILL.md: missing or edited locally/);
});

test('interactive yes approvals finish the initial settlement they approved', async t => {
  const script = `
const path = require('node:path');
const fs = require('node:fs');
const rl = require('node:readline/promises');
rl.createInterface = () => ({question: async () => 'yes', close() {}});
Object.defineProperty(process.stdin, 'isTTY', {value: true});
const {run} = require(${JSON.stringify(path.join(__dirname, '..', 'cli'))});
const {sync} = require(${JSON.stringify(path.join(__dirname, '..', 'cli', 'version.js'))});
(async () => {
  const ctx = {root: process.env.IDP_ROOT, home: process.env.IDP_HOME, cwd: process.env.IDP_CWD, out() {}};
  await run(['install'], ctx);
  const receipt = JSON.parse(fs.readFileSync(path.join(process.env.IDP_HOME, '.idp/platform/receipt.json'), 'utf8'));
  const edited = path.join(receipt.payload, 'skills/design/SKILL.md');
  fs.appendFileSync(edited, '\\napproved overwrite\\n');
  sync(process.env.IDP_NEXT, process.env.IDP_ROOT);
  await run(['update'], ctx);
  const afterUpdate = fs.readFileSync(path.join(process.env.IDP_HOME, '.idp/platform', process.env.IDP_NEXT, 'skills/design/SKILL.md'), 'utf8');
  if (/approved overwrite/.test(afterUpdate)) throw new Error('interactive update kept the local edit');
  const receipt2 = JSON.parse(fs.readFileSync(path.join(process.env.IDP_HOME, '.idp/platform/receipt.json'), 'utf8'));
  const uninstallEdited = path.join(receipt2.payload, 'skills/planning/SKILL.md');
  fs.appendFileSync(uninstallEdited, '\\napproved delete\\n');
  await run(['uninstall'], ctx);
  if (fs.existsSync(uninstallEdited)) throw new Error('interactive uninstall preserved the approved deletion');
})().catch(error => { console.error(error.stack || String(error)); process.exit(1); });
`;
  const ctx = fixture(t);
  const child = spawnSync(process.execPath, ['-e', script], {
    cwd: ctx.cwd,
    env: {...process.env, IDP_ROOT: ctx.root, IDP_HOME: ctx.home, IDP_CWD: ctx.cwd, IDP_NEXT: NEXT},
    encoding: 'utf8',
  });
  assert.equal(child.status, 0, child.stderr || child.stdout);
});

test('uninstall ignores an unmanaged newer payload directory', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  const foreign = path.join(stateDirOf(ctx), NEXT);
  fs.mkdirSync(foreign, {recursive: true});
  fs.writeFileSync(path.join(foreign, 'foreign.txt'), 'keep me');
  sync(NEXT, ctx.root);
  await run(['uninstall', '--target', 'all'], ctx);
  assert.equal(fs.existsSync(path.join(foreign, 'foreign.txt')), true);
  assert.equal(fs.existsSync(path.join(stateDirOf(ctx), 'receipt.json')), false);
});

test('manual hook registrations survive update and are cleaned up on uninstall', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'all'], ctx);
  const receipt = receiptOf(ctx);
  const hooksState = hookStateOf(ctx);
  const statusScript = path.join(receipt.payload, 'scripts/hooks/statusline.py');
  const cliffScript = path.join(receipt.payload, 'scripts/hooks/cliff_handoff.py');
  const stableStatus = path.join(stateDirOf(ctx), 'bin/statusline.py');
  const stableCliff = path.join(stateDirOf(ctx), 'bin/cliff_handoff.py');
  const settings = path.join(ctx.home, '.claude/settings.json');
  fs.mkdirSync(path.dirname(settings), {recursive: true});
  fs.writeFileSync(path.join(ctx.home, 'wrapped-statusline.sh'), '#!/bin/sh\nprintf wrapped\n');
  fs.chmodSync(path.join(ctx.home, 'wrapped-statusline.sh'), 0o755);
  fs.writeFileSync(settings, JSON.stringify({statusLine: path.join(ctx.home, 'wrapped-statusline.sh')}));
  execFileSync('python3', [statusScript, '--install', '--settings', settings], {
    env: {...process.env, HOME: ctx.home, IDP_STATE_DIR: hooksState},
    encoding: 'utf8',
  });
  const staleStatus = `python3 "${statusScript}"`;
  const installedSettings = json(settings);
  installedSettings.statusLine.command = staleStatus;
  fs.writeFileSync(settings, JSON.stringify(installedSettings, null, 2) + '\n');
  const hooksDir = path.join(ctx.home, '.copilot/hooks');
  fs.mkdirSync(hooksDir, {recursive: true});
  const copilotFile = path.join(hooksDir, 'idp-cliff-handoff.json');
  const copilot = JSON.parse(execFileSync('python3', [cliffScript, '--print-config', 'copilot'], {
    env: {...process.env, HOME: ctx.home},
    encoding: 'utf8',
  }));
  copilot.hooks.errorOccurred[0].bash = `python3 "${cliffScript}" --host copilot --event errorOccurred`;
  copilot.hooks.sessionEnd[0].bash = `python3 "${cliffScript}" --host copilot --event sessionEnd`;
  fs.writeFileSync(copilotFile, JSON.stringify(copilot, null, 2) + '\n');
  sync(NEXT, ctx.root);
  await run(['update', '--yes'], ctx);
  const migratedSettings = json(settings);
  assert.equal(commandOf(migratedSettings.statusLine), `python3 "${stableStatus}"`);
  const rendered = execFileSync('sh', ['-c', commandOf(migratedSettings.statusLine)], {
    env: {...process.env, HOME: ctx.home, IDP_STATE_DIR: hooksState},
    input: JSON.stringify({session_id: 's-1', context_window: {used_percentage: 10}}),
    encoding: 'utf8',
  });
  assert.equal(rendered, 'wrapped');
  const migratedCopilot = json(copilotFile);
  assert.equal(migratedCopilot.hooks.errorOccurred[0].bash,
               `python3 "${stableCliff}" --host copilot --event errorOccurred`);
  assert.equal(migratedCopilot.hooks.sessionEnd[0].bash,
               `python3 "${stableCliff}" --host copilot --event sessionEnd`);
  await run(['uninstall', '--target', 'all'], ctx);
  assert.equal(json(settings).statusLine, path.join(ctx.home, 'wrapped-statusline.sh'));
  assert.equal(fs.existsSync(copilotFile), false);
});

// --- recoverable update transaction -------------------------------------------------------

const FAULTS = ['writeFileSync', 'mkdirSync', 'renameSync', 'symlinkSync', 'unlinkSync', 'rmSync', 'chmodSync', 'openSync', 'fsyncSync'];
// Wrap one fs function: count its calls, and optionally throw on the n-th. Returns the probe.
function probe(method, failAt) {
  const original = fs[method], probe = {calls: 0, fired: false};
  fs[method] = function (...args) {
    probe.calls++;
    if (probe.calls === failAt) { probe.fired = true; throw new Error(`injected ${method} #${failAt}`); }
    return original.apply(this, args);
  };
  probe.restore = () => { fs[method] = original; };
  return probe;
}
async function installedUpdateFixture(t) {
  const ctx = fixture(t);
  await run(['install'], ctx);
  const edited = path.join(receiptOf(ctx).payload, 'skills/design/SKILL.md');
  fs.appendFileSync(edited, '\nmine\n');
  // NEXT drops a skill, so the update has to add, replace, retarget and delete.
  dropCatalogSkill(ctx, 'planning'); sync(NEXT, ctx.root);
  return ctx;
}
const UPDATE = ['update', '--overwrite'];

test('an update interrupted at any side effect is finished by the next run', async t => {
  const counting = await installedUpdateFixture(t);
  const probes = Object.fromEntries(FAULTS.map(method => [method, probe(method)]));
  try { await run(UPDATE, counting); } finally { Object.values(probes).forEach(p => p.restore()); }
  await assertConsistent(counting, NEXT);
  for (const method of FAULTS) {
    const total = probes[method].calls;
    assert.ok(total > 0, `${method} is exercised by an update`);
    for (const index of new Set([1, 2, Math.ceil(total / 2), total - 1, total].filter(i => i >= 1 && i <= total))) {
      await t.test(`${method} #${index} of ${total}`, async st => {
        const ctx = await installedUpdateFixture(st);
        const fault = probe(method, index);
        let failure;
        try { await run(UPDATE, ctx); } catch (error) { failure = error; } finally { fault.restore(); }
        assert.ok(fault.fired, 'the fault fired');
        assert.match(String(failure), /injected/, 'and surfaced');
        await run(UPDATE, ctx);
        await assertConsistent(ctx, NEXT);
        assert.equal(fs.existsSync(path.join(ctx.home, '.claude/skills/planning')), false);
        assert.ok(fs.existsSync(path.join(ctx.home, '.claude/skills/design/SKILL.md')));
      });
    }
  }
});

test('a crash after the links switched but before the receipt committed leaves a reportable, recoverable state', async t => {
  const ctx = await installedUpdateFixture(t);
  const receiptFile = path.join(ctx.home, '.idp/platform/receipt.json');
  const original = fs.renameSync;
  let fired = false;
  fs.renameSync = function (from, to) {
    if (to === receiptFile) { fired = true; throw new Error('injected receipt commit'); }
    return original.call(this, from, to);
  };
  try { await assert.rejects(run(UPDATE, ctx), /injected receipt commit/); } finally { fs.renameSync = original; }
  assert.ok(fired);
  assert.match(await statusOf(ctx), /interrupted update/);
  await run(['uninstall', '--overwrite'], ctx);
  assert.deepEqual(fs.readdirSync(ctx.home), []);
});
