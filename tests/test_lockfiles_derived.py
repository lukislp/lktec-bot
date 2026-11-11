# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Derived files in the ``lockfiles`` module: the ``derived`` option, the allow-list every
handed-over path goes through, and what the commit job does with a handover that carries them."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lktec_bot.application.modules import _handover, validate_policy
from lktec_bot.application.modules.lockfiles import allowlist
from lktec_bot.application.modules.lockfiles.allowlist import Kind, admit
from lktec_bot.application.modules.lockfiles.ecosystems import is_dependency_file
from lktec_bot.application.modules.lockfiles.options import (
    MAX_GENERATOR_PATHS,
    MAX_GENERATORS,
    DerivedGenerator,
    LockfilesOptions,
)
from lktec_bot.application.modules.lockfiles.service import Decision, HandoverError, read_manifest
from lktec_bot.domain.policy import PolicyError
from tests.test_lockfiles import BRANCH, HEAD, Setup, dependabot_pr, handover

NOTICES = "THIRD-PARTY-NOTICES.md"
DERIVED = (
    "version: 1\n"
    "modules:\n"
    "  lockfiles:\n"
    "    enabled: true\n"
    "    derived:\n"
    "      - name: third-party notices\n"
    "        run: python3 tools/check-licences.py --update\n"
    f"        paths: [{NOTICES}]\n"
)


def generator(**changes: object) -> dict[str, object]:
    return {"name": "notices", "run": "make notices", "paths": [NOTICES], **changes}


def options(*generators: dict[str, object], **more: object) -> LockfilesOptions:
    return LockfilesOptions.model_validate({"derived": list(generators), **more})


# -- the option -----------------------------------------------------------------------------


def test_no_derived_option_means_no_generator() -> None:
    assert LockfilesOptions.model_validate({}).derived == []
    assert LockfilesOptions.model_validate({}).derived_paths() == {}


def test_a_generator_has_a_name_a_command_and_exact_paths() -> None:
    parsed = options(generator(paths=[NOTICES, "apps/mobile/public/notices.json"]))

    only = parsed.derived[0]
    assert (only.name, only.run, only.timeout_minutes) == ("notices", "make notices", 5)
    assert parsed.derived_paths() == {
        NOTICES: "notices",
        "apps/mobile/public/notices.json": "notices",
    }


