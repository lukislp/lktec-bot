# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The automerge module's arm and sweep flows, through a real gate wired to the fakes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from lktec_bot.application.gate import Gate
from lktec_bot.application.modules.automerge.classify import Eligibility
from lktec_bot.application.modules.automerge.options import AutomergeOptions
from lktec_bot.application.modules.automerge.service import Automerge, Decision
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.merge_wait import WaitBounds
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import (
    CheckRun,
    PullRequest,
    PullRequestCommit,
    RateLimit,
    Review,
)
from tests.fakes import BOT, FakeApiFactory, FakeRepository, FakeTokenProvider

DEPENDABOT = "dependabot[bot]"
HEAD = "0123456789abcdef0123456789abcdef01234567"
POLICY = "version: 1\nmodules:\n  automerge:\n    enabled: true\n"
PATCH = "Bump ruff from 0.13.0 to 0.13.1"
MINOR = "Bump ruff from 0.13.0 to 0.14.0"
MAJOR = "Bump ruff from 0.13.0 to 1.0.0"
DIGEST = "bump python from `78387bc` to `2f17fc0`"
BROAD = {"metadata": "read", "contents": "write", "pull_requests": "write", "issues": "write"}


def policy(*lines: str) -> str:
    return POLICY + "".join(f"    {line}\n" for line in lines)


def pr(
    number: int,
    *,
    title: str = PATCH,
    body: str = "",
    author: str = DEPENDABOT,
    auto: bool = False,
    draft: bool = False,
    state: str = "open",
    merge_state: str = "CLEAN",
    head: str = HEAD,
) -> PullRequest:
    return PullRequest(
        number=number,
        html_url=f"https://github.com/acme/widgets/pull/{number}",
        state=state,
        draft=draft,
        head_sha=head,
        auto_merge_method="SQUASH" if auto else None,
        title=title,
        body=body,
        author_login=author,
        merge_state=merge_state,
    )


