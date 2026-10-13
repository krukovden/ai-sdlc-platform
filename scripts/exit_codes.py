#!/usr/bin/env python3
"""Every exit code the platform uses, and the one table the runner routes on.

No script, skill or test writes a numeric exit literal anywhere else;
`tests/test_meta_lints.py` enforces it. A number with two meanings is exactly
how a phase script's "my local state is missing" came within one hop of being
read as "start a fresh session" (IDE-147, *Exit codes*).

Two vocabularies live here, deliberately apart.

**The runner's table** is closed. It is what `run.py` routes on and nothing
else reaches it unmapped:

    0  DONE      finalize, then hand off
    2  BLOCKED   add `idp:blocked` and a reason comment; the card waits for a human
    3  REFUSED   the signal is not the one this phase starts from; nothing changed
    4  HANDOFF   context running out: `handoff.md` committed, start a fresh session

**The scripts' own codes** are the ones IDE-68 §9 defined and every existing
script already exits with. They keep their meanings — a skill's caller reads
them — but they are named, so the number never has to be written down, and
each script's codes reach the runner only through `translate`, which maps them
and never passes one through.
"""

# ---------------------------------------------------------------------------
# The runner's table. Closed: a code not in it does not exist.
# ---------------------------------------------------------------------------

DONE = 0
BLOCKED = 2
REFUSED = 3
HANDOFF = 4

RUNNER = {
    DONE: "done",
    BLOCKED: "blocked",
    REFUSED: "refused",
    HANDOFF: "context-handoff",
}

# ---------------------------------------------------------------------------
# The scripts' codes, IDE-68 §9. Shared by board.py, the adapters and every skill.
# ---------------------------------------------------------------------------

OK = 0
NO = 1             # a yes/no check answered no: `status --quiet`, `memory check`
UNAVAILABLE = 2    # the board, a provider or the remote rejected us or could not be reached
INVALID = 3        # malformed request, or an artifact that failed its schema or the validator
CONFLICT = 4       # state conflict: wrong phase, a limit reached, local state missing
FORBIDDEN = 5      # forbidden input, e.g. a derived field written by the model
CONFIG = 6         # configuration failure: profile, token, permissions
INVALIDATED = 7    # an approval invalidated by a material edit

SCRIPT = {
    OK: "ok",
    NO: "no",
    UNAVAILABLE: "unavailable",
    INVALID: "invalid",
    CONFLICT: "conflict",
    FORBIDDEN: "forbidden",
    CONFIG: "config",
    INVALIDATED: "invalidated",
}

# ---------------------------------------------------------------------------
# Translation: each script's codes into the runner's. Mapped, never passed through.
#
# The rule behind every row: a code that a human has to act on is BLOCKED; a
# code that says "this card is not where this phase starts, nothing changed" is
# REFUSED. HANDOFF is never produced by a translation — only a worker's own
# `run.py report --code 4` means it, so no script's `4` can be mistaken for it.
# ---------------------------------------------------------------------------

_PHASE_SCRIPT = {
    OK: DONE,
    UNAVAILABLE: BLOCKED,
    INVALID: BLOCKED,       # the artifact failed validation: finalize's job to retry
    CONFLICT: REFUSED,      # wrong phase, or local state missing — never "fresh session"
    FORBIDDEN: BLOCKED,
    CONFIG: BLOCKED,
    INVALIDATED: BLOCKED,   # the approval no longer holds; a human must re-approve
}

TRANSLATIONS = {
    "board": {
        OK: DONE,
        NO: REFUSED,        # `status --quiet`: nothing to run on this card
        UNAVAILABLE: BLOCKED,
        INVALID: REFUSED,   # the request was refused and nothing was written
        CONFIG: BLOCKED,
    },
    "design": dict(_PHASE_SCRIPT),
    "planning": dict(_PHASE_SCRIPT),
    "publish_planning": dict(_PHASE_SCRIPT),
    "discovery": dict(_PHASE_SCRIPT),
    "publish_linear": dict(_PHASE_SCRIPT),
    "establish": dict(_PHASE_SCRIPT),
    "validate": {OK: DONE, INVALID: BLOCKED, CONFIG: BLOCKED},
}


def translate(script, code):
    """The runner's code for what `script` exited with.

    Anything this table does not know — an unknown script, an unknown code, a
    crash's 1 — is BLOCKED. The runner cannot route on a number it does not
    understand, and the one thing it must never do is guess that it meant done
    or meant "start a fresh session".
    """
    if code is None:
        code = OK
    table = TRANSLATIONS.get(script)
    if table is None:
        return BLOCKED
    return table.get(code, BLOCKED)


def with_fix(message, fix):
    """A refusal in the one shape every refusal takes: what is wrong, then FIX.

    A refusal that does not say how to get past it is a dead end for a night
    run and a guessing game for a human; `test_meta_lints` requires one.
    """
    message = str(message).rstrip()
    if not fix:
        return message
    return f"{message}\nFIX: {str(fix).strip()}"
