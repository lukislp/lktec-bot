# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``lockfiles`` module's second command: ``derive`` the files a repository generates from
its dependency graph (third-party notices, ...), for the commit job to commit.

It runs in the regenerating job, after the lock files were refreshed. That job holds **no
secret** and its token can only read the repository; this command needs neither the gate nor
the App key and never talks to GitHub.

Who controls what:

- The **commands** come from ``derived`` in the policy of the default branch.
- The **tree** they run in is a commit of the default branch's history (the merge base of the
  pull request), in which only the dependency manifests and lock files are replaced by the pull
  request's version. A pull request can therefore not change a script a generator runs; what it
  can change is the dependency graph, which is the point.
- The **result** is data: files copied into the handover directory, and only the paths the
  generator declared. A generator that fails, runs out of time, changes anything else in the
  tree or produces something that is not a regular file of acceptable size makes the whole
  step hand over nothing; the lock files are not affected.

Nothing here is a security boundary on its own: a dependency can run code while a generator
restores or installs packages. The boundary is the commit job, which re-reads the policy
through the API and admits every path through the same allow-list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from lktec_bot.application.modules import validate_policy
from lktec_bot.application.modules.lockfiles.allowlist import MAX_DERIVED_FILE_BYTES, Kind, admit
from lktec_bot.application.modules.lockfiles.ecosystems import is_dependency_file
from lktec_bot.application.modules.lockfiles.options import DerivedGenerator, LockfilesOptions
from lktec_bot.domain.policy import POLICY_PATH, PolicyError
from lktec_bot.ports.workspace import (
    CommandRunner,
    HandoverSink,
    PreparedTree,
    SourceTree,
    WorkspaceError,
)
from lktec_bot.text import escape_markdown

MODULE = "lockfiles"
# How many paths a message names before it says "and N more".
MAX_LISTED = 5

# Never handed to a generator, whatever the job's environment holds. The job has no secret; this
# keeps it that way for the command even if a caller's workflow sets one of these.
SECRET_NAMES = frozenset(
    {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    }
)
SECRET_PREFIXES = ("LKTEC_BOT_",)
# The runner's command files: a generator must not set outputs, variables or the path of later
# steps, or write the job summary.
RUNNER_FILES = frozenset(
    {"GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE", "GITHUB_STEP_SUMMARY"}
)


class DeriveDecision(StrEnum):
    """The outcome of ``derive`` for one pull request."""

    GENERATED = "generated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeriveReport:
    decision: DeriveDecision
    reason: str
    files: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # The end of the failed generator's output, for the log only.
    output: str = ""

    @property
    def handed_over(self) -> bool:
        return self.decision is DeriveDecision.GENERATED

    def to_markdown(self) -> str:
        text = (
            f"**{self.decision.value.upper()}** `lockfiles derive`: {escape_markdown(self.reason)}"
        )
        for note in self.notes:
            text += f"\n\nNote: {escape_markdown(note)}"
        return text + "\n"


class _Failure(Exception):
    """A generator did not do what it declared; the text is the reason."""

    def __init__(self, reason: str, output: str = "") -> None:
        super().__init__(reason)
        self.output = output


def generator_environment(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without credentials and without the runner's command files."""
    return {
        name: value
        for name, value in env.items()
        if name not in SECRET_NAMES
        and name not in RUNNER_FILES
        and not name.startswith(SECRET_PREFIXES)
    }


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:MAX_LISTED])
    more = len(paths) - MAX_LISTED
    return f"{shown} and {more} more" if more > 0 else shown


