#!/usr/bin/env python3
"""Calling the independent reviewer, and refusing to trust what comes back.

Two things live here and they are separate on purpose.

**Invocation** runs whatever `providers.json` names. The provider is data, not
code, because principle 7 says any model must be able to perform the same
logical job and that only holds if swapping one is an edit to a file.

**Enforcement** validates the response against `reviewer.schema.json` before a
single field of it is used. `codex exec --output-schema` already enforces the
shape on its side; we check again anyway, because a response that arrives
malformed from a provider we did not write is exactly the case where trusting
the other end is how bad data gets into the package.

The validator is a deliberate subset of JSON Schema — required, type, enum,
minLength, minItems, additionalProperties, nested objects and arrays. This
project ships no third-party packages, and the alternative to a small honest
validator is no validation at all.

The reviewer is a *different* model from the one running the interview. A model
reviewing its own draft agrees with itself, and an agreement obtained that way
is worth nothing.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROVIDERS = REPO_ROOT / "registry" / "providers.json"
SCHEMAS = REPO_ROOT / "schemas"

TYPES = {"object": dict, "array": list, "string": str,
         "number": (int, float), "integer": int, "boolean": bool}


class ReviewerError(Exception):
    """Raised with a message the caller turns into an exit code."""


# ---------------------------------------------------------------------------
# Schema enforcement
# ---------------------------------------------------------------------------

def type_names(schema):
    """The declared types, always as a list. `null` arrives as a union member."""
    expected = schema.get("type")
    if expected is None:
        return []
    return expected if isinstance(expected, list) else [expected]


def accepted_types(names):
    """Python types for a list of JSON Schema type names, flattened."""
    accepted = []
    for name in names:
        wanted = TYPES.get(name)
        if wanted is None:
            continue
        accepted.extend(wanted if isinstance(wanted, tuple) else [wanted])
    return tuple(accepted)


def permits_null(schema):
    """Whether this schema accepts null, by type union or by enum member.

    Strict structured output has no notion of an absent property: every key is
    required, and optional means nullable. This is the one predicate that tells
    the two apart everywhere else in the validator.
    """
    return "null" in type_names(schema) or (
        "enum" in schema and None in schema["enum"])


def validate(payload, schema, path="$"):
    """Check payload against the schema subset. Returns a list of problems.

    Every problem names where it is. A validator that says "invalid" and
    nothing else moves the work of finding the fault onto whoever reads the
    message, and that person has less context than the validator did.
    """
    problems = []

    # Null against a nullable schema is a complete answer, not a value to
    # inspect further: minLength and minItems have nothing to measure.
    if payload is None and permits_null(schema):
        return problems

    if "enum" in schema:
        if payload not in schema["enum"]:
            allowed = ", ".join(str(v) for v in schema["enum"])
            problems.append(f"{path}: {payload!r} is not one of: {allowed}")
        return problems

    names = type_names(schema)
    if names:
        wanted = accepted_types(names)
        if wanted and not isinstance(payload, wanted):
            problems.append(f"{path}: expected {' or '.join(names)}, got "
                            f"{type(payload).__name__}")
            return problems

    if isinstance(payload, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(payload.strip()) < minimum:
            problems.append(f"{path}: is empty, and the schema requires content")

    if isinstance(payload, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(payload) < minimum:
            problems.append(f"{path}: needs at least {minimum} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(payload):
                problems += validate(item, item_schema, f"{path}[{index}]")

    if isinstance(payload, dict):
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
            if field not in payload:
                # A key that is required *and* nullable is required of the
                # provider, which emits every key by construction. A response
                # typed by hand — the --response-file path — omits it instead,
                # and omission and null mean the same thing here. Refusing it
                # would make us stricter than the schema we hand the provider.
                if permits_null(properties.get(field, {})):
                    continue
                problems.append(f"{path}: missing required field '{field}'")
        if schema.get("additionalProperties") is False:
            for field in payload:
                if field not in properties:
                    problems.append(f"{path}: unexpected field '{field}'")
        for field, sub_schema in properties.items():
            if field in payload:
                problems += validate(payload[field], sub_schema, f"{path}.{field}")

    return problems


def load_schema(name):
    path = SCHEMAS / name
    if not path.exists():
        raise ReviewerError(f"no schema at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_and_validate(text, schema_name="reviewer.schema.json"):
    """The only door a provider's answer comes through."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewerError(f"the provider did not return JSON: {exc}")

    problems = validate(payload, load_schema(schema_name))
    if problems:
        raise ReviewerError("the provider's response does not match the schema:\n  "
                            + "\n  ".join(problems))
    return payload


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def load_providers(path=None):
    candidate = Path(path or PROVIDERS)
    if not candidate.exists():
        raise ReviewerError(f"no provider configuration at {candidate}")
    return json.loads(candidate.read_text(encoding="utf-8"))


