"""Roles, preferences and limits: which agent and model fill a role (IDE-147, *Executors*).

The profile maps each role to an ordered list of `agent[:model]`. The first
entry that is **available** wins, and available means two checks, both cached
for the run: the agent can be launched (its binary is on PATH), and a cheap
authenticated probe succeeds. A binary on PATH with an expired login is not an
agent; without the probe a night run fails silently at its first review.

When nothing on a role's own list is available, the role falls back to the
entries the other roles name and then to any agent at all, so that with one
agent installed every role resolves to it.

**The review rule is hard.** `review` never resolves to the same agent and
model as the work it reviews. A model left unnamed is the agent's default, and
nobody here knows what that default is, so an unnamed model is treated as
possibly the same as any model of that agent — the conservative reading: an
entry the rule cannot prove different is skipped. Nothing left is a loud
failure, never an unreviewed merge.
"""

import re
from collections import namedtuple

from .agents import UNVERIFIED

AGENTS = ("claude", "codex", "copilot", "cursor", "opencode", "gemini")
ROLES = ("design", "worker", "review", "integration")

ENTRY = re.compile(r"^(?P<agent>[a-z]+)(?::(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*))?$")

DEFAULT_PARALLEL = 1


class ResolutionError(Exception):
    """No agent can fill this role. Carries the fix, like every refusal."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


class Choice(namedtuple("Choice", "agent model")):
    """One `agent[:model]`. `model` None is the agent's own default."""

    def __str__(self):
        return f"{self.agent}:{self.model}" if self.model else self.agent


def parse_entry(text):
    match = ENTRY.match(str(text or "").strip())
    if not match or match.group("agent") not in AGENTS:
        raise ResolutionError(f"'{text}' is not an agent[:model] entry",
                              f"use one of {', '.join(AGENTS)}, optionally with "
                              ":<model>, e.g. claude:opus")
    return Choice(match.group("agent"), match.group("model"))


def as_choice(value):
    if value is None or isinstance(value, Choice):
        return value
    if isinstance(value, (tuple, list)):
        return Choice(*value)
    return parse_entry(value)


def may_be_same(one, other):
    """Could these two be the same agent and model? Unnamed models might be."""
    if one is None or other is None or one.agent != other.agent:
        return False
    return one.model is None or other.model is None or one.model == other.model


class Resolver:
    """Resolves roles for one run. Availability is asked once per entry and kept.

    `launchable(agent)` and `probe(choice)` are injected; the executors supply
    the real ones (`shutil.which` and a time-limited authenticated call), tests
    supply tables.
    """

    def __init__(self, executors=None, launchable=None, probe=None):
        executors = executors or {}
        self.roles = {role: [parse_entry(e) for e in entries]
                      for role, entries in (executors.get("roles") or {}).items()}
        self.parallel = dict(executors.get("parallel") or {})
        self.launchable = launchable or (lambda agent: False)
        self.probe = probe
        self._launchable = {}
        self._available = {}

    # -- availability -------------------------------------------------------

    def can_launch(self, agent):
        if agent not in self._launchable:
            self._launchable[agent] = bool(self.launchable(agent))
        return self._launchable[agent]

    def exhaust(self, choice):
        """Mark an entry unavailable for the rest of the run: it hit a usage limit."""
        self._available[as_choice(choice)] = False

    def unavailable(self):
        """The entries asked about so far and found unavailable — exhausted included."""
        return [choice for choice, answer in self._available.items() if not answer]

    def available(self, choice):
        """Launchable, and past the probe. With no probe to ask, an agent whose
        command line is still unverified (agents.UNVERIFIED) is not available."""
        choice = as_choice(choice)
        if choice not in self._available:
            if not self.can_launch(choice.agent):
                answer = False
            elif self.probe is None:
                answer = choice.agent not in UNVERIFIED
            else:
                answer = bool(self.probe(choice))
            self._available[choice] = answer
        return self._available[choice]

    # -- resolution ---------------------------------------------------------

    def candidates(self, role):
        """The role's own list, then what other roles name, then any agent."""
        if role not in ROLES:
            raise ResolutionError(f"'{role}' is not a role", f"use one of {', '.join(ROLES)}")
        ordered = list(self.roles.get(role) or [])
        for other in ROLES:
            if other != role:
                ordered += self.roles.get(other) or []
        ordered += [Choice(agent, None) for agent in AGENTS]
        seen, unique = set(), []
        for choice in ordered:
            if choice not in seen:
                seen.add(choice)
                unique.append(choice)
        return unique

    def cap(self, agent):
        return int(self.parallel.get(agent, DEFAULT_PARALLEL))

    def resolve(self, role, author=None, busy=None):
        """The choice that fills `role` now, or None when every eligible agent is busy.

        `author` is who wrote the work a `review` looks at; `busy` counts the
        workers already running per agent, held to `parallel`. Raises
        ResolutionError when no available agent may fill the role at all.
        """
        author = as_choice(author)
        busy = busy or {}
        skipped = []
        eligible = []
        for choice in self.candidates(role):
            if not self.available(choice):
                continue
            if role == "review" and may_be_same(choice, author):
                skipped.append(str(choice))
                continue
            eligible.append(choice)
        if not eligible:
            if role == "review" and skipped:
                raise ResolutionError(
                    f"no reviewer differs from the author '{author}': every available "
                    f"entry ({', '.join(skipped)}) may be the same agent and model, and "
                    "the review rule never lets work review itself",
                    f"add an entry with a different agent, or a different named model of "
                    f"'{author.agent}', to executors.roles.review — and name the worker's "
                    "model explicitly, since an unnamed one could be any")
            raise ResolutionError(
                f"no agent can fill the role '{role}': none of "
                f"{', '.join(str(c) for c in self.roles.get(role) or []) or 'its entries'} "
                "is installed and signed in, and no other agent is either",
                "install and sign in to one of the agents the profile names "
                "(a probe like `claude -p ok` must succeed), then re-run")
        for choice in eligible:
            if busy.get(choice.agent, 0) < self.cap(choice.agent):
                return choice
        return None

    def capacity(self):
        """How many workers may run at once: the caps of every available agent, summed."""
        agents = {c.agent for c in self.candidates("worker") if self.available(c)}
        return sum(self.cap(agent) for agent in agents)