class Derive:
    """Runs the generators of one repository's policy and hands over what they produced."""

    def __init__(
        self,
        tree: SourceTree,
        runner: CommandRunner,
        sink: HandoverSink,
        env: Mapping[str, str],
    ) -> None:
        self._tree = tree
        self._runner = runner
        self._sink = sink
        self._env = generator_environment(env)

    def run(self) -> DeriveReport:
        try:
            options = self._options()
            if isinstance(options, DeriveReport):
                return options
            prepared = self._tree.prepare(is_dependency_file)
            notes = _notes(prepared)
            try:
                produced = self._generate(options, prepared)
            except _Failure as failure:
                reason = f"{failure}; no derived file is handed over"
                return DeriveReport(
                    DeriveDecision.FAILED, reason, notes=notes, output=failure.output
                )
            changed = {
                path: content
                for path, content in produced.items()
                if self._tree.head(path) != content
            }
            if not changed:
                return DeriveReport(
                    DeriveDecision.UNCHANGED,
                    "the derived files already match the pull request head",
                    notes=notes,
                )
            self._sink.add(changed)
        except (WorkspaceError, OSError) as exc:
            return DeriveReport(DeriveDecision.FAILED, f"{exc}; no derived file is handed over")
        files = tuple(sorted(changed))
        return DeriveReport(
            DeriveDecision.GENERATED, f"handed over {', '.join(files)}", files, notes
        )

    def _options(self) -> LockfilesOptions | DeriveReport:
        """The module's options from the trusted branch, or why there is nothing to do."""
        text = self._tree.trusted_file(POLICY_PATH)
        if text is None:
            return _skipped(f"{POLICY_PATH} is missing on the default branch")
        try:
            policy = validate_policy(text)
        except PolicyError as exc:
            return _skipped(f"{POLICY_PATH} on the default branch is not valid: {exc}")
        config = policy.modules.get(MODULE)
        if config is None or not config.enabled:
            return _skipped(f"module {MODULE!r} is not enabled in {POLICY_PATH}")
        options = LockfilesOptions.model_validate(config.options)
        if not options.derived:
            return _skipped("the policy declares no derived files")
        return options

    def _generate(self, options: LockfilesOptions, prepared: PreparedTree) -> dict[str, bytes]:
        """Run every generator; the content of every declared path, or :class:`_Failure`."""
        produced: dict[str, bytes] = {}
        for generator in options.derived:
            before = self._tree.changes()
            result = self._runner.run(
                generator.run, prepared.root, generator.timeout_minutes * 60, self._env
            )
            if result.timed_out:
                raise _Failure(
                    f"generator {generator.name!r} was stopped after "
                    f"{generator.timeout_minutes} minute(s)",
                    result.output,
                )
            if result.exit_code != 0:
                raise _Failure(
                    f"generator {generator.name!r} failed with exit code {result.exit_code}",
                    result.output,
                )
            after = self._tree.changes()
            touched = sorted(
                path for path in {*before, *after} if before.get(path) != after.get(path)
            )
            outside = [path for path in touched if path not in generator.paths]
            if outside:
                raise _Failure(
                    f"generator {generator.name!r} wrote outside its declared paths: "
                    f"{_listed(outside)}"
                )
            for path in generator.paths:
                produced[path] = self._collect(generator, path, options)
        return produced

    def _collect(self, generator: DerivedGenerator, path: str, options: LockfilesOptions) -> bytes:
        stat = self._tree.stat(path)
        if stat is None:
            raise _Failure(f"generator {generator.name!r} did not produce {path}")
        if not stat.regular:
            raise _Failure(f"generator {generator.name!r}: {path} is not a regular file")
        # The same allow-list the commit job applies, so a refusal there is seen here first.
        admission = admit(path, stat.size, options)
        if admission.kind is not Kind.DERIVED:
            raise _Failure(f"generator {generator.name!r}: {path}: {admission.problem}")
        content = self._tree.read(path)
        if len(content) > MAX_DERIVED_FILE_BYTES:
            raise _Failure(f"generator {generator.name!r}: {path} grew while it was read")
        return content


def _skipped(reason: str) -> DeriveReport:
    return DeriveReport(DeriveDecision.SKIPPED, reason)


def _notes(prepared: PreparedTree) -> tuple[str, ...]:
    notes = [
        f"generators ran in the tree of {prepared.base[:12]} (default branch history) with "
        f"{len(prepared.overlaid)} dependency file(s) of the pull request"
    ]
    if prepared.ignored:
        notes.append(
            f"{len(prepared.ignored)} file(s) the pull request changes are not dependency "
            f"manifests and were not used: {_listed(list(prepared.ignored))}"
        )
    return tuple(notes)