class Clock:
    """A clock that only moves when the code under test sleeps; ``script[i]`` runs after the
    i-th sleep (0-based), to change what GitHub reports while the bot waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.script: list[Callable[[], None]] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps.append(seconds)
        index = len(self.sleeps) - 1
        if index < len(self.script):
            self.script[index]()


@dataclass
class Setup:
    state: FakeRepository
    tokens: FakeTokenProvider
    factory: FakeApiFactory
    clock: Clock = field(default_factory=Clock)
    logs: list[str] = field(default_factory=list)
    wait: WaitBounds | None = None
    own_run_id: int | None = None

    @property
    def sleeps(self) -> list[float]:
        return self.clock.sleeps

    def set_policy(self, text: str) -> None:
        cast(dict[str, str], self.factory.files)[POLICY_PATH] = text

    @property
    def service(self) -> Automerge:
        gate = Gate(self.tokens, self.factory, handlers())
        return Automerge(
            gate,
            "acme",
            "widgets",
            sleep=self.clock.sleep,
            clock=self.clock,
            wait=self.wait,
            log=self.logs.append,
            own_run_id=self.own_run_id,
        )

    @property
    def requests(self) -> list[dict[str, str]]:
        return [permissions for _, _, permissions in self.tokens.requests]

    def calls(self, name: str) -> list[tuple[str, tuple[object, ...]]]:
        return [call for api in self.factory.created for call in api.calls if call[0] == name]


def setup(
    *pull_requests: PullRequest,
    policy_text: str = POLICY,
    failing_revokes: int = 0,
    tokens: FakeTokenProvider | None = None,
) -> Setup:
    state = FakeRepository(pull_requests={p.number: p for p in pull_requests})
    provider = tokens or FakeTokenProvider(failing_revokes=failing_revokes)
    return Setup(state, provider, FakeApiFactory({POLICY_PATH: policy_text}, state))


# -- arm -------------------------------------------------------------------------------------


@pytest.mark.parametrize("title", [PATCH, MINOR, DIGEST])
def test_arm_approves_then_enables_auto_merge_and_never_merges(title: str) -> None:
    env = setup(pr(7, title=title))

    result = env.service.arm(7)

    assert result.decision is Decision.ARMED
    assert result.exit_code == 0
    assert env.state.log == ["approve #7", "enable-automerge #7 squash"]
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert "approved, auto-merge (squash) enabled" in result.reason
    # policy, read, policy, approve, policy, enable: each with exactly its own permissions
    assert env.requests == [
        {"contents": "read"},
        {"pull_requests": "read"},
        {"contents": "read"},
        {"pull_requests": "write"},
        {"contents": "read"},
        {"contents": "write", "pull_requests": "write"},
    ]
    assert all(token.revoked for token in env.tokens.minted)


def test_arm_twice_is_idempotent_for_the_same_head() -> None:
    env = setup(pr(7))

    first = env.service.arm(7)
    second = env.service.arm(7)

    assert first.decision is second.decision is Decision.ARMED
    assert env.state.log == ["approve #7", "enable-automerge #7 squash"]


def test_arm_approves_a_new_head_again() -> None:
    env = setup(pr(7))
    env.service.arm(7)
    env.state.pull_requests[7] = replace(env.state.pull_requests[7], head_sha="f" * 40)

    env.service.arm(7)

    assert env.state.log == ["approve #7", "enable-automerge #7 squash", "approve #7"]


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_arm_uses_the_configured_merge_method(method: str) -> None:
    env = setup(pr(7), policy_text=policy(f"method: {method}"))

    result = env.service.arm(7)

    assert env.state.log == ["approve #7", f"enable-automerge #7 {method}"]
    assert f"auto-merge ({method}) enabled" in result.reason


def test_arm_leaves_a_major_for_a_person() -> None:
    env = setup(pr(7, title=MAJOR))

    result = env.service.arm(7)

    assert result.decision is Decision.SKIPPED
    assert result.reason == "major update (majors: manual)"
    assert result.exit_code == 0
    assert env.state.log == []


def test_arm_arms_a_major_with_majors_auto() -> None:
    env = setup(pr(7, title=MAJOR), policy_text=policy("majors: auto"))

    assert env.service.arm(7).decision is Decision.ARMED


def test_arm_leaves_an_unreadable_title_for_a_person() -> None:
    env = setup(pr(7, title="Bump actions/checkout from 3d3c42e to a1b2c3d"))

    result = env.service.arm(7)

    assert result.decision is Decision.SKIPPED
    assert env.state.log == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"author": "someone"}, "not opened by dependabot[bot]"),
        ({"author": "dependabot-impostor[bot]"}, "not opened by dependabot[bot]"),
        ({"draft": True}, "pull request is a draft"),
        ({"state": "closed"}, "pull request is closed, not open"),
    ],
)
def test_arm_only_acts_on_open_non_draft_dependabot_pull_requests(
    changes: dict[str, object], reason: str
) -> None:
    env = setup(pr(7, **changes))  # type: ignore[arg-type]

    result = env.service.arm(7)

    assert (result.decision, result.reason) == (Decision.SKIPPED, reason)
    assert env.state.log == []


def test_arm_skips_an_excluded_dependency_in_any_case() -> None:
    env = setup(
        pr(7, title="Bump Ruff from 0.13.0 to 0.13.1"), policy_text=policy("exclude: [ruff]")
    )

    result = env.service.arm(7)

    assert (result.decision, result.reason) == (Decision.SKIPPED, "excluded dependency: Ruff")
    assert env.state.log == []


def test_arm_skips_a_group_with_one_excluded_member() -> None:
    body = "Updates `ruff` from 0.13.0 to 0.13.1\nUpdates `mowdeck` from 1.0.0 to 1.0.1\n"
    env = setup(
        pr(7, title="Bump the python group with 2 updates", body=body),
        policy_text=policy("exclude: [MowDeck]"),
    )

    assert env.service.arm(7).reason == "excluded dependency: mowdeck"
    assert env.state.log == []


def test_arm_workflow_files_manual_leaves_a_pull_request_that_changes_workflows() -> None:
    env = setup(pr(7), policy_text=policy("workflow-files: manual"))
    env.state.pull_files[7] = ["uv.lock", ".github/workflows/ci.yml"]

    result = env.service.arm(7)

    assert result.decision is Decision.SKIPPED
    assert result.reason == "changes files under .github/workflows/ (workflow-files: manual)"
    assert env.state.log == []


def test_arm_workflow_files_manual_arms_one_that_leaves_workflows_alone() -> None:
    env = setup(pr(7), policy_text=policy("workflow-files: manual"))
    env.state.pull_files[7] = ["uv.lock", "pyproject.toml"]

    assert env.service.arm(7).decision is Decision.ARMED


def test_arm_workflow_files_auto_arms_and_does_not_even_list_files() -> None:
    env = setup(pr(7), policy_text=policy("workflow-files: auto"))
    env.state.pull_files[7] = [".github/workflows/ci.yml"]

    assert env.service.arm(7).decision is Decision.ARMED
    assert env.calls("list_pull_request_files") == []


def test_arm_does_not_list_files_for_a_pull_request_it_skips_anyway() -> None:
    env = setup(pr(7, title=MAJOR), policy_text=policy("workflow-files: manual"))

    env.service.arm(7)

    assert env.calls("list_pull_request_files") == []


def test_arm_is_denied_when_the_module_is_not_enabled() -> None:
    env = setup(pr(7), policy_text="version: 1\nmodules:\n  automerge:\n    enabled: false\n")

    result = env.service.arm(7)

    assert result.decision is Decision.DENIED
    assert result.exit_code == 3
    assert "disabled" in result.reason
    assert env.state.log == []
    assert env.requests == [{"contents": "read"}]  # nothing but the policy read


def test_arm_is_denied_without_a_policy_file() -> None:
    env = setup(pr(7), policy_text="")
    env.factory.files = {}

    result = env.service.arm(7)

    assert (result.decision, result.exit_code) == (Decision.DENIED, 3)
    assert "missing" in result.reason


def test_arm_reports_an_error_when_the_read_token_is_broader_than_asked() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={1: BROAD})
    env = setup(pr(7), tokens=tokens)

    result = env.service.arm(7)

    assert (result.decision, result.exit_code) == (Decision.ERROR, 1)
    assert "read token refused" in result.reason
    assert env.state.log == []


def test_arm_reports_an_error_when_the_pull_request_cannot_be_read() -> None:
    env = setup()

    result = env.service.arm(99)

    assert result.decision is Decision.ERROR
    assert result.reason.startswith("automerge could not read: GET pulls/99")


def test_arm_stops_after_a_failed_approval() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={3: BROAD})  # the approve token
    env = setup(pr(7), tokens=tokens)

    result = env.service.arm(7)

    assert result.decision is Decision.ERROR
    assert result.reason.startswith("approve: verb token refused")
    assert env.state.log == []


def test_arm_reports_a_failed_enable_after_the_approval() -> None:
    env = setup(replace(pr(7), auto_merge_method="MERGE"))

    result = env.service.arm(7)

    assert result.decision is Decision.ERROR
    assert result.reason.startswith("enable-automerge: auto-merge is already enabled")
    assert env.state.log == ["approve #7"]


def test_arm_of_a_pull_request_github_calls_mergeable_is_ready_and_green() -> None:
    env = setup(pr(7))
    env.state.auto_merge_refusals[7] = ["clean"] * 6

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert result.exit_code == 0
    assert result.reason == "patch update: approved; already mergeable, needs a person to merge"
    assert result.state == "clean"
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert env.state.log == ["approve #7"]
    assert env.state.created_comments == []  # the arm job runs in the race window: no hint


def test_arm_retries_until_the_checks_have_registered() -> None:
    env = setup(pr(7))
    env.state.auto_merge_refusals[7] = ["clean", "clean"]

    result = env.service.arm(7)

    assert result.decision is Decision.ARMED
    assert env.state.log == ["approve #7", "enable-automerge #7 squash"]


def test_arm_carries_revoke_warnings() -> None:
    env = setup(pr(7), failing_revokes=5)

    result = env.service.arm(7)

    assert result.decision is Decision.ARMED
    assert any("token not revoked" in warning for warning in result.warnings)


def test_arm_markdown_escapes_and_links() -> None:
    env = setup(pr(7), failing_revokes=5)

    text = env.service.arm(7).to_markdown("acme/widgets")

    assert text.startswith("**ARMED** `automerge arm` #7 on `acme/widgets`")
    assert "<https://github.com/acme/widgets/pull/7>" in text
    assert "Warning: token not revoked" in text


# -- sweep -----------------------------------------------------------------------------------


def test_sweep_arms_routine_dependabot_pull_requests_and_leaves_the_rest() -> None:
    env = setup(
        pr(1, title=PATCH),
        pr(2, title=MAJOR),
        pr(3, title=DIGEST),
        pr(4, title=PATCH, draft=True),
        pr(5, title="Bump x from 1.0 to 1.1", state="closed"),
        pr(6, title="Update docs", author="someone"),
    )

    result = env.service.sweep()

    assert result.exit_code == 0
    assert env.state.log == [
        "approve #1",
        "enable-automerge #1 squash",
        "approve #3",
        "enable-automerge #3 squash",
    ]
    assert (result.armed, result.updated, result.recreated, result.errors) == (2, 0, 0, 0)
    assert [row.number for row in result.rows] == [1, 2, 3, 6]
    notes = {row.number: row.note for row in result.rows}
    assert notes[2] == "left for a person (major update (majors: manual))"


READY_HINT = (
    "lktec-bot approved this pull request, but GitHub does not enable auto-merge on a pull "
    "request that can be merged right away (state: unstable). Please merge it.\n\n"
    "<!-- lktec-bot:automerge-ready-0123456789ab -->\n"
)


def test_sweep_counts_a_mergeable_pull_request_as_ready_and_hints_once_per_head() -> None:
    env = setup(pr(1))
    env.state.auto_merge_refusals[1] = ["unstable"] * 12

    first = env.service.sweep()
    second = env.service.sweep()

    assert (first.ready, first.armed, first.errors) == (1, 0, 0)
    assert first.exit_code == 0
    assert first.rows[0].actions == ["approved; already mergeable, needs a person to merge"]
    assert first.rows[0].errors == []
    assert env.state.created_comments == [(1, READY_HINT)]
    # the second tick: ready again, but no new review and no second hint
    assert second.ready == 1
    assert env.state.log == ["approve #1", "comment #1"]
    assert second.approved == 0
    assert "ready 1" in first.to_markdown("acme/widgets")


def test_sweep_hints_again_for_a_new_head() -> None:
    env = setup(pr(1))
    env.state.auto_merge_refusals[1] = ["clean"] * 12
    env.service.sweep()
    env.state.pull_requests[1] = replace(env.state.pull_requests[1], head_sha="e" * 40)

    env.service.sweep()

    assert len(env.state.created_comments) == 2
    assert "automerge-ready-eeeeeeeeeeee" in env.state.created_comments[1][1]


def test_sweep_arms_it_once_the_checks_are_registered() -> None:
    env = setup(pr(1))
    # The sweep tries once per run (the state has settled there), so one refusal per sweep.
    env.state.auto_merge_refusals[1] = ["clean"]
    env.service.sweep()

    second = env.service.sweep()

    assert (second.armed, second.ready) == (1, 0)
    assert env.state.log == ["approve #1", "comment #1", "enable-automerge #1 squash"]


def test_sweep_treats_a_failed_ready_hint_as_a_warning() -> None:
    # mints: 0 policy, 1 read, 2 policy, 3 approve, 4 policy, 5 enable, 6 policy, 7 comment
    tokens = FakeTokenProvider(permissions_by_mint={7: BROAD})
    env = setup(pr(1), tokens=tokens)
    env.state.auto_merge_refusals[1] = ["clean"] * 6

    result = env.service.sweep()

    assert (result.ready, result.errors) == (1, 0)
    assert result.rows[0].errors == []
    assert any(warning.startswith("#1: ready hint not posted:") for warning in result.warnings)
    assert env.state.created_comments == []


def test_sweep_does_not_look_at_the_state_of_a_pull_request_it_leaves_alone() -> None:
    env = setup(pr(2, title=MAJOR))

    env.service.sweep()

    assert env.calls("get_pull_request") == []


def test_sweep_never_updates_a_branch_it_has_just_armed() -> None:
    env = setup(pr(1, merge_state="BEHIND"))

    result = env.service.sweep()

    assert env.state.log == ["approve #1", "enable-automerge #1 squash"]
    assert result.updated == 0
    assert result.rows[0].note == "behind its base, left for the next tick (just armed)"


def test_sweep_updates_it_on_the_next_tick() -> None:
    env = setup(pr(1, merge_state="BEHIND"))
    env.service.sweep()

    second = env.service.sweep()

    assert env.state.log == ["approve #1", "enable-automerge #1 squash", "update-branch #1"]
    assert second.updated == 1
    assert second.approved == 0  # the head it approved moved on with the update: next tick


def test_sweep_updates_behind_pull_requests_with_auto_merge_or_from_a_person() -> None:
    env = setup(
        pr(1, auto=True, merge_state="BEHIND"),
        pr(2, author="someone", title="Fix", merge_state="BEHIND"),
        pr(3, author="someone", title="Fix", auto=True, merge_state="BEHIND"),
        pr(4, author="release-bot[bot]", title="Release", merge_state="BEHIND"),
    )

    result = env.service.sweep()

    assert env.state.log == [f"update-branch #{n}" for n in (1, 2, 3, 4)]
    assert result.updated == 4


def test_sweep_leaves_a_clean_pull_request_from_a_person_alone() -> None:
    env = setup(pr(2, author="someone", title="Fix", merge_state="CLEAN"))

    result = env.service.sweep()

    assert env.state.log == []
    assert result.rows[0].detail() == "nothing to do"


def test_sweep_does_not_rebuild_a_dependabot_major_that_is_left_for_a_person() -> None:
    env = setup(pr(2, title=MAJOR, merge_state="BEHIND"))

    env.service.sweep()

    assert env.state.log == []


def test_sweep_keeps_a_major_a_person_armed_mergeable_but_never_approves_it() -> None:
    env = setup(pr(2, title=MAJOR, auto=True, merge_state="BEHIND"))

    result = env.service.sweep()

    assert env.state.log == ["update-branch #2"]
    assert result.approved == 0


def test_sweep_hints_at_a_recreate_for_a_conflicting_pull_request_once_per_head() -> None:
    env = setup(pr(1, auto=True, merge_state="DIRTY"))

    first = env.service.sweep()
    again = env.service.sweep()

    assert env.state.created_comments == [
        (
            1,
            "lktec-bot cannot rebuild this pull request: it conflicts with its base. "
            "Dependabot takes commands only from people with push access, so a maintainer needs "
            "to comment `@dependabot recreate`; the sweep arms the rebuilt pull request again.\n\n"
            "<!-- lktec-bot:automerge-recreate-0123456789ab -->\n",
        )
    ]
    assert first.recreated == 1
    assert again.recreated == 1  # reported again, but it is the same hint
    assert env.state.log == ["comment #1"]


UNSIGNED = [PullRequestCommit("a" * 40, False, "unsigned")]


@pytest.mark.parametrize("auto", [True, False])
def test_sweep_hints_at_a_recreate_for_a_pull_request_with_an_unsigned_commit(
    auto: bool,
) -> None:
    env = setup(pr(1, auto=auto, merge_state="BEHIND"))
    env.state.pull_commits[1] = [PullRequestCommit("b" * 40, True, "valid"), *UNSIGNED]

    first = env.service.sweep()
    again = env.service.sweep()

    assert env.state.created_comments == [
        (
            1,
            "lktec-bot cannot rebuild this pull request: it has a commit GitHub does not verify "
            "(unsigned), which a ruleset that requires signed commits will not merge. Dependabot "
            "takes commands only from people with push access, so a maintainer needs to comment "
            "`@dependabot recreate`; the sweep arms the rebuilt pull request again.\n\n"
            "<!-- lktec-bot:automerge-recreate-0123456789ab -->\n",
        )
    ]
    assert env.state.log == ["comment #1"]  # no arm, approve or update-branch next to it
    assert (first.armed, first.approved, first.updated, first.recreated) == (0, 0, 0, 1)
    assert again.recreated == 1  # reported again, but it is the same hint
    assert first.rows[0].actions == [
        "has an unsigned commit (unsigned), a maintainer needs to recreate it"
    ]
    assert len(env.calls("list_pull_request_commits")) == 2


def test_sweep_asks_again_for_an_unsigned_commit_on_a_new_head() -> None:
    env = setup(pr(1, auto=True))
    env.state.pull_commits[1] = UNSIGNED
    env.service.sweep()
    env.state.pull_requests[1] = replace(env.state.pull_requests[1], head_sha="e" * 40)

    env.service.sweep()

    assert len(env.state.created_comments) == 2


def test_sweep_leaves_signed_commits_alone() -> None:
    env = setup(pr(1, auto=True))
    env.state.pull_commits[1] = [PullRequestCommit("b" * 40, True, "valid")]

    result = env.service.sweep()

    assert env.state.log == ["approve #1"]
    assert result.recreated == 0
    assert len(env.calls("list_pull_request_commits")) == 1


def test_sweep_does_not_read_commits_of_pull_requests_it_leaves_alone_or_that_conflict() -> None:
    env = setup(
        pr(1, title=MAJOR),  # left for a person
        pr(2, auto=True, merge_state="DIRTY"),  # recreated for the conflict anyway
        pr(3, author="someone", title="Fix"),  # not Dependabot
    )
    env.state.pull_commits.update({1: UNSIGNED, 2: UNSIGNED, 3: UNSIGNED})

    env.service.sweep()

    assert env.calls("list_pull_request_commits") == []
    assert env.state.log == ["comment #2"]


def test_sweep_never_touches_an_unsigned_commit_of_a_person() -> None:
    env = setup(pr(1, author="someone", title="Fix", auto=True))
    env.state.pull_commits[1] = UNSIGNED

    result = env.service.sweep()

    assert env.state.log == []
    assert result.recreated == 0


def test_sweep_lists_a_failed_recreate_of_an_unsigned_commit_in_the_row() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={2: BROAD})
    env = setup(pr(1, auto=True), tokens=tokens)
    env.state.pull_commits[1] = UNSIGNED

    result = env.service.sweep()

    assert result.errors == 1
    assert result.rows[0].errors[0].startswith("comment:")


def test_sweep_asks_again_when_the_head_changed_and_it_still_conflicts() -> None:
    env = setup(pr(1, auto=True, merge_state="DIRTY"))
    env.service.sweep()
    env.state.pull_requests[1] = replace(env.state.pull_requests[1], head_sha="e" * 40)

    env.service.sweep()

    assert len(env.state.created_comments) == 2
    assert "automerge-recreate-eeeeeeeeeeee" in env.state.created_comments[1][1]


def test_sweep_arms_and_then_asks_for_a_recreate_when_the_new_pull_request_conflicts() -> None:
    env = setup(pr(1, merge_state="DIRTY"))

    result = env.service.sweep()

    assert env.state.log == ["approve #1", "enable-automerge #1 squash", "comment #1"]
    assert (result.armed, result.recreated) == (1, 1)


def test_sweep_does_not_recreate_without_auto_merge_or_for_a_person() -> None:
    env = setup(
        pr(1, title=MAJOR, merge_state="DIRTY"),
        pr(2, author="someone", title="Fix", merge_state="DIRTY"),
        pr(3, author="someone", title="Fix", auto=True, merge_state="DIRTY"),
    )

    env.service.sweep()

    assert env.state.log == []


def test_sweep_keeps_an_excluded_pull_request_a_person_armed_mergeable() -> None:
    env = setup(
        pr(1, title="Bump Pillow from 11.0.0 to 11.0.1", auto=True, merge_state="BEHIND"),
        pr(2, title="Bump Pillow from 11.0.0 to 11.0.2", auto=True, merge_state="DIRTY"),
        policy_text=policy("exclude: [pillow]"),
    )

    result = env.service.sweep()

    assert env.state.log == ["update-branch #1", "comment #2"]
    assert result.approved == 0
    assert result.rows[0].note == (
        "excluded dependency: Pillow: kept mergeable, never approved here"
    )


def test_sweep_leaves_an_excluded_pull_request_without_auto_merge_entirely_alone() -> None:
    env = setup(
        pr(1, title="Bump Pillow from 11.0.0 to 11.0.1", merge_state="BEHIND"),
        policy_text=policy("exclude: [pillow]"),
    )

    result = env.service.sweep()

    assert env.state.log == []
    assert result.rows[0].note == "left alone (excluded dependency: Pillow)"
    assert env.calls("get_pull_request") == []


def test_sweep_approves_the_current_head_of_an_armed_routine_pull_request() -> None:
    env = setup(pr(1, auto=True, merge_state="BLOCKED"))

    first = env.service.sweep()
    second = env.service.sweep()

    assert env.state.log == ["approve #1"]  # the second tick finds the approval at this head
    assert (first.approved, second.approved) == (1, 0)


def test_sweep_does_not_approve_again_when_the_head_is_already_approved() -> None:
    env = setup(pr(1, auto=True, merge_state="BLOCKED"))
    env.state.reviews[1] = [Review(BOT, "APPROVED", HEAD, "https://x.test/r")]

    result = env.service.sweep()

    assert env.state.log == []
    assert result.approved == 0


def test_sweep_polls_an_unknown_merge_state_until_github_has_computed_it() -> None:
    env = setup(pr(1, author="someone", title="Fix", merge_state="CLEAN"))
    env.state.merge_state_script[1] = ["UNKNOWN", "UNKNOWN", "BEHIND"]

    result = env.service.sweep()

    assert env.sleeps == [10.0, 10.0]
    assert env.state.log == ["update-branch #1"]
    assert result.rows[0].state == "BEHIND"


def test_sweep_gives_up_on_a_merge_state_that_stays_unknown() -> None:
    env = setup(pr(1, author="someone", title="Fix", merge_state="CLEAN"))
    env.state.merge_state_script[1] = ["UNKNOWN"] * 10

    result = env.service.sweep()

    assert env.sleeps == [10.0] * 5  # six attempts, five waits
    assert env.state.log == []
    assert result.rows[0].state == "UNKNOWN"


def test_sweep_with_workflow_files_manual_leaves_such_a_pull_request_to_a_person() -> None:
    env = setup(
        pr(1),
        pr(2, title="Bump ruff from 0.13.0 to 0.13.2"),
        policy_text=policy("workflow-files: manual"),
    )
    env.state.pull_files[1] = [".github/workflows/ci.yml"]
    env.state.pull_files[2] = ["uv.lock"]

    result = env.service.sweep()

    assert env.state.log == ["approve #2", "enable-automerge #2 squash"]
    assert result.rows[0].note.startswith("left for a person (changes files under")


def test_sweep_reports_a_failed_step_and_carries_on() -> None:
    # mints: 0 policy, 1 read, 2 policy, 3 the approve token of #1
    tokens = FakeTokenProvider(permissions_by_mint={3: BROAD})
    env = setup(pr(1), pr(2, author="someone", title="Fix", merge_state="BEHIND"), tokens=tokens)

    result = env.service.sweep()

    assert result.exit_code == 0
    assert result.errors == 1
    assert result.rows[0].errors[0].startswith("error: approve: verb token refused")
    assert env.state.log == ["update-branch #2"]


@pytest.mark.parametrize(
    ("prs", "needle"),
    [
        ([pr(1, author="someone", title="Fix", merge_state="BEHIND")], "update-branch:"),
        ([pr(1, auto=True, merge_state="DIRTY")], "comment:"),
        ([pr(1, auto=True, merge_state="CLEAN")], "approve:"),
    ],
)
def test_sweep_lists_a_failed_verb_in_the_row(prs: list[PullRequest], needle: str) -> None:
    tokens = FakeTokenProvider(permissions_by_mint={3: BROAD})
    env = setup(*prs, tokens=tokens)

    result = env.service.sweep()

    assert result.errors == 1
    assert needle in result.rows[0].errors[0]
    assert env.state.log == []


def test_sweep_is_denied_when_the_module_is_not_enabled() -> None:
    env = setup(pr(1), policy_text="version: 1\nmodules:\n  ping:\n    enabled: true\n")

    result = env.service.sweep()

    assert result.exit_code == 3
    assert result.failure is not None
    assert "not declared" in result.failure.message
    assert env.state.log == []


def test_sweep_reports_unreadable_pull_requests_as_an_error() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={1: None})
    env = setup(pr(1), tokens=tokens)

    result = env.service.sweep()

    assert result.exit_code == 1
    assert result.failure is not None
    assert "read token refused" in result.failure.message


def test_sweep_markdown_has_a_table_with_one_row_per_pull_request() -> None:
    env = setup(pr(1), pr(2, title=MAJOR), failing_revokes=5)

    text = env.service.sweep().to_markdown("acme/widgets")

    assert "### automerge sweep on `acme/widgets`" in text
    assert (
        "armed 1, merged 0, ready 0, re-approved 0, updated 0, recreate needed 0, errors 0" in text
    )
    assert "| #1 | dependabot\\[bot\\] | CLEAN |" in text
    assert "| #2 | dependabot\\[bot\\] | - | left for a person" in text
    assert "Warning: token not revoked" in text


def test_sweep_markdown_without_pull_requests_and_for_a_failure() -> None:
    env = setup()
    assert "No open pull requests." in env.service.sweep().to_markdown("acme/widgets")

    denied = setup(pr(1), policy_text="version: 1\n").service.sweep()

    assert denied.to_markdown("acme/widgets").startswith("**DENIED** `automerge sweep` on")


# -- merge-ready -----------------------------------------------------------------------------

MERGE_READY = policy("merge-ready: true")
MERGE_TOKEN = {"contents": "write", "pull_requests": "write", "workflows": "write"}


def test_arm_merges_a_pull_request_github_will_not_arm_when_merge_ready_is_on() -> None:
    env = setup(pr(7), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[7] = ["clean"] * 6

    result = env.service.arm(7)

    assert result.decision is Decision.MERGED
    assert result.exit_code == 0
    assert result.reason == (
        "patch update: approved, merged (already mergeable, auto-merge refused)"
    )
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert result.warnings == ()
    assert env.state.log == ["approve #7", "merge #7 squash"]
    assert env.state.merges == [(7, "SQUASH", HEAD)]
    assert MERGE_TOKEN in env.requests
    assert env.state.created_comments == []
    assert "**MERGED** `automerge arm` #7" in result.to_markdown("acme/widgets")


def test_arm_merges_with_the_configured_method() -> None:
    env = setup(pr(7), policy_text=policy("merge-ready: true", "method: rebase"))
    env.state.auto_merge_refusals[7] = ["clean"] * 6

    env.service.arm(7)

    assert env.state.merges == [(7, "REBASE", HEAD)]


def test_arm_without_merge_ready_never_calls_merge() -> None:
    env = setup(pr(7), policy_text=policy("merge-ready: false"))
    env.state.auto_merge_refusals[7] = ["clean"] * 6

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert MERGE_TOKEN not in env.requests


def test_arm_does_not_merge_what_github_armed() -> None:
    env = setup(pr(7), policy_text=MERGE_READY)

    result = env.service.arm(7)

    assert result.decision is Decision.ARMED
    assert env.state.merges == []


def test_arm_falls_back_to_ready_with_a_warning_when_the_merge_is_refused() -> None:
    env = setup(pr(7, merge_state="UNSTABLE"), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[7] = ["unstable"] * 6

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert result.exit_code == 0
    assert result.state == "unstable"
    assert result.reason == "patch update: approved; already mergeable, needs a person to merge"
    assert result.warnings == (
        "#7: merge not done: refusing to merge: #7 is not mergeable right now (state: unstable)",
    )
    assert env.state.merges == []


def test_arm_falls_back_to_ready_when_github_refuses_the_merge_call() -> None:
    env = setup(pr(7), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[7] = ["clean"] * 6
    env.state.merge_refusals[7] = "Head branch was modified"

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert result.warnings == (
        "#7: merge not done: refusing to merge: GitHub says: Head branch was modified",
    )


def test_arm_never_merges_a_head_other_than_the_one_it_decided_on() -> None:
    env = setup(pr(7), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[7] = ["clean"] * 6
    # The head moves between the decision and the merge: the verb sees another head.
    env.state.pull_requests[7] = replace(env.state.pull_requests[7], head_sha="e" * 40)
    result = env.service._arm_verbs(
        7,
        AutomergeOptions.model_validate({"merge-ready": True}),
        Eligibility(True, "patch update"),
        (),
        HEAD,
        attempts=1,
    )

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "not the expected" in result.warnings[0]


def test_sweep_merges_and_counts_it_next_to_armed_and_ready() -> None:
    env = setup(pr(1), pr(2, title=MAJOR), pr(3), pr(4), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[1] = ["clean"]
    env.state.auto_merge_refusals[3] = ["clean"]
    env.state.merge_refusals[3] = "not mergeable"

    result = env.service.sweep()

    assert (result.merged, result.ready, result.armed, result.errors) == (1, 1, 1, 0)
    assert env.state.merges == [(1, "SQUASH", HEAD)]
    assert result.rows[0].actions == [
        "patch update: approved, merged (already mergeable, auto-merge refused)"
    ]
    assert result.rows[2].actions == ["approved; already mergeable, needs a person to merge"]
    assert any(w.startswith("#3: merge not done:") for w in result.warnings)
    assert [number for number, _ in env.state.created_comments] == [3]  # the hint, for #3 only
    assert "merged 1, ready 1" in result.to_markdown("acme/widgets")


def test_sweep_without_merge_ready_leaves_a_mergeable_pull_request_to_a_person() -> None:
    env = setup(pr(1))
    env.state.auto_merge_refusals[1] = ["clean"]

    result = env.service.sweep()

    assert (result.merged, result.ready) == (0, 1)
    assert env.state.merges == []


# -- waiting for a ready pull request that is not clean yet ---------------------------------

WAIT_TOKEN = {"pull_requests": "read", "checks": "read"}


def run(
    name: str, status: str, conclusion: str | None = None, details_url: str | None = None
) -> CheckRun:
    return CheckRun(
        id=sum(map(ord, name)),
        html_url="https://github.com/acme/widgets/runs/1",
        name=name,
        status=status,
        conclusion=conclusion,
        details_url=details_url,
        title=None,
        summary=None,
        text=None,
        app_id=1,
        app_slug="ci",
    )


def waiting(state: str = "UNSTABLE", checks: list[CheckRun] | None = None, **changes: Any) -> Setup:
    """A routine pull request GitHub refuses to arm, in ``state`` with ``checks`` on its head."""
    env = setup(pr(7, merge_state=state, **changes), policy_text=MERGE_READY)
    env.state.auto_merge_refusals[7] = [state.lower()] * 6
    env.state.check_runs[HEAD] = (
        [run("CodeQL", "in_progress"), run("test", "completed", "success")]
        if checks is None
        else checks
    )
    return env


def change(env: Setup, **fields: Any) -> Callable[[], None]:
    def apply() -> None:
        env.state.pull_requests[7] = replace(env.state.pull_requests[7], **fields)

    return apply


def checks_finish(env: Setup) -> Callable[[], None]:
    def apply() -> None:
        env.state.check_runs[HEAD] = [run("CodeQL", "completed", "success")]
        change(env, merge_state="CLEAN")()

    return apply


def test_arm_waits_for_non_required_checks_and_then_merges() -> None:
    env = waiting()
    env.clock.script = [lambda: None, checks_finish(env)]

    result = env.service.arm(7)

    assert result.decision is Decision.MERGED
    assert env.sleeps == [30.0, 30.0]
    assert env.state.merges == [(7, "SQUASH", HEAD)]
    assert env.state.log == ["approve #7", "merge #7 squash"]
    assert WAIT_TOKEN in env.requests
    assert env.logs == [
        "#7: ready but not clean yet (unstable; pending: CodeQL); waiting up to 600s, "
        "looking every 30s",
        "#7: wait ended after 60s: merged",
    ]


def test_arm_waits_while_github_computes_the_merge_state() -> None:
    env = waiting("UNKNOWN", checks=[])
    env.clock.script = [change(env, merge_state="CLEAN")]

    result = env.service.arm(7)

    assert result.decision is Decision.MERGED
    assert env.sleeps == [30.0]
    assert env.logs[0].startswith("#7: ready but not clean yet (unknown); waiting up to 600s")
    # The checks are read for an unstable state only.
    assert env.calls("list_head_check_runs") == []


def test_arm_does_not_wait_when_no_check_is_running() -> None:
    env = waiting(checks=[run("test", "completed", "success")])

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.logs == []
    assert result.warnings == (
        "#7: merge not done: refusing to merge: #7 is not mergeable right now (state: unstable)",
    )


def test_arm_does_not_wait_when_a_check_has_failed_already() -> None:
    env = waiting(checks=[run("CodeQL", "in_progress"), run("lint", "completed", "failure")])

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.state.merges == []


@pytest.mark.parametrize("state", ["DIRTY", "BLOCKED", "BEHIND", "DRAFT", "HAS_HOOKS", ""])
def test_arm_never_waits_for_a_state_that_does_not_clear_by_itself(state: str) -> None:
    env = waiting(state, checks=[run("CodeQL", "in_progress")])

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.calls("list_head_check_runs") == []
    assert env.state.merges == []


def test_arm_stops_waiting_when_a_check_fails_during_the_wait() -> None:
    env = waiting()
    env.clock.script = [
        lambda: env.state.check_runs.update({HEAD: [run("CodeQL", "completed", "failure")]})
    ]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == [30.0]
    assert env.state.merges == []
    assert result.warnings == (
        "#7: merge not done: refusing to merge: #7 is not mergeable right now (state: unstable)",
    )
    assert env.logs[-1].startswith("#7: wait ended after 30s: not merged")


def test_arm_does_not_merge_a_head_that_moved_during_the_wait() -> None:
    env = waiting()
    env.clock.script = [change(env, head_sha="e" * 40, merge_state="CLEAN")]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == [30.0]
    assert env.state.merges == []
    assert "not the expected" in result.warnings[0]


def test_arm_does_not_keep_waiting_for_checks_of_a_head_that_moved() -> None:
    env = waiting()
    env.clock.script = [change(env, head_sha="e" * 40)]  # still unstable, new head, new checks

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == [30.0]
    assert env.state.merges == []


def test_arm_does_not_merge_when_the_approval_is_dismissed_during_the_wait() -> None:
    env = waiting()

    def dismiss() -> None:
        env.state.reviews[7] = [Review(BOT, "DISMISSED", HEAD, "https://github.com/x#r1")]
        change(env, merge_state="CLEAN")()

    env.clock.script = [dismiss]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "has not approved #7" in result.warnings[0]


@pytest.mark.parametrize("state", ["DIRTY", "BLOCKED", "BEHIND"])
def test_arm_does_not_merge_when_the_state_turns_bad_during_the_wait(state: str) -> None:
    env = waiting()
    env.clock.script = [change(env, merge_state=state)]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == [30.0]  # no second wait
    assert env.state.merges == []
    assert f"(state: {state.lower()})" in result.warnings[0]


def test_arm_does_not_merge_when_the_pull_request_is_no_longer_routine() -> None:
    env = waiting()
    env.clock.script = [change(env, title=MAJOR, merge_state="CLEAN")]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "no longer eligible" in result.warnings[0]
    assert "major" in result.warnings[0]


def test_arm_does_not_merge_when_the_pull_request_is_closed_during_the_wait() -> None:
    env = waiting()
    env.clock.script = [change(env, state="closed", merge_state="CLEAN")]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "no longer eligible" in result.warnings[0]


def test_arm_does_not_merge_when_merge_ready_is_switched_off_during_the_wait() -> None:
    env = waiting()

    def switch_off() -> None:
        env.set_policy(policy("merge-ready: false"))
        change(env, merge_state="CLEAN")()

    env.clock.script = [switch_off]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "merge-ready is no longer enabled" in result.warnings[0]


def test_arm_gives_up_when_the_wait_budget_is_used_up() -> None:
    env = waiting()

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == [30.0] * 20  # 600 s in all; a 21st sleep would pass the budget
    assert len(env.calls("list_head_check_runs")) == 21
    assert env.state.merges == []
    assert "the wait budget of 600s is used up" in result.warnings[0]
    assert "(waited 600s)" in result.warnings[0]
    assert env.logs[-1].startswith("#7: wait ended after 600s: not merged")


def test_arm_honours_custom_bounds() -> None:
    env = waiting()
    env.wait = WaitBounds(budget=90, interval=20)

    env.service.arm(7)

    assert env.sleeps == [20.0] * 4  # the 5th look is at 80 s; one more would pass 90 s


def test_arm_stops_waiting_when_the_request_budget_is_low() -> None:
    env = waiting()
    env.factory.last_rate_limit = RateLimit(remaining=120)

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert "the request budget is low (120 left)" in result.warnings[0]
    assert env.state.merges == []


def test_arm_keeps_waiting_when_the_request_budget_is_ample() -> None:
    env = waiting()
    env.factory.last_rate_limit = RateLimit(remaining=4000)
    env.clock.script = [checks_finish(env)]

    assert env.service.arm(7).decision is Decision.MERGED


def test_arm_with_the_wait_turned_off_refuses_at_once() -> None:
    env = waiting()
    env.wait = WaitBounds(budget=0)

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.calls("list_head_check_runs") == []


def test_arm_without_merge_ready_never_waits() -> None:
    env = waiting()
    env.set_policy(POLICY)

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.calls("list_head_check_runs") == []


def test_arm_reports_a_wait_that_cannot_read_the_pull_request_again() -> None:
    env = waiting()

    def vanish() -> None:
        del env.state.pull_requests[7]

    env.clock.script = [vanish]

    result = env.service.arm(7)

    assert result.decision is Decision.READY
    assert env.state.merges == []
    assert "could not be read again" in result.warnings[0]


def test_sweep_never_waits_for_a_ready_pull_request() -> None:
    env = waiting()

    result = env.service.sweep()

    assert (result.merged, result.ready) == (0, 1)
    assert env.sleeps == []
    assert env.calls("list_head_check_runs") == []
    assert env.state.merges == []


# The timeline of lukislp/studylife-developers #201 (2026-11-11): every other check of the head
# had finished, the arm job's own check run (a pull_request_target job is attached to the pull
# request head) was the only one still running, so GitHub reported the state as unstable.
OWN = "https://github.com/acme/widgets/actions/runs/555/job/9"
OTHER = "https://github.com/acme/widgets/actions/runs/444/job/3"


def issue_201_checks() -> list[CheckRun]:
    return [
        run("CodeQL", "completed", "neutral"),
        run("review / dependency-review", "completed", "success", OTHER),
        run("lockfiles / regenerate", "completed", "success", OTHER),
        run("build-and-test", "completed", "success", OTHER),
        run("automerge / sweep", "completed", "skipped", OWN),
        run("automerge / arm", "in_progress", None, OWN),
    ]


def test_arm_does_not_wait_for_its_own_check_run() -> None:
    env = waiting(checks=issue_201_checks())
    env.own_run_id = 555

    result = env.service.arm(7)

    # Waiting could never help: the run ends with the job. No budget is burnt, the pull request
    # is left ready (the hourly sweep merges it), and the log says why.
    assert result.decision is Decision.READY
    assert env.sleeps == []
    assert env.state.merges == []
    assert env.logs == [
        "#7: only this job's own check run is unfinished; not waiting for it, the next sweep merges"
    ]


def test_the_sweep_after_the_arm_job_ended_merges_the_pull_request() -> None:
    env = waiting(checks=issue_201_checks())
    env.own_run_id = 555
    env.service.arm(7)
    assert env.state.merges == []

    # The arm job is over: its check run is complete and the state turned clean. The sweep (a
    # run attached to the default branch) merges it, bound to the approved head.
    env.state.check_runs[HEAD] = [run("automerge / arm", "completed", "success", OWN)]
    change(env, merge_state="CLEAN")()
    env.state.auto_merge_refusals[7] = ["clean"]
    result = env.service.sweep()

    assert result.merged == 1
    assert env.state.merges == [(7, "SQUASH", HEAD)]


def test_arm_waits_for_other_checks_but_not_for_its_own_and_hands_over_at_the_end() -> None:
    env = waiting(checks=[run("CodeQL", "in_progress", None, OTHER), *issue_201_checks()[-1:]])
    env.own_run_id = 555

    def codeql_done() -> None:
        env.state.check_runs[HEAD] = issue_201_checks()

    env.clock.script = [codeql_done]

    result = env.service.arm(7)

    # One wait for CodeQL; then only the own run is left: hand over at once, no 10-minute burn.
    assert result.decision is Decision.READY
    assert env.sleeps == [30.0]
    assert env.logs[0].startswith("#7: ready but not clean yet (unstable; pending: CodeQL)")
    assert any("own check run" in line for line in env.logs)


def test_arm_without_a_run_id_counts_every_unfinished_run() -> None:
    env = waiting(checks=issue_201_checks())

    env.service.arm(7)

    assert env.sleeps == [30.0] * 20  # why the own run has to be told apart
