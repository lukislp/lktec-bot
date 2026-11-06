"""The ``sync`` module's command: ``commit`` generated files onto a branch.

A first job generates the files (it may run code and holds no secret); this command runs in a
second job, with the App credentials, and treats what it is handed as untrusted: plain relative
paths only, files that really lie inside the directory it was given, and nothing outside the
policy's ``paths``. What changed is decided here, by comparing every file with the branch
head's own copy read through the API. The commit itself is the ``commit`` verb, signed by
GitHub, made on top of exactly the head that was compared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from lktec_bot.application.gate import Gate, VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.modules._handover import HandoverError, local_file, read_manifest
from lktec_bot.application.modules.sync.options import SyncOptions
from lktec_bot.domain.paths import within
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import Policy
from lktec_bot.ports.github_api import GitHubApi
from lktec_bot.text import escape_markdown

MODULE = "sync"
READ_PERMISSIONS = {"contents": "read"}
_VERSION = re.compile(r"^[0-9A-Za-z._+-]*\Z")
_BASE_SHA = re.compile(r"^[0-9a-f]{40}\Z")


class Decision(StrEnum):
    """The outcome of ``commit``."""

    COMMITTED = "committed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SyncOutcome:
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
        head = f"**{self.decision.value.upper()}** `sync commit`"
        text = f"{head} on `{repository}`: {escape_markdown(self.reason)}"
        if self.url:
            text += f"\n\n<{self.url}>"
        for warning in self.warnings:
            text += f"\n\nWarning: {escape_markdown(warning)}"
        return text + "\n"


@dataclass(frozen=True, slots=True)
class _Assessment:
    options: SyncOptions | None = None
    # Why the policy forbids this run (``denied``) or why it cannot proceed (``error``).
    denied: str | None = None
    error: str | None = None
    skipped: str | None = None
    head: str = ""
    # Handed-over file -> its text at the branch head (``None``: not there yet).
    heads: dict[str, str | None] = field(default_factory=dict)


class Sync:
    """Commits handed-over files of one repository through a :class:`Gate`."""

    def __init__(self, gate: Gate, owner: str, repo: str) -> None:
        self._gate = gate
        self._owner = owner
        self._repo = repo

    def commit(
        self,
        changed_dir: Path,
        version: str = "",
        branch: str | None = None,
        base_sha: str | None = None,
    ) -> SyncOutcome:
        """Commit the files in ``changed_dir`` that differ from the branch head.

        ``base_sha`` is the commit of the branch the files were prepared from. When the branch
        has moved on and changed one of the handed-over files since, the run is skipped: the
        files were prepared from stale content, and the next run starts over.
        """
        if base_sha is not None:
            base_sha = base_sha.lower()
            if not _BASE_SHA.match(base_sha):
                return SyncOutcome(
                    Decision.ERROR, f"--base-sha {base_sha!r} is not a full commit id"
                )
        if not _VERSION.match(version):
            return SyncOutcome(
                Decision.ERROR, "the version may only hold letters, digits and . _ + -"
            )
        try:
            manifest = read_manifest(changed_dir)
            local = {path: local_file(changed_dir, path) for path in manifest}
        except HandoverError as exc:
            return SyncOutcome(Decision.ERROR, str(exc))

        read = self._gate.read(
            self._owner,
            self._repo,
            MODULE,
            READ_PERMISSIONS,
            lambda api, policy: self._assess(api, policy, manifest, branch, base_sha),
        )
        warnings = read.warnings
        if read.failure is not None or read.value is None:
            failure = read.failure or VerbResult(VerbOutcome.ERROR, "nothing was read")
            return _failed(failure, warnings)
        assessment = read.value
        if assessment.denied is not None:
            return SyncOutcome(Decision.DENIED, assessment.denied, warnings=warnings)
        if assessment.skipped is not None:
            return SyncOutcome(Decision.SKIPPED, assessment.skipped, warnings=warnings)
        if assessment.error is not None or assessment.options is None:
            return SyncOutcome(Decision.ERROR, assessment.error or "no options", warnings=warnings)

        try:
            changed = [
                path
                for path, head in assessment.heads.items()
                if head is None or local[path].read_bytes() != head.encode("utf-8")
            ]
        except OSError as exc:
            return SyncOutcome(Decision.ERROR, f"cannot read a handed-over file: {exc}")
        if not changed:
            return SyncOutcome(
                Decision.UNCHANGED,
                f"nothing to commit: the files already match {assessment.options.branch}",
                warnings=warnings,
            )
        return self._commit(assessment, changed, local, version, warnings)

    def _assess(
        self,
        api: GitHubApi,
        policy: Policy,
        manifest: list[str],
        branch: str | None,
        base_sha: str | None,
    ) -> _Assessment:
        options = SyncOptions.model_validate(policy.modules[MODULE].options)
        if branch is not None and branch != options.branch:
            return _Assessment(
                denied=f"the policy commits onto {options.branch!r}, not onto {branch!r}"
            )
        outside = [p for p in manifest if not any(within(p, s) for s in options.paths)]
        if outside:
            return _Assessment(
                denied=f"outside the policy's paths ({', '.join(options.paths)}): "
                + ", ".join(outside)
            )
        head = api.get_branch_head(self._owner, self._repo, options.branch)
        if head is None:
            return _Assessment(options, error=f"branch {options.branch!r} does not exist")
        if base_sha is not None and base_sha != head:
            moved = [
                path
                for path in manifest
                if api.get_file_content(self._owner, self._repo, path, ref=base_sha)
                != api.get_file_content(self._owner, self._repo, path, ref=head)
            ]
            if moved:
                return _Assessment(
                    options,
                    skipped=(
                        f"the branch moved on from {base_sha[:12]} to {head[:12]} and changed "
                        f"{', '.join(moved)} since the files were prepared; "
                        "the next run starts over"
                    ),
                )
        heads = {
            path: api.get_file_content(self._owner, self._repo, path, ref=head) for path in manifest
        }
        return _Assessment(options, head=head, heads=heads)

    def _commit(
        self,
        assessment: _Assessment,
        changed: list[str],
        local: dict[str, Path],
        version: str,
        warnings: tuple[str, ...],
    ) -> SyncOutcome:
        options = assessment.options
        assert options is not None  # noqa: S101 - narrowing for mypy, checked by the caller
        body = "Generated by lktec-bot:\n" + "".join(f"- {path}\n" for path in changed)
        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.COMMIT,
                args={
                    "branch": options.branch,
                    "message": options.headline(version),
                    "body": body,
                    "add": [f"{path}={local[path]}" for path in changed],
                    "expected_head": assessment.head,
                },
            )
        )
        if not result.ok:
            return _failed(result, warnings, "commit")
        return SyncOutcome(
            Decision.COMMITTED,
            f"{result.message}: {', '.join(changed)}",
            result.url,
            (*warnings, *result.warnings),
        )


def _failed(result: VerbResult, warnings: tuple[str, ...], step: str = "") -> SyncOutcome:
    decision = Decision.DENIED if result.outcome is VerbOutcome.DENIED else Decision.ERROR
    reason = f"{step}: {result.message}" if step else result.message
    return SyncOutcome(decision, reason, result.url, (*warnings, *result.warnings))
