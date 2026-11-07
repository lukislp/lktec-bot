# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``release run`` command: planning, the verb call, dry run, skips and failures."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lktec_bot.application.gate import Gate
from lktec_bot.application.modules.release.service import Decision, Release, ReleaseOutcome
from lktec_bot.application.verbs import handlers
from lktec_bot.cli import app
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import CommitInfo, Comparison, RateLimit
from lktec_bot.summary import mask_secret
from tests.fakes import FakeApiFactory, FakeGitHubApi, FakeRepository, FakeTokenProvider

HEAD = "0123456789abcdef0123456789abcdef01234567"
OLD = "b" * 40
DAY = date(2026, 11, 6)
ENABLED = "version: 1\nmodules:\n  release:\n    enabled: true\n"


def policy(options: str = "") -> str:
    return ENABLED + "".join(f"    {line}\n" for line in options.splitlines())


def info(message: str, index: int = 1) -> CommitInfo:
    return CommitInfo(f"{index:040x}", message)


class Setup:
    def __init__(
        self,
        *,
        policy_text: str = ENABLED,
        tags: dict[str, str] | None = None,
        commits: list[CommitInfo] | None = None,
        status: str = "ahead",
        total: int | None = None,
        branches: dict[str, str] | None = None,
    ) -> None:
        self.state = FakeRepository(
            branches={"main": HEAD} if branches is None else branches,
            tags={"v1.0.0": OLD} if tags is None else tags,
        )
        listed = commits if commits is not None else [info("feat: add things (#7)")]
        self.state.comparisons[("v1.0.0", HEAD)] = Comparison(
            status, len(listed) if total is None else total, tuple(listed)
        )
        self.state.history = list(reversed(listed))
        self.tokens = FakeTokenProvider()
        self.factory = FakeApiFactory({POLICY_PATH: policy_text}, self.state)
        self.gate = Gate(self.tokens, self.factory, handlers())

    def run(self, sha: str | None = None, *, dry_run: bool = False) -> ReleaseOutcome:
        return Release(self.gate, "acme", "widgets").run(sha, dry_run=dry_run, today=DAY)

    @property
    def minted(self) -> list[dict[str, str]]:
        return [permissions for _, _, permissions in self.tokens.requests]


def test_a_feature_since_the_last_tag_releases_a_minor_version() -> None:
    s = Setup()

    outcome = s.run(HEAD)

    assert outcome.decision is Decision.RELEASED
    assert (outcome.version, outcome.tag) == ("1.1.0", "v1.1.0")
    assert outcome.url == "https://github.com/acme/widgets/releases/tag/v1.1.0"
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    [(tag, sha, name, body, latest, prerelease)] = s.state.releases_created
    assert (tag, sha, name, latest, prerelease) == ("v1.1.0", HEAD, "v1.1.0", True, False)
    assert body.startswith("## [1.1.0](https://github.com/acme/widgets/compare/v1.0.0...v1.1.0)")
    assert "(2026-11-06)" in body
    assert "* add things ([#7](https://github.com/acme/widgets/pull/7))" in body
    # policy + module read (both contents:read), then policy + verb (contents:write): nothing else
    assert s.minted == [
        {"contents": "read"},
        {"contents": "read"},
        {"contents": "read"},
        {"contents": "write"},
    ]
    assert all(token.revoked for token in s.tokens.minted)
    assert s.state.log == ["release v1.1.0"]


@pytest.mark.parametrize(
    ("message", "version"),
    [("fix: a", "1.0.1"), ("feat: a", "1.1.0"), ("feat!: a", "2.0.0"), ("perf: a", "1.0.1")],
)
def test_the_highest_bump_of_all_commits_decides(message: str, version: str) -> None:
    s = Setup(commits=[info("chore: noise", 1), info(message, 2), info("docs: more", 3)])

    assert s.run().version == version


