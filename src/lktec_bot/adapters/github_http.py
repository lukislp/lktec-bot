# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""httpx implementation of :class:`~lktec_bot.ports.github_api.GitHubApi` (REST + GraphQL)."""

from __future__ import annotations

import base64
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from lktec_bot.adapters._http_common import DEFAULT_BASE_URL, github_headers
from lktec_bot.ports.github_api import (
    BranchUpdate,
    CheckRun,
    CommitInfo,
    Comparison,
    CreatedCommit,
    IssueComment,
    MergeMethod,
    PullRequest,
    PullRequestCommit,
    RateLimit,
    Release,
    Review,
    ReviewEvent,
)

# GitHub's 422 for an update-branch call that has nothing to merge. Matched loosely on purpose:
# the wording is not part of the API contract.
_UP_TO_DATE_PHRASES = ("no new commits", "up to date", "up-to-date")
# One page of a list; GitHub's maximum.
_PAGE_SIZE = "100"

# Methods that are safe to repeat after a 5xx or a dropped connection. POST and PATCH are not:
# the first attempt may have succeeded before the answer was lost, and a second comment,
# review or mutation is not something a retry may create.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})

_PULL_REQUEST_ID = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { id } }
}
"""

_ENABLE_AUTO_MERGE = """
mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}) {
    pullRequest { url }
  }
}
"""


_CONVERT_TO_DRAFT = """
mutation($pullRequestId: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) { pullRequest { id } }
}
"""

_READY_FOR_REVIEW = """
mutation($pullRequestId: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) { pullRequest { id } }
}
"""

# The compare endpoint serves at most 100 commits per page. A tag that follows a chain of tag
# objects is resolved up to _MAX_TAG_DEPTH (GitHub itself rarely nests more than one).
_COMPARE_PAGE_SIZE = "100"
_MAX_COMPARE_PAGES = 100
_MAX_TAG_DEPTH = 5
# A paginated list endpoint is walked at most this far (10,000 entries at _PAGE_SIZE).
_MAX_PAGES = 100
# Slack added to the wait for a primary rate limit, so the first retry lands after the reset.
_RESET_MARGIN_SECONDS = 2.0

_CREATE_COMMIT_ON_BRANCH = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


class GitHubHttpError(RuntimeError):
    """A GitHub call failed after retries, the transport failed, or GraphQL returned errors."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpGitHubApi:
    """One client, one token.

    Retry policy: 5xx answers are retried for idempotent methods only; a secondary rate limit
    (403/429 with ``Retry-After``, an exhausted ``X-RateLimit-Remaining`` or the words in the
    body) is retried for every method, because GitHub rejected the request before acting on
    it; a transport error (connection dropped, timeout) is retried once for idempotent methods.
    A ``Retry-After`` above ``max_backoff_seconds`` fails the call instead of being capped: the
    job would be waiting for a limit it cannot see.

    The primary rate limit (5,000 requests per hour, per installation, shared by every repository
    the App is installed on) is different: it resets only at ``X-RateLimit-Reset``. When that is
    within ``max_rate_limit_wait_seconds`` the call logs one line, sleeps once and retries once;
    otherwise it fails at once with the reset time, because backing off cannot help.
    ``sleep``, ``clock`` and ``log`` are injectable so tests neither wait nor print.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        max_backoff_seconds: float = 30.0,
        max_rate_limit_wait_seconds: float = 600.0,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._base_url = base_url.rstrip("/")
        self._headers = github_headers(f"Bearer {token}")
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._max_backoff = max_backoff_seconds
        self._max_rate_limit_wait = max_rate_limit_wait_seconds
        self._clock = clock
        self._log = log or _log_to_stderr
        self._last_rate_limit: RateLimit | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def last_rate_limit(self) -> RateLimit | None:
        return self._last_rate_limit

    def close(self) -> None:
        self._client.close()

    # -- GitHubApi -------------------------------------------------------------------------

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        params = {"ref": ref} if ref else None
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params=params, allow_404=True
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("encoding") != "base64"
            or "content" not in payload
        ):
            raise GitHubHttpError(f"{path} is not a file with base64 content")
        return base64.b64decode(payload["content"]).decode("utf-8")

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        payload = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}").json()
        return _pull_request(payload, number)

    def list_pull_requests(self, owner: str, repo: str) -> list[PullRequest]:
        entries = self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            {"state": "open", "sort": "created", "direction": "asc"},
        )
        return [_pull_request(entry, 0) for entry in entries]

    def list_pull_request_files(self, owner: str, repo: str, number: int) -> list[str]:
        paths: list[str] = []
        for entry in self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/files"):
            paths.append(str(entry.get("filename", "")))
            if entry.get("previous_filename"):
                paths.append(str(entry["previous_filename"]))
        return paths

    def list_pull_request_commits(
        self, owner: str, repo: str, number: int
    ) -> list[PullRequestCommit]:
        commits: list[PullRequestCommit] = []
        for entry in self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/commits"):
            commit = entry.get("commit")
            verification = commit.get("verification") if isinstance(commit, dict) else None
            if not isinstance(verification, dict):
                verification = {}
            commits.append(
                PullRequestCommit(
                    sha=str(entry.get("sha", "")),
                    verified=verification.get("verified") is True,
                    reason=str(verification.get("reason", "unknown")),
                )
            )
        return commits

    def list_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        return [
            Review(
                user_login=_login(entry),
                state=str(entry.get("state", "")),
                commit_id=str(entry.get("commit_id", "")),
                html_url=str(entry.get("html_url", "")),
            )
            for entry in self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        ]

    def create_review(
        self, owner: str, repo: str, number: int, event: ReviewEvent, body: str = ""
    ) -> str:
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json={"event": event, "body": body},
        )
        return str(response.json()["html_url"])

    def enable_auto_merge(
        self, owner: str, repo: str, number: int, merge_method: MergeMethod
    ) -> str:
        lookup = self.graphql(_PULL_REQUEST_ID, {"owner": owner, "name": repo, "number": number})
        node_id = _node(lookup, "repository", "pullRequest", "id")
        data = self.graphql(
            _ENABLE_AUTO_MERGE, {"pullRequestId": node_id, "mergeMethod": merge_method}
        )
        return _node(data, "enablePullRequestAutoMerge", "pullRequest", "url")

    def update_branch(
        self, owner: str, repo: str, number: int, expected_head_sha: str
    ) -> BranchUpdate:
        try:
            self._request(
                "PUT",
                f"/repos/{owner}/{repo}/pulls/{number}/update-branch",
                json={"expected_head_sha": expected_head_sha},
            )
        except GitHubHttpError as exc:
            message = str(exc).lower()
            if exc.status == 422 and any(phrase in message for phrase in _UP_TO_DATE_PHRASES):
                return BranchUpdate.UP_TO_DATE
            raise
        return BranchUpdate.ACCEPTED

    def get_branch_head(self, owner: str, repo: str, branch: str) -> str | None:
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='/')}",
            allow_404=True,
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        target = payload.get("object") if isinstance(payload, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(sha, str) or not sha:
            raise GitHubHttpError(f"branch {branch} has no commit in the API answer")
        return sha

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
        message: dict[str, str] = {"headline": headline}
        if body:
            message["body"] = body
        changes: dict[str, list[dict[str, str]]] = {}
        if additions:
            changes["additions"] = [
                {"path": path, "contents": base64.b64encode(content).decode("ascii")}
                for path, content in additions.items()
            ]
        if deletions:
            changes["deletions"] = [{"path": path} for path in deletions]
        data = self.graphql(
            _CREATE_COMMIT_ON_BRANCH,
            {
                "input": {
                    "branch": {"repositoryNameWithOwner": f"{owner}/{repo}", "branchName": branch},
                    "message": message,
                    "expectedHeadOid": expected_head_oid,
                    "fileChanges": changes,
                }
            },
        )
        return CreatedCommit(
            oid=_node(data, "createCommitOnBranch", "commit", "oid"),
            url=_node(data, "createCommitOnBranch", "commit", "url"),
        )

    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def get_release_by_tag(self, owner: str, repo: str, tag: str) -> Release | None:
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}", allow_404=True
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("html_url"):
            raise GitHubHttpError(f"the release for {tag} has no html_url in the API answer")
        return Release(tag=str(payload.get("tag_name") or tag), html_url=str(payload["html_url"]))

    def get_tag_commit(self, owner: str, repo: str, tag: str) -> str | None:
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/git/ref/tags/{quote(tag, safe='/')}", allow_404=True
        )
        if response.status_code == 404:
            return None
        for _ in range(_MAX_TAG_DEPTH):
            payload = response.json()
            target = payload.get("object") if isinstance(payload, dict) else None
            sha = target.get("sha") if isinstance(target, dict) else None
            if not isinstance(target, dict) or not isinstance(sha, str) or not sha:
                raise GitHubHttpError(f"tag {tag} has no object in the API answer")
            if target.get("type") != "tag":
                return sha
            # An annotated tag: the object is the tag object, whose own object is the commit.
            response = self._request("GET", f"/repos/{owner}/{repo}/git/tags/{sha}")
        raise GitHubHttpError(f"tag {tag} points through more than {_MAX_TAG_DEPTH} tag objects")

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
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/releases",
            json={
                "tag_name": tag,
                "target_commitish": sha,
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": prerelease,
                "make_latest": "true" if latest else "false",
            },
        )
        return str(response.json()["html_url"])

    def list_tags(self, owner: str, repo: str, prefix: str) -> list[str]:
        names: list[str] = []
        # No trailing slash for an empty prefix: ``matching-refs/tags/`` is not the same route.
        path = f"/repos/{owner}/{repo}/git/matching-refs/tags"
        # ``matching-refs`` answers with every matching ref in one response and ignores
        # ``per_page``/``page``, so it is read once: paging it would repeat the same list until
        # the installation's rate limit is exhausted (2026-11-06, every repository with more
        # than 100 tags).
        response = self._request("GET", f"{path}/{prefix}" if prefix else path)
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubHttpError(f"GET {path} did not answer with a list")
        for entry in payload:
            ref = str(entry.get("ref", "")) if isinstance(entry, dict) else ""
            if ref.startswith("refs/tags/"):
                names.append(ref.removeprefix("refs/tags/"))
        return names

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> Comparison:
        path = f"/repos/{owner}/{repo}/compare/{quote(base, safe='/')}...{head}"
        # By sha, in order: a page that overlaps the previous one must not count a commit twice
        # and end the loop before the list is complete.
        commits: dict[str, CommitInfo] = {}
        status = ""
        total = 0
        for page in range(1, _MAX_COMPARE_PAGES + 1):
            payload = self._request(
                "GET", path, params={"per_page": _COMPARE_PAGE_SIZE, "page": str(page)}
            ).json()
            if not isinstance(payload, dict) or not isinstance(payload.get("commits"), list):
                raise GitHubHttpError(f"GET {path} did not answer with a comparison")
            if page == 1:
                status = str(payload.get("status", ""))
                total = int(payload.get("total_commits", 0))
            entries = [entry for entry in payload["commits"] if isinstance(entry, dict)]
            for entry in entries:
                info = _commit_info(entry)
                commits.setdefault(info.sha, info)
            if not entries or len(commits) >= total:
                break
        return Comparison(status=status, total_commits=total, commits=tuple(commits.values()))

    def list_commits(self, owner: str, repo: str, sha: str) -> list[CommitInfo]:
        entries = self._paginate(f"/repos/{owner}/{repo}/commits", {"sha": sha})
        return [_commit_info(entry) for entry in entries]

    def list_check_runs(self, owner: str, repo: str, sha: str, name: str) -> list[CheckRun]:
        entries = self._paginate(
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
            {"check_name": name},
            key="check_runs",
        )
        return [_check_run(entry) for entry in entries]

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
        body: dict[str, Any] = {"name": name, "head_sha": sha, "status": status}
        body.update(
            _present(conclusion=conclusion, details_url=details_url, external_id=external_id)
        )
        body["output"] = _present(
            title=title, summary=summary, text=text, annotations=list(annotations) or None
        )
        response = self._request("POST", f"/repos/{owner}/{repo}/check-runs", json=body)
        return _check_run(response.json())

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
        body = _present(
            status=status,
            conclusion=conclusion,
            details_url=details_url,
            external_id=external_id,
        )
        output = _present(title=title, summary=summary, text=text, annotations=annotations)
        if output:
            body["output"] = output
        response = self._request(
            "PATCH", f"/repos/{owner}/{repo}/check-runs/{check_run_id}", json=body
        )
        return _check_run(response.json())

    def find_open_pull_request(
        self, owner: str, repo: str, head: str, base: str
    ) -> PullRequest | None:
        entries = self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            {"state": "open", "head": f"{owner}:{head}", "base": base},
        )
        return _pull_request(entries[0], 0) if entries else None

    def create_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str, *, draft: bool
    ) -> PullRequest:
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        return _pull_request(response.json(), 0)

    def update_pull_request(
        self, owner: str, repo: str, number: int, *, title: str | None, body: str | None
    ) -> None:
        changes = {
            key: value for key, value in (("title", title), ("body", body)) if value is not None
        }
        self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json=changes)

    def set_pull_request_draft(self, owner: str, repo: str, number: int, draft: bool) -> None:
        # REST cannot toggle the draft state; GraphQL can.
        lookup = self.graphql(_PULL_REQUEST_ID, {"owner": owner, "name": repo, "number": number})
        node_id = _node(lookup, "repository", "pullRequest", "id")
        self.graphql(_CONVERT_TO_DRAFT if draft else _READY_FOR_REVIEW, {"pullRequestId": node_id})

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[IssueComment]:
        return [
            IssueComment(
                id=int(entry.get("id", 0)),
                user_login=_login(entry),
                user_type=str((entry.get("user") or {}).get("type", "")),
                body=str(entry.get("body") or ""),
                html_url=str(entry.get("html_url", "")),
            )
            for entry in self._paginate(f"/repos/{owner}/{repo}/issues/{number}/comments")
        ]

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        response = self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )
        return str(response.json()["html_url"])

    def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> str:
        response = self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": body}
        )
        return str(response.json()["html_url"])

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(
            "POST", "/graphql", json={"query": query, "variables": dict(variables or {})}
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubHttpError(
                "GraphQL response is not JSON", status=response.status_code
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubHttpError(
                f"GraphQL response is {type(payload).__name__}, not an object",
                status=response.status_code,
            )
        errors = payload.get("errors")
        if errors:
            raise GitHubHttpError(
                f"GraphQL errors: {_error_messages(errors)}", status=response.status_code
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubHttpError("GraphQL response without data", status=response.status_code)
        return data

    # -- transport -------------------------------------------------------------------------

    def _paginate(
        self, path: str, params: Mapping[str, str] | None = None, *, key: str | None = None
    ) -> list[dict[str, Any]]:
        """Every entry of a list endpoint, page by page, until a page is short.

        ``key`` names the member that holds the list for the endpoints that wrap it in an
        object (``{"total_count": 3, "check_runs": [...]}``).

        An endpoint that ignores ``page`` would answer every page alike; such a page is
        recognised (same first entry as the page before) and the walk stops, and no walk goes
        beyond ``_MAX_PAGES``: an unbounded loop would spend the installation's whole rate limit.
        """
        entries: list[dict[str, Any]] = []
        previous_first: object = None
        for page in range(1, _MAX_PAGES + 1):
            response = self._request(
                "GET",
                path,
                params={**(params or {}), "per_page": _PAGE_SIZE, "page": str(page)},
            )
            payload = response.json()
            if key is not None:
                payload = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(payload, list):
                raise GitHubHttpError(f"GET {path} did not answer with a list")
            if page > 1 and payload and payload[0] == previous_first:
                raise GitHubHttpError(
                    f"GET {path} answers every page with the same entries; it does not paginate"
                )
            previous_first = payload[0] if payload else None
            entries.extend(entry for entry in payload if isinstance(entry, dict))
            if len(payload) < int(_PAGE_SIZE):
                return entries
        raise GitHubHttpError(f"GET {path} has more than {_MAX_PAGES} pages; refusing to walk on")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        transport_retried = False
        primary_waited = False
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.request(
                    method, url, headers=self._headers, json=json, params=params
                )
            except httpx.TransportError as exc:
                retry = (
                    method in IDEMPOTENT_METHODS
                    and not transport_retried
                    and attempt < self._max_attempts
                )
                if not retry:
                    raise GitHubHttpError(
                        f"{method} {path} failed: {type(exc).__name__}: {exc}"
                    ) from exc
                transport_retried = True
                self._sleep(min(float(2 ** (attempt - 1)), self._max_backoff))
                continue
            self._record_rate_limit(response)
            if response.is_success or (allow_404 and response.status_code == 404):
                return response
            error = f"{method} {path} -> {response.status_code}: {_short_body(response)}"
            reset = _primary_rate_limit_reset(response)
            if reset is not None:
                wait = max(0.0, reset - self._clock() + _RESET_MARGIN_SECONDS)
                if primary_waited or wait > self._max_rate_limit_wait:
                    raise GitHubHttpError(
                        _primary_limit_message(error, reset), status=response.status_code
                    )
                self._log(
                    "primary rate limit exhausted for this installation; "
                    f"waiting {wait:.0f} s until it resets"
                )
                self._sleep(wait)
                primary_waited = True
                attempt -= 1  # the one wait-and-retry does not use up a back-off attempt
                continue
            if not self._retryable(method, response) or attempt >= self._max_attempts:
                raise GitHubHttpError(error, status=response.status_code)
            self._sleep(self._backoff(response, attempt, error))

    def _record_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining", "")
        if not remaining.isdigit():
            return
        reset = response.headers.get("x-ratelimit-reset", "")
        self._last_rate_limit = RateLimit(int(remaining), int(reset) if reset.isdigit() else None)

    @staticmethod
    def _retryable(method: str, response: httpx.Response) -> bool:
        if response.status_code >= 500:
            return method in IDEMPOTENT_METHODS
        return _is_secondary_rate_limit(response)

    def _backoff(self, response: httpx.Response, attempt: int, error: str) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            if float(retry_after) > self._max_backoff:
                raise GitHubHttpError(
                    f"{error}; Retry-After {retry_after} s exceeds the {self._max_backoff:g} s "
                    "limit, giving up",
                    status=response.status_code,
                )
            return float(retry_after)
        return min(float(2 ** (attempt - 1)), self._max_backoff)


def _primary_rate_limit_reset(response: httpx.Response) -> int | None:
    """The reset time (unix seconds) when the answer is the primary rate limit, else None.

    Primary: 403/429, no Retry-After, a numeric X-RateLimit-Reset, and either the words
    "API rate limit exceeded" or X-RateLimit-Remaining: 0. The secondary limit's own words keep
    it on the back-off path.
    """
    if response.status_code not in (403, 429) or "retry-after" in response.headers:
        return None
    reset = response.headers.get("x-ratelimit-reset", "")
    if not reset.isdigit():
        return None
    text = response.text.lower()
    if "secondary rate limit" in text:
        return None
    if "api rate limit exceeded" in text or response.headers.get("x-ratelimit-remaining") == "0":
        return int(reset)
    return None


def _primary_limit_message(error: str, reset: int) -> str:
    when = datetime.fromtimestamp(reset, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{error}; the primary rate limit is exhausted for this installation (shared by every "
        f"repository the App is installed on) and resets at {when}; retry the run after that"
    )


def _log_to_stderr(text: str) -> None:
    print(text, file=sys.stderr)


def _is_secondary_rate_limit(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429):
        return False
    if "retry-after" in response.headers:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "secondary rate limit" in response.text.lower()


def _short_body(response: httpx.Response, limit: int = 200) -> str:
    text = response.text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _error_messages(errors: object) -> str:
    if not isinstance(errors, list):
        return str(errors)
    return "; ".join(
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors
    )


def _login(entry: Mapping[str, Any]) -> str:
    user = entry.get("user")
    return str(user.get("login", "")) if isinstance(user, dict) else ""


def _pull_request(payload: object, number: int) -> PullRequest:
    """A pull request from the single-PR or the list answer (the list has no mergeable_state)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("head"), dict):
        which = f"pull request {number}" if number else "a pull request in the list"
        raise GitHubHttpError(f"{which} has no head in the API answer")
    auto_merge = payload.get("auto_merge")
    method: MergeMethod | None = None
    if isinstance(auto_merge, dict):
        method = _merge_method(auto_merge.get("merge_method"))
    head_repo = payload["head"].get("repo")
    return PullRequest(
        head_ref=str(payload["head"].get("ref") or ""),
        head_repo=str(head_repo.get("full_name") or "") if isinstance(head_repo, dict) else "",
        number=int(payload.get("number", number)),
        html_url=str(payload.get("html_url", "")),
        state=str(payload.get("state", "")),
        draft=bool(payload.get("draft", False)),
        head_sha=str(payload["head"].get("sha", "")),
        auto_merge_method=method,
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        author_login=_login(payload),
        merge_state=str(payload.get("mergeable_state") or "").upper(),
    )


