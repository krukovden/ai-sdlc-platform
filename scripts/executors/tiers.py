"""Model tiers, and the floor no role may run below (IDE-152).

`registry/models.json` classifies every model the platform knows into `fast`,
`standard` or `frontier`, and names the least capable tier each role accepts:
design and review are `frontier`, because a mistake in an ADR or a plan
multiplies into every PBI cut from it, and a reviewer weaker than the author
reviews nothing. A model the table does not know meets only the lowest floor.

The floor is checked twice. **The profile first**: an entry on a role's own
list below its floor refuses the run before any session starts, because it
would be the first thing tried. **Then every resolution**: a fallback borrowed
from another role, or an agent's unnamed default, is skipped for a role it is
not good enough for.

The one way past it is deliberate and visible:
`executors.model_floor_override: {"design": "<why>"}`. The run proceeds, and
the session that ran below the floor carries the reason, which the runner
writes on the card the human approves.
"""

import fnmatch
import json
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[2] / "registry" / "models.json"


class FloorError(ValueError):
    """The profile names a model below a role's floor. Carries the fix."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


class Floors:
    """The tier table and the floors, with the profile's overrides applied."""

    def __init__(self, data, overrides=None):
        self.tiers = tuple(data["tiers"])
        self.floors = dict(data.get("floors") or {})
        self.models = dict(data.get("models") or {})
        self.patterns = [(p["match"], p["tier"]) for p in data.get("patterns") or []]
        for tier in list(self.floors.values()) + list(self.models.values()) + \
                [tier for _, tier in self.patterns]:
            if tier not in self.tiers:
                raise ValueError(f"'{tier}' is not a tier of {REGISTRY.name}")
        self.overrides = {}
        if overrides is not None and not isinstance(overrides, dict):
            raise FloorError("executors.model_floor_override is not an object",
                             'write it as {"<role>": "<why this run may go below '
                             'the floor>"}')
        for role, reason in (overrides or {}).items():
            if role not in self.floors:
                raise FloorError(f"executors.model_floor_override names '{role}', "
                                 "which is not a role",
                                 f"use one of {', '.join(self.floors)}")
            if not isinstance(reason, str) or not reason.strip():
                raise FloorError(f"executors.model_floor_override.{role} gives no reason",
                                 "say why this run may go below the floor; the reason "
                                 "is written on the card the human approves")
            self.overrides[role] = reason.strip()

    @classmethod
    def load(cls, overrides=None, path=REGISTRY):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), overrides)

    def tier(self, choice):
        """The tier of `agent[:model]`, or None when the table does not know it."""
        key = str(choice)
        if key in self.models:
            return self.models[key]
        for pattern, tier in self.patterns:
            if fnmatch.fnmatchcase(key, pattern):
                return tier
        return None

    def floor(self, role):
        return self.floors.get(role, self.tiers[0])

    def meets(self, role, choice):
        floor = self.floor(role)
        if floor == self.tiers[0]:
            return True
        tier = self.tier(choice)
        return tier is not None and self.tiers.index(tier) >= self.tiers.index(floor)

    def allowed(self, role, choice):
        return role in self.overrides or self.meets(role, choice)

    def override(self, role, choice):
        """What a session of `choice` in `role` overrode, or None when it met the floor."""
        if choice is None or self.meets(role, choice):
            return None
        return {"role": role, "model": str(choice), "tier": self.tier(choice) or "unknown",
                "floor": self.floor(role), "reason": self.overrides.get(role, "")}

    def check_profile(self, roles):
        """Refuse a role list that names a model below its floor, unless overridden."""
        problems = []
        for role, entries in roles.items():
            if role in self.overrides:
                continue
            for entry in entries:
                if not self.meets(role, entry):
                    problems.append(f"{role}: '{entry}' is "
                                    f"{self.tier(entry) or 'not in the tier table'}, "
                                    f"and {role} needs {self.floor(role)}")
        if problems:
            known = sorted(m for m, t in self.models.items() if t == self.tiers[-1])
            raise FloorError(
                "the profile puts a model below its role's floor: " + "; ".join(problems),
                f"name a {self.tiers[-1]} model for that role (for example "
                f"{', '.join(known[:4]) or 'claude:opus'}), classify a new model in "
                f"registry/{REGISTRY.name}, or — knowingly — set "
                'executors.model_floor_override.<role> to the reason; it is written '
                "on the card the human approves")
