# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``automerge`` module's commands: ``arm`` one pull request, ``sweep`` all of them.

Both only read through :meth:`Gate.read` (a token narrowed to ``pull_requests:read``) and write
through verbs (``approve``, ``enable-automerge``, ``merge``, ``update-branch``, ``comment``),
each with its own narrowed token. GitHub merges once the required checks are green. The one
exception is the ``merge-ready`` option: a pull request GitHub refuses to arm because it is
already mergeable is merged by the ``merge`` verb, bound to the head it was decided on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from lktec_bot.application.gate import Gate, VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.modules.automerge.classify import (
    DEPENDABOT_LOGIN,
    WORKFLOWS_DIR,
    Eligibility,
    classify,
    evaluate,
    touches_workflow_files,
)
from lktec_bot.application.modules.automerge.options import AutomergeOptions
from lktec_bot.domain.permissions import Target, Verb
from lktec_bot.domain.policy import Policy
from lktec_bot.ports.github_api import GitHubApi, PullRequest
from lktec_bot.text import escape_markdown

MODULE = "automerge"
READ_PERMISSIONS = {"pull_requests": "read"}
# Dependabot takes commands only from people with push access: a comment by a GitHub App
# gets "Sorry, only users with push access can use that command." So the sweep cannot ask
# for a rebuild itself; it leaves one hint per head that tells a maintainer what to do.
RECREATE_MARKER = "automerge-recreate"
RECREATE_HINT = (
    "lktec-bot cannot rebuild this pull request: {why}. Dependabot takes commands only from "
    "people with push access, so a maintainer needs to comment `@dependabot recreate`; the "
    "sweep arms the rebuilt pull request again."
)
READY_MARKER = "automerge-ready"
READY_ACTION = "approved; already mergeable, needs a person to merge"
READY_HINT = (
    "lktec-bot approved this pull request, but GitHub does not enable auto-merge on a pull "
    "request that can be merged right away (state: {state}). Please merge it."
)
MERGED_ACTION = "approved, merged (already mergeable, auto-merge refused)"
WHY_CONFLICT = "it conflicts with its base"
WHY_UNSIGNED = (
    "it has a commit GitHub does not verify ({reason}), which a ruleset that requires signed "
    "commits will not merge"
)
# GitHub computes ``mergeable_state`` lazily after a push; UNKNOWN is polled for a while.
UNKNOWN = "UNKNOWN"
BEHIND = "BEHIND"
DIRTY = "DIRTY"


class Decision(StrEnum):
    """The outcome of ``arm`` for one pull request."""

    ARMED = "armed"
    MERGED = "merged"  # was mergeable right now, so GitHub did not arm it; the merge verb merged
    READY = "ready"  # approved, but mergeable right now: GitHub does not arm it, a person merges
    SKIPPED = "skipped"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ArmResult:
    number: int
    decision: Decision
    reason: str
    url: str | None = None
    warnings: tuple[str, ...] = ()
    state: str = ""  # GitHub's mergeable status, set with ``Decision.READY``

    @property
    def exit_code(self) -> int:
        return {Decision.DENIED: 3, Decision.ERROR: 1}.get(self.decision, 0)

    def to_markdown(self, repository: str) -> str:
        head = f"**{self.decision.value.upper()}** `automerge arm` #{self.number} on `{repository}`"
        text = f"{head}: {escape_markdown(self.reason)}"
        if self.url:
            text += f"\n\n<{self.url}>"
        for warning in self.warnings:
            text += f"\n\nWarning: {escape_markdown(warning)}"
        return text + "\n"


@dataclass(slots=True)
class Row:
    """What the sweep did with one pull request, for the summary table."""

    number: int
    author: str
    state: str = ""
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = ""

    def detail(self) -> str:
        parts = [*self.actions, *(f"ERROR {error}" for error in self.errors)]
        if self.note:
            parts.append(self.note)
        return "; ".join(parts) or "nothing to do"


