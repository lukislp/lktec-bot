# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``lockfiles`` module's command: ``commit`` the lock files regenerated for a Dependabot
pull request onto its branch, together with the derived files the policy declares.

The files are produced by a job that runs the pull request's code and holds no secret; this
command runs in a second job, with the App credentials, and treats what it is handed as
untrusted data: only plain relative paths, files that really lie inside the directory it was
given, and only paths :func:`~lktec_bot.application.modules.lockfiles.allowlist.admit` lets
through (a known lock file name, or a path the default branch's policy lists under
``derived``). Nothing handed over is ever executed. What changed is decided here, by comparing
every file with the pull request head's own copy read through the API, not by trusting a list
from the first job. The commit itself is the ``commit`` verb, signed by GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from lktec_bot.application.gate import Gate, VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.modules._handover import HandoverError as HandoverError
from lktec_bot.application.modules._handover import local_file as local_file
from lktec_bot.application.modules._handover import read_manifest as read_handover_manifest
from lktec_bot.application.modules.automerge.classify import DEPENDABOT_LOGIN
from lktec_bot.application.modules.lockfiles import allowlist
from lktec_bot.application.modules.lockfiles.allowlist import Kind, admit
from lktec_bot.application.modules.lockfiles.options import LockfilesOptions
from lktec_bot.domain.permissions import Target, Verb
from lktec_bot.domain.policy import Policy
from lktec_bot.ports.github_api import GitHubApi, PullRequest
from lktec_bot.text import escape_markdown

MODULE = "lockfiles"
READ_PERMISSIONS = {"contents": "read", "pull_requests": "read"}
COMMENT_MARKER = "lockfiles"


class Decision(StrEnum):
    """The outcome of ``commit`` for one pull request."""

    COMMITTED = "committed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    number: int
    decision: Decision
    reason: str
    url: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return {Decision.DENIED: 3, Decision.ERROR: 1}.get(self.decision, 0)

    @property
    def result(self) -> str:
        """The coarse ``result`` output: ``ok``, ``denied`` or ``error``."""
        return {Decision.DENIED: "denied", Decision.ERROR: "error"}.get(self.decision, "ok")

    def to_markdown(self, repository: str) -> str:
        head = f"**{self.decision.value.upper()}** `lockfiles commit` #{self.number}"
        text = f"{head} on `{repository}`: {escape_markdown(self.reason)}"
        if self.url:
            text += f"\n\n<{self.url}>"
        for warning in self.warnings:
            text += f"\n\nWarning: {escape_markdown(warning)}"
        return text + "\n"


@dataclass(frozen=True, slots=True)
class _Assessment:
    options: LockfilesOptions
    pull_request: PullRequest
    skip: str | None = None
    # Why the whole handover is refused: a path the allow-list does not admit.
    refused: str | None = None
    # Selected file -> its text at the pull request head (``None``: not there yet).
    heads: dict[str, str | None] = field(default_factory=dict)
    kinds: dict[str, Kind] = field(default_factory=dict)
    ignored: tuple[str, ...] = ()


def read_manifest(changed_dir: Path) -> list[str]:
    """The repository paths the first job reports as changed.

    Every one must be a plain relative path of characters the bot accepts; if any is not, the
    whole handover is refused before anything is read from GitHub. Which of them may be
    committed is decided afterwards against the policy, by the allow-list.
    """
    return read_handover_manifest(changed_dir, max_files=allowlist.MAX_FILES)


