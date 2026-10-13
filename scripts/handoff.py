#!/usr/bin/env python3
"""The mechanical handoff: a session that ended early leaves its branch continuable.

A session can end early for three reasons — context pressure, running out of
tokens or usage, or a crash — and three layers cover them (IDE-147, *Handoff
when a session ends early*). Layer 1 is the model writing `handoff.md` itself
while it still can. Layers 2 and 3 have no model left to ask, so they run this
script: layer 2 from a hook at the cliff (IDE-147 build step 7, T9), layer 3
from the runner after it saw its worker exit non-zero on a limit. Layer 3 is the
only guarantee, because it depends on no hook firing.

What it does, in order:

1. Stages everything **except** ignored files, files over 5 MB and anything
   matching the secret patterns (`.env*`, keys, tokens), and lists what it left
   out. Commits `wip(handoff): <reason>` with the work item mention and pushes.
2. Writes a mechanical `handoff.md` — the branch, the card, the last commit, the
   files left uncommitted, the reason, the model's last message if there was
   one — commits and pushes it. A handoff.md the model wrote itself (layer 1) is
   kept, with the mechanical facts under it.
3. With `--pause`, swaps `idp:owner:<run>` for `idp:paused:<run>` on the card in
   one fenced write, so another machine can take it at once. That needs a board
   credential, so only the runner does it; a hook inside a model session holds
   none and stops after step 2.

It acts only on a `pbi/*` branch and refuses `main`, `master` and the feature
branch: a `wip(handoff)` commit may not build, which is safe only because a PBI
branch merges through finalize, never directly.

    handoff.py --reason TEXT --card ID [--worktree DIR] [--mention TEXT]
               [--last-message TEXT] [--feature-branch NAME]
               [--pause --run RUN-ID]

Exit codes are the scripts' own, from scripts/exit_codes.py:
    0 ok · 2 the remote could not be reached · 3 refused, nothing changed
"""

import argparse
import fnmatch
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _sibling(name, alias):
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")
tags = _sibling("tags", "idp_tags")

HANDOFF_FILE = "handoff.md"
MECHANICAL = "<!-- idp:mechanical-handoff -->"
SCRIPT_HEADER = "# Handoff\n\nWritten by a script"
SCRATCH = ".idp/"
DECISIONS_GLOB = "decisions.jsonl*"
DECISIONS_HEADING = "## Decisions not yet on the board"
PAUSE_MARK = "idp:pause-request"
PAUSE_REQUEST = re.compile(r"idp:pause-request\s+(\{[^{}]*\})")
DECISIONS_BLOCK = re.compile(re.escape(DECISIONS_HEADING) + r".*?```jsonl\n(.*?)```", re.S)
MAX_BYTES = 5 * 1024 * 1024
PROTECTED = ("main", "master")
BRANCH_PREFIX = "pbi/"

# Matched case-folded against the file's name and against its whole path. Over-
# matching costs a file left uncommitted and listed; under-matching publishes a
# credential to every machine that fetches the branch.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".netrc", ".pypirc",
    "*token*", "*secret*", "*credential*", "*.kdbx",
)


class HandoffError(Exception):
    """A refusal or a failure, with its exit code and the fix."""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


def git(worktree, *args, check=True):
    answer = _run(worktree, *args)
    if check and answer.returncode != EXIT.OK:
        raise HandoffError(EXIT.UNAVAILABLE, f"git {' '.join(args)} failed in {worktree}: "
                                             f"{answer.stderr.strip()}",
                           "check the worktree and its remote, then run the handoff again")
    return answer.stdout


def _run(worktree, *args):
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL)


def current_branch(worktree):
    return git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()


def is_secret(path):
    name = Path(path).name.casefold()
    whole = str(path).casefold()
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(whole, p)
               for p in SECRET_PATTERNS)


def changed_paths(worktree):
    """Every changed or untracked path git would stage; ignored files never appear."""
    raw = git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries, parts, index = [], raw.split("\0"), 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        source = None
        if "R" in status or "C" in status:
            source = parts[index]           # the source path of a rename follows
            index += 1
        entries.append((status, path, source))
    return entries


