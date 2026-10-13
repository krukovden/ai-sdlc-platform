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
"""

from .common import lease
from .gitops import GitError

MERGE_MARK = "idp-merge:"


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

    def __init__(self, git):
        self.git = git

    def open(self, branch, target, title, description, work_item):
        return {"id": f"git:{branch}", "url": None}

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        self.git.fetch()
        if self.git.remote_sha(target) != feature_sha:
            return None
        tree = self.git.run("rev-parse", f"{merged}^{{tree}}")
        squash = self.git.run("commit-tree", tree, "-p", feature_sha, "-m",
                              merge_message(title, mention, branch, merged))
        if not self.git.push(squash, target, expect=feature_sha):
            return None
        return squash

    def merged(self, pr, branch, target):
        self.git.fetch()
        tip = self.git.remote_sha(target)
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

    def __init__(self, board, git, repository):
        self.board, self.git, self.repository = board, git, repository

    def open(self, branch, target, title, description, work_item):
        return self.board.create_pull_request(self.repository, branch, target, title,
                                              description, [work_item])

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        self.git.fetch()
        if self.git.remote_sha(target) != feature_sha:
            return None
        self.board.complete_pull_request(self.repository, pr["id"], merged,
                                         merge_message(title, mention, branch, merged))
        self.git.fetch()
        return self.git.remote_sha(target)

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
    if (profile or {}).get("board") == "azure-devops" and \
            hasattr(board, "create_pull_request"):
        return AzureReposMerger(board, git, repository)
    return GitMerger(git)
