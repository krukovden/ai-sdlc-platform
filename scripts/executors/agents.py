"""How each agent's command line is spelled: to launch a session, and to probe it.

Data, so adding an agent is a row and not a branch. The probes are deliberately
the cheapest call that still needs a signed-in account — one turn, the word
"ok" — and every one runs under a timeout, so a probe that hangs on a login
prompt counts as unavailable rather than wedging the run.

What these flags do on the live tools is recorded in docs/runner.md; Orca's own
launcher is a different backend (IDE-147 build step 6).
"""

PROBE_TIMEOUT = 60

BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "cursor": "cursor-agent",
    "opencode": "opencode",
    "gemini": "gemini",
}


def _model(flag, model):
    return [flag, model] if model else []


def launch_argv(choice, prompt):
    """The command for one unattended session of `choice` working on `prompt`.

    Claude streams JSON so the runner can see `system/api_retry` events — the
    rate-limit and overload signal of handoff layer 3. The others report only
    through their exit and the result file the session writes.
    """
    agent, model = choice.agent, choice.model
    if agent == "claude":
        return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", *_model("--model", model)]
    if agent == "codex":
        return ["codex", "exec", "--full-auto", "--skip-git-repo-check",
                *_model("-m", model), prompt]
    if agent == "copilot":
        return ["copilot", "-p", prompt, "--allow-all-tools", *_model("--model", model)]
    if agent == "cursor":
        return ["cursor-agent", "-p", prompt, "--force", *_model("--model", model)]
    if agent == "opencode":
        return ["opencode", "run", *_model("-m", model), prompt]
    if agent == "gemini":
        return ["gemini", "-p", prompt, "--yolo", *_model("-m", model)]
    raise ValueError(f"no launch line for agent '{agent}'")


def probe_argv(choice):
    """One authenticated turn, as small as the agent allows."""
    agent, model = choice.agent, choice.model
    if agent == "claude":
        return ["claude", "-p", "ok", "--max-turns", "1", *_model("--model", model)]
    if agent == "codex":
        return ["codex", "exec", "--skip-git-repo-check", *_model("-m", model), "ok"]
    if agent == "copilot":
        return ["copilot", "-p", "ok", *_model("--model", model)]
    if agent == "cursor":
        return ["cursor-agent", "-p", "ok", *_model("--model", model)]
    if agent == "opencode":
        return ["opencode", "run", *_model("-m", model), "ok"]
    if agent == "gemini":
        return ["gemini", "-p", "ok", *_model("-m", model)]
    raise ValueError(f"no probe for agent '{agent}'")
