#!/usr/bin/env python3
"""Resolve the installed IDP entry point from a command or skill wrapper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _exit_config():
    import importlib.util

    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing.CONFIG
    spec = importlib.util.spec_from_file_location("idp_exit_codes", SCRIPT_DIR / "exit_codes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module.CONFIG


class InvocationError(ValueError):
    def __init__(self, message, fix):
        super().__init__(message)
        self.message = message
        self.fix = fix


def _git_root(cwd):
    answer = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if answer.returncode == 0 and answer.stdout.strip():
        return Path(answer.stdout.strip()).resolve()
    return Path(cwd).resolve()


def _bundled_from_skill_dir(skill_dir):
    if not skill_dir:
        return None
    candidate = Path(skill_dir).resolve().parents[1] / "scripts" / "board.py"
    return candidate if candidate.exists() else None


def _bundled_from_plugin_root(plugin_root):
    if not plugin_root:
        return None
    candidate = Path(plugin_root).resolve() / "scripts" / "board.py"
    return candidate if candidate.exists() else None


def resolve(cwd=None, home=None, plugin_root=None, skill_dir=None):
    root = _git_root(cwd or os.getcwd())
    local = root / ".local" / "bin" / "idp"
    if local.exists():
        return [sys.executable, str(local)]
    bundled = _bundled_from_skill_dir(skill_dir) or _bundled_from_plugin_root(plugin_root)
    if bundled:
        return [sys.executable, str(bundled)]
    global_launcher = Path(home or Path.home()).expanduser() / ".local" / "bin" / "idp"
    if global_launcher.exists():
        return [sys.executable, str(global_launcher)]
    raise InvocationError(
        "no installed `idp` launcher and no bundled board script were found",
        "install the platform with `idp-platform install`, or use the host's plugin manager")


def run(arguments, cwd=None, home=None, plugin_root=None, skill_dir=None):
    argv = resolve(cwd=cwd, home=home, plugin_root=plugin_root, skill_dir=skill_dir)
    answer = subprocess.run([*argv, *arguments], cwd=cwd)
    return answer.returncode


def main(argv=None):
    args = list(argv or sys.argv[1:])
    skill_dir = None
    if args[:1] == ["--skill-dir"]:
        if len(args) < 2:
            raise InvocationError("missing value for --skill-dir",
                                  "pass the directory that contains SKILL.md")
        skill_dir = args[1]
        args = args[2:]
    if not args:
        raise InvocationError("no idp command was given",
                              "pass one of pipeline, design, develop, qa, setup or status")
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip() or None
    sys.exit(run(args, cwd=os.getcwd(), home=Path.home(), plugin_root=plugin_root,
                 skill_dir=skill_dir))


if __name__ == "__main__":
    try:
        main()
    except InvocationError as exc:
        print(f"ERROR: {exc.message}\nFIX: {exc.fix}", file=sys.stderr)
        sys.exit(_exit_config())