def test_a_command_may_span_lines_and_set_its_timeout() -> None:
    parsed = options(generator(run="corepack enable\npnpm install\n", **{"timeout-minutes": 10}))

    assert parsed.derived[0].run == "corepack enable\npnpm install\n"
    assert parsed.derived[0].timeout_minutes == 10


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"run": "x", "paths": [NOTICES]}, "name"),
        ({"name": "n", "paths": [NOTICES]}, "run"),
        ({"name": "n", "run": "x"}, "paths"),
        (generator(name=""), "must start with a letter or digit"),
        (generator(name=" padded"), "must start with a letter or digit"),
        (generator(name="trailing "), "must start with a letter or digit"),
        (generator(name="a`b"), "must start with a letter or digit"),
        (generator(name="a\nb"), "must start with a letter or digit"),
        (generator(name="n" * 61), "at most 60"),
        (generator(name=7), "valid string"),
        (generator(run=""), "must not be empty"),
        (generator(run="  \n "), "must not be empty"),
        (generator(run="x" * 2001), "at most 2000"),
        (generator(run="a\x00b"), "control characters"),
        (generator(run="a\x1bb"), "control characters"),
        (generator(run=["make"]), "valid string"),
        (generator(run=True), "valid string"),
        (generator(paths=[]), "at least one file"),
        (generator(paths="NOTICES.md"), "valid list"),
        (generator(paths=[f"n{i}.md" for i in range(MAX_GENERATOR_PATHS + 1)]), "at most 8"),
        (generator(paths=[NOTICES, NOTICES]), "listed twice"),
        (generator(paths=["../NOTICES.md"]), "'..' segment"),
        (generator(paths=["docs/../NOTICES.md"]), "'..' segment"),
        (generator(paths=["/etc/NOTICES.md"]), "absolute"),
        (generator(paths=["C:/NOTICES.md"]), "absolute"),
        (generator(paths=["docs\\NOTICES.md"]), "backslash"),
        (generator(paths=["docs/"]), "empty"),
        (generator(paths=[""]), "empty"),
        (generator(paths=["*.md"]), "is a pattern"),
        (generator(paths=["docs/**"]), "is a pattern"),
        (generator(paths=["notices-?.md"]), "is a pattern"),
        (generator(paths=["notices[12].md"]), "is a pattern"),
        (generator(paths=["{a,b}.md"]), "is a pattern"),
        (generator(paths=["a=b.md"]), "characters outside"),
        (generator(paths=["a$b.md"]), "characters outside"),
        (generator(paths=[".git/config"]), "points into .git"),
        (generator(paths=[".github/workflows/ci.yml"]), "workflows"),
        (generator(paths=[".github/lktec-bot.yml"]), "own policy file"),
        (generator(paths=[".GitHub/LKTEC-bot.yml"]), "own policy file"),
        (generator(paths=[".github/dependabot.yml"]), "under .github/"),
        (generator(paths=[".github"]), "under .github/"),
        (generator(paths=[".GITHUB/badges/x.svg"]), "under .github/"),
        (generator(paths=["uv.lock"]), "a lock file is refreshed by the module itself"),
        (generator(paths=["src/App/packages.lock.json"]), "a lock file is refreshed"),
        (generator(**{"timeout-minutes": 0}), "greater than or equal to 1"),
        (generator(**{"timeout-minutes": 11}), "less than or equal to 10"),
        (generator(**{"timeout-minutes": "5"}), "valid integer"),
        (generator(**{"timeout-minutes": True}), "valid integer"),
        (generator(interpreter="zsh"), "Extra inputs"),
        ("make notices", "valid dictionary"),
    ],
)
def test_a_generator_of_any_other_shape_is_rejected(entry: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LockfilesOptions.model_validate({"derived": [entry]})


def test_the_number_of_generators_is_bounded() -> None:
    many = [
        generator(name=f"g{i}", paths=[f"n{i}.md"], **{"timeout-minutes": 1})
        for i in range(MAX_GENERATORS + 1)
    ]

    with pytest.raises(ValidationError, match="at most 4 generators"):
        options(*many)
    assert len(options(*many[:MAX_GENERATORS]).derived) == MAX_GENERATORS


def test_generator_names_and_paths_are_unique_across_generators() -> None:
    with pytest.raises(ValidationError, match="name is used twice"):
        options(generator(), generator(name="Notices", paths=["other.md"]))
    with pytest.raises(ValidationError, match="more than one generator"):
        options(generator(), generator(name="other"))


def test_the_timeouts_together_are_bounded() -> None:
    slow = {"timeout-minutes": 8}

    with pytest.raises(ValidationError, match="add up to 16 minutes"):
        options(generator(**slow), generator(name="b", paths=["b.md"], **slow))


def test_derived_must_be_a_list() -> None:
    with pytest.raises(ValidationError, match="valid list"):
        LockfilesOptions.model_validate({"derived": {"name": "n"}})


def test_the_policy_loader_reports_a_bad_generator_with_its_place() -> None:
    bad = DERIVED.replace(NOTICES, ".github/workflows/ci.yml")

    with pytest.raises(PolicyError) as excinfo:
        validate_policy(bad)

    assert "modules.lockfiles.derived.0.paths" in str(excinfo.value)
    assert validate_policy(DERIVED).modules["lockfiles"].options["derived"]


# -- dependency files -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "Directory.Packages.props",
        "src/App/App.csproj",
        "src/App/packages.lock.json",
        "pnpm-lock.yaml",
        "apps/mobile/package.json",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "services/api/requirements-dev.txt",
        "build/Common.targets",
    ],
)
def test_manifests_and_lock_files_are_dependency_files(path: str) -> None:
    assert is_dependency_file(path)


@pytest.mark.parametrize(
    "path",
    [
        "tools/check-licences.py",
        "tools/licences/policy.toml",
        NOTICES,
        ".github/workflows/ci.yml",
        "package.json.sh",
        "src/requirements.py",
        "notes.txt",
    ],
)
def test_everything_else_is_not_a_dependency_file(path: str) -> None:
    assert not is_dependency_file(path)


# -- the allow-list -------------------------------------------------------------------------


def test_a_lock_file_is_admitted_by_its_name() -> None:
    verdict = admit("src/App/packages.lock.json", 10, options())

    assert (verdict.kind, verdict.refused, verdict.left_out) == (Kind.LOCKFILE, False, False)


def test_a_lock_file_the_options_exclude_is_left_out_not_refused() -> None:
    assert admit("uv.lock", 1, options(ecosystems=["dotnet"])).left_out
    assert admit("uv.lock", 1, options(paths=["services"])).left_out
    assert not admit("services/uv.lock", 1, options(paths=["services"])).left_out


def test_a_declared_derived_path_is_admitted_exactly() -> None:
    declared = options(generator())

    assert admit(NOTICES, 10, declared).kind is Kind.DERIVED
    for other in (f"docs/{NOTICES}", NOTICES.lower(), f"{NOTICES}.bak", "tools/run.sh"):
        verdict = admit(other, 10, declared)
        assert verdict.refused
        assert "neither a known lock file nor a path listed under derived[].paths" in (
            verdict.problem
        )


