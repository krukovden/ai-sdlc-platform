"""The tech lead is the runner: it merges only what was tested, one PR at a time.

Behind one interface, `Merger`, three implementations:

* **`AzureReposMerger`** opens the PR `pbi/…/g<N> → feature/…` with the work
  item linked and squash set, **without auto-complete**, so the merge happens
  when the runner says and not when a policy happens to clear; it completes it
  pinning `lastMergeSourceCommit` to the tested merge `M`. Azure DevOps is
  reached through the board adapter only — the adapter is the one module that
  talks to Azure DevOps (`tests/test_meta_lints.py`).
* **`GitHubMerger`** keeps those PBI merges at git level exactly as they are,
  and only changes the phase-3 hand-off: on a github.com remote the feature PR
  into the human's target branch is opened with the GitHub CLI and reused when
  it already exists.
* **`GitMerger`** is the same contract at git level for any remote with no PR
  integration here: the squash is a commit whose tree is exactly `M`'s with the
  feature head `F` as its parent, pushed with
  `--force-with-lease=<feature>:F`. If the head moved, the push loses and
  nothing merges.

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

import json
import subprocess
import urllib.parse

from .common import lease
from .gitops import GitError

MERGE_MARK = "idp-merge:"

# Never a merge target, whatever the board says. The repository's default
# branch and the profile's `protected_branches` join it at the call.
PROTECTED = ("main", "master")
HEADS = "refs/heads/"

PROTECTED_KIND = "protected"
INVALID_KIND = "invalid"
UNPROVEN_KIND = "unproven"
OPT_IN = "development.azure_complete_unproven"
EXPERIMENT = "experiment 8 (docs/experiments/README.md)"
AZURE_COMPLETE_RETRIES = 3
GH_TIMEOUT = 60


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


def protected_names(extra=()):
    """The names protected without asking the remote, folded for Azure Repos."""
    names = {branch_name(n) for n in tuple(PROTECTED) + tuple(extra or ())}
    return {n.casefold() for n in names if n}


def authoritative_default_branch(git):
    """The remote's default branch, or `MergeRefused` with a FIX line."""
    remote = getattr(git, "remote", "origin")
    try:
        default = git.default_branch()
    except GitError as exc:
        detail = exc.stderr.strip() or str(exc)
        raise MergeRefused(
            f"the runner could not ask remote '{remote}' for its default branch "
            f"(`git ls-remote --symref {remote} HEAD`): {detail}",
            f"restore access to '{remote}' and make `git ls-remote --symref {remote} HEAD` "
            "report `refs/heads/<branch>`; the runner refuses to push or merge without "
            "that authoritative answer", INVALID_KIND)
    default = branch_name(default)
    if not default:
        raise MergeRefused(
            f"remote '{remote}' did not report a default branch from HEAD, so the runner "
            "refuses to trust the clone's cached origin/HEAD",
            f"set remote HEAD so `git ls-remote --symref {remote} HEAD` reports "
            "`refs/heads/<branch>`, then retry", INVALID_KIND)
    return default


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
    protected = protected_names(extra)
    if name.casefold() in protected:
        raise MergeRefused(
            f"'{name}' is a protected branch: main, the repository's default branch or "
            "one the profile lists under protected_branches. The runner never pushes or "
            "merges into it",
            "give the Feature its own feature branch on the board (feature/<id>-<slug>); "
            "the human merges that into main after QA", PROTECTED_KIND)
    if name.casefold() == authoritative_default_branch(git).casefold():
        raise MergeRefused(
            f"'{name}' is a protected branch: main, the repository's default branch or "
            "one the profile lists under protected_branches. The runner never pushes or "
            "merges into it",
            "give the Feature its own feature branch on the board (feature/<id>-<slug>); "
            "the human merges that into main after QA", PROTECTED_KIND)
    return name


def merge_message(title, mention, branch, merged):
    return f"{title} {mention}\n\n{MERGE_MARK} {branch}@{merged}\n"


def _commit_id(value):
    if isinstance(value, dict):
        value = value.get("commitId")
    text = str(value or "").strip()
    return text or None


def _merged_commit(answer):
    for key in ("lastMergeCommit", "mergeCommit"):
        commit = _commit_id((answer or {}).get(key))
        if commit:
            return commit
    return None


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
        return {
            "id": f"git:{branch}",
            "url": None,
            "fix": "this remote has no pull-request integration here. Either use a "
                   "github.com remote, or set \"feature_pr\": \"branch-only\" under "
                   "\"development\" in .idp/profile.json when humans will merge the "
                   "feature branch by hand after QA",
        }

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


def github_remote(url):
    text = str(url or "").strip()
    if not text:
        return False
    if "://" not in text and "@" in text:
        host = text.split("@", 1)[1].split(":", 1)[0].split("/", 1)[0]
        return host.casefold() == "github.com"
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return (parts.hostname or "").casefold() == "github.com"


def _scp_host_and_path(url):
    text = str(url or "").strip()
    if "://" in text or "@" not in text:
        return None, None
    host, _, path = text.split("@", 1)[1].partition(":")
    return host.casefold(), path


def azure_remote(url):
    hosts = {
        ".".join(("ssh", "dev", "azure", "com")),
        ".".join(("vs-ssh", "visualstudio", "com")),
    }
    https = ".".join(("dev", "azure", "com"))
    text = str(url or "").strip()
    if not text:
        return False
    host, _ = _scp_host_and_path(text)
    if host is not None:
        return host in hosts
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    return host in hosts or host == https or host.endswith("." + "visualstudio.com")