def run_provider(spec, prompt, schema_name, timeout=300):
    """Run the configured command and hand back its stdout.

    Not found on PATH is not an error here — it is the fallback's cue, and the
    caller decides. Raising for it would make an absent optional tool look like
    a broken one.
    """
    command = [part.replace("{schema}", str(SCHEMAS / schema_name))
               for part in spec["command"]]
    try:
        result = subprocess.run(command, input=prompt, capture_output=True,
                                text=True, timeout=timeout)
    except FileNotFoundError:
        raise ReviewerError(f"provider '{spec.get('name')}' is not installed")
    except subprocess.TimeoutExpired:
        raise ReviewerError(f"provider '{spec.get('name')}' did not answer in {timeout}s")

    if result.returncode != 0:
        raise ReviewerError(f"provider '{spec.get('name')}' exited {result.returncode}: "
                            f"{(result.stderr or '').strip()[:300]}")
    return result.stdout


def review(prompt, providers=None, schema_name="reviewer.schema.json",
           runner=run_provider):
    """Try the primary provider, then the fallback, and never hide which ran.

    Returns (payload, mode, failures) in every case — one shape, so a caller
    cannot accidentally unpack the success path and crash on the failure one.

    `mode` is `primary` or `skipped` here, and `claude-fallback` only where a
    deployment configured a fallback it can actually run. The Claude subagent
    named in `providers.json` is not one of those: it belongs to the host agent,
    which runs it itself and hands the result to `review --response-file --mode
    claude-fallback`. Returning `skipped` when the only fallback is host-supplied
    is therefore correct and not a missed opportunity — the host has not been
    asked yet.

    The mode is recorded in the package and shown to the Product Owner, because
    a review that silently did not happen reads exactly like one that did and
    found nothing.
    """
    config = providers if providers is not None else load_providers()
    section = config.get("reviewer", {})
    failures = []

    primary = section.get("primary")
    if primary and primary.get("command"):
        try:
            payload = parse_and_validate(runner(primary, prompt, schema_name),
                                         schema_name)
            return payload, "primary", []
        except ReviewerError as exc:
            failures.append(f"{primary.get('name')}: {exc}")

    fallback = section.get("fallback")
    if fallback and fallback.get("command"):
        try:
            payload = parse_and_validate(runner(fallback, prompt, schema_name),
                                         schema_name)
            return payload, "claude-fallback", failures
        except ReviewerError as exc:
            failures.append(f"{fallback.get('name')}: {exc}")

    return None, "skipped", failures


def build_prompt(package, lenses, kind="review"):
    """What the reviewer is given: the draft, the lenses, and the schema's shape.

    Deliberately not chatty. The reviewer's job is to find what is missing, and
    a prompt that explains at length what a good answer looks like is a prompt
    that gets its own words back.
    """
    material = json.dumps(package["material"], indent=2, ensure_ascii=False)
    header = ("You are reviewing a draft feature specification written by a "
              "different model. Your job is to find what is missing or wrong, "
              "not to agree.")
    if kind == "gap-round":
        header = ("Another pass over the same draft. Report only gaps that are "
                  "genuinely new; repeating an earlier one wastes the round.")

    return "\n\n".join([
        header,
        "Apply each of these lenses: " + ", ".join(lenses) + ".",
        "Every resolution must carry a confidence of evidence-backed or "
        "model-inference. Only evidence-backed answers close anything without "
        "a human seeing them; say model-inference when you are reasoning.",
        "Draft:", material,
        "Open questions so far: "
        + json.dumps(package.get("open_questions", []), ensure_ascii=False),
    ])
