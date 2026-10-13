#!/usr/bin/env python3
"""Experiment 7 — work item mentions (IDE-147, "Before implementation" item 7).

Guided. On a scratch Azure Repos repository the operator names, this creates a
branch with two commits — one mentioning `#<id>`, one saying `Fixes #<id>` —
pushes them, and polls the scratch work item for what the board did about
each: did the mention link the commit, and did `Fixes` move the state? If a
link or a transition is absent, the verdict names the repository settings to
check, because that is the answer the design needs ("`reconcile` never depends
on those settings: it parses the mentions itself").

The pre-registration is `docs/experiments/e7-mentions.md`; the verdict is
appended to its Results section.

    git clone <scratch repo> /path/to/scratch-repo
    python3 scripts/experiments/e7_mentions.py \\
        --org <organisation> --project <project> --i-understand-this-writes \\
        --repo-path /path/to/scratch-repo [--push]

Without `--push` each push is shown and waits for you to press Enter after you
have run it yourself. With `--push` the push uses the credential the adapter
uses, handed to git through its environment and never printed.
"""

import argparse
import base64
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment_common as common  # noqa: E402

EXIT = common.EXIT
DOC = common.DOCS_DIR / "e7-mentions.md"
DEFAULT_POLL_SECONDS = 180
DEFAULT_INTERVAL = 10

MENTION, FIXES = "mention", "fixes"
LINKED, ABSENT = "linked", "absent"
TRANSITIONED, UNCHANGED = "transitioned", "unchanged"

SETTINGS_HINT = (
    "Check, in this order: (1) Project Settings → Boards → the process has a work item type "
    "with the state you expect `Fixes` to reach; (2) Project Settings → Repositories → "
    "<repo> → Settings → *Commit mention linking* is On; (3) the same page, *Work item "
    "transitions* is On (this is what makes `Fixes #id` move the state); (4) the transition only "
    "fires for a commit that reaches the repository's default branch or a completed pull "
    "request — merge the scratch branch there, then run the experiment again; (5) the token "
    "that pushed has access to the work item's project, otherwise the mention resolves to "
    "nothing.")


class GitError(RuntimeError):
    pass


