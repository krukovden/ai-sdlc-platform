#!/usr/bin/env python3
"""Publish an approved Feature package to the board.

This script writes no GraphQL. It had a whole transport of its own in the
original plan, and that plan predated the tracker adapter; keeping it would
have meant two places knowing Linear by name, which is exactly the arrangement
the adapter exists to prevent. Everything here goes through `board.py`'s
adapter, so pointing the profile at Azure DevOps changes which module answers
and nothing else.

What publication is, precisely: the approved package becomes a card the next
agent can find. Three writes, and they are idempotent as a set —

    issue     the human summary plus an idp-meta block carrying correlation_id
    document  the full specification and decision trace, attached to the issue
    comment   the idp-approval block: who approved, when, of what hash

**Idempotency is by `correlation_id`, not by title.** A partial failure has to
be safe to re-run, and titles change while an idea is being argued about.

    publish_linear.py --package <path> [--dry-run]

Exit codes are the platform's, from IDE-68 §9:
    0 success · 2 auth or board unreachable · 3 malformed · 4 state conflict
    6 profile · 7 approval invalidated by a material change
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

META_BLOCK = "idp-meta"
APPROVAL_BLOCK = "idp-approval"

# The status a feature is created in. IDE-71 owns this: a card enters the
# feature route at Ready for Design, and moving it to In Design is the claim.
# The design originally said `Todo` with a stage:* label doing the claiming;
# that was reconciled away in IDE-68 §8.1 because a label swap leaves no
# history for the claim protocol to read.
CREATED_IN = "Ready for Design"


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_validator():
    """The artifact standard's own checker, reused rather than re-implemented."""
    spec = importlib.util.spec_from_file_location("idp_validate",
                                                  SCRIPTS / "validate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_validate"] = module
    spec.loader.exec_module(module)
    return module


def check_valid(package, discovery, root=None):
    """Refuse to publish an artifact that fails the standard it claims to follow.

    The checker existed before this call site did, and a checker wired to
    nothing checks nothing: a feature carrying `TODO` inside a mandatory
    section reached the board, and the human who approved it had no way to
    know the standard had not been applied.

    `root` is the repository the artifact belongs to — the product repo the
    Product Owner is working in, not this platform. It decides whether a
    relative link resolves and whether an `IDE-nn` in the text is one this
    project's mirror has heard of. Pointing it at the platform would block a
    feature for mentioning an issue that legitimately lives somewhere else.
    """
    validator = load_validator()
    text = discovery.render_markdown(package)
    try:
        violations = validator.validate_text(text, artifact_type="feature",
                                             stage="final",
                                             root=root or Path.cwd())
    except validator.ConfigError as exc:
        fail(6, f"the artifact checker is misconfigured: {exc}")

    if not violations:
        return
    for violation in violations:
        print(f"  {violation.layer:9} {violation.rule:18} {violation.message}",
              file=sys.stderr)
    fail(3, f"{len(violations)} problems in the rendered feature. Publishing it "
            "would put them on the board, where the next reader has no way to "
            "tell a gap from a decision.")


def load_board():
    """Reuse the facade rather than reimplementing it."""
    spec = importlib.util.spec_from_file_location("idp_board", SCRIPTS / "board.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_board"] = module
    spec.loader.exec_module(module)
    return module


def load_package(path):
    candidate = Path(path)
    if not candidate.exists():
        fail(3, f"no package at {candidate}")
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{candidate} is not valid JSON: {exc}")


def check_publishable(package, discovery):
    """Refuse anything the approval does not actually cover.

    An approval recorded against a different hash is not an approval of this
    package; publishing it would put a human's name on text they never read.
    """
    approval = package.get("approval")
    if not approval:
        fail(4, "the package is not approved; publication is refused")

    current = discovery.content_hash(package["material"])
    if approval.get("content_hash") != current:
        fail(7, "the material changed after approval: the recorded hash is "
                f"{approval['content_hash']}, the package now hashes to {current}. "
                "Re-approve before publishing.")

    if package["provenance"].get("reviewer_mode") == "skipped":
        print("WARNING: published without an independent review; the package "
              "says so and the Product Owner was shown it", file=sys.stderr)


