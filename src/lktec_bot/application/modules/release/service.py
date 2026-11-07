# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``release`` module's command: ``run`` decides whether the head of the release branch
carries a release and, unless it is a dry run, publishes it with the ``release`` verb.

Everything before the publish is a read through :meth:`Gate.read` (a token narrowed to
``contents:read``): the head of the branch, the tags, the commits since the highest tag. The
write is the verb, with its own ``contents:write`` token. Nothing here pushes, moves or deletes
a tag; the verb creates it together with the release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from lktec_bot.application.gate import Gate, VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.modules.release.commits import (
    bump_for,
    bumped,
    format_version,
    highest_version,
    parse_commit,
    parse_version,
)
from lktec_bot.application.modules.release.notes import render_notes
from lktec_bot.application.modules.release.options import Bump, ReleaseOptions
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import Policy
from lktec_bot.ports.github_api import CommitInfo, GitHubApi
from lktec_bot.text import escape_markdown

MODULE = "release"
READ_PERMISSIONS = {"contents": "read"}
DEFAULT_SERVER = "https://github.com"
_OID_LENGTHS = (40, 64)


class Decision(StrEnum):
    """The outcome of ``run``."""

    RELEASED = "released"
    WOULD_RELEASE = "would-release"
    NONE = "none"
    SKIPPED = "skipped"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    decision: Decision
    reason: str
    version: str = ""
    tag: str = ""
    url: str | None = None
    notes: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return {Decision.DENIED: 3, Decision.ERROR: 1}.get(self.decision, 0)

    @property
    def result(self) -> str:
        """The coarse ``result`` output: ``ok``, ``denied`` or ``error``."""
        return {Decision.DENIED: "denied", Decision.ERROR: "error"}.get(self.decision, "ok")

    def to_markdown(self, repository: str) -> str:
        head = f"**{self.decision.value.upper()}** `release run` on `{repository}`"
        text = f"{head}: {escape_markdown(self.reason)}"
        if self.url:
            text += f"\n\n<{self.url}>"
        for warning in self.warnings:
            text += f"\n\nWarning: {escape_markdown(warning)}"
        return text + "\n"


@dataclass(frozen=True, slots=True)
class _Plan:
    """What the read found out: a reason to stop, or the release to make."""

    skip: str | None = None
    nothing: str | None = None
    head: str = ""
    version: str = ""
    tag: str = ""
    notes: str = ""