@dataclass(frozen=True, slots=True)
class SweepResult:
    rows: tuple[Row, ...] = ()
    armed: int = 0
    merged: int = 0
    ready: int = 0
    approved: int = 0
    updated: int = 0
    recreated: int = 0
    errors: int = 0
    failure: VerbResult | None = None
    warnings: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        if self.failure is None:
            return 0
        return 3 if self.failure.outcome is VerbOutcome.DENIED else 1

    def to_markdown(self, repository: str) -> str:
        if self.failure is not None:
            label = self.failure.outcome.value.upper()
            line = f"**{label}** `automerge sweep` on `{repository}`: "
            return line + f"{escape_markdown(self.failure.message)}\n"
        lines = [
            f"### automerge sweep on `{repository}`",
            "",
            f"armed {self.armed}, merged {self.merged}, ready {self.ready}, "
            f"re-approved {self.approved}, updated {self.updated}, "
            f"recreate needed {self.recreated}, errors {self.errors}",
            "",
        ]
        if self.rows:
            lines += ["| PR | Author | State | What happened |", "| --- | --- | --- | --- |"]
            lines += [
                f"| #{row.number} | {escape_markdown(row.author)} | "
                f"{escape_markdown(row.state or '-')} | {escape_markdown(row.detail())} |"
                for row in self.rows
            ]
        else:
            lines.append("No open pull requests.")
        for warning in self.warnings:
            lines += ["", f"Warning: {escape_markdown(warning)}"]
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _Assessment:
    options: AutomergeOptions
    pull_request: PullRequest
    eligibility: Eligibility


@dataclass(frozen=True, slots=True)
class _Entry:
    pull_request: PullRequest  # as of the survey, with ``merge_state`` when it was needed
    eligibility: Eligibility | None  # Dependabot pull requests only
    # GitHub's verification reason of the first unsigned commit; read only for a Dependabot pull
    # request the sweep is responsible for that is not conflicting. ``None``: all signed.
    unsigned: str | None = None

    @property
    def dependabot(self) -> bool:
        return self.pull_request.author_login == DEPENDABOT_LOGIN

    @property
    def auto_merge(self) -> bool:
        return self.pull_request.auto_merge_method is not None


@dataclass(frozen=True, slots=True)
class _Survey:
    options: AutomergeOptions
    entries: tuple[_Entry, ...]


