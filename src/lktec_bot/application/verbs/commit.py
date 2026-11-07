# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``commit``: put file changes onto an existing branch as one commit that GitHub signs.

The commit is made with the GraphQL mutation ``createCommitOnBranch``, so GitHub itself creates
and signs it: it shows as verified, the author is the App's bot and the committer is GitHub
(``web-flow``), with no signing key and no personal access token. The only permission is
``contents:write``.

Safety rules, all checked before anything is sent:

- the branch must exist, unless ``--from BRANCH`` is given: then a missing ``--branch`` is
  created at the head of that branch first (an existing branch is never moved or force-updated);
- the commit is made on top of ``--expected-head`` (default: the branch head resolved right
  before the call) and passed to GitHub as ``expectedHeadOid``, so a push that lands in between
  makes the mutation fail instead of being overwritten;
- every path must be a normalised relative path inside the repository; a path under
  ``.github/workflows/`` is refused because the App has no ``workflows`` permission and GitHub
  would reject it (see :mod:`lktec_bot.domain.paths`).

``--add <repo-path>=<local-file>`` (repeatable) adds or replaces a file with the content of a
local file, ``--delete <repo-path>`` (repeatable) removes one.
"""

from __future__ import annotations

import re
from pathlib import Path

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.domain.paths import repo_path_problem
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi

_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# GraphQL accepts far less than the REST limits; refuse early with a clear message.
MAX_TOTAL_BYTES = 30 * 1024 * 1024


class _Refusal(Exception):
    """A request the verb refuses; the text becomes the result's message."""


def _strings(request: VerbRequest, name: str) -> list[str]:
    raw = request.args.get(name)
    if raw is None:
        return []
    if not isinstance(raw, list | tuple) or not all(isinstance(item, str) for item in raw):
        raise _Refusal(f"--{name} must be a list of strings")
    return [str(item) for item in raw]


def _text(request: VerbRequest, name: str, *, required: bool) -> str:
    raw = request.args.get(name)
    if raw is None or raw == "":
        if required:
            raise _Refusal(f"--{name.replace('_', '-')} is required")
        return ""
    return str(raw)


def _additions(entries: list[str]) -> dict[str, bytes]:
    additions: dict[str, bytes] = {}
    total = 0
    for entry in entries:
        repo_path, separator, local = entry.partition("=")
        if not separator or not local:
            raise _Refusal(f"--add {entry!r} must look like <repo-path>=<local-file>")
        problem = repo_path_problem(repo_path)
        if problem is not None:
            raise _Refusal(f"--add {repo_path!r}: {problem}")
        if repo_path in additions:
            raise _Refusal(f"--add {repo_path!r} is given twice")
        try:
            content = Path(local).read_bytes()
        except OSError as exc:
            raise _Refusal(f"--add {repo_path!r}: cannot read {local}: {exc}") from None
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise _Refusal(f"the added files exceed {MAX_TOTAL_BYTES // (1024 * 1024)} MiB")
        additions[repo_path] = content
    return additions


def _deletions(entries: list[str], additions: dict[str, bytes]) -> list[str]:
    deletions: list[str] = []
    for repo_path in entries:
        problem = repo_path_problem(repo_path)
        if problem is not None:
            raise _Refusal(f"--delete {repo_path!r}: {problem}")
        if repo_path in additions:
            raise _Refusal(f"{repo_path!r} is both added and deleted")
        if repo_path in deletions:
            raise _Refusal(f"--delete {repo_path!r} is given twice")
        deletions.append(repo_path)
    return deletions


@register(
    Verb.COMMIT,
    grants=frozenset({Grant(Permission.CONTENTS, "write")}),
    options=(
        Option(
            "branch", "Branch to commit onto; it must exist unless --from is given.", required=True
        ),
        Option(
            "from_branch",
            "Create --branch from the head of this branch when it does not exist yet.",
            cli_flag="--from",
        ),
        Option("message", "Commit headline (one line).", required=True),
        Option("body", "Commit message body.", kind="text"),
        Option(
            "add",
            "Add or replace a file: <repo-path>=<local-file>. Repeat for more files.",
            kind="multi",
        ),
        Option("delete", "Delete a file: <repo-path>. Repeat for more files.", kind="multi"),
        Option(
            "expected_head",
            "Commit id the branch must still point at; default: the head resolved right now.",
        ),
    ),
)
def commit(api: GitHubApi, request: VerbRequest) -> VerbResult:
    try:
        branch = _text(request, "branch", required=True)
        headline = _text(request, "message", required=True)
        if "\n" in headline or "\r" in headline:
            raise _Refusal("--message is the headline and must be one line; use --body-file")
        if branch.startswith("refs/"):
            raise _Refusal(f"--branch {branch!r} must be a branch name, not a full ref")
        body = _text(request, "body", required=False)
        expected = _text(request, "expected_head", required=False)
        if expected and not _OID.match(expected):
            raise _Refusal(f"--expected-head {expected!r} is not a full commit id")
        source = _text(request, "from_branch", required=False)
        if source.startswith("refs/"):
            raise _Refusal(f"--from {source!r} must be a branch name, not a full ref")
        if source == branch:
            raise _Refusal("--from must name another branch than --branch")
        additions = _additions(_strings(request, "add"))
        deletions = _deletions(_strings(request, "delete"), additions)
        if not additions and not deletions:
            raise _Refusal("nothing to commit: give at least one --add or --delete")
    except _Refusal as refusal:
        return VerbResult(VerbOutcome.ERROR, f"refusing to commit: {refusal}")

    head = api.get_branch_head(request.owner, request.repo, branch)
    created_from = ""
    if head is None:
        if not source:
            return VerbResult(VerbOutcome.ERROR, f"branch {branch!r} does not exist")
        head = api.get_branch_head(request.owner, request.repo, source)
        if head is None:
            return VerbResult(
                VerbOutcome.ERROR,
                f"branch {branch!r} does not exist and --from {source!r} does not either",
            )
        created_from = f" (new branch from {source})"
    if expected and head != expected:
        return VerbResult(
            VerbOutcome.ERROR,
            f"branch {branch!r} moved: expected {expected}, now at {head}; nothing was committed",
        )
    if created_from:
        # Created at the head of the source branch; never a force-update of an existing branch.
        api.create_branch(request.owner, request.repo, branch, head)
    created = api.create_commit_on_branch(
        request.owner,
        request.repo,
        branch,
        headline,
        body,
        expected or head,
        additions,
        deletions,
    )
    changed = f"{len(additions)} file(s) written, {len(deletions)} deleted"
    return VerbResult(
        VerbOutcome.OK,
        f"created commit {created.oid} on {branch}{created_from} ({changed})",
        url=created.url,
    )