def test_without_the_option_no_other_file_is_admitted() -> None:
    assert admit(NOTICES, 10, options()).refused


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../uv.lock", "'..' segment"),
        ("/etc/uv.lock", "absolute"),
        (".github/workflows/uv.lock", "workflows"),
        (".git/uv.lock", "points into .git"),
    ],
)
def test_a_lock_file_name_does_not_excuse_an_unsafe_path(path: str, reason: str) -> None:
    verdict = admit(path, 1, options())

    assert verdict.refused
    assert reason in verdict.problem


@pytest.mark.parametrize(
    "path", [".github/lktec-bot.yml", ".github/dependabot.yml", ".github/workflows/ci.yml"]
)
def test_a_path_under_dot_github_is_refused_even_if_a_policy_declared_it(path: str) -> None:
    # The options model refuses such a declaration; the allow-list must not rely on that.
    forged = LockfilesOptions.model_construct(
        ecosystems=[],
        paths=[],
        derived=[DerivedGenerator.model_construct(name="n", run="x", paths=[path])],
    )

    verdict = admit(path, 1, forged)

    assert verdict.refused
    assert "policy file" in verdict.problem or ".github" in verdict.problem


def test_an_oversized_derived_file_is_refused_but_a_lock_file_keeps_its_own_cap() -> None:
    declared = options(generator())
    too_large = allowlist.MAX_DERIVED_FILE_BYTES + 1

    assert admit(NOTICES, allowlist.MAX_DERIVED_FILE_BYTES, declared).kind is Kind.DERIVED
    assert "larger than 2 MiB" in admit(NOTICES, too_large, declared).problem
    assert admit("uv.lock", too_large, declared).kind is Kind.LOCKFILE


# -- the handover ---------------------------------------------------------------------------


def test_a_manifest_with_too_many_files_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allowlist, "MAX_FILES", 2)
    lines = "a/uv.lock\nb/uv.lock\nc/uv.lock\n"
    (tmp_path / "manifest.txt").write_text(lines, encoding="utf-8")

    with pytest.raises(HandoverError, match="lists 3 files, more than 2"):
        read_manifest(tmp_path)


def test_the_file_cap_is_far_above_a_real_handover() -> None:
    assert allowlist.MAX_FILES == 1000


def test_the_file_cap_is_the_lock_file_modules_own(tmp_path: Path) -> None:
    lines = "".join(f"data/{index}.json\n" for index in range(1001))
    (tmp_path / "manifest.txt").write_text(lines, encoding="utf-8")

    # The sync module reads the same layout without a cap; nothing changes for it.
    assert len(_handover.read_manifest(tmp_path)) == 1001
    with pytest.raises(HandoverError, match="more than 1000"):
        read_manifest(tmp_path)


# -- the commit job -------------------------------------------------------------------------


def derived_setup(**kwargs: object) -> Setup:
    setup = Setup(policy_text=DERIVED, **kwargs)  # type: ignore[arg-type]
    setup.state.ref_files[(HEAD, NOTICES)] = "old notices\n"
    return setup


def test_lock_files_and_derived_files_go_into_one_commit_bound_to_the_head(
    tmp_path: Path,
) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"new lock\n", NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert len(setup.state.commits) == 1
    branch, headline, body, expected, additions, deletions = setup.state.commits[0]
    assert branch == BRANCH
    assert headline == "chore(deps): refresh lockfiles after Dependabot bump"
    assert expected == HEAD
    assert additions == {"uv.lock": b"new lock\n", NOTICES: b"new notices\n"}
    assert deletions == []
    assert f"- {NOTICES}" in body
    # Exactly the tokens of a lock file commit: the derived file needs no further permission.
    assert setup.permissions() == [
        {"contents": "read"},
        {"contents": "read", "pull_requests": "read"},
        {"contents": "read"},
        {"contents": "write"},
        {"contents": "read"},
        {"pull_requests": "write"},
    ]
    comment = setup.state.created_comments[0][1]
    assert "refreshed the lock files and derived files" in comment
    assert f"`{NOTICES}`" in comment


def test_a_derived_file_alone_is_committed_with_the_configured_message(tmp_path: Path) -> None:
    text = DERIVED + '    commit-message: "chore(deps): refresh #{number}"\n'
    setup = derived_setup()
    setup.factory.files = {**setup.factory.files, ".github/lktec-bot.yml": text}
    changed = handover(tmp_path, {NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert setup.state.commits[0][1] == "chore(deps): refresh #7"
    assert list(setup.state.commits[0][4]) == [NOTICES]
    assert "refreshed the derived files of" in setup.state.created_comments[0][1]


def test_when_the_generator_failed_the_lock_files_are_still_committed(tmp_path: Path) -> None:
    # A failed generator hands over nothing of its own: the handover holds the lock file only.
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"new lock\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert list(setup.state.commits[0][4]) == ["uv.lock"]
    assert "refreshed the lock files of" in setup.state.created_comments[0][1]


def test_unchanged_derived_files_are_nothing_to_commit(tmp_path: Path) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"old lock\n", NOTICES: b"old notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.UNCHANGED
    assert outcome.reason == (
        "nothing to commit: the handed-over files already match the pull request head"
    )
    assert setup.state.commits == []
    assert setup.state.log == []


def test_only_the_derived_file_that_differs_is_committed(tmp_path: Path) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"old lock\n", NOTICES: b"new notices\n"})

    setup.lockfiles.commit(7, changed, HEAD)

    assert list(setup.state.commits[0][4]) == [NOTICES]


