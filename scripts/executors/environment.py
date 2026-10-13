"""The environment a model process starts with: an allowlist, never the runner's own.

**Model processes hold no board credential** (IDE-147, checklist item 9). The
runner pre-fetches what a session needs into `.idp/context/` and is the only
writer to the board, and that is a guarantee only if the session cannot reach
the board itself. So a session's environment is built up from nothing:

* a short list of variables any process needs (PATH, HOME, locale, proxies,
  CA bundles);
* the agent's own sign-in variables, for that agent only — Claude never sees
  an OpenAI key and Codex never sees an Anthropic one;
* what the runner sets on purpose (`IDP_*` below).

It is the platform's only such list: the reviewer's provider commands
(`skills/feature-discovery/reviewer.py`) use it too, through `provider_env`.

Then the deny list is applied over the result, and it wins over everything:
the board tokens by name, anything shaped like a personal access token, and
anything that says where a token file is. A variable that is both allowed and
denied is denied.
"""

import base64
import os
import pwd
import re

BASE = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TMPDIR",
    "TZ", "COLORTERM", "NO_COLOR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
BASE_PREFIXES = ("LC_", "XDG_")

# Each agent's own sign-in and configuration, and nobody else's.
AGENT_PREFIXES = {
    "claude": ("ANTHROPIC_", "CLAUDE_"),
    "codex": ("OPENAI_", "CODEX_"),
    "copilot": ("COPILOT_",),
    "cursor": ("CURSOR_",),
    "opencode": ("OPENCODE_", "ANTHROPIC_", "OPENAI_"),
    "gemini": ("GEMINI_",),
}
AGENT_NAMES = {
    "copilot": ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"),
    "gemini": ("GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT"),
}

# What the runner hands a session on purpose. Never a credential.
RUNNER_NAMES = frozenset({"IDP_HOME", "IDP_ROLE", "IDP_CARD", "IDP_WORKTREE",
                          "IDP_CONTEXT_DIR", "IDP_RESULT"})

# Wins over every allowance above.
DENIED_NAMES = frozenset({
    "AZURE_DEVOPS_EXT_PAT", "LINEAR_API_KEY", "SYSTEM_ACCESSTOKEN",
    "ADO_ENV_FILE", "HAL_ADO_ENV_FILE", "HANWHA_ADO_ENV_FILE",
})
DENIED_PATTERNS = (
    re.compile(r"(^|_)PAT($|_)"),            # AZURE_DEVOPS_EXT_PAT, MY_PAT, PAT_TOKEN
    re.compile(r"TOKEN_(PATH|FILE)"),        # where a token lives is as good as the token
    re.compile(r"^AZURE_DEVOPS"),
    re.compile(r"^LINEAR_"),
    re.compile(r"^ADO_"),
)
WITNESS_DENIED_NAMES = frozenset({
    "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
})
WITNESS_DENIED_PATTERNS = DENIED_PATTERNS + (
    re.compile(r"TOKEN"),
)


def denied(name):
    return name in DENIED_NAMES or any(p.search(name) for p in DENIED_PATTERNS)


def denied_for_witness(name):
    return name in WITNESS_DENIED_NAMES or denied(name) or \
        any(p.search(name) for p in WITNESS_DENIED_PATTERNS)


def allowed(name, agent=None):
    if name in BASE or name.startswith(BASE_PREFIXES) or name in RUNNER_NAMES:
        return True
    if agent and (name.startswith(AGENT_PREFIXES.get(agent, ()))
                  or name in AGENT_NAMES.get(agent, ())):
        return True
    return False


def secret_values(source):
    """Known secret values from `source`, longest first for stable redaction."""
    values = set()
    for name, value in (source or {}).items():
        if denied_for_witness(name):
            text = str(value or "")
            if text:
                values.add(text)
                if name == "AZURE_DEVOPS_EXT_PAT":
                    values.add(base64.b64encode(f":{text}".encode("utf-8")).decode("ascii"))
    return sorted(values, key=len, reverse=True)


def redact_text(text, source):
    rendered = str(text or "")
    for secret in secret_values(source):
        rendered = rendered.replace(secret, "******")
    return rendered


def session_env(source, agent=None, extra=None):
    """The environment for one model process of `agent`, built from `source`.

    `extra` is what the runner sets for this session; it goes through the deny
    list like everything else, so not even the runner can hand a token on.
    """
    env = {name: value for name, value in (source or {}).items()
           if allowed(name, agent) and not denied(name)}
    for name, value in (extra or {}).items():
        if not denied(name):
            env[name] = str(value)
    return env


class UnknownAgent(ValueError):
    """A provider whose agent cannot be named. Refused, never widened."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


def agent_of(name):
    """The agent a provider name stands for: `claude-subagent` is claude, `m` is nobody."""
    head = re.split(r"[^a-z]", str(name or "").casefold(), maxsplit=1)[0]
    return head if head in AGENT_PREFIXES else None


def provider_env(source, name=None, agent=None):
    """For a provider command from registry/providers.json (the reviewer's door).

    **Fails closed.** The provider's agent is its explicit `agent` field, or
    else what its `name` starts with; only that agent's sign-in keys pass. A
    provider whose agent cannot be named is refused: handing it every agent's
    keys "to be safe" is exactly the leak this list exists to prevent, and a
    typo in the registry would be the way it happened.
    """
    chosen = agent if agent is not None else agent_of(name)
    if chosen not in AGENT_PREFIXES:
        raise UnknownAgent(
            f"provider '{name}' names no agent this platform knows "
            f"(agent: {agent!r}), so no sign-in variables can be chosen for it",
            f"add \"agent\": one of {', '.join(AGENT_PREFIXES)} to its entry in "
            "registry/providers.json, or name it after its agent")
    return session_env(source, chosen)


def witness_env(source, extra=None):
    """For a command that is not a model at all: no board token and no model key."""
    env = {name: value for name, value in (source or {}).items()
           if allowed(name, None) and not denied_for_witness(name)}
    for name, value in (extra or {}).items():
        if not denied_for_witness(name):
            env[name] = str(value)
    return env


def current_user():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return None


def command_argv(argv, run_as=None, current=None, runuser="/usr/sbin/runuser", sudo="sudo"):
    """`argv`, or a user-switch wrapper around it when `run_as` is configured.

    The platform only switches uid when the profile asks for it. When the current
    user already is `run_as`, nothing is wrapped.
    """
    user = str(run_as or "").strip()
    command = [str(part) for part in argv]
    if not user:
        return command
    holder = current if current is not None else current_user()
    if holder == user:
        return command
    prefix = [runuser, "-u", user, "--"] if holder == "root" else \
        [sudo, "-n", runuser, "-u", user, "--"]
    return prefix + command
