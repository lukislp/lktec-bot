# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The pure rules for waiting on a pull request that is ready but not clean yet."""

from __future__ import annotations

import pytest

from lktec_bot.domain.merge_wait import (
    CheckTally,
    WaitBounds,
    give_up_reason,
    needs_check_tally,
    tally_checks,
    wait_reason,
    workflow_run_id,
)

RUNNING = CheckTally(pending=2, pending_names=("a", "b"))


def test_tally_counts_unfinished_and_failed_runs() -> None:
    tally = tally_checks(
        [
            ("queued-one", "queued", None),
            ("running", "in_progress", None),
            ("held", "waiting", None),
            ("ok", "completed", "success"),
            ("skipped", "completed", "skipped"),
            ("neutral", "completed", "neutral"),
            ("bad", "completed", "failure"),
            ("late", "completed", "timed_out"),
            ("gone", "completed", "cancelled"),
        ]
    )

    assert tally == CheckTally(3, 3, ("queued-one", "running", "held"))


def test_tally_of_nothing_is_empty() -> None:
    assert tally_checks([]) == CheckTally(0, 0, ())


def test_an_unfinished_run_is_never_counted_as_failed() -> None:
    assert tally_checks([("x", "in_progress", "failure")]) == CheckTally(1, 0, ("x",))


@pytest.mark.parametrize("state", ["UNKNOWN", "unknown"])
def test_unknown_is_worth_waiting_for(state: str) -> None:
    assert wait_reason(state, None) is not None


def test_unstable_with_running_checks_is_worth_waiting_for() -> None:
    assert wait_reason("UNSTABLE", RUNNING) == "2 check(s) are still running"
    assert wait_reason("unstable", RUNNING) is not None


@pytest.mark.parametrize(
    "checks",
    [None, CheckTally(), CheckTally(pending=0, failed=3), CheckTally(pending=2, failed=1)],
)
def test_unstable_without_running_checks_or_with_a_failed_one_is_not(
    checks: CheckTally | None,
) -> None:
    assert wait_reason("UNSTABLE", checks) is None


@pytest.mark.parametrize(
    "state", ["CLEAN", "DIRTY", "BLOCKED", "BEHIND", "DRAFT", "HAS_HOOKS", "", "SOMETHING_NEW"]
)
def test_every_other_state_is_never_waited_for(state: str) -> None:
    assert wait_reason(state, RUNNING) is None
    assert wait_reason(state, None) is None


def test_only_unstable_needs_the_checks() -> None:
    assert needs_check_tally("UNSTABLE")
    assert needs_check_tally("unstable")
    assert not any(needs_check_tally(s) for s in ["UNKNOWN", "CLEAN", "DIRTY", "BLOCKED", ""])


def test_default_bounds() -> None:
    bounds = WaitBounds()
    assert (bounds.budget, bounds.interval, bounds.min_rate_remaining) == (600.0, 30.0, 500)
    assert bounds.enabled


@pytest.mark.parametrize(("budget", "interval"), [(0, 30), (-1, 30), (600, 0)])
def test_bounds_with_nothing_to_spend_are_disabled(budget: float, interval: float) -> None:
    assert not WaitBounds(budget=budget, interval=interval).enabled


def test_the_wait_ends_when_the_next_look_would_pass_the_budget() -> None:
    bounds = WaitBounds(budget=100, interval=30)

    assert give_up_reason(bounds, 0, None) is None
    assert give_up_reason(bounds, 70, None) is None  # the next look is at exactly 100 s
    assert give_up_reason(bounds, 71, None) == "the wait budget of 100s is used up"
    assert give_up_reason(bounds, 600, None) is not None


def test_the_wait_ends_when_the_request_budget_is_low() -> None:
    bounds = WaitBounds(min_rate_remaining=500)

    assert give_up_reason(bounds, 0, 499) == "the request budget is low (499 left)"
    assert give_up_reason(bounds, 0, 500) is None
    assert give_up_reason(bounds, 0, None) is None  # not reported yet: no reason to stop


@pytest.mark.parametrize(
    ("url", "run_id"),
    [
        ("https://github.com/a/b/actions/runs/43054008858/job/130638495457", 43054008858),
        ("https://github.com/a/b/actions/runs/7", 7),
        ("https://github.com/a/b/runs/130638457917", None),
        ("https://socket.dev/dashboard/org/x", None),
        ("", None),
        (None, None),
    ],
)
def test_workflow_run_id_comes_from_the_details_url(url: str | None, run_id: int | None) -> None:
    assert workflow_run_id(url) == run_id
