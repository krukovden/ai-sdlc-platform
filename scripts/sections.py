#!/usr/bin/env python3
"""Section identity, and the words a section is written in — two facts (IDE-132).

Every artifact this platform produced was Russian, and not as a matter of
wording: `lint/*.jsonc` and `scripts/validate.py` keyed their required-heading
checks off the same Russian literals the renderers emitted. An English artifact
therefore failed the platform's own validator, which IDE-111 wired in ahead of
publication. The language was the schema.

So code names **ids** — `why`, `what`, `evidence`, `cost` — and this module is
the only thing that turns an id into a heading. The table lives in
`registry/sections.json` next to the other registries.

Which language a project writes in is a property of its **audience**, not of the
platform, so it is a profile setting: `"language": "en"`. English is the
default because the first field project is read by a team that does not read
Russian, and because `CLAUDE.md` has said "artifacts and code in English" from
the start.

Reading is deliberately more forgiving than writing. `detect` recognises a
document by the headings it actually carries, so the Russian artifacts already
on the board keep validating after the default moves to English — nothing is
migrated, and nothing has to be.

Usage:
    sections.py --list [--language ru]      every id and its heading
    sections.py --required feature          the MD043 list for that type
    sections.py --check-lint                lint/*.jsonc agrees with this table
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TABLE_PATH = REPO_ROOT / "registry" / "sections.json"
LINT_DIR = REPO_ROOT / "lint"

# MD043's wildcard: zero or more free headings. A rich document adds its own
# sections between the mandatory ones, which is what makes a five-section
# skeleton liveable for a design document.
WILDCARD = "*"

_TABLE = None


class SectionError(ValueError):
    """A section id or language this table does not define. Named so a caller
    can turn it into an exit code instead of a traceback."""


def table(path=None):
    """The parsed table, read once."""
    global _TABLE
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if _TABLE is None:
        _TABLE = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    return _TABLE


def languages():
    return list(table()["languages"])


def default_language():
    return table()["default_language"]


def language_of(profile=None):
    """What this project writes in.

    A property of the audience, not of the platform — Hanwha is English, and a
    project whose readers are Russian says so in one line of its profile.
    """
    configured = str((profile or {}).get("language") or "").strip()
    if not configured:
        return default_language()
    if configured not in languages():
        raise SectionError(
            f"the profile asks for language '{configured}'; this platform has "
            f"headings for {', '.join(languages())}. Add them to "
            f"registry/sections.json, or pick one of those.")
    return configured


def text(section_id, language=None):
    """The words, without the `##`. For a label rather than a heading."""
    if str(section_id).startswith("//"):
        raise SectionError("'//' is a note in the table, not a section id")
    words = table()["headings"].get(section_id)
    if not words:
        known = ", ".join(sorted(table()["headings"]))
        raise SectionError(f"no section '{section_id}'. Known ids: {known}")
    language = language or default_language()
    if language not in words:
        raise SectionError(
            f"section '{section_id}' has no '{language}' heading in "
            f"registry/sections.json")
    return words[language]


def heading(section_id, language=None, level=2):
    """`## What we build` — the rendered heading, which only this module writes."""
    return f"{'#' * level} {text(section_id, language)}"


def phrase(phrase_id, language=None, **values):
    """One sentence a renderer puts inside an artifact.

    Headings were never the whole of it: a document whose sections are English
    and whose sentences are Russian is no more readable to the team it was
    published for. Same table, same rule — code names the id, this resolves the
    words (IDE-132).
    """
    words = (table().get("phrases") or {}).get(phrase_id)
    if not isinstance(words, dict):
        known = ", ".join(sorted(k for k in (table().get("phrases") or {})
                                 if not k.startswith("//")))
        raise SectionError(f"no phrase '{phrase_id}'. Known ids: {known}")
    language = language or default_language()
    if language not in words:
        raise SectionError(
            f"phrase '{phrase_id}' has no '{language}' wording in "
            f"registry/sections.json")
    try:
        return words[language].format(**values)
    except KeyError as exc:
        raise SectionError(
            f"phrase '{phrase_id}' needs {exc} and was not given it")


def catalogue(name, language=None):
    """An ordered list from the table: `candidate_artifacts`, `axes`.

    Same rule as everything else here — the id is the identity, the words are
    one rendering of it. Returns `[(id, words), ...]` so a caller that needs
    both keeps both.
    """
    entries = table().get(name)
    if not isinstance(entries, dict):
        raise SectionError(f"no catalogue '{name}' in registry/sections.json")
    language = language or default_language()
    return [(key, value[language]) for key, value in entries.items()
            if not key.startswith("//")]


def artifact(artifact_type):
    definition = table()["artifacts"].get(artifact_type)
    if definition is None:
        known = ", ".join(sorted(table()["artifacts"]))
        raise SectionError(f"no artifact type '{artifact_type}'. Known: {known}")
    return definition


def required_ids(artifact_type):
    return list(artifact(artifact_type)["required"])


def required_headings(artifact_type, language=None):
    """The MD043 list: every mandatory heading, wildcards between them."""
    result = []
    for section_id in required_ids(artifact_type):
        result.extend([heading(section_id, language), WILDCARD])
    return result


def criteria_heading(artifact_type, language=None):
    """Which section holds the acceptance criteria, or None where there are none.

    `pbi-agent` is the None case and must stay one: criteria live on the card,
    where a human sees them.
    """
    section_id = artifact(artifact_type).get("criteria")
    return heading(section_id, language) if section_id else None


def deferred_headings(artifact_type, language=None):
    """Mandatory sections a draft may still leave empty (IDE-78's status rule)."""
    return tuple(heading(section_id, language)
                 for section_id in artifact(artifact_type).get("deferred_at_draft", []))


def detect(document_headings, artifact_type=None, fallback=None):
    """Which language this document is written in, judged by what it carries.

    Writing follows the profile; reading follows the document. That asymmetry is
    the whole reason the Russian artifacts already on the board keep validating
    after the default moves to English — a migration nobody has to run.

    Ties go to `fallback`, then to the default: a document with no recognisable
    heading is not evidence of anything.
    """
    carried = {str(h).strip().casefold() for h in document_headings or []}
    if not carried:
        return fallback or default_language()

    wanted = (required_ids(artifact_type) if artifact_type
              else [key for key in table()["headings"] if not key.startswith("//")])
    best, best_score = None, 0
    for language in languages():
        score = sum(1 for section_id in wanted
                    if heading(section_id, language).casefold() in carried)
        if score > best_score:
            best, best_score = language, score
    return best or fallback or default_language()


# ---------------------------------------------------------------------------
# The lint configs are a mirror of this table, and a test refuses to let them
# drift. They stay hand-written because their comments explain why there are six
# of them, and a generator would either lose that or have to carry it as data.
# ---------------------------------------------------------------------------

def lint_headings(artifact_type, lint_dir=None):
    """The MD043 list as `lint/<type>.jsonc` currently states it."""
    path = Path(lint_dir or LINT_DIR) / f"{artifact_type}.jsonc"
    if not path.exists():
        raise SectionError(f"no lint config at {path}")
    text_ = path.read_text(encoding="utf-8")
    stripped = _strip_jsonc(text_)
    md043 = (json.loads(stripped).get("MD043") or {})
    return [str(entry) for entry in md043.get("headings", [])]


def _strip_jsonc(source):
    """`//` comments, without touching one inside a string."""
    out, in_string, escaped, index = [], False, False, 0
    while index < len(source):
        character = source[index]
        if in_string:
            out.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            out.append(character)
            index += 1
            continue
        if source[index:index + 2] == "//":
            while index < len(source) and source[index] != "\n":
                index += 1
            continue
        out.append(character)
        index += 1
    return "".join(out)


def check_lint(lint_dir=None):
    """Every disagreement between this table and the committed lint configs."""
    problems = []
    for artifact_type in sorted(table()["artifacts"]):
        wanted = required_headings(artifact_type, default_language())
        try:
            found = lint_headings(artifact_type, lint_dir)
        except SectionError as exc:
            problems.append(str(exc))
            continue
        if found != wanted:
            problems.append(
                f"lint/{artifact_type}.jsonc: MD043 says {found!r}, "
                f"registry/sections.json says {wanted!r}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="every id and its heading")
    parser.add_argument("--required", metavar="TYPE", help="the MD043 list for a type")
    parser.add_argument("--language", help=f"one of {', '.join(languages())}")
    parser.add_argument("--check-lint", action="store_true",
                        help="verify lint/*.jsonc against this table")
    args = parser.parse_args()

    try:
        language = args.language or default_language()
        if args.check_lint:
            problems = check_lint()
            for problem in problems:
                print(problem, file=sys.stderr)
            print("lint configs agree with registry/sections.json" if not problems
                  else f"{len(problems)} disagreement(s)")
            return 3 if problems else 0
        if args.required:
            for entry in required_headings(args.required, language):
                print(entry)
            return 0
        for section_id in table()["headings"]:
            print(f"{section_id:20} {heading(section_id, language)}")
        return 0
    except SectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())
