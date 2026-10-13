"""The board shape planning always creates, and the work list building reads back.

    Feature
     ├─ PBI 1 ─ Task 1.1, 1.2, …        one Task per brief step
     ├─ PBI 2 ─ Task 2.1, …
     └─ PBI "QA Testing"                depends on every other PBI
          ├─ Task "Write test cases and Playwright"   the tester
          └─ Task "Run the feature's tests"           the platform

(IDE-147, 3.1.) Every item carries an `idp-meta` block with its key, and a
re-run matches on the key — never the title — so running planning twice creates
nothing the second time.

Two board differences are handled here and said once:

* **Linear has no Task kind.** A Task becomes a sub-issue of its PBI, which is
  what Linear offers; it is still found by its key.
* An adapter whose `create_issue` takes no `kind` creates what it creates; the
  item is still found by its key.

The plan's witnesses belong to acceptance criteria, not to steps: `plan.schema`
gives a Task `not_done_if` and deliverables, and a PBI's criteria their witness.
So each Task is given **its own witnesses** by one deterministic rule,
`task_witnesses`, and the assignment is written into the Task's `idp-meta`
block at planning, where every later run reads it back. A Task closes only when
its own witnesses passed at finalize, one Task after another (IDE-147, *Who
moves what*); a Task whose witness is `{manual}` or `{evidence}` is never
closed by the runner.
"""

import inspect
import json
import re

from .common import planning_module
from .plan import witnesses

META = re.compile(r"```idp-meta\s*\n(.*?)\n```", re.DOTALL)
QA_KEY = "qa-testing"
QA_TITLE = "QA Testing"
QA_TASKS = (
    ("write-tests", "Write test cases and Playwright",
     "The tester writes the test cases from the Feature's acceptance criteria and the "
     "Playwright code in the QA repository, every test tagged `@ado-<feature-id>`, and "
     "closes this Task."),
    ("run-tests", "Run the feature's tests",
     "The platform finds every test tagged with the Feature's id and runs it against the "
     "QA environment. Zero tests found is a failure, never green."),
)


def read_meta(description):
    match = META.search(description or "")
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def meta_block(payload):
    return "```idp-meta\n" + json.dumps(payload, indent=2, ensure_ascii=False) + "\n```"


def session_for(feature_issue, profile, plan, header):
    """What the planning renderers need, rebuilt from the card: never from a cache."""
    planning = planning_module()
    return {
        "feature": feature_issue["identifier"],
        "branch": (feature_issue.get("branch") or "").strip(),
        "route": header.get("route") or "feature",
        "cid": header.get("cid"),
        "language": planning.load_sections().language_of(profile or {}),
        "notes": (plan or {}).get("notes"),
    }


def create_item(board, title, body, parent, status, project_id, kind):
    """`create_issue` with the kind, where the adapter takes one and the board has it."""
    if kind == "task" and getattr(board, "kind_of", None) and board.kind_of("task") is None:
        kind = "pbi"                      # Linear: a sub-issue of the PBI
    kwargs = {"title": title, "body": body, "parent": parent, "status": status,
              "project_id": project_id}
    if "kind" in inspect.signature(board.create_issue).parameters:
        kwargs["kind"] = kind
    return board.create_issue(**kwargs)["identifier"]


def status_for(todo_status, kind):
    if isinstance(todo_status, dict):
        return todo_status.get(kind) or todo_status.get("pbi")
    return todo_status


def children_by_key(board, parent):
    found = {}
    for child in board.list_children(parent):
        full = board.get_issue(child["identifier"])
        key = read_meta(full.get("description")).get("key")
        if key:
            found[key] = full
    return found


def task_witnesses(pbi):
    """{task key: [AC ids]} — which of the PBI's witnesses prove which brief step.

    The plan pairs steps and criteria by order: step j owns criterion j, the last
    step also owns every criterion past the last step, and a step past the last
    criterion owns the last one. Every step owns at least one witness and every
    witness has an owner, so no Task can close without a witness of its own.
    """
    tasks = [t["key"] for t in pbi.get("tasks") or []]
    criteria = [c[0] for c in witnesses(pbi)]
    owned = {key: [] for key in tasks}
    if not tasks or not criteria:
        return owned
    for index, key in enumerate(tasks):
        owned[key].append(criteria[min(index, len(criteria) - 1)])
    owned[tasks[-1]] += criteria[len(tasks):]
    return owned


def task_body(session, pbi_key, task, criteria, order, owned=None):
    owned = list(owned if owned is not None else [c[0] for c in criteria])
    criteria = [c for c in criteria if c[0] in owned]
    lines = ["---", "type: task", 'standard: "1.0"', f"parent: {session['feature']}", "---",
             "", task["title"], "", f"NOT DONE if: {task['not_done_if']}",
             "Existing: " + (", ".join(task.get("existing") or []) or "none"),
             "Deliverables: " + "; ".join(task["deliverables"])]
    for path in task.get("delete") or []:
        lines.append(f"Delete: {path}")
    if criteria:
        lines += ["", "This step is done when these witnesses pass at finalize; they are "
                      "read from the plan committed at planning:"]
        for identifier, _, witness in criteria:
            lines.append(f"- {identifier}: " + json.dumps(witness, ensure_ascii=False,
                                                         sort_keys=True))
    lines += ["", meta_block({"type": "task", "feature": session["feature"],
                              "pbi": pbi_key, "key": task["key"], "order": order,
                              "witnesses": owned}), ""]
    return "\n".join(lines)


