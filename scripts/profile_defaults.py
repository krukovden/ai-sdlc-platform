#!/usr/bin/env python3
"""Derive a usable .idp/profile.json from the repository itself.

An explicit profile is still authoritative. This module exists for the case
where there is no `.idp/profile.json` yet and the platform has to make the
smallest honest profile it can from the checkout alone: the remote, the agents
installed on this machine, the repository's test entry point, and the git
identity of the human at the QA gate.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

PROFILE_VERSION = 1
DEFAULT_INVOKER = "idp setup"

# Design and review must meet the `frontier` floor of registry/models.json
# (IDE-152): an agent whose default is not classified there is left out of
# those roles by derive_roles rather than written into a profile that refuses.
AGENT_MODELS = {
    "claude": {"design": "claude:opus", "worker": "claude:sonnet",
               "review": "claude:opus", "integration": "claude:opus"},
    "codex": {"design": "codex", "worker": "codex",
              "review": "codex", "integration": "codex"},
    "copilot": {"design": "copilot:gpt-5.4", "worker": "copilot:gpt-5.4",
                "review": "copilot:gpt-6-astra", "integration": "copilot:gpt-5.4"},
    "cursor": {"design": "cursor", "worker": "cursor",
               "review": "cursor", "integration": "cursor"},
    "opencode": {"design": "opencode", "worker": "opencode",
                 "review": "opencode", "integration": "opencode"},
    "gemini": {"design": "gemini", "worker": "gemini",
               "review": "gemini", "integration": "gemini"},
}

ROLE_ORDER = {
    "design": ["claude", "codex", "copilot", "cursor", "opencode", "gemini"],
    "worker": ["codex", "claude", "copilot", "cursor", "opencode", "gemini"],
    "review": ["copilot", "claude", "codex", "cursor", "opencode", "gemini"],
    "integration": ["claude", "codex", "copilot", "cursor", "opencode", "gemini"],
}

PYTEST_FILES = ("pytest.ini", ".pytest.ini", "conftest.py")
PYTEST_CONFIGS = ("pyproject.toml", "tox.ini", "setup.cfg")
TEST_FILE = re.compile(r"^test[^/]*\.py$")
ADO_HTTPS = re.compile(r"^/([^/]+)/([^/]+)/_git/([^/]+?)(?:\.git)?/?$")
ADO_VISUALSTUDIO = re.compile(r"^/(?:[^/]+/)?([^/]+)/_git/([^/]+?)(?:\.git)?/?$")
ADO_SSH = re.compile(r"^(?:ssh://)?git@ssh\.dev\.azure\.com[:/]v3/([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$")


class DerivationError(ValueError):
    """A profile default the repository alone cannot answer."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message = message
        self.fix = fix


def _run(argv, cwd, allow_failure=False):
    answer = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if answer.returncode == 0:
        return (answer.stdout or "").strip()
    if allow_failure:
        return ""
    raise DerivationError(
        f"{' '.join(argv)} failed while deriving the profile: "
        f"{(answer.stderr or answer.stdout).strip() or 'no output'}",
        "run the command yourself, fix the repository's git configuration, then retry")


def remote_url(root):
    text = _run(["git", "remote", "get-url", "origin"], root, allow_failure=True)
    if text:
        return text
    raise DerivationError(
        "this repository has no usable `origin` remote, so the board cannot be derived",
        "set the `origin` remote, or write .idp/profile.json by hand")


