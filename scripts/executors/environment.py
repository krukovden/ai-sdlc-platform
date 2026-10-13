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

Then the deny list is applied over the result, and it wins over everything:
the board tokens by name, anything shaped like a personal access token, and
anything that says where a token file is. A variable that is both allowed and
denied is denied.
"""

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


def denied(name):
    return name in DENIED_NAMES or any(p.search(name) for p in DENIED_PATTERNS)


def allowed(name, agent=None):
    if name in BASE or name.startswith(BASE_PREFIXES) or name in RUNNER_NAMES:
        return True
    if agent and (name.startswith(AGENT_PREFIXES.get(agent, ()))
                  or name in AGENT_NAMES.get(agent, ())):
        return True
    return False


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


def witness_env(source, extra=None):
    """For a command that is not a model at all: no board token and no model key."""
    return session_env(source, agent=None, extra=extra)
