"""The tech lead is the runner: it merges only what was tested, one PR at a time.

Behind one interface, `Merger`, two implementations:

* **`AzureReposMerger`** opens the PR `pbi/…/g<N> → feature/…` with the work
  item linked and squash set, **without auto-complete**, so the merge happens
  when the runner says and not when a policy happens to clear; it completes it
  pinning `lastMergeSourceCommit` to the tested merge `M`. Azure DevOps is
  reached through the board adapter only — the adapter is the one module that
  talks to Azure DevOps (`tests/test_meta_lints.py`).
* **`GitMerger`** is the same contract at git level, for Linear (a GitHub PR is
  optional and not opened here) and for any board whose adapter has no PR
  calls: the squash is a commit whose tree is exactly `M`'s with the feature
  head `F` as its parent, pushed with `--force-with-lease=<feature>:F`. If the
  head moved, the push loses and nothing merges.

Both answer `complete` with the new feature head, or `None` when the feature
head is no longer `F` — the caller re-tests on the new head and tries again.

**Neither ever merges into a protected branch.** Merging into `main` is never
done by the runner (IDE-147, *The runner*), and a branch name read from the
board is data, not a promise: each implementation refuses `main`, the
repository's actual default branch and every branch the profile lists under
`protected_branches` on its own, whatever its caller checked before it. The
answer is `MergeRefused`, never a merge.

**Azure Repos completion is refused until it is proven.** The completion call
pins the source and nothing pins the target, so a sibling landing in the window
merges a combination nobody tested. Until experiment 8 shows the service
refuses that, `AzureReposMerger.complete` leaves the pull request open and says
so; a profile opts in with `"development": {"azure_complete_unproven": true}`,
and then the runner's post-merge parent check is what contains the race.
"""

from .common import lease
from .gitops import GitError

MERGE_MARK = "idp-merge:"

# Never a merge target, whatever the board says. The repository's default
# branch and the profile's `protected_branches` join it at the call.
PROTECTED = ("main",)
HEADS = "refs/heads/"

PROTECTED_KIND = "protected"
INVALID_KIND = "invalid"
UNPROVEN_KIND = "unproven"
OPT_IN = "development.azure_complete_unproven"
EXPERIMENT = "experiment 8 (docs/experiments/README.md)"


class MergeRefused(Exception):
    """A merge this side will not make. `kind` is one of the three above."""

    def __init__(self, message, fix, kind):
        super().__init__(message)
        self.message, self.fix, self.kind = message, fix, kind


def branch_name(name):
    """A branch as the runner writes it: whitespace and every `refs/heads/` off."""
    text = str(name or "").strip()
    while text.startswith(HEADS):
        text = text[len(HEADS):].strip()
    return text


def protected_names(git, extra=()):
    """`main`, the remote's default branch, and the profile's list — folded.

    Folded because Azure Repos treats branch names without case: `Main` is the
    branch `main` there.
    """
    names = {branch_name(n) for n in tuple(PROTECTED) + tuple(extra or ())}
    try:
        default = git.default_branch()
    except GitError:
        default = None
    if default:
        names.add(branch_name(default))
    return {n.casefold() for n in names if n}


def writable_target(target, git, extra=()):
    """The target as a plain branch name, or `MergeRefused`.

    Refused: anything that is not a branch (`refs/tags/…`, `HEAD`, a name git
    rejects), `main`, the repository's default branch, and the profile's
    `protected_branches`.
    """
    name = branch_name(target)
    if not name or name == "HEAD" or name.startswith("refs/") or \
            not git.ok("check-ref-format", "--branch", name):
        raise MergeRefused(
            f"'{target}' is not a branch the runner may write to",
            "give the Feature a plain feature branch name on the board, such as "
            "feature/<id>-<slug>; the runner writes only to that branch and to "
            "pbi/* branches", INVALID_KIND)
    if name.casefold() in protected_names(git, extra):
        raise MergeRefused(
            f"'{name}' is a protected branch: main, the repository's default branch or "
            "one the profile lists under protected_branches. The runner never pushes or "
            "merges into it",
            "give the Feature its own feature branch on the board (feature/<id>-<slug>); "
            "the human merges that into main after QA", PROTECTED_KIND)
    return name


def merge_message(title, mention, branch, merged):
    return f"{title} {mention}\n\n{MERGE_MARK} {branch}@{merged}\n"


