"""What counts as a platform unit, and what this install of the platform holds (IDE-151).

A unit is named from a path relative to the platform root:

    skills/<unit>/<file>             <unit>, a skill
    scripts/hooks/<name>.py          <name>, a hook
    scripts/<name>.py                <name>, a workflow (the runner, board.py, ...)
    scripts/<package>/<...>.py       <package>, a workflow (phase3, executors, ...)

Underscores become hyphens so every name fits the report schema.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
UNIT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Never reported by the hook: the reporter itself (no loop) and the feedback command (it reports to the user).
EXCLUDED = {"platform-telemetry", "platform-feedback"}


def _name(text):
    name = text.lower().replace("_", "-")
    return name if UNIT_RE.fullmatch(name) else None


def unit_of(rel):
    """(name, kind) for a root-relative script path, else None."""
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "skills":
        name = _name(parts[1])
        return (name, "skill") if name else None
    if len(parts) >= 2 and parts[0] == "scripts":
        if len(parts) == 3 and parts[1] == "hooks":
            name = _name(os.path.splitext(parts[2])[0])
            return (name, "hook") if name else None
        name = _name(os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1])
        return (name, "workflow") if name else None
    return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def installed_units(root=ROOT):
    """{name: kind} for every unit of the platform at `root`."""
    units = {}
    catalog = _read_json(os.path.join(root, "catalog.json")) or {}
    for entry in catalog.get("skills") or []:
        name = _name(str(entry.get("name", ""))) if isinstance(entry, dict) else None
        if name:
            units[name] = "skill"
    scripts = os.path.join(root, "scripts")
    try:
        for entry in sorted(os.listdir(scripts)):
            path = os.path.join(scripts, entry)
            if entry == "hooks" and os.path.isdir(path):
                for hook in sorted(os.listdir(path)):
                    if hook.endswith(".py"):
                        found = unit_of(f"scripts/hooks/{hook}")
                        if found:
                            units.setdefault(*found)
            elif (entry.endswith(".py") or os.path.isdir(path)) and not entry.startswith(("_", ".")):
                found = unit_of(f"scripts/{entry}" if entry.endswith(".py") else f"scripts/{entry}/x.py")
                if found:
                    units.setdefault(*found)
    except OSError:
        pass
    return units


def framework(root=ROOT, env=None):
    """The platform this telemetry ships in: version, install scope and its units."""
    env = os.environ if env is None else env
    package = _read_json(os.path.join(root, "package.json")) or {}
    version = package.get("version") if isinstance(package.get("version"), str) else None
    unix = root.replace("\\", "/")
    scope = "source"
    match = re.search(r"^(.*)/\.idp/platform/[^/]+$", unix)
    if match:
        home = os.path.abspath(env.get("IDP_HOME") or os.path.expanduser("~")).replace("\\", "/")
        scope = "global" if match.group(1) == home else "project"
    elif re.search(r"/plugins/cache/|/installed-plugins/", unix):
        scope = "plugin"
    units = installed_units(root)
    return {"name": "ai-sdlc-platform", "version": version, "scope": scope, "bundles": [],
            "units": [{"name": name, "version": version, "edited": None} for name in sorted(units)]}
