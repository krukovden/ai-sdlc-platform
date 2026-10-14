#!/usr/bin/env python3
"""Initialise project memory in a product repository — a step of `/idp-setup`.

Resumable: every step that is already in place reports `kept`, nothing is
recreated, and an existing `index.md` is never overwritten.

    memory_init.py --project DIR [--dir memory] [--name NAME] [--json]

Steps, one line each (`<step>  <state>  <detail>`):

    folder     memory/units/ and memory/.store.json
    index      a skeleton index.md: `reconciled-pr: none`, empty sections
    pointers   the managed block in CLAUDE.md, .github/copilot-instructions.md, AGENTS.md
    hook       a post-commit line queueing every commit in memory/.pending
    gitignore  memory/.pending ignored

There is one store, committed in the repository (IDE-155). The machine-only
`local` store engineering-catalog offered is gone: memory that does not travel
with the repository reaches neither teammates nor an agent on another machine.
A block an older catalog install wrote is recognised and replaced in place.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

POINTER_VERSION = 2
HOOK_VERSION = 2
PREFIXES = ("<!-- idp:memory:start", "<!-- engineering-catalog:memory:start")
ENDS = ("<!-- idp:memory:end -->", "<!-- engineering-catalog:memory:end -->")
HOOK_PREFIXES = ("# idp:memory:start", "# engineering-catalog:memory:start")
HOOK_ENDS = ("# idp:memory:end", "# engineering-catalog:memory:end")
POINTER_FILES = ("CLAUDE.md", ".github/copilot-instructions.md", "AGENTS.md")


def managed_block(existing, prefixes, ends, start, end, content):
    """Replace a whole-line managed block in place (either generation), or append one."""
    block = f"{start}\n{content}\n{end}"
    for prefix, old_end in zip(prefixes, ends):
        pattern = re.compile(
            rf"^[ \t]*{re.escape(prefix)}(?: v=\d+)?(?: -->)?[ \t]*$.*?^[ \t]*{re.escape(old_end)}[ \t]*$",
            re.M | re.S)
        if pattern.search(existing):
            return pattern.sub(lambda _: block, existing, count=1)
    if not existing:
        return block + "\n"
    return existing + ("\n" if existing.endswith("\n") else "\n\n") + block + "\n"


def pointer_content(directory):
    return "\n".join([
        "## Project memory",
        f"Read `{directory}/index.md` first. Load `{directory}/units/<slug>.md` only when work "
        "touches that unit.",
        "Write an entry only when the change is hard to reverse, not obvious without context, "
        "and the result of a real trade-off — all three.",
        'A request for something listed under "Rejected at project level" is not implemented: '
        "quote the rejection and its reason, ask whether to overturn it, and change nothing "
        "until the person says yes.",
        "Unit files win over the index. Never record credentials, personal data or session "
        "narrative.",
        f"Before reporting a task done, drain `{directory}/.pending` (the memory skill says "
        "how): read each queued commit, write an entry only if it passes the test, then mark "
        "the queue reconciled.",
    ])


def index_skeleton(name):
    grade = "_Not yet written — `/idp-setup` fills this in from the code, graded `inferred`._"
    return "\n".join([
        "<!-- memory:index — what this project is, its architecture, one row per unit, "
        "project-level rejections.",
        "     Machine-read: the `reconciled-pr:` line. The Units table is generated from each "
        "unit's",
        "     `summary:` (project_memory.py index); Rejected is hand-written. Unit files win "
        "on conflict. -->",
        f"# {name} — memory index",
        "",
        "reconciled-pr: none",
        "store: committed",
        "",
        "## What this project is",
        grade,
        "",
        "## Architecture",
        grade,
        "",
        "## Units",
        "| Unit | Summary |",
        "|---|---|",
        "",
        "## Rejected at project level",
        f"- (none recorded — history before {date.today().isoformat()} predates tracking)",
        "",
    ])


def write_atomic(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    if mode is not None:
        os.chmod(temp, mode)
    os.replace(temp, path)


def step_folder(ctx):
    units, store = ctx["memory"] / "units", ctx["memory"] / ".store.json"
    existed = units.is_dir() and store.exists()
    units.mkdir(parents=True, exist_ok=True)
    if existed:
        data = json.loads(store.read_text(encoding="utf-8") or "{}")
        if data.get("store") == "local":
            raise RuntimeError("this store says `local`, which is gone (IDE-155): memory is "
                               "committed in the repository. Move the folder in and rewrite "
                               ".store.json")
        return "kept", str(ctx["memory"])
    store.write_text(json.dumps({"store": "committed", "dir": ctx["dir"],
                                 "created": date.today().isoformat()}, indent=2) + "\n",
                     encoding="utf-8")
    return "done", str(ctx["memory"])


def step_index(ctx):
    path = ctx["memory"] / "index.md"
    if path.exists():
        return "kept", str(path)
    path.write_text(index_skeleton(ctx["name"]), encoding="utf-8")
    return "done", str(path)


def step_pointers(ctx):
    content = pointer_content(ctx["dir"])
    start = f"{PREFIXES[0]} v={POINTER_VERSION} -->"
    details, changed = [], False
    for rel in POINTER_FILES:
        path = ctx["project"] / rel
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = managed_block(existing, PREFIXES, ENDS, start, ENDS[0], content)
        details.append(f"{rel}: {'kept' if updated == existing else 'done'}")
        if updated != existing:
            write_atomic(path, updated)
            changed = True
    return ("done" if changed else "kept"), "; ".join(details)


def step_hook(ctx):
    answer = subprocess.run(["git", "-C", str(ctx["project"]), "rev-parse", "--git-path",
                             "hooks"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if answer.returncode != 0:
        return "skipped", "not a git repository"
    hooks = Path(answer.stdout.strip())
    if not hooks.is_absolute():
        hooks = ctx["project"] / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "post-commit"
    line = f'git rev-parse HEAD >> "$(git rev-parse --show-toplevel)/{ctx["dir"]}/.pending"'
    existing = path.read_text(encoding="utf-8") if path.exists() else "#!/bin/sh\n"
    updated = managed_block(existing, HOOK_PREFIXES, HOOK_ENDS,
                            f"{HOOK_PREFIXES[0]} v={HOOK_VERSION}", HOOK_ENDS[0], line)
    if updated == existing and path.exists():
        os.chmod(path, 0o755)
        return "kept", str(path)
    write_atomic(path, updated, mode=0o755)
    return "done", str(path)


def step_gitignore(ctx):
    path = ctx["project"] / ".gitignore"
    rel = f"{ctx['dir']}/.pending"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(l.strip() == rel for l in existing.splitlines()):
        return "kept", str(path)
    sep = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{sep}{rel}\n", encoding="utf-8")
    return "done", str(path)


STEPS = (("folder", step_folder), ("index", step_index), ("pointers", step_pointers),
         ("hook", step_hook), ("gitignore", step_gitignore))


def initialise(project, directory="memory", name=None):
    project = Path(project).resolve()
    directory = directory.strip("/").replace("\\", "/")
    ctx = {"project": project, "dir": directory, "memory": project / directory,
           "name": name or project.name}
    steps = []
    for step, function in STEPS:
        try:
            state, detail = function(ctx)
        except Exception as exc:                       # noqa: BLE001 - reported, then exit 1
            steps.append({"name": step, "state": "error", "detail": str(exc)})
            break
        steps.append({"name": step, "state": state, "detail": detail})
    return {"memory": str(ctx["memory"]), "steps": steps}


def needs_filling(memory):
    """Whether the index still carries the skeleton's placeholders (setup's Step 3)."""
    path = Path(memory) / "index.md"
    return path.exists() and "_Not yet written" in path.read_text(encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="memory_init.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dir", default="memory")
    parser.add_argument("--name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = initialise(args.project, args.dir, args.name)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"memory dir: {result['memory']}")
        for step in result["steps"]:
            print(f"{step['name']}\t{step['state']}\t{step['detail']}")
    return 1 if any(s["state"] == "error" for s in result["steps"]) else 0


if __name__ == "__main__":
    sys.exit(main())
