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
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment_common as common  # noqa: E402

EXIT = common.EXIT
DOC = common.DOCS_DIR / "e7-mentions.md"
DEFAULT_POLL_SECONDS = 180
DEFAULT_INTERVAL = 10

MENTION, FIXES = "mention", "fixes"
LINKED, ABSENT = "linked", "absent"
TRANSITIONED, PRIOR, UNCHANGED = "transitioned", "prior", "unchanged"
AUTOMATION_MARKERS = (
    "microsoft.visualstudio.services.tfs",
    "microsoft visualstudio services tfs",
    "azure devops",
    "commit-link",
)
ADO_HOST = (urllib.parse.urlsplit(
    common.adapter.organization_url({"workspace": "example"})).hostname or "").casefold()

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


def sanitize_url(url):
    parts = urllib.parse.urlsplit(str(url or ""))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def azure_remote(url):
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").casefold()
    path = [piece for piece in parts.path.split("/") if piece]
    if parts.scheme != "https":
        raise GitError(f"push URL '{sanitize_url(url)}' is not HTTPS")
    if host == ADO_HOST:
        if len(path) < 4 or path[2] != "_git":
            raise GitError(f"push URL '{sanitize_url(url)}' is not an Azure DevOps repository URL")
        org, project = path[0], path[1]
    elif host.endswith(".visualstudio.com"):
        if len(path) < 3 or path[1] != "_git":
            raise GitError(f"push URL '{sanitize_url(url)}' is not an Azure DevOps repository URL")
        org, project = host[:-len(".visualstudio.com")], path[0]
    else:
        raise GitError(f"push URL '{sanitize_url(url)}' is not an Azure DevOps host")
    return {"url": urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            "sanitized": sanitize_url(url), "host": host, "org": org.casefold(),
            "project": project.casefold()}


def normalize_org(org):
    parsed = urllib.parse.urlsplit(common.adapter.organization_url({"workspace": org}))
    host = (parsed.hostname or "").casefold()
    if host == ADO_HOST:
        return parsed.path.strip("/").split("/", 1)[0].casefold()
    if host.endswith(".visualstudio.com"):
        return host[:-len(".visualstudio.com")]
    return str(org or "").strip().strip("/").casefold()


def resolve_push_urls(repo, remote):
    urls = [line.strip() for line in git(repo, "remote", "get-url", "--push", "--all", remote)
            .splitlines() if line.strip()]
    if not urls:
        raise GitError(f"remote '{remote}' has no push URL")
    return urls


def validated_push_urls(repo, remote, org, project):
    expected_org = normalize_org(org)
    expected_project = str(project or "").strip().casefold()
    validated = []
    for url in resolve_push_urls(repo, remote):
        parsed = azure_remote(url)
        if parsed["org"] != expected_org or parsed["project"] != expected_project:
            raise GitError(f"remote '{remote}' resolves to {parsed['sanitized']}, not to the "
                           f"Azure DevOps project named by --org/--project")
        validated.append(parsed["url"])
    return validated


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


