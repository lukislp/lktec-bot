# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.domain.permissions import Verb
from tests.fakes import FakeRepository
from tests.verb_helpers import HEAD, LOCKFILES, Harness

BRANCH = "dependabot/uv/ruff-0.13.1"
OTHER = "f" * 40


def harness(*, branches: dict[str, str] | None = None) -> tuple[Harness, FakeRepository]:
    state = FakeRepository(branches={BRANCH: HEAD} if branches is None else branches)
    return Harness(state, policy=LOCKFILES), state


def run(h: Harness, **args: object) -> VerbResult:
    return h.run(
        Verb.COMMIT, {"branch": BRANCH, "message": "chore: refresh", **args}, module="lockfiles"
    )


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    path = tmp_path / "uv.lock"
    path.write_bytes(b"version = 1\n\xc3\xa4\n")
    return path


def test_commit_adds_files_on_the_resolved_head_with_contents_write_only(lock: Path) -> None:
    h, state = harness()

    result = h.run(
        Verb.COMMIT,
        {
            "branch": BRANCH,
            "message": "chore(deps): refresh",
            "body": "why\n",
            "add": [f"uv.lock={lock}", f"sub/dir/a.txt={lock}"],
            "delete": ["old.lock"],
        },
        module="lockfiles",
    )

    assert result.outcome is VerbOutcome.OK
    assert result.message.startswith("created commit ")
    assert f"on {BRANCH} (2 file(s) written, 1 deleted)" in result.message
    assert result.url and result.url.startswith("https://github.com/acme/widgets/commit/")
    assert h.tokens.requests[1][2] == {"contents": "write"}
    assert state.commits == [
        (
            BRANCH,
            "chore(deps): refresh",
            "why\n",
            HEAD,
            {"uv.lock": lock.read_bytes(), "sub/dir/a.txt": lock.read_bytes()},
            ["old.lock"],
        )
    ]


def test_commit_passes_a_given_expected_head_through(lock: Path) -> None:
    h, state = harness()

    result = run(h, add=[f"uv.lock={lock}"], expected_head=HEAD)

    assert result.ok
    assert state.commits[0][3] == HEAD


def test_commit_refuses_when_the_branch_moved_since_the_expected_head(lock: Path) -> None:
    h, state = harness()

    result = run(h, add=[f"uv.lock={lock}"], expected_head=OTHER)

    assert result.outcome is VerbOutcome.ERROR
    assert f"expected {OTHER}, now at {HEAD}; nothing was committed" in result.message
    assert state.commits == []


def test_commit_needs_an_existing_branch(lock: Path) -> None:
    h, state = harness(branches={})

    result = run(h, add=[f"uv.lock={lock}"])

    assert result.message == f"branch {BRANCH!r} does not exist"
    assert state.commits == []


def test_commit_from_creates_the_missing_branch_at_the_head_of_the_source(lock: Path) -> None:
    h, state = harness(branches={"main": OTHER})

    result = run(h, add=[f"uv.lock={lock}"], from_branch="main")

    assert result.ok
    assert f"on {BRANCH} (new branch from main) (1 file(s) written, 0 deleted)" in result.message
    assert state.created_branches == [(BRANCH, OTHER)]
    # the commit sits on the head the branch was created at
    assert state.commits[0][3] == OTHER
    assert state.log == [f"create-branch {BRANCH}", f"commit {BRANCH}"]
    assert h.tokens.requests[1][2] == {"contents": "write"}


def test_commit_from_is_ignored_for_an_existing_branch_and_never_moves_it(lock: Path) -> None:
    h, state = harness(branches={BRANCH: HEAD, "main": OTHER})

    result = run(h, add=[f"uv.lock={lock}"], from_branch="main")

    assert result.ok
    assert "new branch" not in result.message
    assert state.created_branches == []
    assert state.commits[0][3] == HEAD


def test_commit_from_needs_the_source_branch_to_exist(lock: Path) -> None:
    h, state = harness(branches={})

    result = run(h, add=[f"uv.lock={lock}"], from_branch="main")

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == f"branch {BRANCH!r} does not exist and --from 'main' does not either"
    assert state.created_branches == []
    assert state.commits == []