def test_the_policy_rules_change_the_bump() -> None:
    rules = (
        "rules:\n  - {breaking: true, release: minor}\n  - {type: fix, scope: ci, release: none}"
    )
    s = Setup(policy_text=policy(rules), commits=[info("feat!: big", 1), info("fix(ci): pin", 2)])

    outcome = s.run()

    assert outcome.version == "1.1.0"


def test_without_a_matching_tag_the_first_version_is_released_from_the_whole_history() -> None:
    s = Setup(
        policy_text=policy("first-version: 0.1.0"),
        tags={"latest": OLD, "v1.0.0-rc.1": OLD, "other-1.0.0": OLD},
        commits=[info("feat: one", 1), info("fix: two", 2)],
    )

    outcome = s.run()

    assert outcome.decision is Decision.RELEASED
    assert (outcome.version, outcome.tag) == ("0.1.0", "v0.1.0")
    body = s.state.releases_created[0][3]
    assert body.startswith("## [0.1.0](https://github.com/acme/widgets/tree/v0.1.0)")
    # oldest first: the history is reversed before it is analysed
    assert body.index("one") < body.index("two")
    assert ("list_tags", ("acme", "widgets", "v")) in s.factory.created[1].calls
    assert not any(name == "compare_commits" for name, _ in s.factory.created[1].calls)


def test_no_tag_and_nothing_releasable_is_none() -> None:
    s = Setup(tags={}, commits=[info("chore: init")])

    outcome = s.run()

    assert outcome.decision is Decision.NONE
    assert "no releasable commits since the start of the branch" in outcome.reason
    assert s.minted == [{"contents": "read"}, {"contents": "read"}]


def test_nothing_releasable_since_the_tag_is_none_with_exit_0() -> None:
    s = Setup(commits=[info("chore: bump"), info("docs: words", 2), info("Update stuff", 3)])

    outcome = s.run(HEAD)

    assert outcome.decision is Decision.NONE
    assert outcome.exit_code == 0
    assert outcome.reason == "no releasable commits since v1.0.0 (3 commit(s), 2 conventional)"
    assert (outcome.version, outcome.tag) == ("", "")
    assert s.state.releases_created == []
    assert len(s.tokens.minted) == 2


def test_an_identical_head_is_none() -> None:
    s = Setup(commits=[], status="identical")

    assert s.run().decision is Decision.NONE


def test_a_branch_that_moved_on_is_skipped() -> None:
    s = Setup()
    stale = "f" * 40

    outcome = s.run(stale)

    assert outcome.decision is Decision.SKIPPED
    assert outcome.exit_code == 0
    assert "main moved on from ffffffffffff to 0123456789ab" in outcome.reason
    assert s.state.releases_created == []
    assert not any(name == "list_tags" for name, _ in s.factory.created[1].calls)


def test_the_highest_tag_wins_and_prereleases_are_ignored() -> None:
    s = Setup(tags={"v1.2.0": OLD, "v1.10.0": OLD, "v2.0.0-rc.1": OLD, "v1.9.9": OLD})
    s.state.comparisons[("v1.10.0", HEAD)] = Comparison("ahead", 1, (info("fix: x"),))

    outcome = s.run()

    assert outcome.tag == "v1.10.1"
    assert s.state.releases_created[0][3].startswith(
        "## [1.10.1](https://github.com/acme/widgets/compare/v1.10.0...v1.10.1)"
    )


def test_a_custom_branch_and_tag_format() -> None:
    s = Setup(
        policy_text=policy("branch: master\ntag-format: app-{version}"),
        tags={"app-1.0.0": OLD, "v9.9.9": OLD},
        branches={"master": HEAD},
    )
    s.state.comparisons[("app-1.0.0", HEAD)] = Comparison("ahead", 1, (info("fix: x"),))

    outcome = s.run(HEAD)

    assert outcome.tag == "app-1.0.1"
    assert ("list_tags", ("acme", "widgets", "app-")) in s.factory.created[1].calls


