# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""In-memory ports for the gate tests. They record every call so tests can assert on them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from lktec_bot.adapters.github_http import GitHubHttpError
from lktec_bot.ports.github_api import (
    BranchUpdate,
    CommitInfo,
    Comparison,
    CreatedCommit,
    Issue,
    IssueComment,
    MergeMethod,
    PullRequest,
    PullRequestCommit,
    RateLimit,
    Release,
    Review,
    ReviewEvent,
)
from lktec_bot.ports.token_provider import TokenError

BOT = "fake-app[bot]"


@dataclass
class FakeToken:
    value: str
    permissions: Mapping[str, str] | None
    repositories: frozenset[str] | None
    actor: str | None = BOT
    revoked: bool = False
    revoke_calls: int = 0
    failing_revokes: int = 0  # how many revoke calls raise before one succeeds

    def revoke(self) -> None:
        self.revoke_calls += 1
        if self.revoke_calls <= self.failing_revokes:
            raise TokenError(f"revoking the token failed: attempt {self.revoke_calls}")
        self.revoked = True


@dataclass
class FakeTokenProvider:
    """Mints a distinct token per call and keeps them all for inspection.

    By default a token reports exactly the requested permissions plus ``metadata:read``, the
    one target repository and the actor ``fake-app[bot]``, as GitHub does.
    ``permissions_by_mint`` and ``repositories_by_mint`` override what the n-th minted token
    reports (``None`` means the provider reports nothing); ``failing_revokes`` makes the first
    n revoke calls of every token raise.
    """

    minted: list[FakeToken] = field(default_factory=list)
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    fail_with: Exception | None = None
    permissions_by_mint: dict[int, Mapping[str, str] | None] = field(default_factory=dict)
    repositories_by_mint: dict[int, frozenset[str] | None] = field(default_factory=dict)
    actor: str | None = BOT
    failing_revokes: int = 0
    closed: bool = False

    def token_for(self, owner: str, repo: str, permissions: Mapping[str, str]) -> FakeToken:
        self.requests.append((owner, repo, dict(permissions)))
        if self.fail_with is not None:
            raise self.fail_with
        index = len(self.minted)
        granted = self.permissions_by_mint.get(index, {"metadata": "read", **permissions})
        repositories = self.repositories_by_mint.get(index, frozenset({repo}))
        token = FakeToken(
            value=f"tok-{index}",
            permissions=None if granted is None else dict(granted),
            repositories=repositories,
            actor=self.actor,
            failing_revokes=self.failing_revokes,
        )
        self.minted.append(token)
        return token

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeRepository:
    """The GitHub state the verbs act on, shared by every client a factory builds.

    ``comments`` and ``reviews`` hold what exists before the verb runs; the verbs' writes land
    in ``created_comments``, ``updated_comments``, ``reviews_created``, ``auto_merge`` and
    ``branch_updates``.
    """

    pull_requests: dict[int, PullRequest] = field(default_factory=dict)
    reviews: dict[int, list[Review]] = field(default_factory=dict)
    comments: dict[int, list[IssueComment]] = field(default_factory=dict)
    branch_update: BranchUpdate = BranchUpdate.ACCEPTED
    created_comments: list[tuple[int, str]] = field(default_factory=list)
    updated_comments: list[tuple[int, str]] = field(default_factory=list)
    reviews_created: list[tuple[int, str, str]] = field(default_factory=list)
    auto_merge: list[tuple[int, str]] = field(default_factory=list)
    branch_updates: list[tuple[int, str]] = field(default_factory=list)
    next_comment_id: int = 100
    # Changed files per pull request; what ``list_pull_request_files`` answers.
    pull_files: dict[int, list[str]] = field(default_factory=dict)
    # Commits per pull request; what ``list_pull_request_commits`` answers (none: all verified).
    pull_commits: dict[int, list[PullRequestCommit]] = field(default_factory=dict)
    # ``merge_state`` answers handed out one per ``get_pull_request`` call before the stored
    # state applies, to play GitHub's lazily computed UNKNOWN.
    merge_state_script: dict[int, list[str]] = field(default_factory=dict)
    # Every write in order (``approve #7``, ``enable-automerge #7``, ...), across all clients.
    log: list[str] = field(default_factory=list)
    # File text at a given ref: ``(ref, path) -> text``; what ``get_file_content`` answers with
    # a ``ref`` (without one it serves the factory's ``files``).
    ref_files: dict[tuple[str, str], str] = field(default_factory=dict)
    # Branch name -> head commit id; what ``get_branch_head`` answers.
    branches: dict[str, str] = field(default_factory=dict)
    # Every commit made: (branch, headline, body, expected_head_oid, additions, deletions).
    commits: list[tuple[str, str, str, str, dict[str, bytes], list[str]]] = field(
        default_factory=list
    )
    # Branches created through ``create_branch``: (name, sha).
    created_branches: list[tuple[str, str]] = field(default_factory=list)
    # Tag name -> commit id, and tag name -> published release.
    tags: dict[str, str] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)
    # Every release created: (tag, sha, name, body, latest, prerelease).
    releases_created: list[tuple[str, str, str, str, bool, bool]] = field(default_factory=list)
    # ``compare_commits`` answers by (base tag, head); the history ``list_commits`` serves
    # (newest first); the number of compare pages that were fetched is not modelled.
    comparisons: dict[tuple[str, str], Comparison] = field(default_factory=dict)
    history: list[CommitInfo] = field(default_factory=list)
    # The base branch of each pull request (``find_open_pull_request`` matches on it).
    pull_bases: dict[int, str] = field(default_factory=dict)
    pulls_created: list[tuple[str, str, str, str, bool]] = field(default_factory=list)
    pulls_updated: list[tuple[int, str | None, str | None]] = field(default_factory=list)
    draft_changes: list[tuple[int, bool]] = field(default_factory=list)
    # Issues by number (what ``list_issues`` filters), every issue written, and the labels the
    # repository knows (``None``: any label is accepted; otherwise an unknown one is a 422).
    issues: dict[int, Issue] = field(default_factory=dict)
    issue_writes: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)
    known_labels: set[str] | None = None
    # Play a server that ignores ``creator=``: ``list_issues`` then lists everyone's issues.
    list_ignores_creator: bool = False