def github_repository(url):
    text = str(url or "").strip().rstrip("/")
    if not github_remote(text):
        return None
    host, path = _scp_host_and_path(text)
    if host is not None:
        if host != "github.com":
            return None
    else:
        try:
            parts = urllib.parse.urlsplit(text)
        except ValueError:
            return None
        host = (parts.hostname or "").casefold()
        path = parts.path
    parts = [piece for piece in path.lstrip("/").split("/") if piece]
    if len(parts) < 2:
        return None
    owner, repository = parts[-2], parts[-1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    return f"{host}/{owner}/{repository}" if host and owner and repository else None


class GitHubMerger(GitMerger):
    """Git-level merges plus feature pull requests opened with `gh`."""

    name = "github"

    def __init__(self, git, protected=(), run=None):
        super().__init__(git, protected=protected)
        self.run = run or self._run_gh
        try:
            remote_url = getattr(git, "url", lambda: None)()
        except GitError:
            remote_url = None
        self.repository = github_repository(remote_url)

    def _run_gh(self, argv):
        return subprocess.run(argv, capture_output=True, text=True, cwd=str(self.git.path),
                              stdin=subprocess.DEVNULL, timeout=GH_TIMEOUT)

    @staticmethod
    def _read_list(answer):
        if answer.returncode != 0:
            return None
        try:
            parsed = json.loads(answer.stdout or "[]")
        except ValueError:
            return None
        if not isinstance(parsed, list):
            return None
        for found in parsed:
            if isinstance(found, dict) and found.get("number") and found.get("url"):
                return {"id": found["number"], "url": found["url"]}
        return {}

    def _failed(self, branch, detail=None):
        extra = f" ({detail})" if detail else ""
        return {
            "id": f"github:{branch}",
            "url": None,
            "fix": "make the GitHub CLI work in this clone: install `gh`, authenticate it, "
                   f"and make `gh pr list --repo <host/owner/repo> --head <branch> --base <target> "
                   f"--state open --json number,url` succeed on this github.com remote{extra}; "
                   "or set \"feature_pr\": \"branch-only\" under \"development\" in "
                   ".idp/profile.json when this repository has no PR workflow",
        }

    def _gh(self, *args):
        try:
            return self.run(["gh", *args]), None
        except OSError:
            return None, "the GitHub CLI is not installed or could not start"
        except subprocess.TimeoutExpired:
            return None, f"`gh {' '.join(args[:2])}` timed out after {GH_TIMEOUT} seconds"

    def _list(self, branch, target):
        listed, error = self._gh("pr", "list", "--repo", self.repository,
                                 "--head", branch, "--base", target, "--state", "open",
                                 "--json", "number,url")
        if listed is None:
            return None, error
        found = self._read_list(listed)
        if found is None:
            return None, "the GitHub CLI did not return valid PR JSON"
        return found, None

    def open(self, branch, target, title, description, work_item):
        if branch_name(branch).startswith("pbi/"):
            return super().open(branch, target, title, description, work_item)
        if not self.repository:
            return self._failed(
                branch,
                "the selected github.com remote does not identify an owner/repository",
            )
        found, error = self._list(branch, target)
        if found is None:
            return self._failed(branch, error)
        if found:
            return found
        created, error = self._gh("pr", "create", "--repo", self.repository,
                                  "--head", branch, "--base", target,
                                  "--title", title, "--body", description)
        if created is None:
            return self._failed(branch, error)
        if created.returncode != 0:
            return self._failed(branch, "the GitHub CLI could not create the pull request")
        found, error = self._list(branch, target)
        if found is None:
            return self._failed(branch, error)
        return found if found else self._failed(
            branch, "the pull request was not visible in GitHub after creation")


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
        answer = {}
        for _ in range(AZURE_COMPLETE_RETRIES):
            answer = self.board.get_pull_request(self.repository, pr["id"]) or {}
            if str(answer.get("status") or "").casefold() == "completed":
                break
        if str(answer.get("status") or "").casefold() != "completed":
            return None
        source = _commit_id(answer.get("lastMergeSourceCommit"))
        if source and source != merged:
            return None
        self.git.fetch()
        return _merged_commit(answer)

    def merged(self, pr, branch, target):
        if not pr:
            return None
        answer = self.board.get_pull_request(self.repository, pr["id"]) or {}
        if str(answer.get("status") or "").casefold() != "completed":
            return None
        return _merged_commit(answer)

    def comment(self, board, pr, work_item, text, key):
        self.board.comment_pull_request(self.repository, pr["id"], text)


def select(board, profile, git, repository):
    """The repository remote chooses the PR provider; the board only gates Azure calls."""
    profile = profile or {}
    protected = profile.get("protected_branches") or ()
    try:
        url = git.url()
    except (AttributeError, GitError):
        url = None
    if github_remote(url):
        return GitHubMerger(git, protected=protected)
    if profile.get("board") == "azure-devops" and hasattr(board, "create_pull_request"):
        if not azure_remote(url):
            return GitMerger(git, protected=protected)
        unproven = (profile.get("development") or {}).get("azure_complete_unproven")
        return AzureReposMerger(board, git, repository, protected=protected,
                                unproven=unproven is True)
    return GitMerger(git, protected=protected)
