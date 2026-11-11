# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``lockfiles derive``: the use case that runs the policy's generators in the job without
secrets. Tested against fakes of the three ports; no git, no shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lktec_bot.application.modules.lockfiles import allowlist
from lktec_bot.application.modules.lockfiles.derive import (
    Derive,
    DeriveDecision,
    DeriveReport,
    generator_environment,
)
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.workspace import CommandResult, FileStat, PreparedTree, WorkspaceError

NOTICES = "THIRD-PARTY-NOTICES.md"
ROOT = Path("/work/derived-tree")
BASE = "b" * 40


def policy(*generators: str, enabled: bool = True) -> str:
    text = f"version: 1\nmodules:\n  lockfiles:\n    enabled: {str(enabled).lower()}\n"
    if generators:
        text += "    derived:\n" + "".join(generators)
    return text


def entry(name: str = "notices", run: str = "make notices", paths: str = NOTICES) -> str:
    return f"      - name: {name}\n        run: {run}\n        paths: [{paths}]\n"


@dataclass
class FakeTree:
    """The prepared tree as a dict; ``links`` are paths that are not regular files."""

    policy_text: str | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    links: set[str] = field(default_factory=set)
    head_files: dict[str, bytes] = field(default_factory=dict)
    overlaid: tuple[str, ...] = ("Directory.Packages.props",)
    ignored: tuple[str, ...] = ()
    prepare_error: str | None = None
    prepared: int = 0
    asked: list[str] = field(default_factory=list)

    def trusted_file(self, path: str) -> str | None:
        self.asked.append(path)
        return self.policy_text

    def prepare(self, is_dependency_file: Callable[[str], bool]) -> PreparedTree:
        if self.prepare_error is not None:
            raise WorkspaceError(self.prepare_error)
        self.prepared += 1
        assert is_dependency_file("uv.lock")
        return PreparedTree(ROOT, BASE, self.overlaid, self.ignored)

    def changes(self) -> Mapping[str, str]:
        return {path: content.hex() for path, content in self.files.items()} | {
            path: "link" for path in self.links
        }

    def stat(self, path: str) -> FileStat | None:
        if path in self.links:
            return FileStat(regular=False)
        if path not in self.files:
            return None
        return FileStat(regular=True, size=len(self.files[path]))

    def read(self, path: str) -> bytes:
        return self.files[path]

    def head(self, path: str) -> bytes | None:
        return self.head_files.get(path)


@dataclass
class FakeRunner:
    """Each command is a function that edits the tree and returns how it ended."""

    tree: FakeTree
    commands: dict[str, Callable[[FakeTree], CommandResult]] = field(default_factory=dict)
    ran: list[tuple[str, Path, int, dict[str, str]]] = field(default_factory=list)

    def run(
        self, command: str, cwd: Path, timeout_seconds: int, env: Mapping[str, str]
    ) -> CommandResult:
        self.ran.append((command, cwd, timeout_seconds, dict(env)))
        return self.commands[command](self.tree)


@dataclass
class FakeSink:
    added: list[dict[str, bytes]] = field(default_factory=list)
    error: OSError | None = None

    def add(self, files: Mapping[str, bytes]) -> None:
        if self.error is not None:
            raise self.error
        self.added.append(dict(files))


def writes(files: Mapping[str, bytes]) -> Callable[[FakeTree], CommandResult]:
    def command(tree: FakeTree) -> CommandResult:
        tree.files.update(files)
        return CommandResult(0)

    return command


def write_notices(content: bytes = b"new notices\n") -> Callable[[FakeTree], CommandResult]:
    return writes({NOTICES: content})


class Case:
    def __init__(self, policy_text: str | None, env: Mapping[str, str] | None = None) -> None:
        self.tree = FakeTree(policy_text=policy_text, head_files={NOTICES: b"old notices\n"})
        self.runner = FakeRunner(self.tree, {"make notices": write_notices()})
        self.sink = FakeSink()
        self.derive = Derive(self.tree, self.runner, self.sink, env or {"PATH": "/usr/bin"})

    def run(self) -> DeriveReport:
        return self.derive.run()


# -- the happy path -------------------------------------------------------------------------


def test_a_generator_runs_in_the_prepared_tree_and_its_file_is_handed_over() -> None:
    case = Case(policy(entry()))

    report = case.run()

    assert report.decision is DeriveDecision.GENERATED
    assert report.handed_over
    assert report.files == (NOTICES,)
    assert case.sink.added == [{NOTICES: b"new notices\n"}]
    assert case.tree.asked == [POLICY_PATH]
    assert case.runner.ran == [("make notices", ROOT, 300, {"PATH": "/usr/bin"})]
    assert "bbbbbbbbbbbb" in report.notes[0]
    assert "1 dependency file(s) of the pull request" in report.notes[0]