def test_commit_from_still_honours_expected_head(lock: Path) -> None:
    h, state = harness(branches={"main": OTHER})

    result = run(h, add=[f"uv.lock={lock}"], from_branch="main", expected_head=HEAD)

    assert result.outcome is VerbOutcome.ERROR
    assert "moved" in result.message
    assert state.commits == []
    assert state.created_branches == []  # nothing is created for a commit that will not happen


@pytest.mark.parametrize(
    ("source", "message"),
    [("refs/heads/main", "must be a branch name, not a full ref"), (BRANCH, "another branch")],
)
def test_commit_from_refuses_bad_sources(source: str, message: str, lock: Path) -> None:
    h, state = harness(branches={})

    result = run(h, add=[f"uv.lock={lock}"], from_branch=source)

    assert result.outcome is VerbOutcome.ERROR
    assert message in result.message
    assert state.created_branches == []


def test_commit_deletes_only(tmp_path: Path) -> None:
    h, state = harness()

    result = run(h, delete=["a.lock", "b.lock"])

    assert result.ok
    assert state.commits[0][4:] == ({}, ["a.lock", "b.lock"])


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../x", "a/../../x", "C:/x", ".github/workflows/ci.yml", ".git/config", "a\\b"],
)
def test_commit_refuses_unsafe_paths_before_any_call(path: str, lock: Path) -> None:
    h, state = harness()

    added = run(h, add=[f"{path}={lock}"])
    deleted = run(h, delete=[path])

    for result in (added, deleted):
        assert result.outcome is VerbOutcome.ERROR
        assert result.message.startswith("refusing to commit: --")
    assert state.commits == []
    assert h.verb_api_calls == []


def test_workflow_refusal_names_the_missing_permission(lock: Path) -> None:
    h, _ = harness()

    result = run(h, add=[f".github/workflows/x.yml={lock}"])

    assert "no 'workflows' permission" in result.message


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "nothing to commit: give at least one --add or --delete"),
        ({"add": ["noequals"]}, "must look like <repo-path>=<local-file>"),
        ({"add": ["a="]}, "must look like <repo-path>=<local-file>"),
        ({"add": ["a=/definitely/not/there.lock"]}, "cannot read /definitely/not/there.lock"),
        ({"delete": ["a", "a"]}, "is given twice"),
        ({"expected_head": "abc"}, "is not a full commit id"),
        ({"message": "two\nlines", "delete": ["a"]}, "must be one line"),
        ({"branch": "refs/heads/x", "delete": ["a"]}, "not a full ref"),
        ({"add": 5}, "must be a list of strings"),
    ],
)
def test_commit_refuses_bad_arguments(args: dict[str, object], message: str) -> None:
    h, state = harness()

    result = run(h, **args)

    assert result.outcome is VerbOutcome.ERROR
    assert message in result.message
    assert state.commits == []


def test_commit_refuses_the_same_path_twice_and_added_plus_deleted(lock: Path) -> None:
    h, state = harness()

    twice = run(h, add=[f"uv.lock={lock}", f"uv.lock={lock}"])
    both = run(h, add=[f"uv.lock={lock}"], delete=["uv.lock"])

    assert "given twice" in twice.message
    assert "both added and deleted" in both.message
    assert state.commits == []


def test_commit_requires_branch_and_message() -> None:
    h, _ = harness()

    no_branch = h.run(Verb.COMMIT, {"message": "m", "delete": ["a"]}, module="lockfiles")
    no_message = h.run(Verb.COMMIT, {"branch": BRANCH, "delete": ["a"]}, module="lockfiles")

    assert "--branch is required" in no_branch.message
    assert "--message is required" in no_message.message


def test_commit_caps_the_total_size(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lktec_bot.application.verbs.commit.MAX_TOTAL_BYTES", 5)
    h, state = harness()

    result = run(h, add=[f"uv.lock={lock}"])

    assert "exceed" in result.message
    assert state.commits == []


def test_commit_is_denied_for_a_module_that_may_not_commit(lock: Path) -> None:
    h = Harness(FakeRepository(branches={BRANCH: HEAD}))

    result = h.gate.run(
        VerbRequest("acme", "widgets", "automerge", Verb.COMMIT, args={"branch": BRANCH})
    )

    assert result.outcome is VerbOutcome.DENIED
