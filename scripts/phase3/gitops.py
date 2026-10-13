"""Git, the way phase 3 uses it: the remote is the authority, every base is a SHA.

Nothing here resolves a branch name late. A worktree starts from a SHA recorded
in a claim comment; a merge is decided against a SHA read from the remote; a
push that would overwrite something it did not see is a compare-and-swap that
loses rather than wins (`--force-with-lease=<ref>:<expected>`).

A failure is a `GitError`. Whether it is infrastructure — the network, the
remote, a lock — or the work itself is the caller's question: a merge conflict
is the work's, a refused fetch is not, and finalize routes them differently.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .common import EXIT

GIT_TIMEOUT = 600


class GitError(Exception):
    def __init__(self, args, stderr, code=None):
        super().__init__(f"git {' '.join(args)}: {stderr.strip()}")
        self.args_, self.stderr, self.code = args, stderr, code


def slug(text, limit=40):
    words = re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")
    return words[:limit].strip("-") or "work"


def mention(identifier):
    """How a commit names a work item: `#123` on Azure Repos, the key elsewhere."""
    text = str(identifier)
    return f"#{text}" if text.isdigit() else text


class Git:
    """One repository: a clone of the product repo, or a worktree of it."""

    def __init__(self, path, remote="origin", env=None):
        self.path = Path(path)
        self.remote = remote
        self.env = env

    def run(self, *args, check=True, input_text=None):
        answer = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                                text=True, input=input_text, timeout=GIT_TIMEOUT,
                                env=self.env,
                                stdin=None if input_text is not None else subprocess.DEVNULL)
        if check and answer.returncode != EXIT.OK:
            raise GitError(args, answer.stderr or answer.stdout, answer.returncode)
        return answer.stdout.strip() if check else answer

    def ok(self, *args):
        return self.run(*args, check=False).returncode == EXIT.OK

    # -- the remote --------------------------------------------------------

    def fetch(self):
        self.run("fetch", "--prune", self.remote)

    def remote_sha(self, branch):
        """The branch's tip on the remote, or None when it does not exist there."""
        out = self.run("ls-remote", "--heads", self.remote, f"refs/heads/{branch}")
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}":
                return sha.strip()
        return None

    def tracking(self, branch):
        """The branch's tip as the last fetch saw it: the remote's answer, read offline.

        Only ever read right after `fetch`, which is what makes it the remote's
        tip and not a guess; it saves asking the remote twice in one breath.
        """
        answer = self.run("rev-parse", "--verify", "-q",
                          f"refs/remotes/{self.remote}/{branch}", check=False)
        if answer.returncode != EXIT.OK:
            return None
        return answer.stdout.strip() or None

    def default_branch(self):
        out = self.run("ls-remote", "--symref", self.remote, "HEAD")
        for line in out.splitlines():
            if line.startswith("ref:"):
                return line.split()[1].rsplit("/", 1)[-1]
        return None

    def push(self, sha, branch, expect=None):
        """Point the remote branch at `sha`. With `expect`, only if it is still there.

        Returns False when the lease lost — the branch moved since it was read.
        """
        args = ["push", self.remote, f"{sha}:refs/heads/{branch}"]
        if expect is not None:
            # An empty `expect` is "the branch must not exist yet": create, never move.
            args.insert(1, f"--force-with-lease=refs/heads/{branch}:{expect}")
        answer = self.run(*args, check=False)
        if answer.returncode == EXIT.OK:
            return True
        text = (answer.stderr or "") + (answer.stdout or "")
        if expect is not None and ("stale info" in text or "rejected" in text):
            return False
        raise GitError(args, text, answer.returncode)

    def ensure_remote_branch(self, branch, sha):
        """Create `branch` on the remote at `sha` unless it is there already."""
        if self.remote_sha(branch) is None:
            self.push(sha, branch)
            return True
        return False

    def url(self):
        return self.run("remote", "get-url", self.remote)

    # -- objects -----------------------------------------------------------

    def rev(self, ref):
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}")

    def is_ancestor(self, older, newer):
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def show(self, sha, path):
        answer = self.run("show", f"{sha}:{path}", check=False)
        return answer.stdout if answer.returncode == EXIT.OK else None

    def changed_files(self, base, head):
        out = self.run("diff", "--name-only", base, head)
        return [line for line in out.splitlines() if line.strip()]

    def diff(self, base, head, context=3):
        return self.run("diff", f"-U{context}", base, head)

    def log_grep(self, ref, pattern):
        return self.run("log", ref, "--format=%H", "-F", f"--grep={pattern}")

    # -- worktrees ---------------------------------------------------------

    def worktree(self, target, sha=None, branch=None):
        """A worktree at exactly `sha`, on `branch` if one is named, else detached.

        An existing worktree on the same branch is kept as it is: on a resume
        the previous session's uncommitted work is exactly what must survive.
        """
        target = Path(target)
        if target.exists():
            try:
                here = Git(target).run("rev-parse", "--abbrev-ref", "HEAD")
                if branch and here == branch:
                    return Git(target, self.remote, self.env)
                if not branch and Git(target).run("rev-parse", "HEAD") == sha:
                    return Git(target, self.remote, self.env)
            except GitError:
                pass
            self.run("worktree", "remove", "--force", str(target), check=False)
            shutil.rmtree(target, ignore_errors=True)
            self.run("worktree", "prune", check=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if branch:
            self.run("worktree", "add", "-B", branch, str(target), sha)
        else:
            self.run("worktree", "add", "--detach", str(target), sha)
        return Git(target, self.remote, self.env)

    def reset_to(self, sha):
        """Make the working tree exactly `sha`; ignored files (the `.idp/` dir) survive."""
        self.run("reset", "--hard", sha)
        self.run("clean", "-fd")

    def head(self):
        return self.run("rev-parse", "HEAD")

    def commit_all(self, message, paths=None):
        self.run("add", "-A", "--", *(paths or ["."]))
        if not self.run("diff", "--cached", "--name-only"):
            return None
        self.run("commit", "--no-verify", "-m", message)
        return self.head()


def local_excludes(worktree):
    """`.idp/` is the runner's scratch space in every worktree, never committed."""
    directory = Path(worktree) / ".idp"
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return directory


def repository_name(url):
    """The repository's name from its remote URL: what a PR API is addressed by."""
    text = str(url or "").rstrip("/")
    if "/_git/" in text:
        return text.rsplit("/_git/", 1)[1].split("/")[0]
    return os.path.splitext(text.rsplit("/", 1)[-1].rsplit(":", 1)[-1])[0]