@pytest.mark.parametrize("status", ["diverged", "behind"])
def test_a_tag_that_is_not_an_ancestor_is_an_error(status: str) -> None:
    s = Setup(status=status)

    outcome = s.run()

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    assert "is not an ancestor" in outcome.reason
    assert f"'{status}'" in outcome.reason
    assert s.state.releases_created == []
    assert len(s.tokens.minted) == 2


def test_a_partial_commit_list_is_an_error() -> None:
    s = Setup(total=500)

    outcome = s.run()

    assert outcome.decision is Decision.ERROR
    assert "holds 500 commits but only 1 could be read" in outcome.reason
    assert s.state.releases_created == []


def test_a_missing_release_branch_is_an_error() -> None:
    s = Setup(branches={})

    outcome = s.run()

    assert outcome.decision is Decision.ERROR
    assert "the release branch 'main' does not exist" in outcome.reason


@pytest.mark.parametrize("sha", ["HEAD", "abc", "g" * 40, "G" * 40])
def test_a_bad_sha_is_an_error(sha: str) -> None:
    outcome = Setup().run(sha)

    assert outcome.decision is Decision.ERROR
    assert "is not a full commit id" in outcome.reason


def test_an_upper_case_sha_is_the_same_commit() -> None:
    outcome = Setup().run(HEAD.upper())

    assert outcome.decision is Decision.RELEASED
    assert outcome.tag == "v1.1.0"


def test_a_sha_of_sixty_four_hex_digits_is_a_commit_id() -> None:
    s = Setup(branches={"main": "a" * 64})

    outcome = s.run("b" * 64)

    assert outcome.decision is Decision.SKIPPED


def test_dry_run_computes_everything_and_mints_no_write_token() -> None:
    s = Setup()

    outcome = s.run(HEAD, dry_run=True)

    assert outcome.decision is Decision.WOULD_RELEASE
    assert (outcome.version, outcome.tag) == ("1.1.0", "v1.1.0")
    assert outcome.reason == f"would release v1.1.0 at {HEAD}"
    assert outcome.url is None
    assert outcome.exit_code == 0
    assert outcome.result == "ok"
    assert "### Features" in outcome.notes
    assert s.state.releases_created == []
    assert s.state.log == []
    assert s.minted == [{"contents": "read"}, {"contents": "read"}]


def test_dry_run_reports_none_and_skipped_like_the_real_run() -> None:
    quiet = Setup(commits=[info("chore: x")])
    moved = Setup()

    assert quiet.run(dry_run=True).decision is Decision.NONE
    assert moved.run("f" * 40, dry_run=True).decision is Decision.SKIPPED


def test_running_again_after_a_release_finds_nothing_to_do() -> None:
    s = Setup()
    s.run()
    # The new tag is now the highest one and the head is identical to it.
    s.state.comparisons[("v1.1.0", HEAD)] = Comparison("identical", 0, ())

    second = s.run()

    assert second.decision is Decision.NONE
    assert len(s.state.releases_created) == 1


def test_the_verb_refusing_the_release_is_an_error() -> None:
    s = Setup()
    s.state.tags["v1.1.0"] = "c" * 40
    # Hide the clashing tag from the tag listing, as if it appeared between the read and the write.
    original = s.factory

    def hidden(token: str) -> FakeGitHubApi:
        api = original(token)
        listing = api.list_tags
        api.list_tags = lambda o, r, p: [t for t in listing(o, r, p) if t != "v1.1.0"]  # type: ignore[method-assign,assignment]
        return api

    s.gate = Gate(s.tokens, hidden, handlers())

    outcome = s.run()

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    assert outcome.result == "error"
    assert "tag v1.1.0 already points at" in outcome.reason
    assert outcome.tag == "v1.1.0"
    assert s.state.releases_created == []