def push(repo, remote, ref, token, urls):
    """Push one ref. The credential reaches git through the environment, never argv."""
    env = dict(os.environ)
    if token:
        pair = base64.b64encode(f":{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = str(len(urls))
        for index, url in enumerate(urls):
            env[f"GIT_CONFIG_KEY_{index}"] = f"http.{url}.extraheader"
            env[f"GIT_CONFIG_VALUE_{index}"] = f"Authorization: Basic {pair}"
    git(repo, "push", remote, ref, env=env)


def commit_linked(links, sha):
    """Does any artifact link on the card point at this commit?"""
    wanted = sha.lower()
    return any(wanted in (a.get("url") or "").lower() for a in links.get("artifacts") or [])


def update_revision(update):
    value = update.get("rev", update.get("id"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def state_change(update):
    field = (update.get("fields") or {}).get("System.State") or {}
    if not isinstance(field, dict):
        return None
    old = field.get("oldValue")
    new = field.get("newValue")
    if old == new or new is None:
        return None
    return {"old": old, "new": new}


def update_author(update):
    who = update.get("revisedBy") or update.get("authorizedAs") or {}
    if isinstance(who, dict):
        return " ".join(str(who.get(key) or "") for key in ("displayName", "uniqueName", "id"))
    return str(who or "")


def automation_authored(update):
    author = update_author(update).casefold()
    return any(marker in author for marker in AUTOMATION_MARKERS)


def update_artifacts(update):
    relations = update.get("relations") or {}
    if isinstance(relations, dict):
        relations = list(relations.get("added") or []) + list(relations.get("updated") or [])
    return [r for r in relations if (r.get("rel") or "") == "ArtifactLink"]


def matching_transition(snapshot, sha, before_rev):
    wanted = str(sha or "").casefold()
    for update in sorted(snapshot.get("updates") or [], key=lambda item: update_revision(item) or -1):
        rev = update_revision(update)
        if rev is None or before_rev is None or rev <= before_rev:
            continue
        changed = state_change(update)
        if not changed or not automation_authored(update):
            continue
        urls = [str(relation.get("url") or "").casefold() for relation in update_artifacts(update)]
        if any(wanted in url for url in urls):
            return {"rev": rev, "author": update_author(update), **changed}
    return None


def watch(scratch, sha, before, timeout, interval, sleep=time.sleep, clock=time.monotonic):
    """Poll the card until `sha` is linked and the state moved, or the time is up."""
    before = dict(before or {})
    deadline = clock() + timeout
    seen = {"linked": False, "state": before.get("state"), "rev": before.get("rev"),
            "before_state": before.get("state"), "before_rev": before.get("rev"),
            "polls": 0, "transition": None}
    while True:
        links = scratch.links()
        seen["polls"] += 1
        seen["linked"] = seen["linked"] or commit_linked(links, sha)
        seen["state"] = links.get("state")
        seen["rev"] = links.get("rev")
        seen["transition"] = seen["transition"] or matching_transition(
            links, sha, before.get("rev"))
        if seen["linked"] and (seen["transition"] or seen["state"] != before.get("state")):
            break
        if clock() >= deadline:
            break
        sleep(interval)
    return seen


def analyse(initial, mention_seen, fixes_seen):
    """The facts the design needs, from what the two polls saw."""
    initial_state = initial.get("state")
    transitioned = TRANSITIONED if fixes_seen.get("transition") else (
        PRIOR if fixes_seen.get("before_state") != initial_state else UNCHANGED)
    return {
        "mention_links_commit": LINKED if mention_seen["linked"] else ABSENT,
        "fixes_links_commit": LINKED if fixes_seen["linked"] else ABSENT,
        "fixes_transitions": transitioned,
        "initial_state": initial_state,
        "final_state": fixes_seen["state"],
        "mention_alone_moved_state": mention_seen["state"] != initial_state,
        "fixes_transition": fixes_seen.get("transition"),
        "fixes_before_state": fixes_seen.get("before_state"),
        "fixes_unattributed_change": fixes_seen["state"] != fixes_seen.get("before_state")
        and not fixes_seen.get("transition"),
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
    if facts["fixes_transitions"] == PRIOR:
        lines.append("- NOTE: the state had already changed before the `Fixes` push, so this run "
                     "records that transition as prior rather than attributing it to `Fixes`.")
    if facts["fixes_unattributed_change"]:
        lines.append("- NOTE: the state changed after the `Fixes` push, but no later "
                     "automation-authored revision linked to the `Fixes` commit was observed, so "
                     "the run does not attribute that change to `Fixes`.")
    if facts["fixes_transition"]:
        lines.append(f"- transition evidence: revision {facts['fixes_transition']['rev']} by "
                     f"{facts['fixes_transition']['author']}")
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
    repo = Path(args.repo_path)
    try:
        push_urls = validated_push_urls(repo, args.remote, args.org, args.project)
    except GitError as exc:
        common.fail(EXIT.INVALID, str(exc),
                    fix="point --repo-path at a clean scratch clone whose push URL resolves "
                        "to the Azure DevOps organisation and project named by --org/--project")

    board = common.connect(args)
    scratch = common.AdoScratch(board, "e7", close_state=args.close_state)
    item = scratch.create()
    print(f"scratch work item {item} created")
    probe = None
    try:
        initial = scratch.links()
        probe = prepare(repo, item)
        token = board.token
        for kind in (MENTION, FIXES):
            ref = f"{probe['shas'][kind]}:refs/heads/{probe['branch']}"
            before = scratch.links() if kind == FIXES else initial
            if args.push:
                push(repo, args.remote, ref, token, push_urls)
            else:
                confirm(f"Push the {kind} commit:\n  git -C {repo} push {args.remote} {ref}", False)
            if kind == MENTION:
                mention_seen = watch(scratch, probe["shas"][MENTION], initial,
                                     args.poll_seconds, args.interval)
            else:
                fixes_seen = watch(scratch, probe["shas"][FIXES], before,
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
