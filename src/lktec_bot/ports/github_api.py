# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The minimal GitHub surface the verbs need. Every method is one API call (or two, where a
node id has to be resolved first, or a few, where a list is paginated)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
MergeMethod = Literal["MERGE", "SQUASH", "REBASE"]


@dataclass(frozen=True, slots=True)
class RateLimit:
    """The budget GitHub reported on the last answer: requests left and the reset (unix s)."""

    remaining: int
    reset: int | None = None


@dataclass(frozen=True, slots=True)
class PullRequest:
    """What the verbs need to know about a pull request before they touch it."""

    number: int
    html_url: str
    state: str  # "open" or "closed"
    draft: bool
    head_sha: str
    auto_merge_method: MergeMethod | None = None  # set when auto-merge is already enabled
    title: str = ""
    body: str = ""
    author_login: str = ""  # ``dependabot[bot]`` for Dependabot, a user's login otherwise
    # GitHub's ``mergeable_state`` in upper case (``BEHIND``, ``DIRTY``, ``CLEAN``, ``BLOCKED``,
    # ``UNSTABLE``, ``UNKNOWN``, ...). Only the single-pull-request call reports it; GitHub
    # computes it lazily, so ``UNKNOWN`` means "ask again in a moment". Empty when not reported.
    merge_state: str = ""
    # The head branch name and the repository it lives in (``owner/name``; empty when the head
    # repository is gone). A fork head is a different repository than the pull request's own.
    head_ref: str = ""
    head_repo: str = ""

    @property
    def open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True, slots=True)
class PullRequestCommit:
    """One commit of a pull request and GitHub's verdict on its signature."""

    sha: str
    verified: bool
    reason: str  # GitHub's ``verification.reason``: ``valid``, ``unsigned``, ``unknown``, ...


@dataclass(frozen=True, slots=True)
class Review:
    """One submitted pull request review."""

    user_login: str
    state: str  # "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"
    commit_id: str
    html_url: str


@dataclass(frozen=True, slots=True)
class IssueComment:
    """One comment on an issue or pull request conversation."""

    id: int
    user_login: str
    user_type: str  # "User" or "Bot"
    body: str
    html_url: str


@dataclass(frozen=True, slots=True)
class Issue:
    """One issue (never a pull request) with what ``open-issue`` needs to decide on."""

    number: int
    title: str
    body: str
    state: str  # "open" or "closed"
    html_url: str
    labels: tuple[str, ...]
    author_login: str


@dataclass(frozen=True, slots=True)
class CreatedCommit:
    """The commit ``create_commit_on_branch`` made."""

    oid: str
    url: str