def git(repo, *args, env=None):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def prepare(repo, work_item, label=None):
    """Create the probe branch and its two commits. Returns branch, base and both shas."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git working tree")
    if git(repo, "status", "--porcelain"):
        raise GitError(f"{repo} has uncommitted changes; use a clean scratch clone")
    base = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    label = label or common.stamp().lower()
    branch = f"{common.EXPERIMENT_MARK}/e7-{label}"
    git(repo, "checkout", "-b", branch)
    folder = repo / ".idp-experiments"
    folder.mkdir(exist_ok=True)
    shas = {}
    for kind, subject in ((MENTION, f"e7 probe: mention #{work_item}"),
                          (FIXES, f"e7 probe: Fixes #{work_item}")):
        (folder / f"e7-{label}-{kind}.txt").write_text(
            f"{subject}\nscratch file for the {common.EXPERIMENT_MARK} work item mention probe\n",
            encoding="utf-8")
        git(repo, "add", ".idp-experiments")
        git(repo, "commit", "-m", subject)
        shas[kind] = git(repo, "rev-parse", "HEAD")
    return {"branch": branch, "base": base, "shas": shas}


def push(repo, remote, ref, token):
    """Push one ref. The credential reaches git through the environment, never argv."""
    env = dict(os.environ)
    if token:
        pair = base64.b64encode(f":{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {pair}"})
    git(repo, "push", remote, ref, env=env)


def commit_linked(links, sha):
    """Does any artifact link on the card point at this commit?"""
    wanted = sha.lower()
    return any(wanted in (a.get("url") or "").lower() for a in links.get("artifacts") or [])


def watch(scratch, sha, initial_state, timeout, interval, sleep=time.sleep, clock=time.monotonic):
    """Poll the card until `sha` is linked and the state moved, or the time is up."""
    deadline = clock() + timeout
    seen = {"linked": False, "state": initial_state, "polls": 0}
    while True:
        links = scratch.links()
        seen["polls"] += 1
        seen["linked"] = seen["linked"] or commit_linked(links, sha)
        seen["state"] = links.get("state")
        if seen["linked"] and seen["state"] != initial_state:
            break
        if clock() >= deadline:
            break
        sleep(interval)
    return seen


def analyse(initial_state, mention_seen, fixes_seen):
    """The facts the design needs, from what the two polls saw."""
    transitioned = fixes_seen["state"] != initial_state or mention_seen["state"] != initial_state
    return {
        "mention_links_commit": LINKED if mention_seen["linked"] else ABSENT,
        "fixes_links_commit": LINKED if fixes_seen["linked"] else ABSENT,
        "fixes_transitions": TRANSITIONED if transitioned else UNCHANGED,
        "initial_state": initial_state,
        "final_state": fixes_seen["state"],
        "mention_alone_moved_state": mention_seen["state"] != initial_state,
    }


def render(facts, branch, shas, scratch_id):
    lines = [
        f"- scratch card: {scratch_id} (correlation id prefix `{common.EXPERIMENT_MARK}-e7-`), closed at the end",
        f"- probe branch `{branch}`: `#id` commit {shas[MENTION][:10]}, `Fixes #id` commit {shas[FIXES][:10]}",
        f"- `#id` in a commit links the commit to the work item: **{facts['mention_links_commit']}**",
        f"- `Fixes #id` links the commit: **{facts['fixes_links_commit']}**",
        f"- `Fixes #id` transitions the item: **{facts['fixes_transitions']}** "
        f"({facts['initial_state']} → {facts['final_state']})",
    ]
    if facts["mention_alone_moved_state"]:
        lines.append("- NOTE: the plain `#id` mention already moved the state; "
                     "`Fixes` is not needed for a transition on this repository.")
    absent = ABSENT in (facts["mention_links_commit"], facts["fixes_links_commit"]) \
        or facts["fixes_transitions"] == UNCHANGED
    if absent:
        lines.append(f"- SETTINGS TO CHECK: {SETTINGS_HINT}")
    lines.append("- `reconcile` parses the mentions itself and does not depend on any of this; "
                 "this result only tells the team what the board does on its own.")
    verdict = "RECORDED" if not absent else "RECORDED (something absent — see settings)"
    return common.result_block("E7 work item mentions", verdict, lines)


def confirm(message, automatic):
    print(message)
    if automatic:
        return
    if not sys.stdin.isatty():
        raise GitError("this step needs you to run the command above and press Enter, but "
                       "there is no terminal to read it from; re-run with --push")
    input("Press Enter when it has been pushed… ")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common.add_target_arguments(parser)
    parser.add_argument("--repo-path", help="a clean clone of the scratch Azure Repos repository")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--push", action="store_true",
                        help="push with the adapter's credential instead of asking you to")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--no-write-results", action="store_true",
                        help="print the verdict without appending it to the pre-registration")
    args = parser.parse_args(argv)

    common.require_explicit(args)
    if not args.repo_path:
        common.fail(EXIT.INVALID, "--repo-path is required: this experiment commits to a "
                                  "repository you name and never picks one.",
                    fix="clone a scratch Azure Repos repository and pass its path as --repo-path")

    board = common.connect(args)
    scratch = common.AdoScratch(board, "e7", close_state=args.close_state)
    item = scratch.create()
    print(f"scratch work item {item} created")
    repo = Path(args.repo_path)
    probe = None
    try:
        initial = scratch.links()["state"]
        probe = prepare(repo, item)
        token = board.token
        for kind in (MENTION, FIXES):
            ref = f"{probe['shas'][kind]}:refs/heads/{probe['branch']}"
            if args.push:
                push(repo, args.remote, ref, token)
            else:
                confirm(f"Push the {kind} commit:\n  git -C {repo} push {args.remote} {ref}", False)
            if kind == MENTION:
                mention_seen = watch(scratch, probe["shas"][MENTION], initial,
                                     args.poll_seconds, args.interval)
            else:
                fixes_seen = watch(scratch, probe["shas"][FIXES], initial,
                                   args.poll_seconds, args.interval)
        facts = analyse(initial, mention_seen, fixes_seen)
    except GitError as exc:
        common.fail(EXIT.INVALID, str(exc),
                    fix="fix the repository state named above and re-run; the scratch work item "
                        "was closed")
    finally:
        if probe:
            try:
                git(repo, "checkout", probe["base"])
            except GitError:
                pass
        scratch.close()
        print(f"scratch work item {item} moved to {args.close_state}")

    block = render(facts, probe["branch"], probe["shas"], item)
    print(block)
    print(f"cleanup, when you are done: git -C {repo} branch -D {probe['branch']} && "
          f"git -C {repo} push {args.remote} --delete {probe['branch']}")
    if not args.no_write_results:
        print(f"results appended to {common.append_results(DOC, block)}")
    return EXIT.OK


if __name__ == "__main__":
    sys.exit(main())
