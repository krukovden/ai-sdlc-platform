#!/usr/bin/env python3
"""Install the platform once; use it from every repository.

Copying the platform into each project is the failure this avoids: a copy
diverges from the original on the first day and fixes never reach it, which is
how frameworks distributed by fork rot. Half the mechanism already exists —
`board.py` walks up from the working directory looking for `.idp/profile.json`
— so installation is one symlink, not a package.

    python3 scripts/install.py --dry-run     say what would happen, change nothing
    python3 scripts/install.py               link ~/.local/bin/idp at this checkout
    python3 scripts/install.py --uninstall   remove the link, touch nothing else

Deliberately not a copy. The link points at this checkout, so `git pull` here
updates every project at once — which is the whole argument for installing
rather than vendoring.

No administrator rights, no system Python, no site-packages. Uninstalling
removes one symlink: the commands stop working and not a single project file
is touched, because nothing of the platform was ever written into a project
except its own profile.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "board.py"
DEFAULT_TARGET = Path("~/.local/bin/idp").expanduser()

VERSION = json.loads((SOURCE.parent.parent / "package.json").read_text())["version"]
STANDARD = "1.0"          # the authoring standard this build implements (IDE-78)


def _exit_codes():
    """`scripts/exit_codes.py`: every exit code, named. No literal lives elsewhere."""
    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing
    import importlib.util
    path = Path(__file__).resolve().parent / "exit_codes.py"
    spec = importlib.util.spec_from_file_location("idp_exit_codes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module


EXIT = _exit_codes()


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


def on_path(directory):
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return any(p == directory for p in entries)


def current_target(link):
    """Where the link points, or None. A broken link still has a target."""
    if link.is_symlink():
        return Path(os.readlink(link))
    return None


def plan(target, force):
    """Decide before touching anything, so --dry-run and the real run agree."""
    steps = []
    if not SOURCE.exists():
        fail(EXIT.CONFIG, f"no {SOURCE} — run this from the platform checkout",
             fix="run install.py from a full checkout of the platform repository")

    if target.exists() or target.is_symlink():
        existing = current_target(target)
        if existing == SOURCE:
            steps.append(f"already linked: {target} -> {SOURCE}")
            return steps, False
        if existing is None:
            # A real file, not a link. Overwriting someone's own script without
            # being told to is not an upgrade, it is data loss.
            if not force:
                fail(EXIT.INVALID, f"{target} exists and is not a symlink. Move it, or pass --force.",
                     fix="move the file away, or pass --force to replace it")
            steps.append(f"replace the regular file {target}")
        else:
            if not force:
                fail(EXIT.INVALID, f"{target} already points at {existing}, not at this checkout. "
                                   "Pass --force to repoint it.",
                     fix="pass --force to repoint the link at this checkout")
            steps.append(f"repoint {target} from {existing}")
    else:
        steps.append(f"create {target.parent} if it is missing")

    steps.append(f"link {target} -> {SOURCE}")
    if not on_path(target.parent):
        steps.append(f"NOTE: {target.parent} is not on PATH; add it to your shell profile")
    return steps, True


def install(target, force, dry_run):
    steps, changes = plan(target, force)
    for step in steps:
        print(("would " if dry_run and changes else "") + step)
    if dry_run or not changes:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(SOURCE)
    SOURCE.chmod(SOURCE.stat().st_mode | 0o111)
    print(f"\nInstalled. `idp` now runs {SOURCE}")
    print("Each project keeps its own .idp/profile.json; the platform keeps none.")


def uninstall(target, dry_run):
    if not (target.exists() or target.is_symlink()):
        print(f"{target} is not installed; nothing to do")
        return
    existing = current_target(target)
    if existing is not None and existing != SOURCE:
        fail(EXIT.INVALID, f"{target} points at {existing}, not at this checkout. "
                           "Refusing to remove someone else's link.",
             fix="remove that link by hand if it is really yours")
    if existing is None:
        fail(EXIT.INVALID, f"{target} is a regular file, not our link. Refusing to remove it.",
             fix="remove the file by hand if it is really yours")

    print(("would remove " if dry_run else "removed ") + str(target))
    if not dry_run:
        target.unlink()
        print("Projects are untouched: their profiles and their boards are unchanged.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                        help=f"where to put the command (default: {DEFAULT_TARGET})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="repoint or replace whatever is already there")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    target = Path(args.target).expanduser()
    if args.uninstall:
        uninstall(target, args.dry_run)
    else:
        install(target, args.force, args.dry_run)


if __name__ == "__main__":
    main()