def parse_board_from_remote(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host == "dev.azure.com":
        match = ADO_HTTPS.match(parsed.path or "")
        if match:
            workspace, project, _ = match.groups()
            project = unquote(project)
            return {"board": "azure-devops", "workspace": unquote(workspace),
                    "project_id": project, "team_key": project}
    if host.endswith(".visualstudio.com"):
        match = ADO_VISUALSTUDIO.match(parsed.path or "")
        if match:
            project, _ = match.groups()
            workspace = host.split(".", 1)[0]
            project = unquote(project)
            return {"board": "azure-devops", "workspace": unquote(workspace),
                    "project_id": project, "team_key": project}
    match = ADO_SSH.match(url.strip())
    if match:
        workspace, project, _ = match.groups()
        project = unquote(project)
        return {"board": "azure-devops", "workspace": unquote(workspace),
                "project_id": project, "team_key": project}
    return None


def require_linear_team_key(team_key=None, command=DEFAULT_INVOKER):
    value = str(team_key or "").strip()
    if value:
        return value
    shell = str(command or DEFAULT_INVOKER).strip() or DEFAULT_INVOKER
    slash = "/idp-setup" if shell.endswith("setup") else "/idp-setup"
    raise DerivationError(
        "the `origin` remote is not Azure DevOps, so the board cannot be inferred from git "
        "alone; a Linear team key is still needed",
        f"run `{slash}` interactively (or `{shell}` in a shell) and "
        "answer the Linear team key")


def installed_agents(which=shutil.which):
    return [agent for agent in ROLE_ORDER["design"] if which(binary(agent))]


def binary(agent):
    return {
        "claude": "claude",
        "codex": "codex",
        "copilot": "copilot",
        "cursor": "cursor-agent",
        "opencode": "opencode",
        "gemini": "gemini",
    }[agent]


def _floors():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "idp_tiers", Path(__file__).resolve().parent / "executors" / "tiers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Floors.load()


def derive_roles(available, floors=None):
    """Each role's entries from the installed agents, held to the role's floor.

    A role no installed agent meets is left out, so the run refuses naming the
    floor instead of quietly running design on whatever is there.
    """
    floors = floors or _floors()
    roles = {}
    available = list(available)
    for role, order in ROLE_ORDER.items():
        entries = [AGENT_MODELS[agent][role] for agent in order if agent in available]
        if not entries and available:
            entries = [AGENT_MODELS[available[0]][role]]
        entries = [entry for entry in entries if floors.meets(role, entry)]
        if entries:
            roles[role] = entries
    return roles


def derive_executors(which=shutil.which):
    available = installed_agents(which)
    roles = derive_roles(available)
    if not roles:
        return None
    return {"backend": "auto", "roles": roles}


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def repository_root(root):
    start = Path(root or os.getcwd()).resolve()
    text = _run(["git", "rev-parse", "--show-toplevel"], start, allow_failure=True)
    return Path(text).resolve() if text else start


def _pytest_layout(root):
    root = Path(root)
    if any((root / name).exists() for name in PYTEST_FILES):
        return True
    for name in PYTEST_CONFIGS:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in text or "[pytest]" in text:
            return True
    return False


def _annotate_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    return tree


def detect_test_command(root):
    package = _json(Path(root) / "package.json")
    if isinstance(package, dict):
        scripts = package.get("scripts")
        if isinstance(scripts, dict):
            test = str(scripts.get("test") or "").strip()
            if test:
                return "npm test"

    root = Path(root)
    if _pytest_layout(root):
        return "python3 -m pytest"
    tests = root / "tests"
    if tests.is_dir():
        layout = {"files": False, "pytest": False, "unittest": False}
        for path in tests.rglob("*.py"):
            if not TEST_FILE.match(path.name):
                continue
            layout["files"] = True
            try:
                tree = _annotate_parents(ast.parse(path.read_text(encoding="utf-8")))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "pytest" for alias in node.names):
                        layout["pytest"] = True
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "pytest":
                        layout["pytest"] = True
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("test_") and isinstance(getattr(node, "parent", None),
                                                                    ast.Module):
                        layout["pytest"] = True
                elif isinstance(node, ast.ClassDef):
                    has_test_methods = any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name.startswith("test_")
                        for child in node.body
                    )
                    unittest_case = False
                    for base in node.bases:
                        text = ast.unparse(base) if hasattr(ast, "unparse") else ""
                        if text.endswith("TestCase") or text == "TestCase":
                            layout["unittest"] = True
                            unittest_case = True
                    if has_test_methods and node.name.startswith("Test") and not unittest_case:
                        layout["pytest"] = True
            if layout["pytest"] and layout["unittest"]:
                return "python3 -m pytest"
        if layout["pytest"]:
            return "python3 -m pytest"
        if layout["unittest"]:
            return "python3 -m unittest discover -s tests"

    makefile = root / "Makefile"
    if makefile.exists():
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^test\s*:", line):
                return "make test"
    return None


def approvers(root):
    email = _run(["git", "config", "user.email"], root, allow_failure=True)
    return [email] if email else []


def derive(root=None, team_key=None, which=shutil.which, command=DEFAULT_INVOKER):
    root = repository_root(root or os.getcwd())
    profile = {"schema_version": PROFILE_VERSION}

    remote = parse_board_from_remote(remote_url(root))
    if remote is None:
        profile["board"] = "linear"
        profile["team_key"] = require_linear_team_key(team_key, command=command)
    else:
        profile.update(remote)

    executors = derive_executors(which)
    if executors:
        profile["executors"] = executors

    test = detect_test_command(root)
    if test:
        profile["development"] = {"integration": {"test": test}}

    found = approvers(root)
    if found:
        profile["approvers"] = found
    return profile
