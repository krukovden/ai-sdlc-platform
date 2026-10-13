#!/usr/bin/env python3
"""Area blueprints: what an area of the product does now, checked by a script.

One area is one file, `docs/blueprint/<area>.md` in the product repository
(IDE-147, *Area blueprints*). The documenter — the last step of 3.3 — lifts a
Feature's acceptance criteria into the blueprints of the areas it touched, with
links to the code and the tests that satisfy them. The model writes the prose;
this script decides whether what it wrote is a blueprint:

    # <Area>
    <one paragraph: what this area is and where its edges are>

    ## Requirements
    ### <REQ-AREA-001> <title>
    <why, one or two sentences>
    - [x] AC-1: <observable behaviour> — [code](src/…) [test](tests/…)
    - [ ] AC-2: <not built yet>

    ## Architecture
    <how it is built, at architecture altitude only>
    | Requirement | Code | Role |

The checks, and nothing more, because a blueprint states current truth and its
reader is the next agent:

* **shape** — the H1, a paragraph, `## Requirements` then `## Architecture` and
  no other second-level heading; every requirement is `### REQ-<AREA>-<NNN>
  <title>` with a unique id and at least one AC line; the Architecture section
  carries the `| Requirement | Code | Role |` table;
* **links** — every built AC (`- [x]`) links its code and its test, and every
  relative link resolves, from the file's directory or from the repository
  root, so a blueprint never points at code that is gone;
* **size** — at most 3,000 words per area file. A ceiling, never a target: past
  it, split the area.

Usage:
    blueprint.py check [FILE ...] [--root DIR] [--json]
        every docs/blueprint/*.md under --root (default: here) when no FILE
        is named. Exit 0 when every file passes, 3 otherwise, with a FIX line.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _exit_codes():
    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("idp_exit_codes", SCRIPT_DIR / "exit_codes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module


EXIT = _exit_codes()

DIRECTORY = "docs/blueprint"
MAX_WORDS = 3000
REQUIREMENTS = "Requirements"
ARCHITECTURE = "Architecture"
TABLE_HEADER = ("Requirement", "Code", "Role")

AREA_FILE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")
H1 = re.compile(r"^# (?!#)\s*(\S.*)$")
H2 = re.compile(r"^## (?!#)\s*(\S.*?)\s*$")
REQ = re.compile(r"^### (?!#)\s*(?P<id>REQ-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d{3})\s+(?P<title>\S.*)$")
H3 = re.compile(r"^### (?!#)")
AC = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<id>AC-\d+):\s*(?P<text>\S.*)$")
LINK = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+)\)")
WORD = re.compile(r"[^\W_][\w'’-]*")


def words(text):
    """Words a reader reads: link targets are not prose and are not counted."""
    return len(WORD.findall(LINK.sub(lambda m: m.group("label"), text)))


def _local(target):
    return not re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) and \
        not target.startswith("#")


def link_exists(target, here, root):
    """A relative link resolves from the file's directory, or from the repository root.

    Both, because the template writes `src/…` and a renderer resolves from the
    file: either spelling is a working pointer, and only one that resolves
    from neither is broken.
    """
    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return True
    root = Path(root).resolve()
    for base in (Path(here).parent, root):
        candidate = (base / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists():
            return True
    return False


def check_text(text, name="blueprint.md", here=None, root=None):
    """Every problem with one blueprint, as strings. Empty means it passes."""
    problems = []
    if not AREA_FILE.match(Path(name).name):
        problems.append(f"{name}: an area file is named `<area>.md` in lower case, "
                        "letters, digits and dashes")
    lines = text.splitlines()
    body = [l for l in lines if l.strip()]
    if not body or not H1.match(body[0]):
        problems.append(f"{name}: the first line is `# <Area>`")

    headings = [(i, H2.match(l).group(1)) for i, l in enumerate(lines) if H2.match(l)]
    titles = [t for _, t in headings]
    if titles != [REQUIREMENTS, ARCHITECTURE]:
        problems.append(f"{name}: the second-level headings are exactly "
                        f"`## {REQUIREMENTS}` then `## {ARCHITECTURE}`; found "
                        + (", ".join(f"`## {t}`" for t in titles) or "none"))
        return problems + _size(text, name)

    start_req, start_arch = headings[0][0], headings[1][0]
    intro = [l for l in lines[1:start_req] if l.strip() and not H1.match(l)]
    if not intro:
        problems.append(f"{name}: one paragraph under the title says what this area is "
                        "and where its edges are")

    seen, current, acs = set(), None, 0
    for number, line in enumerate(lines[start_req + 1:start_arch], start_req + 2):
        if H3.match(line):
            if current and not acs:
                problems.append(f"{name}: {current} has no AC line")
            match = REQ.match(line)
            if not match:
                problems.append(f"{name}:{number}: a requirement heading is "
                                "`### REQ-<AREA>-<NNN> <title>`")
                current, acs = None, 1
                continue
            current, acs = match.group("id"), 0
            if current in seen:
                problems.append(f"{name}:{number}: {current} appears twice")
            seen.add(current)
            continue
        ac = AC.match(line.strip())
        if line.lstrip().startswith("- [") and not ac:
            problems.append(f"{name}:{number}: an AC line is `- [x] AC-<n>: <behaviour>` or "
                            "`- [ ] AC-<n>: <behaviour>`")
            continue
        if ac:
            if current is None:
                problems.append(f"{name}:{number}: {ac.group('id')} sits under no requirement")
                continue
            acs += 1
            if ac.group("mark").lower() == "x":
                labels = {m.group("label").strip().casefold()
                          for m in LINK.finditer(ac.group("text"))}
                for wanted in ("code", "test"):
                    if wanted not in labels:
                        problems.append(f"{name}:{number}: {current} {ac.group('id')} is "
                                        f"built and links no [{wanted}](…)")
    if current and not acs:
        problems.append(f"{name}: {current} has no AC line")
    if not seen:
        problems.append(f"{name}: `## {REQUIREMENTS}` carries no `### REQ-…` requirement")

    table = False
    for line in lines[start_arch + 1:]:
        cells = tuple(c.strip() for c in line.strip().strip("|").split("|"))
        if line.strip().startswith("|") and cells == TABLE_HEADER:
            table = True
    if not table:
        problems.append(f"{name}: `## {ARCHITECTURE}` carries the table "
                        "`| Requirement | Code | Role |`")

    if root is not None:
        here = Path(here) if here else Path(root) / DIRECTORY / Path(name).name
        for number, line in enumerate(lines, 1):
            for match in LINK.finditer(line):
                target = match.group("target")
                if _local(target) and not link_exists(target, here, root):
                    problems.append(f"{name}:{number}: the link `{target}` resolves to "
                                    "nothing in the repository")
    return problems + _size(text, name)


def _size(text, name):
    count = words(text)
    if count > MAX_WORDS:
        return [f"{name}: {count} words, and an area file holds at most {MAX_WORDS}. "
                "Split the area into two files rather than trimming what is true"]
    return []


def check_file(path, root):
    path = Path(path)
    return check_text(path.read_text(encoding="utf-8"), name=path.name, here=path, root=root)


def area_files(root):
    directory = Path(root) / DIRECTORY
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def check(paths, root):
    """{file: [problems]} for every file, passing ones included."""
    return {str(p): check_file(p, root) for p in paths}


FIX = ("rewrite the named blueprint to the template in skills/documenter/SKILL.md; "
       "split an area past 3,000 words; link only code and tests that exist")


def cmd_check(args):
    root = Path(args.root).resolve()
    paths = [Path(p) if Path(p).is_absolute() else root / p for p in args.files] \
        or area_files(root)
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"ERROR: {EXIT.with_fix('no such blueprint: ' + ', '.join(missing), FIX)}",
              file=sys.stderr)
        sys.exit(EXIT.INVALID)
    found = check(paths, root)
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
    else:
        for name, problems in found.items():
            print(f"{name}: " + ("ok" if not problems else f"{len(problems)} problem(s)"))
            for problem in problems:
                print(f"  {problem}")
    if any(found.values()):
        if not args.json:
            print(f"FIX: {FIX}", file=sys.stderr)
        sys.exit(EXIT.INVALID)
    sys.exit(EXIT.OK)


def build_parser():
    parser = argparse.ArgumentParser(prog="blueprint.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="check area blueprints against the template")
    p.add_argument("files", nargs="*")
    p.add_argument("--root", default=".", help="the product repository's root")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