class Lockfiles:
    """Commits regenerated lock files and derived files of one repository through a
    :class:`Gate`."""

    def __init__(self, gate: Gate, owner: str, repo: str) -> None:
        self._gate = gate
        self._owner = owner
        self._repo = repo

    def commit(self, number: int, changed_dir: Path, head_sha: str | None = None) -> CommitOutcome:
        """Commit the files in ``changed_dir`` that differ from pull request ``number``.

        ``head_sha`` is the head the files were regenerated for; a pull request that has moved
        on is skipped (its new head triggers a run of its own).
        """
        try:
            manifest = read_manifest(changed_dir)
            local = {path: local_file(changed_dir, path) for path in manifest}
        except HandoverError as exc:
            return CommitOutcome(number, Decision.ERROR, str(exc))
        try:
            contents = {path: file.read_bytes() for path, file in local.items()}
        except OSError as exc:
            return CommitOutcome(number, Decision.ERROR, f"cannot read a lock file: {exc}")
        sizes = {path: len(content) for path, content in contents.items()}

        read = self._gate.read(
            self._owner,
            self._repo,
            MODULE,
            READ_PERMISSIONS,
            lambda api, policy: self._assess(api, policy, number, sizes, head_sha),
        )
        warnings = read.warnings
        if read.failure is not None or read.value is None:
            failure = read.failure or VerbResult(VerbOutcome.ERROR, "nothing was read")
            return _failed(number, failure, warnings)
        assessment = read.value
        pull_request = assessment.pull_request
        if assessment.skip is not None:
            return CommitOutcome(
                number, Decision.SKIPPED, assessment.skip, pull_request.html_url, warnings
            )
        if assessment.refused is not None:
            return CommitOutcome(
                number, Decision.ERROR, assessment.refused, pull_request.html_url, warnings
            )

        changed = [
            path
            for path, head in assessment.heads.items()
            if head is None or contents[path] != head.encode("utf-8")
        ]
        if not changed:
            what = "lock files" if _only_lockfiles(assessment.kinds) else "handed-over files"
            reason = f"nothing to commit: the {what} already match the pull request head"
            if assessment.ignored:
                reason += f" (left out by the options: {', '.join(assessment.ignored)})"
            return CommitOutcome(
                number, Decision.UNCHANGED, reason, pull_request.html_url, warnings
            )
        return self._commit(number, assessment, changed, local, warnings)

    def _assess(
        self,
        api: GitHubApi,
        policy: Policy,
        number: int,
        sizes: dict[str, int],
        head_sha: str | None,
    ) -> _Assessment:
        options = LockfilesOptions.model_validate(policy.modules[MODULE].options)
        pull_request = api.get_pull_request(self._owner, self._repo, number)
        skip = self._skip_reason(pull_request, head_sha)
        if skip is not None:
            return _Assessment(options, pull_request, skip=skip)
        # Every handed-over path goes through the one allow-list, with the policy the gate read
        # from the default branch. A single refused path refuses the whole handover.
        admissions = [admit(path, size, options) for path, size in sizes.items()]
        problems = [f"{a.path!r}: {a.problem}" for a in admissions if a.refused]
        if problems:
            return _Assessment(
                options, pull_request, refused="refusing the handover: " + "; ".join(problems)
            )
        kinds = {a.path: a.kind for a in admissions if a.kind is not None and not a.left_out}
        heads = {
            path: api.get_file_content(self._owner, self._repo, path, ref=pull_request.head_sha)
            for path in kinds
        }
        ignored = tuple(a.path for a in admissions if a.left_out)
        return _Assessment(options, pull_request, heads=heads, kinds=kinds, ignored=ignored)

    def _skip_reason(self, pull_request: PullRequest, head_sha: str | None) -> str | None:
        if not pull_request.open:
            return f"pull request is {pull_request.state}, not open"
        if pull_request.draft:
            return "pull request is a draft"
        if pull_request.author_login != DEPENDABOT_LOGIN:
            return f"not opened by {DEPENDABOT_LOGIN}"
        if pull_request.head_repo.lower() != f"{self._owner}/{self._repo}".lower():
            return "the pull request branch lives in another repository"
        if not pull_request.head_ref:
            return "the pull request has no head branch"
        if head_sha is not None and pull_request.head_sha != head_sha:
            return (
                f"the pull request moved on from {head_sha[:12]} to "
                f"{pull_request.head_sha[:12]}; the run for the new head regenerates"
            )
        return None

    def _commit(
        self,
        number: int,
        assessment: _Assessment,
        changed: list[str],
        local: dict[str, Path],
        warnings: tuple[str, ...],
    ) -> CommitOutcome:
        pull_request = assessment.pull_request
        body = "Regenerated by lktec-bot:\n" + "".join(f"- {path}\n" for path in changed)
        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.COMMIT,
                args={
                    "branch": pull_request.head_ref,
                    "message": assessment.options.headline(number),
                    "body": body,
                    "add": [f"{path}={local[path]}" for path in changed],
                    "expected_head": pull_request.head_sha,
                },
            )
        )
        if not result.ok:
            return _failed(number, result, warnings, "commit")
        outcome_warnings = (*warnings, *result.warnings)
        note = self._comment(number, changed, assessment.kinds, result.url)
        if note is not None:
            outcome_warnings = (*outcome_warnings, note)
        return CommitOutcome(
            number,
            Decision.COMMITTED,
            f"{result.message}: {', '.join(changed)}",
            result.url,
            outcome_warnings,
        )

    def _comment(
        self, number: int, changed: list[str], kinds: dict[str, Kind], url: str | None
    ) -> str | None:
        """Tell the pull request what happened; a failure is a warning, never the outcome."""
        files = ", ".join(f"`{path}`" for path in changed)
        link = f" ([commit]({url}))" if url else ""
        committed = {kinds[path] for path in changed}
        what = " and ".join(
            label
            for kind, label in ((Kind.LOCKFILE, "lock files"), (Kind.DERIVED, "derived files"))
            if kind in committed
        )
        text = f"lktec-bot refreshed the {what} of this pull request: {files}{link}."
        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.COMMENT,
                target=Target.PULL_REQUEST,
                args={
                    "number": number,
                    "body": text,
                    "marker": COMMENT_MARKER,
                },
            )
        )
        return (
            None
            if result.ok
            else f"the lock files were committed but the comment failed: {result.message}"
        )


def _only_lockfiles(kinds: dict[str, Kind]) -> bool:
    return all(kind is Kind.LOCKFILE for kind in kinds.values())


def _failed(
    number: int, result: VerbResult, warnings: tuple[str, ...], step: str = ""
) -> CommitOutcome:
    decision = Decision.DENIED if result.outcome is VerbOutcome.DENIED else Decision.ERROR
    reason = f"{step}: {result.message}" if step else result.message
    return CommitOutcome(number, decision, reason, result.url, (*warnings, *result.warnings))
