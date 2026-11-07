# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``sync`` module: options, handover checks, containment, and the commit it makes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lktec_bot.application.gate import Gate
from lktec_bot.application.modules import MODULES, SyncOptions, validate_policy
from lktec_bot.application.modules.sync import service
from lktec_bot.application.modules.sync.options import DEFAULT_COMMIT_MESSAGE
from lktec_bot.application.modules.sync.service import Decision, Sync
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import POLICY_PATH
from tests.fakes import FakeApiFactory, FakeGitHubApi, FakeRepository, FakeTokenProvider

HEAD = "0123456789abcdef0123456789abcdef01234567"
POLICY = "version: 1\nmodules:\n  sync:\n    enabled: true\n    paths: [k8s, deploy/app.yaml]\n"
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "lktec-bot.yml"


def policy(extra: str = "") -> str:
    return POLICY + "".join(f"    {line}\n" for line in extra.splitlines())


class Setup:
    def __init__(self, *, policy_text: str = POLICY, branch: str = "main"):
        self.state = FakeRepository(
            branches={branch: HEAD},
            ref_files={(HEAD, "k8s/web.yaml"): "image: x:1\n"},
        )
        self.tokens = FakeTokenProvider()
        self.factory = FakeApiFactory({POLICY_PATH: policy_text}, self.state)
        self.gate = Gate(self.tokens, self.factory, handlers())
        self.sync = Sync(self.gate, "acme", "widgets")

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


# -- options --------------------------------------------------------------------------------


def test_the_module_is_registered_with_its_verbs() -> None:
    assert MODULES["sync"].verbs == {Verb.COMMIT, Verb.OPEN_ISSUE, Verb.CHECK}
    assert MODULES["sync"].options is SyncOptions


def test_options_defaults() -> None:
    options = SyncOptions.model_validate({"paths": ["k8s"]})

    assert options.branch == "main"
    assert options.commit_message == DEFAULT_COMMIT_MESSAGE
    assert options.headline("1.2.3") == "chore(deploy): image 1.2.3 [skip ci]"
    assert options.headline("") == "chore(deploy): image  [skip ci]"