def select(worktree, entries):
    """(paths to stage, [(path, why it was left out)])."""
    staged, left_out = [], []
    for status, path, source in entries:
        if path == HANDOFF_FILE or path.startswith(SCRATCH):
            # `.idp/` is the runner's scratch space; what in it must survive — the
            # session's unposted decisions — travels inside handoff.md instead.
            continue
        full = Path(worktree) / path
        if is_secret(path):
            left_out.append((path, "matches a secret pattern"))
        elif full.is_file() and full.stat().st_size > MAX_BYTES:
            left_out.append((path, "larger than 5 MB"))
        else:
            # A rename's source is not a second change: without it `commit --only`
            # records an add and leaves the deletion staged, so the old path
            # survives on the branch.
            if source and not is_secret(source) and not str(source).startswith(SCRATCH):
                staged.append(source)
            staged.append(path)
    return staged, left_out


def _nothing_to_commit(answer):
    text = f"{answer.stdout}\n{answer.stderr}".casefold()
    return ("nothing to commit" in text or "nothing added to commit" in text
            or "no changes added to commit" in text)


def commit_paths(worktree, paths, message):
    """Commit the worktree state of `paths` and nothing else already staged.

    A plain `git commit` publishes the whole index. A worker that staged
    `.env`, a file over 5 MB or `.idp/` scratch before the limit would have
    that file in the wip commit and again in the handoff.md commit, then
    pushed, while `select` reported it left out. `--only` commits the named
    paths and leaves every other index entry staged and unpushed. The worktree
    is not touched.
    """
    if not paths:
        return False
    for path in paths:
        added = _run(worktree, "add", "-A", "--", path)
        if added.returncode != EXIT.OK:
            err = added.stderr.strip()
            # A rename source already absent from the index and the worktree is
            # not a pathspec `git add` can name; the commit still needs it.
            if "did not match any file" in err:
                continue
            raise HandoffError(EXIT.UNAVAILABLE, f"git add {path} failed in {worktree}: {err}",
                               "check the worktree and its remote, then run the handoff again")
    answer = _run(worktree, "commit", "--no-verify", "-o", "-m", message, "--", *paths)
    if answer.returncode == EXIT.OK:
        return True
    if answer.returncode == 1 and _nothing_to_commit(answer):
        return False
    detail = (answer.stderr or answer.stdout).strip()
    raise HandoffError(EXIT.UNAVAILABLE, f"git commit failed in {worktree}: {detail}",
                       "check the worktree and its remote, then run the handoff again")


