# Experiment 3 — the attachment round trip

Spec: "Before implementation" item 3. Script: `scripts/experiments/e3_attachment_roundtrip.py`. Status: pre-registered, not run.

## Pre-registration

**Question.** Does a Markdown file survive upload, replace and download through the adapter's REST path byte for byte, and does a replace leave exactly one relation?

**Hypotheses.**

- H1: for every test file, the sha256 of the text downloaded equals the sha256 of the text uploaded.
- H2: after replacing `design.md`, the card has exactly one `AttachedFile` relation for that title, the old relation is gone, and the download is the new content.

**Procedure.** The script creates a scratch work item (correlation id `idp-experiment-e3-<time>`). For each of six files it uploads, downloads by attachment id and compares hashes: plain ASCII with LF; Cyrillic, umlauts, CJK, an em dash and an emoji; CRLF line endings; mixed CR / LF / CRLF; no final newline; a fenced JSON block with HTML-looking text. Then it attaches `design.md`, replaces it with a second version (non-ASCII, mixed endings), counts the relations for that title before and after, and downloads the replacement. The scratch item is closed in a `finally`. Refuses to start without `--org`, `--project` and `--i-understand-this-writes`.

**n.** 6 round trips and 1 replace sequence; no repetition.

**Pass rule.** PASS only if all six hashes match, the replace leaves exactly one relation, the old relation is not among them, and the replacement downloads identical. Anything else is FAIL.

**Stop rule.** None needed: the whole run is seven operations. A failure in one case does not stop the others, so the report shows which kinds of content break.

**Limit of the test.** The adapter returns the download decoded as UTF-8, so bytes that are not valid UTF-8 cannot be compared. The platform only attaches text it wrote, so this is the right boundary.

**If it fails.** The card cannot carry `design.md` as it stands: wrap the file (base64 or a fenced container), or keep the ADR in the wiki or the repository with only a hash on the card. A replace that leaves two relations means phase 3 would refuse to start on a redesigned feature (spec: "The approval is bound to the bytes").

**Interim.** The adapter has no replace method yet. The script uses `replace_document` if the adapter has one; otherwise it attaches the new file and removes the old relations through the adapter's `_rest`. The experiment then tests that path, and the adapter should adopt it.

## Results
