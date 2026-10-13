'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync, spawnSync} = require('node:child_process');
const {run, ROOT} = require('../cli');
const {sync} = require('../cli/version');
const {check} = require('../skills/platform-freshness/check');
const {fixture, json} = require('./fixture');
const {CURRENT, NEXT, AFTER} = require('./fixture');
for (const target of ['claude', 'copilot', 'orca']) {
  test(`${target}: install, status, simulated update, check and clean uninstall`, async t => {
    const ctx = fixture(t);
    const cli = (...args) => execFileSync(process.execPath, [path.join(ctx.root, 'bin/idp-platform.js'), ...args], {
      cwd: ctx.cwd, env: {...process.env, HOME: ctx.home}, encoding: 'utf8'});
    cli('install', '--target', target);
    const folder = {claude: '.claude', copilot: '.copilot', orca: '.agents'}[target];
    const installed = path.join(ctx.home, folder, 'skills/design/design.py');
    assert.match(execFileSync('python3', [installed, '--help'], {cwd: ctx.cwd, env: {...process.env, HOME: ctx.home}, encoding: 'utf8'}), /design.py/);
    const explicit = path.join(ctx.home, folder, 'skills/idp');
    assert.ok(fs.existsSync(path.join(explicit, 'SKILL.md')));
    if (target === 'claude') assert.ok(fs.existsSync(path.join(ctx.home, '.claude/commands/idp.md')));
    assert.match(execFileSync('python3', [path.join(ctx.home, '.local/bin/idp'), '--help'], {cwd: ctx.cwd, env: {...process.env, HOME: ctx.home}, encoding: 'utf8'}), /board.py/);
    assert.match(cli('status'), new RegExp(CURRENT.replace(/\./g, '\\.')));
    const receipt = json(path.join(ctx.home, '.idp/platform/receipt.json'));
    const edited = path.join(receipt.payload, 'skills/design/SKILL.md');
    fs.appendFileSync(edited, '\nLocal change\n');
    sync(NEXT, ctx.root);
    const refused = spawnSync(process.execPath, [path.join(ctx.root, 'bin/idp-platform.js'), 'update', '--yes'], {env: {...process.env, HOME: ctx.home}, encoding: 'utf8'});
    assert.ok(refused.status);
    assert.match(refused.stderr, /Local edits preserved/);
    assert.match(fs.readFileSync(edited, 'utf8'), /Local change/);
    assert.match(cli('update', '--keep-local'), /~ package.json/);
    assert.match(cli('status'), /edited locally/);
    cli('update', '--overwrite', '--yes');
    assert.doesNotMatch(cli('status'), /edited locally/);
    const remote = path.join(ctx.temp, 'remote');
    fs.mkdirSync(remote);
    execFileSync('git', ['init', '-q', remote]);
    execFileSync('git', ['-C', remote, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'release']);
    execFileSync('git', ['-C', remote, 'tag', `v${AFTER}`]);
    const catalogFile = path.join(ctx.root, 'catalog.json');
    const catalog = json(catalogFile); catalog.source = remote; fs.writeFileSync(catalogFile, JSON.stringify(catalog));
    assert.ok(cli('check').includes(`${NEXT} → ${AFTER} available`));
    cli('uninstall');
    assert.deepEqual(fs.readdirSync(ctx.home), []);
  });
}
test('local all-target install protects existing files and leaves unrelated state intact', async t => {
  const ctx = fixture(t);
  const foreign = path.join(ctx.cwd, '.claude/skills/design');
  fs.mkdirSync(foreign, {recursive: true}); fs.writeFileSync(path.join(foreign, 'SKILL.md'), 'mine');
  await assert.rejects(run(['install', '--local'], ctx), /Unmanaged path/);
  assert.equal(fs.existsSync(path.join(ctx.cwd, '.idp')), false);
  fs.rmSync(path.join(ctx.cwd, '.claude'), {recursive: true});
  await run(['install', '--local'], ctx);
  assert.ok(fs.existsSync(path.join(ctx.cwd, '.github/skills/design')));
  assert.ok(fs.existsSync(path.join(ctx.cwd, '.claude/commands/idp.md')));
  fs.writeFileSync(path.join(ctx.cwd, '.idp/profile.json'), '{}');
  await run(['uninstall', '--local'], ctx);
  assert.deepEqual(fs.readdirSync(path.join(ctx.cwd, '.idp')), ['profile.json']);
});
test('freshness is one line, weekly, and never updates', async t => {
  const ctx = fixture(t); await run(['install'], ctx); ctx.output.length = 0;
  let calls = 0;
  const options = {...ctx, now: 1000000000, getLatest: () => { calls++; return '99.0.0'; }};
  check(options); check(options);
  assert.equal(calls, 1); assert.equal(ctx.output.length, 1);
  assert.match(ctx.output[0], /available/);
  assert.equal(json(path.join(ctx.home, '.idp/platform/receipt.json')).version, CURRENT);
  check({...options, now: options.now + 8 * 24 * 60 * 60 * 1000}); assert.equal(calls, 2);
});
test('version sync keeps manifests and catalog skill entries aligned', t => {
  const ctx = fixture(t); sync('1.2.3', ctx.root);
  assert.equal(json(path.join(ctx.root, 'package.json')).version, '1.2.3');
  for (const file of ['.claude-plugin/marketplace.json', '.github/plugin/marketplace.json']) {
    const manifest = json(path.join(ctx.root, file));
    assert.equal(manifest.metadata.version, '1.2.3'); assert.equal(manifest.plugins[0].version, '1.2.3');
    assert.equal(manifest.plugins[0].source, './');
  }
  const plugin = json(path.join(ctx.root, '.claude-plugin/plugin.json'));
  assert.equal(plugin.version, '1.2.3');
  for (const skill of plugin.skills) assert.ok(fs.existsSync(path.join(ctx.root, skill, 'SKILL.md')));
  for (const command of plugin.commands) assert.ok(fs.existsSync(path.join(ctx.root, command)));
  const current = json(path.join(ROOT, 'package.json')).version;
  assert.equal(json(path.join(ROOT, '.claude-plugin/plugin.json')).version, current);
  assert.ok(json(path.join(ROOT, '.claude-plugin/plugin.json')).commands.includes('./commands/idp.md'));
  for (const file of ['.claude-plugin/marketplace.json', '.github/plugin/marketplace.json']) {
    const manifest = json(path.join(ROOT, file));
    assert.equal(manifest.metadata.version, current); assert.equal(manifest.plugins[0].version, current);
  }
});
test('catalog additions and removals need no installer edits', async t => {
  const ctx = fixture(t);
  await run(['install', '--target', 'orca'], ctx);
  const file = path.join(ctx.root, 'catalog.json'), catalog = json(file);
  const skill = path.join(ctx.root, 'skills/new-user-skill'); fs.mkdirSync(skill);
  fs.writeFileSync(path.join(skill, 'SKILL.md'), '---\nname: new-user-skill\ndescription: Test\n---\n');
  catalog.skills.push({name: 'new-user-skill', path: 'skills/new-user-skill'});
  catalog.skills = catalog.skills.filter(s => s.name !== 'planning');
  fs.writeFileSync(file, JSON.stringify(catalog)); sync(NEXT, ctx.root);
  ctx.output.length = 0; await run(['update'], ctx);
  assert.match(ctx.output.join('\n'), /\+ skills\/new-user-skill\/SKILL.md/);
  assert.match(ctx.output.join('\n'), /- skills\/planning\/SKILL.md/);
  assert.ok(fs.existsSync(path.join(ctx.home, '.agents/skills/new-user-skill/SKILL.md')));
  assert.equal(fs.existsSync(path.join(ctx.home, '.agents/skills/planning')), false);
  await run(['uninstall'], ctx); assert.deepEqual(fs.readdirSync(ctx.home), []);
});
test('plugin freshness works without a CLI receipt and throttles failures', t => {
  const ctx = fixture(t); let calls = 0;
  const options = {...ctx, now: 1000000000, getLatest: () => { calls++; throw new Error('offline'); }};
  check(options); check(options); assert.equal(calls, 1); assert.deepEqual(ctx.output, []);
});
test('uninstall refuses modified files and preserves explicit keep-local choices', async t => {
  const ctx = fixture(t); await run(['install'], ctx);
  const receipt = json(path.join(ctx.home, '.idp/platform/receipt.json'));
  const edited = path.join(receipt.payload, 'skills/design/SKILL.md'); fs.appendFileSync(edited, 'mine');
  await assert.rejects(run(['uninstall', '--yes'], ctx), /Local edits preserved/);
  await run(['uninstall', '--keep-local'], ctx);
  assert.match(fs.readFileSync(edited, 'utf8'), /mine$/);
  assert.equal(fs.existsSync(path.join(ctx.home, '.claude')), false);
  assert.equal(fs.existsSync(path.join(ctx.home, '.idp/platform/receipt.json')), false);
});