def pending_decisions(worktree):
    """The decisions `run.py decide` recorded and nobody has posted yet, as raw lines."""
    lines = []
    scratch = Path(worktree) / SCRATCH
    for path in sorted(scratch.glob(DECISIONS_GLOB)) if scratch.exists() else []:
        lines += [line for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    return lines


def pause_marker(request):
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return [f"<!-- {PAUSE_MARK} {encoded} -->", f"`{PAUSE_MARK} {encoded}`"]


def read_pause_request(text):
    """The pause request a handoff.md carries, or None. Either rendering counts."""
    for match in PAUSE_REQUEST.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("card"):
            return data
    return None


def read_decisions(text):
    """The decisions a handoff.md carries for the next run to post."""
    match = DECISIONS_BLOCK.search(text or "")
    found = []
    for line in (match.group(1).splitlines() if match else []):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def render(branch, card, reason, last_commit, left_out, last_message, decisions=(),
           pause_request=None):
    lines = ["# Handoff", "",
             "Written by a script, not by the model: the session ended before it could "
             "write its own. Continue from the branch and the card.", "",
             f"- Branch: `{branch}`",
             f"- Card: {card}",
             f"- Reason: {reason}",
             f"- Last commit: `{last_commit['sha']}` {last_commit['subject']}", "",
             "## Left uncommitted", ""]
    if left_out:
        lines += [f"- `{path}` — {why}" for path, why in left_out]
    else:
        lines.append("Nothing: every change was committed in the `wip(handoff)` commit.")
    lines += ["", "## The model's last message", "",
              (last_message or "").strip() or "None was recorded.", ""]
    if decisions:
        lines += [DECISIONS_HEADING, "",
                  "Calls the session made alone in agent mode (`run.py decide`). The next "
                  "run posts each one on the Feature; posting twice is once.", "",
                  "```jsonl", *decisions, "```", ""]
    if pause_request:
        lines += ["## Pause request", "",
                  f"Run `{pause_request.get('run') or 'the owner'}` could not pause "
                  f"{pause_request['card']} itself. The next runner or `run.py reap` pauses "
                  "it at once, while this handoff is still the branch's last commit.", "",
                  *pause_marker(pause_request), ""]
    return "\n".join(lines)


def compose(existing, text):
    """The handoff.md to write: ours, under what the model wrote if it wrote one.

    Layer 1 is the model writing handoff.md while it still can, and that is the
    richer record; a script that overwrote it would destroy the best evidence
    of where the session stopped. So a model's file is kept and the mechanical
    facts go under a marker — replaced there on a second run, never appended twice.
    """
    if not existing or existing.startswith(SCRIPT_HEADER):
        return text
    model = existing.split(MECHANICAL, 1)[0].rstrip()
    ours = text.replace("# Handoff", "## Mechanical handoff", 1)
    return f"{model}\n\n{MECHANICAL}\n\n{ours}"


def handoff(worktree, reason, card, mention=None, last_message=None, remote="origin",
            feature_branch=None, pause_request=None):
    """Steps 1 and 2: the work and a mechanical handoff.md, committed and pushed.

    `pause_request` — `{run, card, reason}` — is step 3 for a caller with no
    board credential: written into handoff.md, it asks whoever reads the branch
    next to make the owner-to-paused swap, without waiting out the lease.
    """
    worktree = Path(worktree)
    branch = current_branch(worktree)
    protected = set(PROTECTED) | ({feature_branch} if feature_branch else set())
    if branch in protected or not branch.startswith(BRANCH_PREFIX):
        raise HandoffError(EXIT.INVALID,
                           f"the handoff acts only on a {BRANCH_PREFIX}* branch, and this "
                           f"worktree is on '{branch}'. Nothing was committed.",
                           "run it in the PBI's worktree; main and the feature branch merge "
                           "only through the runner's finalize")
    mention = mention or f"#{card}"

    staged, left_out = select(worktree, changed_paths(worktree))
    wip = commit_paths(worktree, staged, f"wip(handoff): {reason} {mention}")

    sha = git(worktree, "rev-parse", "HEAD").strip()
    subject = git(worktree, "log", "-1", "--format=%s").strip()
    text = render(branch, mention, reason, {"sha": sha, "subject": subject}, left_out,
                  last_message, decisions=pending_decisions(worktree),
                  pause_request=pause_request)
    target = worktree / HANDOFF_FILE
    text = compose(target.read_text(encoding="utf-8") if target.exists() else None, text)
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
    commit_paths(worktree, [HANDOFF_FILE],
                 f"docs(handoff): where this session stopped {mention}")
    git(worktree, "push", remote, f"HEAD:refs/heads/{branch}")
    return {"branch": branch, "commit": git(worktree, "rev-parse", "HEAD").strip(),
            "wip": wip, "left_out": [{"path": p, "why": w} for p, w in left_out]}


def pause(board, card, run):
    """Step 3: `idp:owner:<run>` becomes `idp:paused:<run>` in one fenced write."""
    return tags.pause(board, card, run)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="handoff.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--card", required=True)
    parser.add_argument("--worktree", default=".")
    parser.add_argument("--mention", help="the work item mention; default #<card>")
    parser.add_argument("--last-message", default=None)
    parser.add_argument("--feature-branch", default=None,
                        help="refused like main, so a feature branch is never handed off")
    parser.add_argument("--pause", action="store_true",
                        help="also swap the owner tag for idp:paused (needs the board)")
    parser.add_argument("--run", help="the run whose owner tag becomes paused")
    args = parser.parse_args(argv)
    try:
        result = handoff(args.worktree, args.reason, args.card, mention=args.mention,
                         last_message=args.last_message, feature_branch=args.feature_branch)
    except HandoffError as exc:
        fail(exc.code, exc.message, fix=exc.fix)
    if args.pause:
        if not args.run:
            fail(EXIT.INVALID, "--pause needs --run: the paused tag names the run",
                 fix="pass --run <run-id>, the run whose owner tag is on the card")
        board_cli = _sibling("board", "idp_board")
        _, _, board = board_cli.open_board()
        result["pause"] = pause(board, args.card, args.run)["outcome"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(EXIT.OK)


if __name__ == "__main__":
    main()
