#!/bin/sh
# End-to-end check of the platform in a clean container, the way a user meets it:
# the test suites, then a real `npx git+…` install for every target, a release of a
# newer version, `check`, `update` with a locally edited file, and `uninstall`.
#
# The GitHub URL users type is redirected to a local bare repository with git's
# insteadOf, so nothing here touches the network except the optional host CLIs.
set -eu

SRC=/platform
URL=https://github.com/krukovden/ai-sdlc-platform
REMOTE=/srv/ai-sdlc-platform.git
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

git config --global user.email e2e@example.invalid
git config --global user.name e2e
git config --global init.defaultBranch main
git config --global advice.detachedHead false

step "python suite"
(cd "$SRC" && python3 -m unittest discover -s tests 2>&1 | tail -3)

step "node suite"
(cd "$SRC" && node --test --test-reporter=spec test/ 2>&1 | grep -E '(tests|pass|fail) [0-9]+$')

step "publish the checkout as a local release remote"
rm -rf /tmp/release && cp -r "$SRC" /tmp/release && cd /tmp/release
rm -rf .git && git init -q && git add -A && git commit -qm "release"
V1=$(node -p "require('./package.json').version")
git tag "v$V1"
git clone -q --bare /tmp/release "$REMOTE"
git config --global url."file://$REMOTE".insteadOf "$URL"
echo "released v$V1"

export HOME=/home/e2e && rm -rf "$HOME" && mkdir -p "$HOME"
export PATH="$HOME/.local/bin:$PATH"
CLI="npx --yes git+$URL"

step "install --target all"
$CLI install --target all --yes
for dir in .claude/skills .copilot/skills .agents/skills; do
  for skill in feature-discovery design planning establish-project; do
    [ -e "$HOME/$dir/$skill/SKILL.md" ] || fail "$dir/$skill/SKILL.md missing after install"
  done
done
echo "skills present for claude, copilot and orca"

step "status"
STATUS=$($CLI status); echo "$STATUS"
echo "$STATUS" | grep -q '^!' && fail "status reports a problem on a fresh install"

step "installed commands run"
idp --help >/dev/null || fail "idp launcher does not run"
python3 "$HOME/.claude/skills/design/design.py" --help >/dev/null || fail "installed design.py does not run"
PAYLOAD=$(node -p "require('$HOME/.idp/platform/receipt.json').payload")
python3 "$PAYLOAD/scripts/run.py" --help >/dev/null || fail "installed run.py does not run"
echo "idp, design.py and run.py run from the installed payload"

step "check reports no newer release"
$CLI check | tee /tmp/check1 && grep -q "no newer release" /tmp/check1 || fail "check found a release that does not exist"

step "release a newer version"
cd /tmp/release
V2=$(node -e "const [a,b,c]=require('./package.json').version.split('.').map(Number);console.log([a,b,c+1].join('.'))")
node cli/version.js "$V2"
printf '\n<!-- e2e: changed in v%s -->\n' "$V2" >> skills/design/SKILL.md
git add -A && git commit -qm "release v$V2" && git tag "v$V2" && git push -q "$REMOTE" main --tags
echo "released v$V2"

step "check sees it"
$CLI check | tee /tmp/check2 && grep -q "$V1 → $V2" /tmp/check2 || fail "check did not see v$V2"

step "a locally edited file survives update --keep-local"
EDITED="$HOME/.claude/skills/planning/SKILL.md"
printf '\nmy local note\n' >> "$EDITED"
$CLI update --keep-local --yes
grep -q "my local note" "$EDITED" || fail "update --keep-local lost a local edit"
$CLI status | grep -q "$V2" || fail "update did not move to v$V2"
grep -q "changed in v$V2" "$HOME/.claude/skills/design/SKILL.md" || fail "the changed skill was not updated"

step "update --overwrite takes the release's version of the edited file"
$CLI update --overwrite --yes
grep -q "my local note" "$EDITED" && fail "update --overwrite kept the local edit"
STATUS=$($CLI status); echo "$STATUS"
echo "$STATUS" | grep -q '^!' && fail "status reports a problem after update"

step "uninstall --target all leaves nothing behind"
$CLI uninstall --target all --yes
for path in .claude/skills/design .copilot/skills/design .agents/skills/design .local/bin/idp .idp/platform/receipt.json; do
  [ -e "$HOME/$path" ] && fail "$path left behind by uninstall"
done
echo "clean"

step "host CLIs read the manifests (best effort: needs network to install them)"
if npm install -g --silent @anthropic-ai/claude-code >/dev/null 2>&1; then
  claude plugin validate "$SRC" && echo "claude: plugin manifest valid"
  claude plugin validate "$SRC/.claude-plugin/marketplace.json" && echo "claude: marketplace manifest valid"
else
  echo "claude CLI not installable here; skipped"
fi
if npm install -g --silent @github/copilot >/dev/null 2>&1; then
  copilot --version
  node -e "const m=require('$SRC/.github/plugin/marketplace.json');if(!m.plugins?.length)process.exit(1);console.log('copilot: marketplace manifest lists', m.plugins.map(p=>p.name+'@'+p.version).join(', '))"
else
  echo "copilot CLI not installable here; skipped"
fi

printf '\nE2E PASSED (v%s → v%s)\n' "$V1" "$V2"
