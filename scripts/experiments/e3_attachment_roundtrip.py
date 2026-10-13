#!/usr/bin/env python3
"""Experiment 3 — the attachment round trip (IDE-147, "Before implementation" item 3).

Upload, replace and download a Markdown `design.md` through the adapter's REST
path (IDE-137 is the precedent for why this is tested rather than assumed).
The bytes that come back must hash identically to the bytes that went up —
including non-ASCII text, CRLF line endings and a missing final newline — and a
replace must leave **exactly one** relation for the title, with the old one
removed.

The pre-registration is `docs/experiments/e3-attachment-roundtrip.md`; the
verdict is appended to its Results section.

    python3 scripts/experiments/e3_attachment_roundtrip.py \\
        --org <organisation> --project <project> --i-understand-this-writes
"""

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment_common as common  # noqa: E402

EXIT = common.EXIT
DOC = common.DOCS_DIR / "e3-attachment-roundtrip.md"
PASS, FAIL = "PASS", "FAIL"

DESIGN = "design.md"

# Each case is (name, text). The text is what a design document really contains.
CASES = (
    ("ascii-lf", "# Design\n\nPlain ASCII.\n\n- one\n- two\n"),
    ("non-ascii", "# Дизайн\n\nКириллица, ümlauts, 日本語, an em dash — and an emoji 🚀.\n"),
    ("crlf", "# Design\r\n\r\nWindows line endings.\r\n\r\n- one\r\n- two\r\n"),
    ("mixed-endings", "line one\r\nline two\nline three\rline four\n"),
    ("no-final-newline", "# Design\n\nNo newline at the end"),
    ("fenced-and-html", "# Design\n\n```json\n{\"a\": \"<b>&amp;</b>\"}\n```\n\n<script>x</script>\n"),
)

REPLACED = "# Design v2\n\nReplaced — Дизайн 2.\r\nSecond revision.\n"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def roundtrip_case(scratch, name, text):
    """Upload one file, download it, compare hashes."""
    made = scratch.attach(f"e3-{name}.md", text)
    back = scratch.download(made["slugId"])
    sent, got = sha256(text), sha256(back)
    return {"case": name, "sent": sent, "got": got, "match": sent == got,
            "bytes_sent": len(text.encode("utf-8")), "bytes_got": len(back.encode("utf-8"))}


def relations_for(scratch, title):
    name = common.adapter.attachment_name(title)
    return [r for r in scratch.relations() if r["comment"] == title or r["name"] == name]


def replace_check(scratch, first, second):
    """Attach `design.md`, replace it, and look at what is left on the card."""
    scratch.attach(DESIGN, first)
    before = relations_for(scratch, DESIGN)
    made = scratch.replace(DESIGN, second)
    after = relations_for(scratch, DESIGN)
    back = scratch.download(made["slugId"])
    old_urls = {r["url"] for r in before}
    return {"relations_before": len(before), "relations_after": len(after),
            "old_relation_gone": not (old_urls & {r["url"] for r in after}),
            "download_match": sha256(back) == sha256(second),
            "sent": sha256(second), "got": sha256(back)}


def verdict_of(cases, replaced):
    if not all(c["match"] for c in cases):
        return FAIL
    if replaced["relations_after"] != 1 or not replaced["old_relation_gone"] \
            or not replaced["download_match"]:
        return FAIL
    return PASS


def render(cases, replaced, verdict, scratch_id):
    lines = [f"- scratch card: {scratch_id} (correlation id prefix `{common.EXPERIMENT_MARK}-e3-`), "
             "closed at the end", "- round trips (sha256 of the bytes sent vs. the bytes downloaded):"]
    for c in cases:
        mark = "identical" if c["match"] else f"DIFFERENT ({c['bytes_sent']} → {c['bytes_got']} bytes)"
        lines.append(f"  - {c['case']}: {mark}")
    lines.append(f"- replace: {replaced['relations_before']} relation before, "
                 f"{replaced['relations_after']} after (must be 1); old relation gone: "
                 f"{replaced['old_relation_gone']}; replaced content identical: "
                 f"{replaced['download_match']}")
    if verdict == FAIL:
        lines.append("- CONSEQUENCE: the card cannot carry `design.md` as it stands. Either the "
                     "bytes do not survive (wrap the file — base64 or a fenced container — or keep "
                     "the ADR in the wiki or the repository with only a hash on the card) or a "
                     "redesign leaves two ADRs side by side, which phase 3 refuses to start on.")
    else:
        lines.append("- the approval can be bound to the attachment's bytes, as designed.")
    return common.result_block("E3 attachment round trip", verdict, lines)


def run(scratch):
    cases = [roundtrip_case(scratch, name, text) for name, text in CASES]
    replaced = replace_check(scratch, CASES[0][1], REPLACED)
    return cases, replaced


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common.add_target_arguments(parser)
    parser.add_argument("--no-write-results", action="store_true",
                        help="print the verdict without appending it to the pre-registration")
    args = parser.parse_args(argv)

    common.require_explicit(args)
    board = common.connect(args)
    scratch = common.AdoScratch(board, "e3", close_state=args.close_state)
    item = scratch.create()
    print(f"scratch work item {item} created")
    try:
        cases, replaced = run(scratch)
    finally:
        scratch.close()
        print(f"scratch work item {item} moved to {args.close_state}")

    verdict = verdict_of(cases, replaced)
    block = render(cases, replaced, verdict, item)
    print(block)
    if not args.no_write_results:
        print(f"results appended to {common.append_results(DOC, block)}")
    return EXIT.OK if verdict == PASS else EXIT.NO


if __name__ == "__main__":
    sys.exit(main())