@dataclass
class FakeGitHubApi:
    """Serves files from a dict and records which token was used for which call."""

    token: str
    files: Mapping[str, str]
    state: FakeRepository = field(default_factory=FakeRepository)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    closed: bool = False
    last_rate_limit: RateLimit | None = None

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        self.calls.append(("get_file_content", (owner, repo, path, ref)))
        if ref is not None:
            return self.state.ref_files.get((ref, path))
        return self.files.get(path)

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        self.calls.append(("get_pull_request", (owner, repo, number)))
        try:
            pull_request = self.state.pull_requests[number]
        except KeyError:
            raise RuntimeError(f"GET pulls/{number} -> 404: Not Found") from None
        script = self.state.merge_state_script.get(number)
        if script:
            pull_request = replace(pull_request, merge_state=script.pop(0))
        return pull_request

    def list_pull_requests(self, owner: str, repo: str) -> list[PullRequest]:
        self.calls.append(("list_pull_requests", (owner, repo)))
        return [
            replace(pull_request, merge_state="")
            for _, pull_request in sorted(self.state.pull_requests.items())
            if pull_request.open
        ]

    def list_pull_request_files(self, owner: str, repo: str, number: int) -> list[str]:
        self.calls.append(("list_pull_request_files", (owner, repo, number)))
        return list(self.state.pull_files.get(number, []))

    def list_pull_request_commits(
        self, owner: str, repo: str, number: int
    ) -> list[PullRequestCommit]:
        self.calls.append(("list_pull_request_commits", (owner, repo, number)))
        return list(
            self.state.pull_commits.get(number, [PullRequestCommit("c0ffee", True, "valid")])
        )

    def list_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        self.calls.append(("list_reviews", (owner, repo, number)))
        return list(self.state.reviews.get(number, []))

    def create_review(
        self, owner: str, repo: str, number: int, event: ReviewEvent, body: str = ""
    ) -> str:
        self.calls.append(("create_review", (owner, repo, number, event, body)))
        self.state.reviews_created.append((number, event, body))
        self.state.log.append(f"approve #{number}" if event == "APPROVE" else f"review #{number}")
        if event == "APPROVE":
            head = self.state.pull_requests[number].head_sha
            url = f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-1"
            self.state.reviews.setdefault(number, []).append(Review(BOT, "APPROVED", head, url))
        return f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-1"

    def enable_auto_merge(
        self, owner: str, repo: str, number: int, merge_method: MergeMethod
    ) -> str:
        self.calls.append(("enable_auto_merge", (owner, repo, number, merge_method)))
        self.state.auto_merge.append((number, merge_method))
        self.state.log.append(f"enable-automerge #{number} {merge_method.lower()}")
        pull_request = self.state.pull_requests[number]
        self.state.pull_requests[number] = replace(pull_request, auto_merge_method=merge_method)
        return f"https://github.com/{owner}/{repo}/pull/{number}"

    def update_branch(
        self, owner: str, repo: str, number: int, expected_head_sha: str
    ) -> BranchUpdate:
        self.calls.append(("update_branch", (owner, repo, number, expected_head_sha)))
        self.state.branch_updates.append((number, expected_head_sha))
        self.state.log.append(f"update-branch #{number}")
        return self.state.branch_update

    def get_branch_head(self, owner: str, repo: str, branch: str) -> str | None:
        self.calls.append(("get_branch_head", (owner, repo, branch)))
        return self.state.branches.get(branch)

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
        self.calls.append(
            ("create_commit_on_branch", (owner, repo, branch, headline, expected_head_oid))
        )
        self.state.commits.append(
            (branch, headline, body, expected_head_oid, dict(additions), list(deletions))
        )
        self.state.log.append(f"commit {branch}")
        oid = f"{len(self.state.commits):040x}"
        self.state.branches[branch] = oid
        return CreatedCommit(oid, f"https://github.com/{owner}/{repo}/commit/{oid}")

    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        self.calls.append(("create_branch", (owner, repo, branch, sha)))
        self.state.created_branches.append((branch, sha))
        self.state.log.append(f"create-branch {branch}")
        self.state.branches[branch] = sha

    def get_release_by_tag(self, owner: str, repo: str, tag: str) -> Release | None:
        self.calls.append(("get_release_by_tag", (owner, repo, tag)))
        return self.state.releases.get(tag)

    def get_tag_commit(self, owner: str, repo: str, tag: str) -> str | None:
        self.calls.append(("get_tag_commit", (owner, repo, tag)))
        return self.state.tags.get(tag)

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
        self.calls.append(("create_release", (owner, repo, tag, sha)))
        self.state.releases_created.append((tag, sha, name, body, latest, prerelease))
        self.state.log.append(f"release {tag}")
        url = f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
        self.state.tags.setdefault(tag, sha)
        self.state.releases[tag] = Release(tag, url)
        return url

    def list_tags(self, owner: str, repo: str, prefix: str) -> list[str]:
        self.calls.append(("list_tags", (owner, repo, prefix)))
        return [tag for tag in self.state.tags if tag.startswith(prefix)]

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> Comparison:
        self.calls.append(("compare_commits", (owner, repo, base, head)))
        return self.state.comparisons[(base, head)]

    def list_commits(self, owner: str, repo: str, sha: str) -> list[CommitInfo]:
        self.calls.append(("list_commits", (owner, repo, sha)))
        return list(self.state.history)

    def find_open_pull_request(
        self, owner: str, repo: str, head: str, base: str
    ) -> PullRequest | None:
        self.calls.append(("find_open_pull_request", (owner, repo, head, base)))
        for number, pull_request in sorted(self.state.pull_requests.items()):
            if (
                pull_request.open
                and pull_request.head_ref == head
                and self.state.pull_bases.get(number) == base
            ):
                return pull_request
        return None

    def create_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str, *, draft: bool
    ) -> PullRequest:
        self.calls.append(("create_pull_request", (owner, repo, head, base)))
        self.state.pulls_created.append((head, base, title, body, draft))
        self.state.log.append(f"open-pr {head}")
        number = max(self.state.pull_requests, default=0) + 1
        pull_request = PullRequest(
            number=number,
            html_url=f"https://github.com/{owner}/{repo}/pull/{number}",
            state="open",
            draft=draft,
            head_sha="a" * 40,
            title=title,
            body=body,
            head_ref=head,
        )
        self.state.pull_requests[number] = pull_request
        self.state.pull_bases[number] = base
        return pull_request

    def update_pull_request(
        self, owner: str, repo: str, number: int, *, title: str | None, body: str | None
    ) -> None:
        self.calls.append(("update_pull_request", (owner, repo, number)))
        self.state.pulls_updated.append((number, title, body))
        self.state.log.append(f"update-pr #{number}")
        current = self.state.pull_requests[number]
        self.state.pull_requests[number] = replace(
            current,
            title=current.title if title is None else title,
            body=current.body if body is None else body,
        )

    def set_pull_request_draft(self, owner: str, repo: str, number: int, draft: bool) -> None:
        self.calls.append(("set_pull_request_draft", (owner, repo, number, draft)))
        self.state.draft_changes.append((number, draft))
        self.state.log.append(f"draft #{number} {draft}")
        self.state.pull_requests[number] = replace(self.state.pull_requests[number], draft=draft)

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[IssueComment]:
        self.calls.append(("list_issue_comments", (owner, repo, number)))
        return list(self.state.comments.get(number, []))

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        self.calls.append(("create_issue_comment", (owner, repo, number, body)))
        self.state.created_comments.append((number, body))
        self.state.log.append(f"comment #{number}")
        comment_id = self.state.next_comment_id
        self.state.next_comment_id += 1
        url = f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-{comment_id}"
        comment = IssueComment(comment_id, BOT, "Bot", body, url)
        self.state.comments.setdefault(number, []).append(comment)
        return url

    def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> str:
        self.calls.append(("update_issue_comment", (owner, repo, comment_id, body)))
        self.state.updated_comments.append((comment_id, body))
        return f"https://github.com/{owner}/{repo}/issues/comments/{comment_id}"

    def list_issues(
        self, owner: str, repo: str, *, creator: str, state: str = "all"
    ) -> list[Issue]:
        self.calls.append(("list_issues", (owner, repo, creator, state)))
        return [
            issue
            for _, issue in sorted(self.state.issues.items())
            if (self.state.list_ignores_creator or issue.author_login == creator)
            and state in ("all", issue.state)
        ]

    def _check_labels(self, labels: Sequence[str]) -> None:
        known = self.state.known_labels
        unknown = [label for label in labels if known is not None and label not in known]
        if unknown:
            raise GitHubHttpError(
                f"POST issues -> 422: Validation Failed: label {unknown[0]!r} does not exist",
                status=422,
            )

    def create_issue(
        self, owner: str, repo: str, *, title: str, body: str, labels: Sequence[str]
    ) -> Issue:
        self.calls.append(("create_issue", (owner, repo, title, body, tuple(labels))))
        self._check_labels(labels)
        number = max(self.state.issues, default=0) + 1
        issue = Issue(
            number=number,
            title=title,
            body=body,
            state="open",
            html_url=f"https://github.com/{owner}/{repo}/issues/{number}",
            labels=tuple(labels),
            author_login=BOT,
        )
        self.state.issues[number] = issue
        self.state.issue_writes.append(
            ("create", number, {"title": title, "body": body, "labels": list(labels)})
        )
        self.state.log.append(f"open-issue #{number}")
        return issue

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
        self.calls.append(("update_issue", (owner, repo, number)))
        if labels is not None:
            self._check_labels(labels)
        changes: dict[str, Any] = {
            key: value
            for key, value in (("title", title), ("body", body), ("state", state))
            if value is not None
        }
        if labels is not None:
            changes["labels"] = list(labels)
        current = self.state.issues[number]
        issue = replace(
            current,
            title=current.title if title is None else title,
            body=current.body if body is None else body,
            state=current.state if state is None else state,
            labels=current.labels if labels is None else tuple(labels),
        )
        self.state.issues[number] = issue
        self.state.issue_writes.append(("update", number, changes))
        self.state.log.append(f"update-issue #{number}")
        return issue

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("graphql", (query, dict(variables or {}))))
        return {}

    def close(self) -> None:
        self.closed = True


class FakeApiFactory:
    """Builds FakeGitHubApi instances bound to a token and remembers every one."""

    # Every client this factory builds reports this budget (a test sets it on the class).
    last_rate_limit: RateLimit | None = None

    def __init__(self, files: Mapping[str, str], state: FakeRepository | None = None) -> None:
        self.files = files
        self.state = state if state is not None else FakeRepository()
        self.created: list[FakeGitHubApi] = []

    def __call__(self, token: str) -> FakeGitHubApi:
        api = FakeGitHubApi(
            token=token, files=self.files, state=self.state, last_rate_limit=self.last_rate_limit
        )
        self.created.append(api)
        return api