class Release:
    """Releases the head of one repository's release branch through a :class:`Gate`."""

    def __init__(
        self, gate: Gate, owner: str, repo: str, *, server_url: str = DEFAULT_SERVER
    ) -> None:
        self._gate = gate
        self._owner = owner
        self._repo = repo
        self._server_url = server_url

    def run(
        self, sha: str | None = None, *, dry_run: bool = False, today: date | None = None
    ) -> ReleaseOutcome:
        """Release ``sha`` (default: the branch head) when its history since the last tag holds
        a releasable commit.

        A ``sha`` that is no longer the head of the branch is skipped: the push that moved it
        has a run of its own. With ``dry_run`` nothing is written and no write token is minted.
        """
        day = today or datetime.now(UTC).date()
        read = self._gate.read(
            self._owner,
            self._repo,
            MODULE,
            READ_PERMISSIONS,
            lambda api, policy: self._plan(api, policy, sha, day),
        )
        warnings = read.warnings
        if read.failure is not None or read.value is None:
            failure = read.failure or VerbResult(VerbOutcome.ERROR, "nothing was read")
            return _failed(failure, warnings)
        plan = read.value
        if plan.skip is not None:
            return ReleaseOutcome(Decision.SKIPPED, plan.skip, warnings=warnings)
        if plan.nothing is not None:
            return ReleaseOutcome(Decision.NONE, plan.nothing, warnings=warnings)
        if dry_run:
            reason = f"would release {plan.tag} at {plan.head}"
            return ReleaseOutcome(
                Decision.WOULD_RELEASE, reason, plan.version, plan.tag, None, plan.notes, warnings
            )

        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.RELEASE,
                args={
                    "tag": plan.tag,
                    "sha": plan.head,
                    "name": plan.tag,
                    "body": plan.notes,
                    "latest": True,
                    "prerelease": False,
                },
            )
        )
        if not result.ok:
            return _failed(result, warnings, plan)
        return ReleaseOutcome(
            Decision.RELEASED,
            result.message,
            plan.version,
            plan.tag,
            result.url,
            plan.notes,
            (*warnings, *result.warnings),
        )

    def _plan(self, api: GitHubApi, policy: Policy, sha: str | None, day: date) -> _Plan:
        options = ReleaseOptions.model_validate(policy.modules[MODULE].options)
        if sha is not None:
            # Git prints object ids in lower case; a caller may still hand one over upper-cased.
            sha = sha.lower()
            if len(sha) not in _OID_LENGTHS or not _is_hex(sha):
                raise ValueError(f"--sha {sha!r} is not a full commit id")
        head = api.get_branch_head(self._owner, self._repo, options.branch)
        if head is None:
            raise RuntimeError(f"the release branch {options.branch!r} does not exist")
        if sha is not None and sha != head:
            return _Plan(
                skip=(
                    f"{options.branch} moved on from {sha[:12]} to {head[:12]}; "
                    "the run for the newer push releases"
                )
            )

        previous = highest_version(
            api.list_tags(self._owner, self._repo, options.prefix), options.prefix, options.suffix
        )
        commits = self._commits_since(api, previous[1] if previous else None, head)
        parsed = [
            (analysed, bump_for(analysed, options.rules))
            for commit in commits
            if (analysed := parse_commit(commit)) is not None
        ]
        bump = max((bump for _, bump in parsed), default=Bump.NONE)
        since = f"{previous[1]}" if previous else "the start of the branch"
        if bump is Bump.NONE:
            return _Plan(
                nothing=(
                    f"no releasable commits since {since} "
                    f"({len(commits)} commit(s), {len(parsed)} conventional)"
                ),
                head=head,
            )

        if previous:
            version = format_version(bumped(previous[0], bump))
        else:
            first = parse_version(options.first_version)
            version = format_version(first) if first else options.first_version
        tag = options.tag_for(version)
        notes = render_notes(
            server_url=self._server_url,
            repository=f"{self._owner}/{self._repo}",
            version=version,
            tag=tag,
            previous_tag=previous[1] if previous else None,
            day=day,
            commits=parsed,
            options=options,
        )
        return _Plan(head=head, version=version, tag=tag, notes=notes)

    def _commits_since(self, api: GitHubApi, tag: str | None, head: str) -> list[CommitInfo]:
        """The commits ``head`` adds to ``tag``, oldest first; all of its history without a tag."""
        if tag is None:
            return list(reversed(api.list_commits(self._owner, self._repo, head)))
        comparison = api.compare_commits(self._owner, self._repo, tag, head)
        if comparison.status not in ("ahead", "identical"):
            raise RuntimeError(
                f"the tag {tag} is not an ancestor of {head[:12]} (compare says "
                f"{comparison.status!r}); refusing to release from a history that does not "
                "contain the last release"
            )
        if comparison.total_commits > len(comparison.commits):
            raise RuntimeError(
                f"the comparison holds {comparison.total_commits} commits but only "
                f"{len(comparison.commits)} could be read; refusing to release from a partial list"
            )
        return list(comparison.commits)


def _is_hex(text: str) -> bool:
    return all(char in "0123456789abcdef" for char in text)


def _failed(
    result: VerbResult, warnings: tuple[str, ...], plan: _Plan | None = None
) -> ReleaseOutcome:
    decision = Decision.DENIED if result.outcome is VerbOutcome.DENIED else Decision.ERROR
    return ReleaseOutcome(
        decision,
        result.message,
        plan.version if plan else "",
        plan.tag if plan else "",
        result.url,
        warnings=(*warnings, *result.warnings),
    )