def test_the_timeout_of_the_policy_reaches_the_runner() -> None:
    case = Case(policy(entry() + "        timeout-minutes: 2\n"))

    case.run()

    assert case.runner.ran[0][2] == 120


def test_generators_run_in_order_and_everything_is_handed_over_together() -> None:
    case = Case(policy(entry(), entry("app", "make app", "apps/mobile/notices.json")))
    case.runner.commands["make app"] = writes({"apps/mobile/notices.json": b"{}\n"})

    report = case.run()

    assert [ran[0] for ran in case.runner.ran] == ["make notices", "make app"]
    assert report.files == ("THIRD-PARTY-NOTICES.md", "apps/mobile/notices.json")
    assert len(case.sink.added) == 1


def test_files_the_pull_request_changes_besides_manifests_are_named_as_unused() -> None:
    case = Case(policy(entry()))
    case.tree.ignored = tuple(f"tools/f{i}.py" for i in range(7))

    report = case.run()

    assert report.decision is DeriveDecision.GENERATED
    assert "7 file(s) the pull request changes are not dependency manifests" in report.notes[1]
    assert "tools/f4.py and 2 more" in report.notes[1]


# -- nothing to do --------------------------------------------------------------------------


def test_a_generated_file_equal_to_the_head_is_not_handed_over() -> None:
    case = Case(policy(entry()))
    case.runner.commands["make notices"] = write_notices(b"old notices\n")

    report = case.run()

    assert report.decision is DeriveDecision.UNCHANGED
    assert not report.handed_over
    assert case.sink.added == []


def test_only_the_generated_files_that_differ_are_handed_over() -> None:
    case = Case(policy(entry(paths=f"{NOTICES}, other.md")))
    case.tree.head_files["other.md"] = b"same\n"
    case.runner.commands["make notices"] = writes({NOTICES: b"new\n", "other.md": b"same\n"})

    report = case.run()

    assert report.files == (NOTICES,)
    assert case.sink.added == [{NOTICES: b"new\n"}]


@pytest.mark.parametrize(
    ("policy_text", "reason"),
    [
        (None, "is missing on the default branch"),
        ("version: 1\nmodules: {}\n", "module 'lockfiles' is not enabled"),
        (policy(entry(), enabled=False), "module 'lockfiles' is not enabled"),
        (policy(), "declares no derived files"),
        ("version: 2\n", "is not valid"),
        (policy(entry(paths=".github/workflows/ci.yml")), "is not valid"),
        (policy(entry()) + "    typo: 1\n", "is not valid"),
    ],
)
def test_without_a_valid_policy_that_declares_generators_nothing_runs(
    policy_text: str | None, reason: str
) -> None:
    case = Case(policy_text)

    report = case.run()

    assert report.decision is DeriveDecision.SKIPPED
    assert reason in report.reason
    assert case.tree.prepared == 0
    assert case.runner.ran == []
    assert case.sink.added == []


# -- failures: nothing is handed over ---------------------------------------------------------


def failed(case: Case) -> DeriveReport:
    report = case.run()
    assert report.decision is DeriveDecision.FAILED
    assert not report.handed_over
    assert report.reason.endswith("no derived file is handed over")
    assert case.sink.added == []
    return report


def test_a_generator_that_exits_non_zero_fails_with_its_name_and_output() -> None:
    case = Case(policy(entry()))
    case.runner.commands["make notices"] = lambda tree: CommandResult(3, output="boom\n")

    report = failed(case)

    assert "generator 'notices' failed with exit code 3" in report.reason
    assert report.output == "boom\n"


def test_a_generator_that_runs_out_of_time_fails() -> None:
    case = Case(policy(entry()))
    case.runner.commands["make notices"] = lambda tree: CommandResult(None, timed_out=True)

    report = failed(case)

    assert "generator 'notices' was stopped after 5 minute(s)" in report.reason


def test_a_generator_that_writes_outside_its_declared_paths_fails() -> None:
    case = Case(policy(entry()))
    case.runner.commands["make notices"] = writes(
        {NOTICES: b"new\n", "tools/check.py": b"evil", ".github/workflows/ci.yml": b"x"}
    )

    report = failed(case)

    assert "generator 'notices' wrote outside its declared paths" in report.reason
    assert ".github/workflows/ci.yml, tools/check.py" in report.reason