def test_a_disabled_or_missing_module_is_denied_before_anything_is_read() -> None:
    disabled = Setup(policy_text="version: 1\nmodules:\n  release:\n    enabled: false\n")
    other = Setup(policy_text="version: 1\nmodules:\n  ping:\n    enabled: true\n")

    for s in (disabled, other):
        outcome = s.run()

        assert outcome.decision is Decision.DENIED
        assert outcome.exit_code == 3
        assert outcome.result == "denied"
        assert len(s.tokens.minted) == 1  # the policy read only


def test_mode_pr_is_rejected_by_the_policy_validation() -> None:
    s = Setup(policy_text=policy("mode: pr"))

    outcome = s.run()

    assert outcome.decision is Decision.DENIED
    assert "modules.release.mode" in outcome.reason
    assert "mode 'pr' is not available yet" in outcome.reason


def test_revoke_warnings_are_carried() -> None:
    s = Setup()
    s.tokens.failing_revokes = 5

    outcome = s.run()

    assert outcome.decision is Decision.RELEASED
    assert any(w.startswith("token not revoked") for w in outcome.warnings)


def test_markdown_escapes_the_reason_and_lists_url_and_warnings() -> None:
    outcome = ReleaseOutcome(
        Decision.RELEASED, "released *v1*", "1", "v1", "https://example.test/r", warnings=("w_1",)
    )

    text = outcome.to_markdown("acme/widgets")

    assert text.startswith("**RELEASED** `release run` on `acme/widgets`: released \\*v1\\*")
    assert "<https://example.test/r>" in text
    assert "Warning: w\\_1" in text


# -- the command line ------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Setup:
    s = Setup()

    def fake_build_gate(env: Mapping[str, str] | None = None) -> Gate:
        return Gate(s.tokens, s.factory, handlers(), mask=mask_secret)

    monkeypatch.setattr("lktec_bot.cli.build_gate", fake_build_gate)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    return s