def test_options_accept_the_documented_keys() -> None:
    options = SyncOptions.model_validate(
        {"paths": ["data", "a.json"], "branch": "release/x", "commit-message": "data {version}"}
    )

    assert options.headline("7") == "data 7"
    assert options.branch == "release/x"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "Field required"),
        ({"paths": []}, "at least 1 item"),
        ({"paths": ["../x"]}, "'..' segment"),
        ({"paths": [".github/workflows"]}, "workflows"),
        ({"paths": ["a/.github/x", ".github/workflows/ci.yml"]}, "workflows"),
        ({"paths": ["k8s"], "branch": "refs/heads/main"}, "plain branch name"),
        ({"paths": ["k8s"], "branch": "a b"}, "plain branch name"),
        ({"paths": ["k8s"], "commit-message": " "}, "must not be empty"),
        ({"paths": ["k8s"], "commit-message": "a\nb"}, "single line"),
        ({"paths": ["k8s"], "commit-message": "x" * 201}, "at most 200"),
        ({"paths": ["k8s"], "typo": 1}, "Extra inputs"),
    ],
)
def test_options_reject_bad_values(raw: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SyncOptions.model_validate(raw)


def test_the_policy_template_validates_and_declares_the_sync_block() -> None:
    parsed = validate_policy(TEMPLATE.read_text(encoding="utf-8"))

    assert "sync" in parsed.modules


def test_a_policy_without_paths_is_invalid() -> None:
    with pytest.raises(Exception, match=r"modules\.sync\.paths"):
        validate_policy("version: 1\nmodules:\n  sync:\n    enabled: true\n")


# -- the handover ---------------------------------------------------------------------------


def test_a_bad_handover_is_an_error_before_any_token(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(
        tmp_path, {"k8s/web.yaml": b"x"}, manifest=["k8s/web.yaml", "../etc/passwd", "k8s/gone"]
    )

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    assert "'..' segment" in outcome.reason
    assert setup.tokens.minted == []


def test_a_workflow_path_in_the_manifest_is_refused(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {".github/workflows/ci.yml": b"x"})

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.ERROR
    assert "workflows" in outcome.reason
    assert setup.tokens.minted == []


def test_a_listed_file_that_is_missing_is_an_error(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"x"}, manifest=["k8s/web.yaml", "k8s/b.yaml"])

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.ERROR
    assert "k8s/b.yaml is missing" in outcome.reason


def test_a_missing_manifest_is_an_error(tmp_path: Path) -> None:
    outcome = Setup().sync.commit(tmp_path)

    assert outcome.decision is Decision.ERROR
    assert "cannot read manifest.txt" in outcome.reason


def test_a_version_with_odd_characters_is_refused(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1.0\nfix: pwned")

    assert outcome.decision is Decision.ERROR
    assert "version may only hold" in outcome.reason
    assert setup.tokens.minted == []


# -- containment and branch -----------------------------------------------------------------


def test_a_path_outside_the_policy_paths_denies_everything(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(
        tmp_path, {"k8s/web.yaml": b"new\n", "src/app.py": b"evil\n", "k8sx/a": b"x"}
    )

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.DENIED
    assert outcome.exit_code == 3
    assert outcome.result == "denied"
    assert "src/app.py, k8sx/a" in outcome.reason
    assert "k8s/web.yaml" not in outcome.reason.split(": ", 1)[1]
    assert setup.state.commits == []


def test_a_file_path_in_the_policy_matches_exactly(tmp_path: Path) -> None:
    setup = Setup()
    allowed = handover(tmp_path / "a", {"deploy/app.yaml": b"x"})
    other = handover(tmp_path / "b", {"deploy/other.yaml": b"x"})

    assert setup.sync.commit(allowed).decision is Decision.COMMITTED
    assert setup.sync.commit(other).decision is Decision.DENIED


def test_another_branch_than_the_policy_denies(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1.0.0", "develop")

    assert outcome.decision is Decision.DENIED
    assert "'main', not onto 'develop'" in outcome.reason
    assert setup.state.commits == []


def test_the_matching_branch_is_accepted(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    assert setup.sync.commit(changed, "1.0.0", "main").decision is Decision.COMMITTED


def test_a_repository_without_the_module_enabled_is_denied(tmp_path: Path) -> None:
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    off = Setup(policy_text=POLICY.replace("enabled: true", "enabled: false"))
    other = Setup(policy_text="version: 1\nmodules:\n  ping:\n    enabled: true\n")
    typo = Setup(policy_text=policy("brnach: main"))

    for setup in (off, other, typo):
        outcome = setup.sync.commit(changed)
        assert outcome.decision is Decision.DENIED
        assert outcome.exit_code == 3
        assert setup.state.commits == []


# -- commit ---------------------------------------------------------------------------------


def test_a_changed_file_is_committed_on_top_of_the_resolved_head(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:2\n"})

    outcome = setup.sync.commit(changed, "2.0.0", "main")

    assert outcome.decision is Decision.COMMITTED
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    assert outcome.url and "/commit/" in outcome.url
    assert outcome.reason.endswith(": k8s/web.yaml")
    branch, headline, body, expected, additions, deletions = setup.state.commits[0]
    assert branch == "main"
    assert headline == "chore(deploy): image 2.0.0 [skip ci]"
    assert "- k8s/web.yaml" in body
    assert expected == HEAD
    assert additions == {"k8s/web.yaml": b"image: x:2\n"}
    assert deletions == []
    # policy, read, policy, commit: each token exactly as narrow as its job.
    assert setup.permissions() == [
        {"contents": "read"},
        {"contents": "read"},
        {"contents": "read"},
        {"contents": "write"},
    ]
    assert all(token.revoked for token in setup.tokens.minted)


def test_an_unchanged_file_is_nothing_to_commit(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:1\n"})

    outcome = setup.sync.commit(changed, "1.0.0")

    assert outcome.decision is Decision.UNCHANGED
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    assert outcome.reason.startswith("nothing to commit")
    assert setup.state.commits == []
    assert len(setup.tokens.minted) == 2  # the policy and the read; no write token


def test_only_files_that_differ_are_committed_and_new_files_count(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(
        tmp_path, {"k8s/web.yaml": b"image: x:1\n", "k8s/new.yaml": b"n\n", "k8s/b.yaml": b"b\n"}
    )
    setup.state.ref_files[(HEAD, "k8s/b.yaml")] = "old\n"

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.COMMITTED
    assert sorted(setup.state.commits[0][4]) == ["k8s/b.yaml", "k8s/new.yaml"]


def test_the_branch_and_message_options_are_used(tmp_path: Path) -> None:
    setup = Setup(
        policy_text=policy('branch: deploy\ncommit-message: "data: refresh {version}"'),
        branch="deploy",
    )
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    setup.sync.commit(changed, "9")

    assert setup.state.commits[0][:2] == ("deploy", "data: refresh 9")


def test_a_missing_branch_is_an_error(tmp_path: Path) -> None:
    setup = Setup()
    setup.state.branches.clear()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.ERROR
    assert "branch 'main' does not exist" in outcome.reason


def test_a_failing_commit_is_an_error_with_the_step_named(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})
    state = setup.state

    class Moving(FakeApiFactory):
        def __call__(self, token: str) -> FakeGitHubApi:
            # The branch moves between the read (tokens 0 and 1) and the commit (token 3).
            if token == "tok-3":
                state.branches["main"] = "f" * 40
            return super().__call__(token)

    moving = Moving({POLICY_PATH: POLICY}, state)
    outcome = Sync(Gate(setup.tokens, moving, handlers()), "acme", "widgets").commit(changed)

    assert outcome.decision is Decision.ERROR
    assert outcome.reason.startswith("commit: branch 'main' moved")
    assert setup.state.commits == []


def test_a_file_that_vanishes_before_the_comparison_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})
    monkeypatch.setattr(service, "local_file", lambda directory, path: directory / "gone")

    outcome = setup.sync.commit(changed)

    assert outcome.decision is Decision.ERROR
    assert "cannot read a handed-over file" in outcome.reason


def test_the_markdown_summary_escapes_the_reason() -> None:
    outcome = service.SyncOutcome(Decision.COMMITTED, "a_b", "https://x.test/c", ("careful *now*",))

    text = outcome.to_markdown("acme/widgets")

    assert text.startswith("**COMMITTED** `sync commit` on `acme/widgets`: a\\_b")
    assert "<https://x.test/c>" in text
    assert "Warning: careful \\*now\\*" in text


# -- base sha ---------------------------------------------------------------------------------

BASE = "b" * 40


def moved_setup(*, base_text: str | None, head_text: str | None) -> Setup:
    setup = Setup()
    setup.state.ref_files.clear()
    for ref, text in ((BASE, base_text), (HEAD, head_text)):
        if text is not None:
            setup.state.ref_files[(ref, "k8s/web.yaml")] = text
    return setup


def test_a_base_sha_equal_to_the_head_changes_nothing(tmp_path: Path) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, HEAD)

    assert outcome.decision is Decision.COMMITTED


def test_a_moved_branch_that_left_the_files_alone_is_committed_on_the_head(tmp_path: Path) -> None:
    setup = moved_setup(base_text="same\n", head_text="same\n")
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, BASE.upper())

    assert outcome.decision is Decision.COMMITTED
    assert setup.state.commits[0][3] == HEAD


def test_a_file_absent_at_base_and_head_counts_as_equal(tmp_path: Path) -> None:
    setup = moved_setup(base_text=None, head_text=None)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    assert setup.sync.commit(changed, "1", None, BASE).decision is Decision.COMMITTED


@pytest.mark.parametrize(("base_text", "head_text"), [("a\n", "b\n"), (None, "b\n"), ("a\n", None)])
def test_a_moved_branch_that_changed_a_file_skips(
    tmp_path: Path, base_text: str | None, head_text: str | None
) -> None:
    setup = moved_setup(base_text=base_text, head_text=head_text)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, BASE)

    assert outcome.decision is Decision.SKIPPED
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    assert "bbbbbbbbbbbb to 0123456789ab and changed k8s/web.yaml" in outcome.reason
    assert "the next run starts over" in outcome.reason
    assert setup.state.commits == []
    assert len(setup.tokens.minted) == 2  # no write token


@pytest.mark.parametrize("sha", ["abc", "g" * 40, "b" * 39, "b" * 64])
def test_an_invalid_base_sha_is_an_error_before_any_token(tmp_path: Path, sha: str) -> None:
    setup = Setup()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, sha)

    assert outcome.decision is Decision.ERROR
    assert "is not a full commit id" in outcome.reason
    assert setup.tokens.minted == []