def meta_block(package):
    """The machine header, carrying the identity that makes this idempotent."""
    payload = {
        "type": "feature",
        "route": "feature",
        "standard": "1.0",
        "artifact_type": package["artifact_type"],
        "cid": package["correlation_id"],
        "package_version": package["package_version"],
        "schema_version": package["schema_version"],
    }
    return "```" + META_BLOCK + "\n" + json.dumps(
        payload, indent=2, ensure_ascii=False) + "\n```"


def approval_block(package):
    approval = package["approval"]
    payload = {
        "approver": approval["approver"],
        "approved_at": approval["approved_at"],
        "content_hash": approval["content_hash"],
        "package_version": approval["package_version"],
    }
    body = "```" + APPROVAL_BLOCK + "\n" + json.dumps(
        payload, indent=2, ensure_ascii=False) + "\n```"
    return (body + "\n\nLinear cannot record who approved what, so this comment is "
            "the record. The hash is of the material subtree only: editing evidence "
            "or formatting does not void it, editing what the feature *is* does.")


def find_existing(board, project_id, correlation_id):
    """Search by correlation id, which is what makes re-publication safe.

    Matching on title instead would create a duplicate the first time somebody
    reworded the feature, and the duplicate is the thing nobody notices until
    two agents are building from two cards.

    A board that can answer this in one query says so by exposing
    `find_by_correlation` — Azure DevOps does, with a WIQL query on the
    `sdlc:cid=` tag (IDE-68 §8.3). It stays an optional facade method rather
    than a branch on the board's name, so this script still knows no tracker.
    """
    finder = getattr(board, "find_by_correlation", None)
    if finder:
        return finder(correlation_id)

    for issue in board.list_project(project_id):
        full = board.get_issue(issue["identifier"])
        if correlation_id in (full.get("description") or ""):
            return full
    return None


def title_for(package):
    problem = (package["material"].get("problem") or package["slug"]).strip()
    first = re.split(r"[.!?\n]", problem)[0].strip()
    return f"[Feature] {first[:90]}" if first else f"[Feature] {package['slug']}"


def publish(board, profile, package, discovery, dry_run=False):
    project_id = profile.get("project_id")
    if not project_id:
        fail(6, "the profile has no project_id; publication has nowhere to go")

    body = discovery.render_markdown(package) + "\n\n" + meta_block(package)
    title = title_for(package)
    existing = find_existing(board, project_id, package["correlation_id"])

    if dry_run:
        action = f"update {existing['identifier']}" if existing else "create"
        print(f"would {action}: {title}")
        print(f"  correlation_id: {package['correlation_id']}")
        print(f"  status:         {CREATED_IN}" if not existing else "  status: unchanged")
        print(f"  document:       {package['slug']} — specification")
        print(f"  comment:        {APPROVAL_BLOCK} by {package['approval']['approver']}")
        return {"identifier": existing["identifier"] if existing else None,
                "created": not existing, "dry_run": True}

    if existing:
        # Status is deliberately left alone on update. The card may already
        # have been claimed by the design agent, and re-publishing a corrected
        # specification must not yank it back into the queue.
        result = board.update_issue(existing["identifier"], title=title, body=body)
        identifier = existing["identifier"]
        created = False
    else:
        issue = board.create_issue(title=title, body=body, status=CREATED_IN,
                                   project_id=project_id)
        identifier = issue["identifier"]
        created = True

    board.attach_document(f"{package['slug']} — specification",
                          specification_document(package), identifier=identifier)
    board.add_comment(identifier, approval_block(package))
    return {"identifier": identifier, "created": created, "dry_run": False}


