# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Options of the ``automerge`` module, as they appear under ``modules.automerge`` in
``.github/lktec-bot.yml``. Unknown keys are errors, so a typo never changes behaviour."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AutomergeOptions(BaseModel):
    """Options of the ``automerge`` module.

    ``majors``
        ``manual`` (default): a major update is never armed, a person merges it. ``auto``: a
        major is treated like any other update.
    ``exclude``
        Dependency names (exact, case-insensitive) that are never approved or armed. A grouped
        pull request is skipped as a whole when any of its members is listed.
    ``workflow-files``
        ``auto`` (default): a pull request that changes ``.github/workflows/`` is armed like
        any other. ``manual``: such a pull request is never armed.
    ``method``
        How GitHub merges once the checks are green: ``squash`` (default), ``merge`` or
        ``rebase``.
    ``merge-ready``
        ``false`` (default): a pull request GitHub will not arm because it is already mergeable
        is approved and left for a person. ``true``: the ``merge`` verb merges it, bound to the
        head the decision was made on and to branch protection.
    """

    model_config = ConfigDict(extra="forbid")

    majors: Literal["manual", "auto"] = "manual"
    exclude: list[str] = []
    workflow_files: Literal["auto", "manual"] = Field(default="auto", alias="workflow-files")
    method: Literal["squash", "merge", "rebase"] = "squash"
    merge_ready: StrictBool = Field(default=False, alias="merge-ready")
