#!/usr/bin/env python3
"""Derive a usable .idp/profile.json from the repository itself.

An explicit profile is still authoritative. This module exists for the case
where there is no `.idp/profile.json` yet and the platform has to make the
smallest honest profile it can from the checkout alone: the remote, the agents
installed on this machine, the repository's test entry point, and the git
identity of the human at the QA gate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

PROFILE_VERSION = 1
DEFAULT_INVOKER = "idp setup"

AGENT_MODELS = {
    "claude": {"design": "claude:opus", "worker": "claude:sonnet",
               "review": "claude:opus", "integration": "claude:opus"},
    "codex": {"design": "codex", "worker": "codex",
              "review": "codex", "integration": "codex"},
    "copilot": {"design": "copilot:gpt", "worker": "copilot:gpt",
                "review": "copilot:gpt", "integration": "copilot:gpt"},
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
ADO_VISUALSTUDIO = re.compile(r"^/([^/]+)/_git/([^/]+?)(?:\.git)?/?$")
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
            return {"board": "azure-devops", "workspace": workspace,
                    "project_id": project, "team_key": project}
    if host.endswith(".visualstudio.com"):
        match = ADO_VISUALSTUDIO.match(parsed.path or "")
        if match:
            project, _ = match.groups()
            workspace = host.split(".", 1)[0]
            return {"board": "azure-devops", "workspace": workspace,
                    "project_id": project, "team_key": project}
    match = ADO_SSH.match(url.strip())
    if match:
        workspace, project, _ = match.groups()
        return {"board": "azure-devops", "workspace": workspace,
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


def derive_roles(available):
    roles = {}
    available = list(available)
    for role, order in ROLE_ORDER.items():
        entries = [AGENT_MODELS[agent][role] for agent in order if agent in available]
        if not entries and available:
            entries = [AGENT_MODELS[available[0]][role]]
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


def detect_test_command(root):
    package = _json(Path(root) / "package.json")
    if isinstance(package, dict):
        scripts = package.get("scripts")
        if isinstance(scripts, dict):
            test = str(scripts.get("test") or "").strip()
            if test:
                return "npm test"

    root = Path(root)
    if any((root / name).exists() for name in PYTEST_FILES):
        return "python3 -m pytest"
    for name in PYTEST_CONFIGS:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in text or "[pytest]" in text:
            return "python3 -m pytest"

    tests = root / "tests"
    if tests.is_dir():
        found = False
        for path in tests.rglob("*.py"):
            if TEST_FILE.match(path.name):
                found = True
                break
        if found:
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
    root = Path(root or os.getcwd()).resolve()
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
