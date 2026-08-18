#!/usr/bin/env python3
"""Turn a validated plan into cards, briefs and one feature branch.

Everything that writes lives here, and nothing that decides does. `planning.py`
has already refused every plan that cannot be published; this script's only
judgements are about identity — does this branch exist, does this card exist —
and both are answered by asking the authority rather than by looking at what
happens to be lying around locally.

Two of those authorities deserve naming.

**The remote is the authority on the branch.** Not the working copy. IDE-76
wrote that lesson down after a drift check believed a stale clone, and a branch
that exists in someone's checkout and nowhere else is exactly the same mistake
wearing different clothes. `git ls-remote` asks, and nothing here ever switches
the working copy.

**The `key` is the authority on the card.** A re-run matches PBIs by the key in
their `idp-meta` block, never by title, because a title is reworded while an
idea is being argued about and the duplicate nobody notices is the one two
agents build from.

    publish_planning.py IDE-nn [--remote origin] [--base main] [--dry-run]

Exit codes are the platform's:
    0 success · 2 the board or the remote is unreachable · 3 malformed
    4 state conflict · 6 profile · 7 the ADR changed after the session started
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parent.parent

META = re.compile(r"```idp-meta\s*\n(.*?)\n```", re.DOTALL)


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_planning():
    spec = importlib.util.spec_from_file_location("idp_planning",
                                                  SKILL_DIR / "planning.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_planning"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# git, behind one seam so the tests never shell out
# ---------------------------------------------------------------------------

def run_git(args, cwd=None):
    result = subprocess.run(["git", *args], cwd=str(cwd or REPO_ROOT),
                            capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def remote_has_branch(branch, remote="origin", runner=run_git, cwd=None):
    """Ask the remote, and only the remote.

    A remote that will not answer is exit 2 and not "the branch does not
    exist": treating an unreachable remote as an absence is how a second
    feature branch gets pushed over a first one.
    """
    code, out, err = runner(["ls-remote", "--heads", remote,
                             f"refs/heads/{branch}"], cwd)
    if code != 0:
        fail(2, f"cannot reach remote '{remote}': {err or 'git ls-remote failed'}. "
                "The branch check is against the remote, so this is not something "
                "to work around locally.")
    return bool(out.strip())


def default_base(remote="origin", runner=run_git, cwd=None):
    """Ask the remote which branch it defaults to, instead of assuming `main`.

    A hardcoded `main` is not merely wrong on a repository that uses `master`
    or a release line — it is wrong *silently*. `rev-parse main` on such a repo
    either fails at a moment that reads like a git problem, or worse resolves
    something plausible, and the feature branch is then cut from the wrong
    place. Nobody sees it until the pull request diff is enormous.

    Asked of the remote, for the same reason the branch-existence check is:
    a local clone can be behind, and being behind is exactly the failure mode
    the drift detector already exists to prevent.
    """
    code, out, err = runner(["ls-remote", "--symref", remote, "HEAD"], cwd)
    if code != 0:
        fail(2, f"cannot reach remote '{remote}': {err or 'git ls-remote failed'}. "
                "The base branch is asked of the remote, so this is not something "
                "to work around locally.")
    for line in out.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].rsplit("/", 1)[-1]
    fail(2, f"remote '{remote}' did not report a default branch. Pass --base "
            "explicitly rather than letting the feature branch be cut from a guess.")


def ensure_branch(branch, remote="origin", base=None, runner=run_git, cwd=None):
    """Exactly one feature branch, created on the remote or reused.

    Created by pushing a ref, not by checking one out: this process may be
    running in a worktree somebody else is using, and switching it out from
    under them to create a branch nobody asked to visit is a side effect no
    caller of a planner expects.
    """
    if remote_has_branch(branch, remote=remote, runner=runner, cwd=cwd):
        return {"branch": branch, "created": False}

    base = base or default_base(remote=remote, runner=runner, cwd=cwd)

    # The remote-tracking ref first: it is what the remote actually has. A bare
    # local name resolves to whatever this clone last fetched, which is the
    # stale-clone problem in a different coat.
    code, sha, err = runner(["rev-parse", f"{remote}/{base}"], cwd)
    if code != 0 or not sha:
        code, sha, err = runner(["rev-parse", base], cwd)
    if code != 0 or not sha:
        fail(2, f"cannot resolve the base '{base}': {err or 'git rev-parse failed'}")

    code, _, err = runner(["push", remote, f"{sha}:refs/heads/{branch}"], cwd)
    if code != 0:
        fail(2, f"cannot create '{branch}' on '{remote}': {err or 'git push failed'}")
    return {"branch": branch, "created": True}


# ---------------------------------------------------------------------------
# Identity of what is already there
# ---------------------------------------------------------------------------

def read_meta(description):
    match = META.search(description or "")
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def existing_children(board, feature):
    """{key: {identifier, description, meta}} for every PBI already on the card."""
    found = {}
    for child in board.list_children(feature):
        full = board.get_issue(child["identifier"])
        meta = read_meta(full.get("description"))
        key = meta.get("key")
        if key:
            found[key] = {"identifier": full["identifier"],
                          "title": full.get("title") or "",
                          "description": full.get("description") or "",
                          "meta": meta}
    return found


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

def publish(board, profile, session, plan, planning, remote="origin", base=None,
            git_runner=run_git, cwd=None, dry_run=False):
    """One branch, then every PBI as a card and a brief made by one action."""
    project_id = profile.get("project_id")
    if not project_id:
        fail(6, "the profile has no project_id; the PBIs have nowhere to go")

    planning.check_adr_unchanged(board, profile, session)

    by_key = {p["key"]: p for p in plan["pbis"]}
    order = planning.topological(planning.build_graph(plan["pbis"]))
    existing = existing_children(board, session["feature"])

    if dry_run:
        print(f"branch {session['branch']} on {remote}: "
              + ("exists, reused" if remote_has_branch(
                  session["branch"], remote=remote, runner=git_runner, cwd=cwd)
                 else f"would be created from {base}"))
        for key in order:
            known = existing.get(key)
            print(f"  {key}: "
                  + (f"update {known['identifier']}" if known else "create")
                  + ("" if known and known["meta"].get("brief_url")
                     else " + attach the agent brief"))
        return {"branch": session["branch"], "created": [], "updated": [],
                "dry_run": True}

    branch = ensure_branch(session["branch"], remote=remote, base=base,
                           runner=git_runner, cwd=cwd)
    ready = board.phase_status("pbi", "ready")

    created, updated, attached = [], [], []
    for key in order:
        pbi = by_key[key]
        known = existing.get(key)
        brief_url = (known or {}).get("meta", {}).get("brief_url")
        title = f"[PBI] {pbi['title']}"
        body = planning.render_card(session, pbi, brief_url=brief_url)

        if known:
            # Rewrite only what actually differs. A re-run that updates every
            # card unconditionally fills the board's history with edits nobody
            # made, and history is what the claim protocol reads.
            identifier = known["identifier"]
            if (body.strip() != known["description"].strip()
                    or title != known["title"]):
                board.update_issue(identifier, title=title, body=body)
                updated.append(identifier)
        else:
            issue = board.create_issue(title=title, body=body,
                                       parent=session["feature"], status=ready,
                                       project_id=project_id)
            identifier = issue["identifier"]
            created.append(identifier)

        # The brief is part of the same act. If it is missing — first run, or a
        # run whose attachment failed — attach it and stamp its address into the
        # card, so the next run can tell an attached brief from a lost one
        # without downloading every document on the project.
        if not brief_url:
            url = board.attach_document(planning.brief_title(identifier),
                                        planning.render_brief(session, pbi),
                                        identifier=identifier)
            attached.append(identifier)
            board.update_issue(identifier, body=planning.render_card(
                session, pbi, brief_url=url))

    hand_on(board, session)
    session["published"] = {"branch": branch, "pbis": order}
    planning.transition(session, "BRANCHED")
    planning.transition(session, "PUBLISHED")
    planning.save_session(session)
    return {"branch": branch, "created": created, "updated": updated,
            "attached": attached, "dry_run": False}


def hand_on(board, session):
    """Move the feature out of planning — unless somebody already did.

    Idempotency reaches this far too. `finish_phase` refuses to move a card that
    is not in `In Planning`, which is correct for a first run and wrong for a
    second, so the second asks first instead of tripping over a refusal that
    means nothing went wrong.
    """
    issue = board.get_issue(session["feature"])
    target = board.phase_status("planning", "next")
    if (issue.get("status") or "").casefold() == target.casefold():
        print(f"{session['feature']} is already in '{target}'", file=sys.stderr)
        return
    board.finish_phase(session["feature"], "planning")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("id")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default=None,
                        help="base to cut the feature branch from; "
                             "default: whatever the remote says is its default branch")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be written, and write nothing")
    args = parser.parse_args(argv)

    planning = load_planning()
    board_module = planning.load_board()
    profile, _, board = board_module.open_board()

    session = planning.load_session(args.id)
    if session["state"] not in ("VALIDATED", "BRANCHED", "PUBLISHED"):
        fail(4, f"the session for {args.id} is in {session['state']}; publication "
                "needs a validated plan. Run: planning.py validate")
    plan = planning.load_plan(session)

    result = publish(board, profile, session, plan, planning,
                     remote=args.remote, base=args.base, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"branch {result['branch']['branch']}: "
              + ("created" if result["branch"]["created"] else "reused"))
        print(f"created: {', '.join(result['created']) or '—'}")
        print(f"updated: {', '.join(result['updated']) or '—'}")
    return result


if __name__ == "__main__":
    main()