def _present(**fields: Any) -> dict[str, Any]:
    """The fields that are not ``None``; ``None`` means "leave alone" in a PATCH body."""
    return {name: value for name, value in fields.items() if value is not None}


def _check_run(payload: object) -> CheckRun:
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
        raise GitHubHttpError("a check run in the API answer has no id")
    output = payload.get("output")
    output = output if isinstance(output, dict) else {}
    app = payload.get("app")
    app = app if isinstance(app, dict) else {}
    app_id = app.get("id")
    return CheckRun(
        id=payload["id"],
        html_url=str(payload.get("html_url") or ""),
        name=str(payload.get("name") or ""),
        status=str(payload.get("status") or ""),
        conclusion=_optional(payload.get("conclusion")),
        details_url=_optional(payload.get("details_url")),
        title=_optional(output.get("title")),
        summary=_optional(output.get("summary")),
        text=_optional(output.get("text")),
        app_id=app_id if isinstance(app_id, int) else None,
        app_slug=str(app.get("slug") or ""),
        external_id=_optional(payload.get("external_id")),
    )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _commit_info(entry: Mapping[str, Any]) -> CommitInfo:
    commit = entry.get("commit")
    message = commit.get("message") if isinstance(commit, dict) else None
    return CommitInfo(
        sha=str(entry.get("sha", "")),
        message=str(message or ""),
        html_url=str(entry.get("html_url") or ""),
    )


def _merge_method(raw: object) -> MergeMethod | None:
    """GitHub reports the method in lower case; the port speaks the GraphQL enum."""
    if raw == "merge":
        return "MERGE"
    if raw == "squash":
        return "SQUASH"
    if raw == "rebase":
        return "REBASE"
    return None


def _node(data: Mapping[str, Any], *path: str) -> str:
    """Walk ``path`` through a GraphQL ``data`` object; a missing or null node is an error."""
    current: Any = data
    for depth, key in enumerate(path, start=1):
        if not isinstance(current, dict) or current.get(key) is None:
            where = ".".join(path[:depth])
            raise GitHubHttpError(f"GraphQL response has no {where} (missing or null)")
        current = current[key]
    return str(current)
