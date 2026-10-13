'use strict';
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
function sync(version, directory = root) {
  const read = file => JSON.parse(fs.readFileSync(path.join(directory, file)));
  const write = (file, value) => fs.writeFileSync(path.join(directory, file), JSON.stringify(value, null, 2) + '\n');
  const pkg = read('package.json');
  if (version) pkg.version = version;
  if (!/^\d+\.\d+\.\d+$/.test(pkg.version)) throw new Error('Expected a release semver X.Y.Z');
  write('package.json', pkg);
  const catalog = read('catalog.json');
  const plugin = {name: catalog.name, description: pkg.description, version: pkg.version,
    author: {name: 'krukovden'}, repository: catalog.source,
    skills: catalog.skills.map(s => './' + s.path),
    commands: (catalog.commands || []).map(c => './' + c.path),
    hooks: './hooks/hooks.json'};
  write('.claude-plugin/plugin.json', plugin);
  for (const file of ['.claude-plugin/marketplace.json', '.github/plugin/marketplace.json']) {
    write(file, {name: catalog.name, owner: {name: 'krukovden'}, metadata: {description: pkg.description, version: pkg.version},
      plugins: [{name: catalog.name, source: './', description: pkg.description, version: pkg.version}]});
  }
}
if (require.main === module) sync(process.argv[2]);
module.exports = {sync};