def specification_document(package):
    """The full specification and decision trace, as its own document.

    Separate from the card body on purpose: the card is what a human scans in a
    list, the document is what the next agent reads in full.
    """
    material = package["material"]
    lines = [f"# {package['slug']}", "",
             f"`correlation_id: {package['correlation_id']}` · "
             f"version {package['package_version']}", "",
             "## Проблема", "", material.get("problem", "") or "—", "",
             "## Результат", "", material.get("outcome", "") or "—", ""]

    for heading, key in (("Пользователи", "users"), ("Что строим", "scope"),
                         ("Чего не делаем", "non_goals"),
                         ("Ограничения", "constraints"),
                         ("Зависимости", "dependencies")):
        if material.get(key):
            lines += [f"## {heading}", ""]
            lines += [f"* {item}" for item in material[key]]
            lines.append("")

    if material.get("functional_requirements"):
        lines += ["## Требования", ""]
        for requirement in material["functional_requirements"]:
            lines.append(f"* **{requirement.get('id')}** — {requirement.get('text')}")
        lines.append("")

    if material.get("acceptance_criteria"):
        lines += ["## Критерии приёмки", ""]
        for criterion in material["acceptance_criteria"]:
            lines.append(f"* **{criterion.get('id')}** — если {criterion.get('given')}, "
                         f"когда {criterion.get('when')}, то {criterion.get('then')}")
        lines.append("")

    # Decisions and assumptions stay separate, so that reading this never
    # ratifies as decided something nobody was asked about.
    if package.get("decision_trace"):
        lines += ["## Решения Product Owner", ""]
        for entry in package["decision_trace"]:
            lines.append(f"* {entry.get('decision')} — {entry.get('rationale') or '—'}")
        lines.append("")

    if material.get("assumptions"):
        lines += ["## Допущения — их никто не подтверждал", ""]
        for assumption in material["assumptions"]:
            mark = "подтверждено" if assumption.get("validated") else "не подтверждено"
            lines.append(f"* {assumption.get('text')} ({mark})")
        lines.append("")

    if package.get("evidence"):
        lines += ["## Доказательства", ""]
        for entry in package["evidence"]:
            lines.append(f"* `{entry['id']}` {entry['uri']} — «{entry['quote']}»")
        lines.append("")

    provenance = package["provenance"]
    lines += ["## Как это получилось", "",
              f"* Независимая проверка: {provenance.get('reviewer_mode')}"
              + (f" ({provenance['reviewer']})" if provenance.get("reviewer") else ""),
              f"* Раундов поиска пробелов: {provenance.get('gap_rounds_run')}",
              f"* Исследование практик: {provenance.get('practice_research')}"]
    if provenance.get("gap_search_truncated"):
        lines.append("* **Поиск остановлен лимитом, а не исчерпанием** — это не полный поиск")

    if package.get("open_questions"):
        lines += ["", "## Осталось открытым", ""]
        for question in package["open_questions"]:
            lines.append(f"* {question['text']} — риск {question.get('risk')}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be written, and write nothing")
    parser.add_argument("--root", default=None,
                        help="the repository this feature belongs to; links and "
                             "IDE-nn are resolved against it. Default: the "
                             "current directory")
    parser.add_argument("--skip-validation", action="store_true",
                        help="publish without checking the artifact against the "
                             "standard. Journalled, and it is a hole, not a flag")
    args = parser.parse_args(argv)

    board_module = load_board()
    discovery = load_discovery()
    package = load_package(args.package)
    check_publishable(package, discovery)
    if args.skip_validation:
        print("WARNING: published without checking it against the standard.",
              file=sys.stderr)
    else:
        check_valid(package, discovery, root=args.root)

    profile, _, board = board_module.open_board()
    result = publish(board, profile, package, discovery, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"{result['identifier']}  {'created' if result['created'] else 'updated'}")
    return result


def load_discovery():
    spec = importlib.util.spec_from_file_location(
        "idp_discovery", Path(__file__).resolve().parent / "discovery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_discovery"] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    main()