class Merger:
    name = None

    def open(self, branch, target, title, description, work_item):
        raise NotImplementedError

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        raise NotImplementedError

    def merged(self, pr, branch, target):
        """The feature head carrying this branch's squash, or None."""
        raise NotImplementedError

    def comment(self, board, pr, work_item, text, key):
        raise NotImplementedError


class GitMerger(Merger):
    """Pull requests as git-level merges into the feature branch."""

    name = "git"

    def __init__(self, git, protected=()):
        self.git = git
        self.protected = tuple(protected or ())

    def open(self, branch, target, title, description, work_item):
        return {"id": f"git:{branch}", "url": None}

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        # No look before the leap: the push is a compare-and-swap on F, so a head
        # that moved makes it lose, which is the only check that cannot race.
        target = writable_target(target, self.git, self.protected)
        tree = self.git.run("rev-parse", f"{merged}^{{tree}}")
        squash = self.git.run("commit-tree", tree, "-p", feature_sha, "-m",
                              merge_message(title, mention, branch, merged))
        if not self.git.push(squash, target, expect=feature_sha):
            return None
        return squash

    def merged(self, pr, branch, target):
        self.git.fetch()
        tip = self.git.tracking(target)
        if tip is None:
            return None
        try:
            found = self.git.log_grep(tip, f"{MERGE_MARK} {branch}@")
        except GitError:
            return None
        return found.splitlines()[0] if found else None

    def comment(self, board, pr, work_item, text, key):
        lease.post_event(board, work_item, "review-comment", key, text=text)


class AzureReposMerger(Merger):
    """Azure Repos pull requests, through the board adapter's PR calls."""

    name = "azure-repos"

    def __init__(self, board, git, repository, protected=(), unproven=False):
        self.board, self.git, self.repository = board, git, repository
        self.protected = tuple(protected or ())
        self.unproven = bool(unproven)

    def open(self, branch, target, title, description, work_item):
        return self.board.create_pull_request(self.repository, branch, target, title,
                                              description, [work_item])

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        """Complete, pinned to M — with a window this side cannot close.

        `lastMergeSourceCommit` pins the source; nothing in the completion call
        pins the target. So the target head is asked of the remote immediately
        before the call (one `ls-remote`, no fetch in between), and the caller
        checks afterwards that the squash's parent is the tested F. Whether the
        service itself refuses a completion onto a moved target is unmeasured
        (CLAUDE.md, Known gaps), so this refuses by default: `MergeRefused` of
        kind `unproven`, and the pull request stays open, until the profile
        opts in.
        """
        target = writable_target(target, self.git, self.protected)
        if not self.unproven:
            raise MergeRefused(
                f"Azure Repos completion of {branch} into {target} is refused: the "
                "completion call pins only the source commit, nothing pins the target, "
                f"and whether the service refuses a moved target is {EXPERIMENT}, not "
                "yet run. The pull request stays open",
                f"complete the pull request yourself once `{target}` is still at "
                f"{feature_sha[:12]}, then remove idp:blocked; or, accepting the window, "
                f"set \"{OPT_IN.split('.')[1]}\": true under \"development\" in "
                ".idp/profile.json", UNPROVEN_KIND)
        if self.git.remote_sha(target) != feature_sha:
            return None
        self.board.complete_pull_request(self.repository, pr["id"], merged,
                                         merge_message(title, mention, branch, merged))
        self.git.fetch()
        return self.git.tracking(target)

    def merged(self, pr, branch, target):
        if not pr:
            return None
        answer = self.board.get_pull_request(self.repository, pr["id"]) or {}
        if str(answer.get("status") or "").casefold() != "completed":
            return None
        self.git.fetch()
        return self.git.remote_sha(target)

    def comment(self, board, pr, work_item, text, key):
        self.board.comment_pull_request(self.repository, pr["id"], text)


def select(board, profile, git, repository):
    """Azure Repos when the board is Azure DevOps and its adapter has the PR calls."""
    profile = profile or {}
    protected = profile.get("protected_branches") or ()
    if profile.get("board") == "azure-devops" and hasattr(board, "create_pull_request"):
        unproven = (profile.get("development") or {}).get("azure_complete_unproven")
        return AzureReposMerger(board, git, repository, protected=protected,
                                unproven=unproven is True)
    return GitMerger(git, protected=protected)
