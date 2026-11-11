# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""When a pull request that is ready to merge is worth waiting for: pure rules, no I/O.

The ``merge`` verb merges only a pull request whose GitHub ``mergeable_state`` is ``clean``.
Two other states clear by themselves and are worth a short wait:

- ``unknown``: GitHub has not computed the state yet.
- ``unstable`` while a check of the head commit is still running. ``unstable`` means a check
  that is not required is pending or failed. A pending one finishes and the state turns
  ``clean``; a failed one keeps it ``unstable`` for good, so a single failed check ends the wait.

Every other state (``dirty``, ``blocked``, ``behind``, ``draft``, ``has_hooks``, an empty or
unreadable one) needs somebody to act and is never waited for. The wait is bounded by a
:class:`WaitBounds`; the loop that applies these rules lives in the application layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

UNKNOWN = "UNKNOWN"
UNSTABLE = "UNSTABLE"
# A check run that has not finished: queued, running, or held back (``waiting``, ``pending``,
# ``requested`` are the states GitHub reports for deployments and workflow approvals).
_UNFINISHED = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
# A conclusion that keeps a pull request ``unstable``; success, neutral and skipped do not.
_FAILED = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)


@dataclass(frozen=True, slots=True)
class CheckTally:
    """How many check runs of a head commit are unfinished and how many have failed.

    ``pending_names`` are the names of the unfinished ones, for the log line.
    """

    pending: int = 0
    failed: int = 0
    pending_names: tuple[str, ...] = ()


def tally_checks(runs: Iterable[tuple[str, str, str | None]]) -> CheckTally:
    """Count ``(name, status, conclusion)`` triples of check runs (GitHub's words)."""
    names: list[str] = []
    failed = 0
    for name, status, conclusion in runs:
        if status.lower() in _UNFINISHED:
            names.append(name)
        elif (conclusion or "").lower() in _FAILED:
            failed += 1
    return CheckTally(len(names), failed, tuple(names))


def needs_check_tally(merge_state: str) -> bool:
    """Whether :func:`wait_reason` needs the checks (only ``unstable`` does)."""
    return merge_state.upper() == UNSTABLE


def wait_reason(merge_state: str, checks: CheckTally | None) -> str | None:
    """Why waiting can help, or ``None`` when the state is not worth waiting for.

    ``checks`` is the tally of the head commit; ``None`` (not read, or unreadable) never
    justifies waiting for an ``unstable`` pull request.
    """
    state = merge_state.upper()
    if state == UNKNOWN:
        return "GitHub is still computing the merge state"
    if state == UNSTABLE and checks is not None and checks.pending > 0 and checks.failed == 0:
        return f"{checks.pending} check(s) are still running"
    return None


@dataclass(frozen=True, slots=True)
class WaitBounds:
    """How long and how often to look again, and the request budget below which to stop.

    ``budget`` and ``interval`` are seconds. ``min_rate_remaining`` is the number of requests
    the installation must still have; the installation shares one hourly budget across every
    repository the App is installed on, so a wait never spends the last of it.
    """

    budget: float = 600.0
    interval: float = 30.0
    min_rate_remaining: int = 500

    @property
    def enabled(self) -> bool:
        return self.budget > 0 and self.interval > 0


def give_up_reason(bounds: WaitBounds, elapsed: float, rate_remaining: int | None) -> str | None:
    """Why the wait ends now instead of looking once more, or ``None`` to carry on.

    The next look happens one ``interval`` from now, so it must still fall inside the budget.
    An unknown rate limit (no answer reported it yet) does not stop the wait.
    """
    if rate_remaining is not None and rate_remaining < bounds.min_rate_remaining:
        return f"the request budget is low ({rate_remaining} left)"
    if elapsed + bounds.interval > bounds.budget:
        return f"the wait budget of {bounds.budget:.0f}s is used up"
    return None