def qa_body(session, keys):
    return "\n".join([
        "---", "type: pbi", 'standard: "1.0"', f"parent: {session['feature']}", "---", "",
        "The human's QA of the whole Feature. It depends on every other PBI, so while it "
        "is the one open PBI the board says: everything written, nothing tested yet. The "
        "runner never starts a worker on it.", "",
        meta_block({"type": "pbi", "feature": session["feature"], "key": QA_KEY,
                    "qa": True, "depends_on": list(keys)}), ""])


def qa_task_body(session, key, text, order):
    return "\n".join(["---", "type: task", 'standard: "1.0"',
                      f"parent: {session['feature']}", "---", "", text, "",
                      meta_block({"type": "task", "feature": session["feature"],
                                  "pbi": QA_KEY, "key": f"{QA_KEY}/{key}", "order": order}),
                      ""])


def _upsert(board, existing, key, title, body, parent, status, project_id, kind, made):
    known = existing.get(key)
    if known:
        if (known.get("description") or "").strip() != body.strip() or \
                (known.get("title") or "") != title:
            board.update_issue(known["identifier"], title=title, body=body)
        return known["identifier"]
    identifier = create_item(board, title, body, parent, status, project_id, kind)
    made.append(identifier)
    return identifier


def ensure(board, feature_issue, plan, profile, header, todo_status=None):
    """Create whatever of the shape is missing; change nothing that is already right.

    Returns {key: identifier} for the PBIs, the QA Testing PBI included.
    """
    planning = planning_module()
    session = session_for(feature_issue, profile, plan, header)
    feature = session["feature"]
    project = (profile or {}).get("project_id")
    order = planning.topological(planning.build_graph(plan["pbis"]))
    by_key = {p["key"]: p for p in plan["pbis"]}
    existing = children_by_key(board, feature)
    made, ids = [], {}

    for key in order:
        pbi = by_key[key]
        ids[key] = _upsert(board, existing, key, f"[PBI] {pbi['title']}",
                           planning.render_card(session, pbi), feature,
                           status_for(todo_status, "pbi"),
                           project, "pbi", made)
        board.attach_document(planning.brief_title(ids[key]),
                              planning.render_brief(session, pbi), identifier=ids[key])
        tasks = children_by_key(board, ids[key])
        criteria = witnesses(pbi)
        owners = task_witnesses(pbi)
        for index, task in enumerate(pbi.get("tasks") or [], 1):
            _upsert(board, tasks, task["key"], f"[Task] {task['title']}",
                    task_body(session, key, task, criteria, index, owners[task["key"]]),
                    ids[key], status_for(todo_status, "task"), project, "task", made)

    ids[QA_KEY] = _upsert(board, existing, QA_KEY, f"[PBI] {QA_TITLE}",
                          qa_body(session, order), feature, status_for(todo_status, "pbi"),
                          project, "pbi", made)
    tasks = children_by_key(board, ids[QA_KEY])
    for index, (key, title, text) in enumerate(QA_TASKS, 1):
        _upsert(board, tasks, f"{QA_KEY}/{key}", f"[Task] {title}",
                qa_task_body(session, key, text, index), ids[QA_KEY],
                status_for(todo_status, "task"), project, "task", made)
    return ids, made


# ---------------------------------------------------------------------------
# The work list
# ---------------------------------------------------------------------------

def is_qa(item):
    return item.get("key") == QA_KEY or bool(item.get("meta", {}).get("qa"))


def work_items(board, feature, plan=None):
    """Every child PBI and Bug of the Feature, with its key, deps and labels.

    Dependencies come from the pinned plan when the item is in it, and from the
    card's own header otherwise; a Bug filed by a human has none.
    """
    items = []
    for child in board.list_children(feature):
        full = board.get_issue(child["identifier"])
        meta = read_meta(full.get("description"))
        if meta.get("type") == "task" or full.get("kind") == "task":
            continue
        key = meta.get("key") or str(full["identifier"])
        planned = None
        for pbi in (plan or {}).get("pbis") or []:
            if pbi.get("key") == key:
                planned = pbi
        depends = list((planned or {}).get("depends_on") or meta.get("depends_on") or [])
        items.append({"id": str(full["identifier"]), "key": key, "meta": meta,
                      "title": full.get("title") or "", "issue": full,
                      "labels": list(full.get("labels") or []), "depends_on": depends,
                      "planned": planned})
    return items


def tasks_of(board, pbi):
    found = []
    for child in board.list_children(pbi):
        full = board.get_issue(child["identifier"])
        meta = read_meta(full.get("description"))
        found.append((meta.get("order") or 0, str(full["identifier"]), full))
    return [(identifier, full) for _, identifier, full in sorted(found)]