class Automerge:
    """Arms and sweeps the pull requests of one repository through a :class:`Gate`."""

    def __init__(
        self,
        gate: Gate,
        owner: str,
        repo: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_attempts: int = 6,
        poll_interval: float = 10.0,
    ) -> None:
        self._gate = gate
        self._owner = owner
        self._repo = repo
        self._sleep = sleep
        self._poll_attempts = max(1, poll_attempts)
        self._poll_interval = poll_interval

    # -- arm -------------------------------------------------------------------------------

    def arm(self, number: int) -> ArmResult:
        """Classify pull request ``number`` and, when it is routine, approve and arm it."""
        read = self._gate.read(
            self._owner,
            self._repo,
            MODULE,
            READ_PERMISSIONS,
            lambda api, policy: self._assess(api, policy, number),
        )
        warnings = read.warnings
        if read.failure is not None or read.value is None:
            failure = read.failure or VerbResult(VerbOutcome.ERROR, "nothing was read")
            return _failed(number, failure, warnings)
        assessment = read.value
        pull_request = assessment.pull_request
        if not assessment.eligibility.arm:
            return ArmResult(
                number,
                Decision.SKIPPED,
                assessment.eligibility.reason,
                pull_request.html_url,
                warnings,
            )
        return self._arm_verbs(
            number, assessment.options, assessment.eligibility, warnings, pull_request.head_sha
        )

    def _assess(self, api: GitHubApi, policy: Policy, number: int) -> _Assessment:
        options = _options(policy)
        pull_request = api.get_pull_request(self._owner, self._repo, number)
        if not pull_request.open:
            eligibility = Eligibility(False, f"pull request is {pull_request.state}, not open")
        elif pull_request.draft:
            eligibility = Eligibility(False, "pull request is a draft")
        else:
            eligibility = self._eligibility(api, pull_request, options)
        return _Assessment(options, pull_request, eligibility)

    def _eligibility(
        self, api: GitHubApi, pull_request: PullRequest, options: AutomergeOptions
    ) -> Eligibility:
        """The module's decision for one open, non-draft pull request."""
        if pull_request.author_login != DEPENDABOT_LOGIN:
            return Eligibility(False, f"not opened by {DEPENDABOT_LOGIN}")
        eligibility = evaluate(classify(pull_request.title, pull_request.body), options)
        if eligibility.arm and options.workflow_files == "manual":
            files = api.list_pull_request_files(self._owner, self._repo, pull_request.number)
            if touches_workflow_files(files):
                return Eligibility(
                    False,
                    f"changes files under {WORKFLOWS_DIR} (workflow-files: manual)",
                    eligibility.kind,
                )
        return eligibility

    def _arm_verbs(
        self,
        number: int,
        options: AutomergeOptions,
        eligibility: Eligibility,
        warnings: tuple[str, ...],
        head_sha: str,
        attempts: int | None = None,
    ) -> ArmResult:
        approve = self._verb(Verb.APPROVE, number)
        if not approve.ok:
            return _failed(number, approve, warnings, "approve")
        # The arm job races the registration of the required checks and retries; the sweep sees
        # the settled state, so one try is enough there.
        extra: dict[str, object] = {} if attempts is None else {"attempts": attempts}
        enable = self._verb(Verb.ENABLE_AUTOMERGE, number, method=options.method, **extra)
        if not enable.ok:
            return _failed(number, enable, (*warnings, *approve.warnings), "enable-automerge")
        if enable.outputs.get("decision") == "ready":
            # GitHub does not arm a pull request that can be merged right now; the arm job is
            # green on purpose, a red check would only keep the pull request UNSTABLE.
            carried = (*warnings, *approve.warnings, *enable.warnings)
            if options.merge_ready:
                merge = self._verb(
                    Verb.MERGE, number, method=options.method, expected_head_sha=head_sha
                )
                carried = (*carried, *merge.warnings)
                if merge.ok:
                    return ArmResult(
                        number,
                        Decision.MERGED,
                        f"{eligibility.reason}: {MERGED_ACTION}",
                        merge.url or enable.url,
                        carried,
                    )
                # A refusal (or a failed call) falls back to the person-merges path.
                carried = (*carried, f"#{number}: merge not done: {merge.message}")
            return ArmResult(
                number,
                Decision.READY,
                f"{eligibility.reason}: {READY_ACTION}",
                enable.url,
                carried,
                enable.outputs.get("state", ""),
            )
        reason = f"{eligibility.reason}: approved, auto-merge ({options.method}) enabled"
        return ArmResult(
            number,
            Decision.ARMED,
            reason,
            enable.url,
            (*warnings, *approve.warnings, *enable.warnings),
        )

    # -- sweep -----------------------------------------------------------------------------

    def sweep(self) -> SweepResult:
        """Arm, keep up to date and unstick every open pull request of the repository."""
        read = self._gate.read(self._owner, self._repo, MODULE, READ_PERMISSIONS, self._survey)
        if read.failure is not None or read.value is None:
            failure = read.failure or VerbResult(VerbOutcome.ERROR, "nothing was read")
            return SweepResult(failure=failure, warnings=read.warnings)
        survey = read.value
        rows: list[Row] = []
        counts = {"armed": 0, "merged": 0, "ready": 0, "approved": 0, "updated": 0, "recreated": 0}
        warnings = list(read.warnings)
        for entry in survey.entries:
            row = self._sweep_one(entry, survey.options, counts, warnings)
            rows.append(row)
        return SweepResult(
            rows=tuple(rows),
            armed=counts["armed"],
            merged=counts["merged"],
            ready=counts["ready"],
            approved=counts["approved"],
            updated=counts["updated"],
            recreated=counts["recreated"],
            errors=sum(len(row.errors) for row in rows),
            warnings=tuple(warnings),
        )

    def _survey(self, api: GitHubApi, policy: Policy) -> _Survey:
        options = _options(policy)
        entries: list[_Entry] = []
        for listed in api.list_pull_requests(self._owner, self._repo):
            if not listed.open or listed.draft:
                continue
            dependabot = listed.author_login == DEPENDABOT_LOGIN
            eligibility = self._eligibility(api, listed, options) if dependabot else None
            auto_merge = listed.auto_merge_method is not None
            # A Dependabot pull request that is left for a person (excluded, a major, ...) and
            # not armed is not touched, so its merge state is not needed either.
            needs_state = (
                auto_merge or not dependabot or (eligibility is not None and eligibility.arm)
            )
            pull_request = self._with_merge_state(api, listed) if needs_state else listed
            unsigned = None
            if dependabot and needs_state and pull_request.merge_state != DIRTY:
                # A commit GitHub does not verify can never satisfy a signature ruleset.
                unsigned = self._unsigned_reason(api, pull_request.number)
            entries.append(_Entry(pull_request, eligibility, unsigned))
        return _Survey(options, tuple(entries))

    def _unsigned_reason(self, api: GitHubApi, number: int) -> str | None:
        for commit in api.list_pull_request_commits(self._owner, self._repo, number):
            if not commit.verified:
                return commit.reason
        return None

    def _with_merge_state(self, api: GitHubApi, listed: PullRequest) -> PullRequest:
        pull_request = listed
        for attempt in range(self._poll_attempts):
            pull_request = api.get_pull_request(self._owner, self._repo, listed.number)
            if pull_request.merge_state != UNKNOWN:
                break
            if attempt + 1 < self._poll_attempts:
                self._sleep(self._poll_interval)
        return pull_request

    def _sweep_one(
        self,
        entry: _Entry,
        options: AutomergeOptions,
        counts: dict[str, int],
        warnings: list[str],
    ) -> Row:
        pull_request = entry.pull_request
        number = pull_request.number
        row = Row(number, pull_request.author_login or "unknown", pull_request.merge_state)
        auto_merge = entry.auto_merge
        just_armed = False
        eligibility = entry.eligibility

        if entry.unsigned is not None:
            # A recreate replaces the head, so nothing else is done to this one in this run.
            self._recreate(
                pull_request,
                row,
                warnings,
                counts,
                f"has an unsigned commit ({entry.unsigned}), a maintainer needs to recreate it",
                WHY_UNSIGNED.format(reason=entry.unsigned),
            )
            return row

        if entry.dependabot and eligibility is not None and not auto_merge:
            if eligibility.excluded:
                row.note = f"left alone ({eligibility.reason})"
                return row
            if not eligibility.arm:
                row.note = f"left for a person ({eligibility.reason})"
                return row
            armed = self._arm_verbs(
                number, options, eligibility, (), pull_request.head_sha, attempts=1
            )
            warnings.extend(armed.warnings)
            if armed.decision is Decision.MERGED:
                row.actions.append(armed.reason)
                counts["merged"] += 1
                return row
            if armed.decision is Decision.READY:
                # Approved and mergeable now: nothing else to do to it, a person merges it.
                row.actions.append(READY_ACTION)
                counts["ready"] += 1
                self._hint_ready(pull_request, armed.state, warnings)
                return row
            if armed.decision is not Decision.ARMED:
                row.errors.append(f"{armed.decision.value}: {armed.reason}")
                return row
            row.actions.append(armed.reason)
            counts["armed"] += 1
            auto_merge = True
            just_armed = True

        updated = False
        # A branch the sweep has just armed is not touched: its checks are not queued yet and
        # a new head would only start them over. The next tick looks at it.
        if pull_request.merge_state == BEHIND and (auto_merge or not entry.dependabot):
            if just_armed:
                row.note = "behind its base, left for the next tick (just armed)"
            else:
                updated = self._update_branch(number, row, warnings, counts)

        if entry.dependabot and auto_merge:
            if pull_request.merge_state == DIRTY:
                self._recreate(pull_request, row, warnings, counts)
            elif (
                not just_armed
                and not updated
                and eligibility is not None
                and eligibility.arm
                and not eligibility.excluded
            ):
                # A new head after update-branch or a rebase needs a review of its own.
                self._reapprove(number, row, warnings, counts)
        if entry.dependabot and eligibility is not None and eligibility.excluded:
            row.note = f"{eligibility.reason}: kept mergeable, never approved here"
        return row

    def _update_branch(
        self, number: int, row: Row, warnings: list[str], counts: dict[str, int]
    ) -> bool:
        result = self._verb(Verb.UPDATE_BRANCH, number)
        warnings.extend(result.warnings)
        if not result.ok:
            row.errors.append(f"update-branch: {result.message}")
            return False
        row.actions.append("updated onto its base")
        counts["updated"] += 1
        return True

    def _recreate(
        self,
        pull_request: PullRequest,
        row: Row,
        warnings: list[str],
        counts: dict[str, int],
        action: str = "has conflicts, a maintainer needs to recreate it",
        why: str = WHY_CONFLICT,
    ) -> None:
        # One marker per head: the hint is posted once for a head, and again when Dependabot or
        # someone pushed a new one and it still needs a rebuild.
        marker = f"{RECREATE_MARKER}-{pull_request.head_sha[:12]}"
        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.COMMENT,
                target=Target.PULL_REQUEST,
                args={
                    "number": pull_request.number,
                    "body": RECREATE_HINT.format(why=why),
                    "marker": marker,
                },
            )
        )
        warnings.extend(result.warnings)
        if not result.ok:
            row.errors.append(f"comment: {result.message}")
            return
        row.actions.append(action)
        counts["recreated"] += 1

    def _hint_ready(self, pull_request: PullRequest, state: str, warnings: list[str]) -> None:
        # One marker per head, like the recreate hint. A failed hint costs nothing but the hint,
        # so it is a warning and never an error of the row.
        result = self._gate.run(
            VerbRequest(
                self._owner,
                self._repo,
                MODULE,
                Verb.COMMENT,
                target=Target.PULL_REQUEST,
                args={
                    "number": pull_request.number,
                    "body": READY_HINT.format(state=state),
                    "marker": f"{READY_MARKER}-{pull_request.head_sha[:12]}",
                },
            )
        )
        warnings.extend(result.warnings)
        if not result.ok:
            warnings.append(f"#{pull_request.number}: ready hint not posted: {result.message}")

    def _reapprove(
        self, number: int, row: Row, warnings: list[str], counts: dict[str, int]
    ) -> None:
        result = self._verb(Verb.APPROVE, number)
        warnings.extend(result.warnings)
        if not result.ok:
            row.errors.append(f"approve: {result.message}")
        elif "already approved" not in result.message:
            row.actions.append("approved the current head")
            counts["approved"] += 1

    # -- verbs -----------------------------------------------------------------------------

    def _verb(self, verb: Verb, number: int, **args: object) -> VerbResult:
        return self._gate.run(
            VerbRequest(self._owner, self._repo, MODULE, verb, args={"number": number, **args})
        )


def _options(policy: Policy) -> AutomergeOptions:
    """The module's options from a policy the gate has already validated."""
    config = policy.modules[MODULE]
    return AutomergeOptions.model_validate(config.options)


def _failed(
    number: int, result: VerbResult, warnings: tuple[str, ...], step: str = ""
) -> ArmResult:
    decision = Decision.DENIED if result.outcome is VerbOutcome.DENIED else Decision.ERROR
    reason = f"{step}: {result.message}" if step else result.message
    return ArmResult(number, decision, reason, result.url, (*warnings, *result.warnings))