def test_a_generator_that_removes_another_file_fails() -> None:
    case = Case(policy(entry()))
    case.tree.files["Directory.Packages.props"] = b"<Project/>"

    def command(tree: FakeTree) -> CommandResult:
        del tree.files["Directory.Packages.props"]
        tree.files[NOTICES] = b"new\n"
        return CommandResult(0)

    case.runner.commands["make notices"] = command

    report = failed(case)

    assert "wrote outside its declared paths: Directory.Packages.props" in report.reason


def test_a_generator_may_not_write_the_paths_of_another_generator() -> None:
    case = Case(policy(entry(), entry("app", "make app", "app.json")))
    case.runner.commands["make notices"] = writes({NOTICES: b"n\n", "app.json": b"{}"})
    case.runner.commands["make app"] = writes({})

    report = failed(case)

    assert "generator 'notices' wrote outside its declared paths: app.json" in report.reason
    assert [ran[0] for ran in case.runner.ran] == ["make notices"]


def test_a_later_failure_drops_what_an_earlier_generator_produced() -> None:
    case = Case(policy(entry(), entry("app", "make app", "app.json")))
    case.runner.commands["make app"] = lambda tree: CommandResult(1)

    report = failed(case)

    assert "generator 'app' failed with exit code 1" in report.reason


def test_a_declared_file_that_was_not_produced_fails() -> None:
    case = Case(policy(entry()))
    case.runner.commands["make notices"] = writes({})

    assert f"generator 'notices' did not produce {NOTICES}" in failed(case).reason


def test_a_declared_path_that_is_not_a_regular_file_fails() -> None:
    case = Case(policy(entry()))
    case.tree.links.add(NOTICES)
    case.runner.commands["make notices"] = writes({})

    assert f"{NOTICES} is not a regular file" in failed(case).reason


def test_an_oversized_generated_file_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(allowlist, "MAX_DERIVED_FILE_BYTES", 4)
    case = Case(policy(entry()))

    assert "the derived file is larger than" in failed(case).reason


def test_a_file_that_grows_between_the_check_and_the_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = Case(policy(entry()))
    monkeypatch.setattr("lktec_bot.application.modules.lockfiles.derive.MAX_DERIVED_FILE_BYTES", 4)

    assert "grew while it was read" in failed(case).reason


def test_a_tree_that_cannot_be_prepared_fails_without_running_anything() -> None:
    case = Case(policy(entry()))
    case.tree.prepare_error = "git merge-base failed: no common ancestor"

    report = failed(case)

    assert "no common ancestor" in report.reason
    assert case.runner.ran == []


def test_a_handover_that_cannot_be_written_fails() -> None:
    case = Case(policy(entry()))
    case.sink.error = OSError("disk full")

    report = case.run()

    assert report.decision is DeriveDecision.FAILED
    assert "disk full" in report.reason


# -- the environment and the summary ----------------------------------------------------------


def test_a_generator_never_sees_credentials_or_the_runners_command_files() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/runner",
        "DOTNET_ROOT": "/opt/dotnet",
        "GITHUB_TOKEN": "ghs_x",
        "GH_TOKEN": "ghs_y",
        "ACTIONS_RUNTIME_TOKEN": "t",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://x.test",
        "LKTEC_BOT_PRIVATE_KEY": "pem",
        "LKTEC_BOT_APP_ID": "1",
        "GITHUB_ENV": "/run/env",
        "GITHUB_OUTPUT": "/run/out",
        "GITHUB_PATH": "/run/path",
        "GITHUB_STATE": "/run/state",
        "GITHUB_STEP_SUMMARY": "/run/summary",
        "GITHUB_SHA": "abc",
    }

    assert generator_environment(env) == {
        "PATH": "/usr/bin",
        "HOME": "/home/runner",
        "DOTNET_ROOT": "/opt/dotnet",
        "GITHUB_SHA": "abc",
    }

    case = Case(policy(entry()), env)
    case.run()
    assert case.runner.ran[0][3] == generator_environment(env)


def test_the_summary_names_the_decision_and_escapes_the_reason() -> None:
    report = DeriveReport(DeriveDecision.FAILED, "generator 'a_b' failed", notes=("in *tree*",))

    text = report.to_markdown()

    assert text.startswith("**FAILED** `lockfiles derive`: generator 'a\\_b' failed")
    assert "Note: in \\*tree\\*" in text
    assert text.endswith("\n")