def test_a_derived_file_the_head_does_not_have_yet_is_committed(tmp_path: Path) -> None:
    setup = Setup(policy_text=DERIVED)
    changed = handover(tmp_path, {NOTICES: b"first notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.COMMITTED


@pytest.mark.parametrize(
    "extra",
    [
        "tools/check-licences.py",
        f"docs/{NOTICES}",
        ".github/dependabot.yml",
        ".github/lktec-bot.yml",
        "run.sh",
    ],
)
def test_one_unlisted_path_refuses_the_whole_handover(tmp_path: Path, extra: str) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"new lock\n", NOTICES: b"new notices\n", extra: b"x"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    assert f"refusing the handover: {extra!r}: neither a known lock file" in outcome.reason
    assert setup.state.commits == []
    assert {"contents": "write"} not in setup.permissions()


def test_a_workflow_path_refuses_the_handover_before_anything_is_read(tmp_path: Path) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {NOTICES: b"n\n", ".github/workflows/ci.yml": b"x"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert "no 'workflows' permission" in outcome.reason
    assert setup.tokens.minted == []


def test_a_traversal_refuses_the_handover_before_anything_is_read(tmp_path: Path) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {NOTICES: b"n\n"}, manifest=[NOTICES, f"../{NOTICES}"])

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert "'..' segment" in outcome.reason
    assert setup.tokens.minted == []


def test_a_symlinked_derived_file_refuses_the_handover(tmp_path: Path) -> None:
    setup = derived_setup()
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    changed = handover(tmp_path / "h", {"uv.lock": b"new\n"}, manifest=["uv.lock", NOTICES])
    try:
        (changed / "files" / NOTICES).symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available here")

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert "not a plain file" in outcome.reason
    assert setup.tokens.minted == []


def test_an_oversized_derived_file_refuses_the_handover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allowlist, "MAX_DERIVED_FILE_BYTES", 4)
    setup = derived_setup()
    changed = handover(tmp_path, {"uv.lock": b"a longer lock file\n", NOTICES: b"12345"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert f"{NOTICES!r}: the derived file is larger than" in outcome.reason
    assert setup.state.commits == []


def test_a_derived_file_is_refused_when_the_default_branch_policy_does_not_list_it(
    tmp_path: Path,
) -> None:
    # The policy the commit job reads is the default branch's: no ``derived`` there, no commit,
    # whatever the job that ran the pull request's code handed over.
    setup = Setup()
    changed = handover(tmp_path, {NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert setup.state.commits == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"author_login": "someone"},
        {"author_login": "dependabot-preview[bot]"},
        {"draft": True},
        {"state": "closed"},
        {"head_repo": "fork/widgets"},
    ],
)
def test_nothing_is_committed_for_a_pull_request_that_is_not_dependabots(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    setup = derived_setup(pull_request=dependabot_pr(**overrides))
    changed = handover(tmp_path, {"uv.lock": b"new\n", NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.SKIPPED
    assert setup.state.commits == []
    assert len(setup.tokens.minted) == 2  # the policy and the read; no write token
    # Not even the head's copies were read.
    assert all(call[0] != "get_file_content" or call[1][3] is None for call in _calls(setup))


def test_derived_files_are_not_committed_onto_a_head_that_moved_on(tmp_path: Path) -> None:
    setup = derived_setup()
    changed = handover(tmp_path, {NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, "f" * 40)

    assert outcome.decision is Decision.SKIPPED
    assert "moved on" in outcome.reason
    assert setup.state.commits == []


def test_a_push_between_the_read_and_the_commit_fails_instead_of_overwriting(
    tmp_path: Path,
) -> None:
    setup = derived_setup()
    setup.state.branches[BRANCH] = "a" * 40  # the branch moved after the pull request was read
    changed = handover(tmp_path, {NOTICES: b"new notices\n"})

    outcome = setup.lockfiles.commit(7, changed, HEAD)

    assert outcome.decision is Decision.ERROR
    assert "moved: expected" in outcome.reason
    assert setup.state.commits == []


def _calls(setup: Setup) -> list[tuple[str, tuple[object, ...]]]:
    return [call for api in setup.factory.created for call in api.calls]