@dataclass(frozen=True, slots=True)
class Release:
    """A published GitHub release, as far as the release verb needs to know."""

    tag: str
    html_url: str


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """One commit of a history listing: its id and the full message."""

    sha: str
    message: str
    html_url: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    """``compare/{base}...{head}``: how head relates to base and the commits it adds.

    ``commits`` is oldest first. ``total_commits`` is what GitHub says the comparison holds, so
    a caller can tell a partial list from a complete one.
    """

    status: str  # "ahead", "behind", "identical" or "diverged"
    total_commits: int
    commits: tuple[CommitInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One check run on a commit, as far as the check verb needs to know.

    ``app_id`` is ``None`` when GitHub does not report the App; ``app_slug`` is empty then too.
    ``title``, ``summary`` and ``text`` come from the run's ``output``.
    """

    id: int
    html_url: str
    name: str
    status: str  # "queued", "in_progress" or "completed"
    conclusion: str | None
    details_url: str | None
    title: str | None
    summary: str | None
    text: str | None
    app_id: int | None
    app_slug: str
    external_id: str | None = None


class AutoMergeNotNeeded(Exception):
    """GitHub refused to enable auto-merge because the pull request can be merged right now.

    GitHub only arms auto-merge on a pull request that is blocked by something pending; with no
    pending required check it answers ``is in clean status`` (or ``unstable``, ``has_hooks``).
    ``state`` is that status in lower case.
    """

    def __init__(self, state: str) -> None:
        super().__init__(f"pull request is already mergeable ({state})")
        self.state = state


class BranchUpdate(StrEnum):
    """What ``update_branch`` achieved: the update was queued, or there was nothing to do."""

    ACCEPTED = "accepted"
    UP_TO_DATE = "up-to-date"


class GitHubApi(Protocol):
    """A GitHub client bound to one token. Adapters decide how the calls are made."""

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Return the decoded text of a file, or ``None`` when it does not exist.

        Without ``ref`` the file is read from the repository's default branch.
        """
        ...

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        """Fetch the state of pull request ``number``."""
        ...

    def list_pull_requests(self, owner: str, repo: str) -> Sequence[PullRequest]:
        """Every open pull request, oldest first. ``merge_state`` is not reported by a list."""
        ...

    def list_pull_request_files(self, owner: str, repo: str, number: int) -> Sequence[str]:
        """The paths pull request ``number`` changes (a renamed file contributes both paths)."""
        ...

    def list_pull_request_commits(
        self, owner: str, repo: str, number: int
    ) -> Sequence[PullRequestCommit]:
        """The commits of pull request ``number`` with their signature verification."""
        ...

    def list_reviews(self, owner: str, repo: str, number: int) -> Sequence[Review]:
        """Every submitted review on pull request ``number``, oldest first."""
        ...

    def create_review(
        self, owner: str, repo: str, number: int, event: ReviewEvent, body: str = ""
    ) -> str:
        """Submit a pull request review and return its HTML URL."""
        ...

    def enable_auto_merge(
        self, owner: str, repo: str, number: int, merge_method: MergeMethod
    ) -> str:
        """Enable auto-merge on pull request ``number`` and return the PR URL.

        The adapter resolves the pull request's GraphQL node id itself. Raises
        :class:`AutoMergeNotNeeded` when GitHub refuses because the pull request can be merged
        right now.
        """
        ...

    def update_branch(
        self, owner: str, repo: str, number: int, expected_head_sha: str
    ) -> BranchUpdate:
        """Update a pull request branch with its base.

        ``ACCEPTED`` when GitHub queued the merge commit, ``UP_TO_DATE`` when the branch already
        contains the base; anything else is an error.
        """
        ...

    def get_branch_head(self, owner: str, repo: str, branch: str) -> str | None:
        """The commit id the branch points at, or ``None`` when the branch does not exist."""
        ...

    def create_commit_on_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        headline: str,
        body: str,
        expected_head_oid: str,
        additions: Mapping[str, bytes],
        deletions: Sequence[str],
    ) -> CreatedCommit:
        """Commit the changes onto ``branch`` through GraphQL ``createCommitOnBranch``.

        GitHub creates and signs the commit; it fails when the branch no longer points at
        ``expected_head_oid`` instead of overwriting a concurrent push. ``additions`` maps a
        repository path to the file's new content (the adapter encodes it).
        """
        ...

    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        """Create ``refs/heads/<branch>`` at ``sha``. Fails when the branch already exists."""
        ...

    def get_release_by_tag(self, owner: str, repo: str, tag: str) -> Release | None:
        """The published release for ``tag``, or ``None`` when there is none."""
        ...

    def get_tag_commit(self, owner: str, repo: str, tag: str) -> str | None:
        """The commit id ``tag`` points at (an annotated tag is resolved), or ``None``."""
        ...

    def create_release(
        self,
        owner: str,
        repo: str,
        tag: str,
        sha: str,
        name: str,
        body: str,
        *,
        latest: bool,
        prerelease: bool,
    ) -> str:
        """Create a release for ``tag`` and return its HTML URL.

        GitHub creates a lightweight tag at ``sha`` when the tag does not exist.
        """
        ...

    def list_tags(self, owner: str, repo: str, prefix: str) -> Sequence[str]:
        """Every tag name that starts with ``prefix`` (``git/matching-refs/tags/<prefix>``)."""
        ...

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> Comparison:
        """``base...head`` with every commit page fetched (up to the API's own ceiling)."""
        ...

    def list_commits(self, owner: str, repo: str, sha: str) -> Sequence[CommitInfo]:
        """Every commit reachable from ``sha``, newest first."""
        ...

    def list_check_runs(self, owner: str, repo: str, sha: str, name: str) -> Sequence[CheckRun]:
        """Every check run called ``name`` on commit ``sha``, of any App, every page fetched."""
        ...

    def create_check_run(
        self,
        owner: str,
        repo: str,
        *,
        name: str,
        sha: str,
        status: str,
        conclusion: str | None,
        details_url: str | None,
        external_id: str | None,
        title: str,
        summary: str,
        text: str | None,
        annotations: Sequence[Mapping[str, Any]],
    ) -> CheckRun:
        """Create a check run on ``sha``. ``annotations`` are at most 50, already validated."""
        ...

    def update_check_run(
        self,
        owner: str,
        repo: str,
        check_run_id: int,
        *,
        status: str | None = None,
        conclusion: str | None = None,
        details_url: str | None = None,
        external_id: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        text: str | None = None,
        annotations: Sequence[Mapping[str, Any]] | None = None,
    ) -> CheckRun:
        """Change a check run; ``None`` leaves a field alone.

        GitHub wants ``title`` and ``summary`` in ``output`` whenever any output field is sent,
        so the caller passes them together with ``text`` or ``annotations``.
        """
        ...

    def find_open_pull_request(
        self, owner: str, repo: str, head: str, base: str
    ) -> PullRequest | None:
        """The open pull request from branch ``head`` (same repository) into ``base``."""
        ...

    def create_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str, *, draft: bool
    ) -> PullRequest:
        """Open a pull request from branch ``head`` into ``base``."""
        ...

    def update_pull_request(
        self, owner: str, repo: str, number: int, *, title: str | None, body: str | None
    ) -> None:
        """Change the title and/or body of a pull request (``None`` leaves a field alone)."""
        ...

    def set_pull_request_draft(self, owner: str, repo: str, number: int, draft: bool) -> None:
        """Convert a pull request to a draft or mark it ready for review (GraphQL)."""
        ...

    def list_issue_comments(self, owner: str, repo: str, number: int) -> Sequence[IssueComment]:
        """Every comment on issue or pull request ``number``, oldest first."""
        ...

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        """Comment on an issue or pull request and return the comment's HTML URL."""
        ...

    def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> str:
        """Replace the body of an existing comment and return its HTML URL."""
        ...

    def list_issues(
        self, owner: str, repo: str, *, creator: str, state: str = "all"
    ) -> Sequence[Issue]:
        """Every issue opened by ``creator`` in ``state`` (``open``, ``closed``, ``all``).

        Pull requests, which the issues endpoint also returns, are left out.
        """
        ...

    def create_issue(
        self, owner: str, repo: str, *, title: str, body: str, labels: Sequence[str]
    ) -> Issue:
        """Open an issue."""
        ...

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: Sequence[str] | None = None,
    ) -> Issue:
        """Change the given fields of an issue (``None`` leaves one alone); ``labels`` replaces."""
        ...

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL query and return its ``data`` object."""
        ...

    @property
    def last_rate_limit(self) -> RateLimit | None:
        """The rate-limit budget from the most recent answer, None until one reported it."""
        ...

    def close(self) -> None:
        """Release the client's connections. Idempotent."""
        ...


# Builds a client for a freshly minted token; the gate calls it once per token and closes it.
GitHubApiFactory = Callable[[str], GitHubApi]
