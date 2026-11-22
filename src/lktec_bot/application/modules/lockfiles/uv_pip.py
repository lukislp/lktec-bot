# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``lockfiles`` module's third command: ``uv-pip`` regenerates the requirements files a
repository produces with ``uv pip compile``, for the commit job to commit.

A Dependabot bump of such a file (``.clusterfuzzlite/requirements.txt``, say) can drop pins that
``uv pip compile --generate-hashes`` would have kept, such as a transitive ``setuptools``, and
``pip install --require-hashes`` then fails. The cure is the one ``uv.lock`` gets: run the tool
again and commit what it writes.

It runs in the regenerating job, which holds **no secret**; this command needs neither the gate
nor the App key and never talks to GitHub. Who controls what:

- Which files are compiled comes from ``uv-pip-compile`` in the policy of the **default
  branch**: for each file an input and an output path, both exact.
- The **command** is recorded in the output file's own header, which is text of the pull
  request. It is never run as written: :mod:`compile_command` accepts one fixed shape with a
  few flags, and only when it reads the configured input and writes the configured output.
  What runs is that argument list, started without a shell.
- The **result** is data: the output file's new content, handed over for the commit job, which
  admits the path only because the default branch's policy lists it.

A file whose header is missing or not accepted is skipped with the reason; the others still run.
Nothing here is a security boundary on its own, the commit job is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from lktec_bot.application.modules import validate_policy
from lktec_bot.application.modules.lockfiles.allowlist import MAX_DERIVED_FILE_BYTES
from lktec_bot.application.modules.lockfiles.compile_command import (
    CompileCommandError,
    header_command,
    parse_command,
)
from lktec_bot.application.modules.lockfiles.derive import generator_environment
from lktec_bot.application.modules.lockfiles.options import LockfilesOptions, UvPipCompileFile
from lktec_bot.domain.policy import POLICY_PATH, PolicyError
from lktec_bot.ports.workspace import (
    Checkout,
    HandoverSink,
    ProgramRunner,
    WorkspaceError,
)
from lktec_bot.text import escape_markdown

MODULE = "lockfiles"
TIMEOUT_SECONDS = 5 * 60


class UvPipDecision(StrEnum):
    """The outcome of ``uv-pip`` for one pull request."""

    GENERATED = "generated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UvPipReport:
    decision: UvPipDecision
    reason: str
    files: tuple[str, ...] = ()
    # One line per file that was skipped or failed, with its reason.
    notes: tuple[str, ...] = ()
    # The end of a failed run's output, for the log only.
    output: str = ""

    @property
    def handed_over(self) -> bool:
        return self.decision is UvPipDecision.GENERATED

    def to_markdown(self) -> str:
        text = (
            f"**{self.decision.value.upper()}** `lockfiles uv-pip`: {escape_markdown(self.reason)}"
        )
        for note in self.notes:
            text += f"\n\nNote: {escape_markdown(note)}"
        return text + "\n"


@dataclass(slots=True)
class _Progress:
    changed: dict[str, bytes] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    output: str = ""


class UvPip:
    """Compiles the requirements files of one repository's policy and hands over what changed.

    ``uv`` is the program to start (an absolute path to the uv the workflow installed, so a later
    installation of another uv in the job cannot replace it), ``root`` the pull request checkout
    the files are compiled in place in.
    """

    def __init__(
        self,
        checkout: Checkout,
        runner: ProgramRunner,
        sink: HandoverSink,
        env: Mapping[str, str],
        uv: str,
        root: Path,
    ) -> None:
        self._checkout = checkout
        self._runner = runner
        self._sink = sink
        self._env = generator_environment(env)
        self._uv = uv
        self._root = root

    def run(self) -> UvPipReport:
        try:
            options = self._options()
            if isinstance(options, UvPipReport):
                return options
            progress = _Progress()
            for entry in options.uv_pip_compile:
                self._compile(entry, progress)
            if progress.changed:
                self._sink.add(progress.changed)
        except (WorkspaceError, OSError) as exc:
            return UvPipReport(UvPipDecision.FAILED, f"{exc}; no file is handed over")
        return _report(progress)

    def _options(self) -> LockfilesOptions | UvPipReport:
        """The module's options from the trusted branch, or why there is nothing to do."""
        text = self._checkout.trusted_file(POLICY_PATH)
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
        if not options.uv_pip_compile:
            return _skipped("the policy declares no uv pip compile files")
        return options

    def _compile(self, entry: UvPipCompileFile, progress: _Progress) -> None:
        output = entry.output
        before = self._checkout.head(output)
        if before is None:
            progress.skipped.append(f"{output}: not a regular file in the pull request")
            return
        if self._checkout.head(entry.input) is None:
            progress.skipped.append(f"{output}: its input {entry.input} is not a regular file")
            return
        recorded = header_command(before.decode("utf-8", errors="replace"))
        if recorded is None:
            progress.skipped.append(f"{output}: the file records no 'uv pip compile' command")
            return
        try:
            command = parse_command(recorded, entry.input, entry.output)
        except CompileCommandError as exc:
            progress.skipped.append(f"{output}: {exc}")
            return
        result = self._runner.run(
            [self._uv, *command.arguments], self._root, TIMEOUT_SECONDS, self._env
        )
        if result.timed_out or result.exit_code != 0:
            how = (
                f"was stopped after {TIMEOUT_SECONDS // 60} minute(s)"
                if result.timed_out
                else f"failed with exit code {result.exit_code}"
            )
            progress.failed.append(f"{output}: uv pip compile {how}")
            progress.output = result.output
            return
        after = self._checkout.head(output)
        if after is None:
            progress.failed.append(f"{output}: uv pip compile left no regular file there")
        elif len(after) > MAX_DERIVED_FILE_BYTES:
            progress.failed.append(f"{output}: the result is larger than the handover accepts")
        elif after == before:
            progress.unchanged.append(output)
        else:
            progress.changed[output] = after


def _report(progress: _Progress) -> UvPipReport:
    notes = tuple([*progress.skipped, *progress.failed])
    if progress.changed:
        files = tuple(sorted(progress.changed))
        return UvPipReport(UvPipDecision.GENERATED, f"handed over {', '.join(files)}", files, notes)
    if progress.failed:
        return UvPipReport(
            UvPipDecision.FAILED,
            "uv pip compile did not finish; no file is handed over",
            notes=notes,
            output=progress.output,
        )
    if progress.unchanged:
        return UvPipReport(
            UvPipDecision.UNCHANGED,
            "the compiled requirements files already match the pull request head",
            notes=notes,
        )
    return UvPipReport(UvPipDecision.SKIPPED, "no configured file could be compiled", notes=notes)


def _skipped(reason: str) -> UvPipReport:
    return UvPipReport(UvPipDecision.SKIPPED, reason)