def outputs(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_release_run_releases_and_writes_the_outputs(wired: Setup, tmp_path: Path) -> None:
    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets", "--sha", HEAD])

    assert result.exit_code == 0, result.output
    assert "released: created release v1.1.0" in result.stdout
    assert "version: 1.1.0" in result.stdout
    assert "url: https://github.com/acme/widgets/releases/tag/v1.1.0" in result.stdout
    assert "### Features" not in result.stdout  # notes are printed for dry runs only
    values = outputs(tmp_path)
    assert values["result"] == "ok"
    assert values["decision"] == "released"
    assert values["version"] == "1.1.0"
    assert values["tag"] == "v1.1.0"
    assert values["url"].endswith("/releases/tag/v1.1.0")
    assert values["reason"] == values["message"]
    assert "**RELEASED** `release run`" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert wired.tokens.closed


def test_release_run_dry_run_prints_the_notes_and_the_same_output_keys(
    wired: Setup, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["release", "run", "--repository", "acme/widgets", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert f"would-release: would release v1.1.0 at {HEAD}" in result.stdout
    assert "version: 1.1.0" in result.stdout
    assert "### Features" in result.stdout
    values = outputs(tmp_path)
    assert set(values) == {"result", "decision", "version", "tag", "url", "reason", "message"}
    assert values["decision"] == "would-release"
    assert values["version"] == "1.1.0"
    assert values["tag"] == "v1.1.0"
    assert values["url"] == ""
    assert wired.state.releases_created == []
    assert len(wired.tokens.minted) == 2


def test_release_run_uses_the_server_url_of_the_runner(
    wired: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://ghe.example.test")

    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets", "--dry-run"])

    assert "https://ghe.example.test/acme/widgets/compare/v1.0.0...v1.1.0" in result.stdout


def test_release_run_with_nothing_to_release_exits_0(wired: Setup, tmp_path: Path) -> None:
    wired.state.comparisons[("v1.0.0", HEAD)] = Comparison("ahead", 1, (info("chore: x"),))

    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets"])

    assert result.exit_code == 0
    assert "none: no releasable commits since v1.0.0" in result.stdout
    values = outputs(tmp_path)
    assert (values["result"], values["decision"], values["version"]) == ("ok", "none", "")


def test_release_run_skipped_exits_0(wired: Setup, tmp_path: Path) -> None:
    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets", "--sha", "f" * 40])

    assert result.exit_code == 0
    assert outputs(tmp_path)["decision"] == "skipped"


def test_release_run_error_exits_1_and_prints_warnings(wired: Setup, tmp_path: Path) -> None:
    wired.state.comparisons[("v1.0.0", HEAD)] = Comparison("diverged", 1, (info("fix: x"),))
    wired.tokens.failing_revokes = 5

    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets"])

    assert result.exit_code == 1
    assert "warning: token not revoked" in result.stderr
    values = outputs(tmp_path)
    assert (values["result"], values["decision"]) == ("error", "error")


def test_release_run_denied_exits_3(
    monkeypatch: pytest.MonkeyPatch, wired: Setup, tmp_path: Path
) -> None:
    factory = FakeApiFactory({POLICY_PATH: "version: 1\nmodules:\n  ping:\n    enabled: true\n"})
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(FakeTokenProvider(), factory, handlers())
    )

    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets"])

    assert result.exit_code == 3
    assert outputs(tmp_path)["result"] == "denied"


def test_release_run_rejects_a_bad_repository() -> None:
    result = runner.invoke(app, ["release", "run", "-R", "nope"])

    assert result.exit_code == 2


# -- the release verb as a subcommand ------------------------------------------------------------


def verb_args(*extra: str) -> list[str]:
    return [
        "release",
        "-R",
        "acme/widgets",
        "-m",
        "release",
        "--tag",
        "v3.0.0",
        "--sha",
        HEAD,
        "--name",
        "v3.0.0",
        "--body",
        "notes",
        *extra,
    ]


def test_the_release_verb_subcommand_makes_a_latest_release_by_default(
    wired: Setup, tmp_path: Path
) -> None:
    result = runner.invoke(app, verb_args())

    assert result.exit_code == 0, result.output
    assert "ok: created release v3.0.0" in result.stdout
    assert wired.state.releases_created == [("v3.0.0", HEAD, "v3.0.0", "notes", True, False)]
    values = outputs(tmp_path)
    assert (values["result"], values["decision"], values["tag"]) == ("ok", "created", "v3.0.0")
    assert values["url"].endswith("/releases/tag/v3.0.0")


def test_the_release_verb_subcommand_takes_the_flags(wired: Setup) -> None:
    result = runner.invoke(app, verb_args("--no-latest", "--prerelease"))

    assert result.exit_code == 0, result.output
    assert wired.state.releases_created[0][4:] == (False, True)


def test_the_release_verb_subcommand_reports_errors_with_a_decision(
    wired: Setup, tmp_path: Path
) -> None:
    wired.state.tags["v3.0.0"] = "c" * 40

    result = runner.invoke(app, verb_args())

    assert result.exit_code == 1
    values = outputs(tmp_path)
    assert (values["result"], values["decision"], values["tag"]) == ("error", "error", "v3.0.0")


def test_a_denied_release_verb_reports_the_decision_denied(
    monkeypatch: pytest.MonkeyPatch, wired: Setup, tmp_path: Path
) -> None:
    factory = FakeApiFactory({POLICY_PATH: "version: 1\nmodules:\n  ping:\n    enabled: true\n"})
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(FakeTokenProvider(), factory, handlers())
    )

    result = runner.invoke(app, verb_args())

    assert result.exit_code == 3
    assert outputs(tmp_path)["decision"] == "denied"


def test_release_run_appends_the_remaining_rate_limit(
    wired: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeApiFactory, "last_rate_limit", RateLimit(12))

    result = runner.invoke(app, ["release", "run", "-R", "acme/widgets", "--sha", HEAD])

    assert "(rate limit remaining: 12)" in result.stdout
