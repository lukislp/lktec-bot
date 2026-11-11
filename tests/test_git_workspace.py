# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The git adapter behind ``lockfiles derive``, against real repositories in a temp directory,
the handover directory on disk, and the ``derive`` command wired to both."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lktec_bot.adapters.git_workspace import DirectoryHandover, GitWorkspace
from lktec_bot.application.modules.lockfiles.ecosystems import is_dependency_file
from lktec_bot.application.modules.lockfiles.service import local_file, read_manifest
from lktec_bot.cli import app
from lktec_bot.ports.workspace import CommandResult, FileStat, WorkspaceError

NOTICES = "THIRD-PARTY-NOTICES.md"
POLICY = (
    "version: 1\n"
    "modules:\n"
    "  lockfiles:\n"
    "    enabled: true\n"
    "    derived:\n"
    "      - name: notices\n"
    "        run: make notices\n"
    f"        paths: [{NOTICES}]\n"
)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - a fixed program on fixture repositories
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        check=True,
    )
    return done.stdout.decode("utf-8").strip()


def write(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def commit(root: Path, message: str, files: Mapping[str, str]) -> str:
    write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-gpg-sign", "-m", message)
    return git(root, "rev-parse", "HEAD")


class Repo:
    """A default branch ``trunk`` and a pull request branch checked out on top of it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "trunk")
        self.base = commit(
            self.root,
            "base",
            {
                ".github/lktec-bot.yml": POLICY,
                ".gitignore": "obj/\n",
                "tools/generate.py": "trusted generator\n",
                "Directory.Packages.props": "<Project>1.0</Project>\n",
                NOTICES: "old notices\n",
            },
        )
        git(self.root, "checkout", "-q", "-b", "dependabot/nuget/x")
        self.tree_dir = tmp_path / "tree"

    def workspace(self, ref: str = "trunk") -> GitWorkspace:
        return GitWorkspace(self.root, ref, self.tree_dir)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


# -- the trusted branch ---------------------------------------------------------------------


def test_the_policy_is_read_from_the_trusted_branch_not_from_the_pull_request(repo: Repo) -> None:
    commit(repo.root, "pr", {".github/lktec-bot.yml": "version: 1\nmodules: {}\n"})

    assert repo.workspace().trusted_file(".github/lktec-bot.yml") == POLICY


def test_a_file_the_trusted_branch_does_not_have_is_none(repo: Repo) -> None:
    commit(repo.root, "pr", {"only-in-pr.txt": "x\n"})

    assert repo.workspace().trusted_file("only-in-pr.txt") is None


def test_an_unknown_trusted_branch_is_an_error(repo: Repo) -> None:
    with pytest.raises(WorkspaceError, match="git rev-parse failed"):
        repo.workspace("origin/missing").trusted_file(".github/lktec-bot.yml")


def test_a_trusted_file_that_is_not_text_is_an_error(repo: Repo) -> None:
    git(repo.root, "checkout", "-q", "trunk")
    (repo.root / "blob.bin").write_bytes(b"\xff\xfe\x00")
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "--no-gpg-sign", "-m", "blob")

    with pytest.raises(WorkspaceError, match="not UTF-8"):
        repo.workspace().trusted_file("blob.bin")


@pytest.mark.parametrize("ref", ["", "-x", "--upload-pack=x", "a b", "trunk~1", "a..b", "x:y"])
def test_a_reference_that_is_not_a_plain_branch_is_refused(tmp_path: Path, ref: str) -> None:
    with pytest.raises(WorkspaceError, match="not a plain branch reference"):
        GitWorkspace(tmp_path, ref, tmp_path / "tree")


# -- the prepared tree ----------------------------------------------------------------------


def test_the_tree_is_the_base_commit_with_only_the_pull_requests_dependency_files(
    repo: Repo,
) -> None:
    commit(
        repo.root,
        "bump",
        {
            "Directory.Packages.props": "<Project>2.0</Project>\n",
            "tools/generate.py": "changed by the pull request\n",
            "src/new.py": "new code\n",
        },
    )
    # A lock file refreshed in this job, not committed.
    write(repo.root, {"Directory.Packages.props": "<Project>2.0 refreshed</Project>\n"})

    prepared = repo.workspace().prepare(is_dependency_file)

    assert prepared.root == repo.tree_dir
    assert prepared.base == repo.base
    assert prepared.overlaid == ("Directory.Packages.props",)
    assert prepared.ignored == ("src/new.py", "tools/generate.py")
    tree = repo.tree_dir
    assert (tree / "Directory.Packages.props").read_text(encoding="utf-8") == (
        "<Project>2.0 refreshed</Project>\n"
    )
    assert (tree / "tools/generate.py").read_text(encoding="utf-8") == "trusted generator\n"
    assert not (tree / "src/new.py").exists()


def test_the_base_is_the_merge_base_when_the_default_branch_moved_on(repo: Repo) -> None:
    commit(repo.root, "bump", {"Directory.Packages.props": "<Project>2.0</Project>\n"})
    git(repo.root, "checkout", "-q", "trunk")
    commit(repo.root, "later on trunk", {"tools/generate.py": "newer generator\n"})
    git(repo.root, "checkout", "-q", "dependabot/nuget/x")

    prepared = repo.workspace().prepare(is_dependency_file)

    assert prepared.base == repo.base
    assert (repo.tree_dir / "tools/generate.py").read_text(encoding="utf-8") == (
        "trusted generator\n"
    )


def test_a_dependency_file_the_pull_request_adds_or_removes_follows_it(repo: Repo) -> None:
    commit(repo.root, "add", {"src/App/App.csproj": "<Project/>\n"})
    git(repo.root, "rm", "-q", "Directory.Packages.props")
    git(repo.root, "commit", "-q", "--no-gpg-sign", "-m", "remove")

    prepared = repo.workspace().prepare(is_dependency_file)

    assert prepared.overlaid == ("Directory.Packages.props", "src/App/App.csproj")
    assert not (repo.tree_dir / "Directory.Packages.props").exists()
    assert (repo.tree_dir / "src/App/App.csproj").is_file()


def test_a_dependency_file_that_is_a_symlink_is_not_followed(repo: Repo) -> None:
    secret = repo.root.parent / "secret.txt"
    secret.write_text("outside\n", encoding="utf-8")
    (repo.root / "Directory.Packages.props").unlink()
    try:
        (repo.root / "Directory.Packages.props").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available here")

    prepared = repo.workspace().prepare(is_dependency_file)

    assert prepared.ignored == ("Directory.Packages.props",)
    assert (repo.tree_dir / "Directory.Packages.props").read_text(encoding="utf-8") == (
        "<Project>1.0</Project>\n"
    )


def test_a_tree_directory_that_exists_is_refused(repo: Repo) -> None:
    repo.tree_dir.mkdir()

    with pytest.raises(WorkspaceError, match="already exists"):
        repo.workspace().prepare(is_dependency_file)


def test_unrelated_histories_are_an_error(repo: Repo) -> None:
    git(repo.root, "checkout", "-q", "--orphan", "elsewhere")
    commit(repo.root, "root", {"x.txt": "x\n"})

    with pytest.raises(WorkspaceError, match="git merge-base failed"):
        repo.workspace().prepare(is_dependency_file)


def test_git_that_cannot_run_is_an_error(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("no git here")

    monkeypatch.setattr("lktec_bot.adapters.git_workspace.subprocess.run", broken)

    with pytest.raises(WorkspaceError, match="git merge-base could not run: no git here"):
        repo.workspace().prepare(is_dependency_file)


# -- what a command changed -------------------------------------------------------------------


def test_changes_see_new_changed_and_removed_files_but_not_ignored_ones(repo: Repo) -> None:
    workspace = repo.workspace()
    workspace.prepare(is_dependency_file)
    tree = repo.tree_dir
    before = workspace.changes()

    write(
        tree, {NOTICES: "new notices\n", "tools/extra.sh": "x\n", "obj/project.assets.json": "{}"}
    )
    (tree / "tools/generate.py").unlink()
    after = workspace.changes()

    assert before == {}
    assert set(after) == {NOTICES, "tools/extra.sh", "tools/generate.py"}
    assert after["tools/generate.py"] == "absent"
    assert len(after[NOTICES]) == 64


def test_changes_tell_a_rewrite_of_an_overlaid_file_from_leaving_it_alone(repo: Repo) -> None:
    commit(repo.root, "bump", {"Directory.Packages.props": "<Project>2.0</Project>\n"})
    workspace = repo.workspace()
    workspace.prepare(is_dependency_file)
    before = workspace.changes()

    assert set(before) == {"Directory.Packages.props"}
    assert workspace.changes() == before
    write(repo.tree_dir, {"Directory.Packages.props": "<Project>3.0</Project>\n"})
    assert workspace.changes() != before


def test_a_symlink_in_the_tree_is_fingerprinted_as_a_link(repo: Repo) -> None:
    workspace = repo.workspace()
    workspace.prepare(is_dependency_file)
    try:
        (repo.tree_dir / "link.md").symlink_to(repo.tree_dir / NOTICES)
    except OSError:
        pytest.skip("symlinks are not available here")

    assert workspace.changes()["link.md"].startswith("link:")
    assert workspace.stat("link.md") == FileStat(regular=False)


def test_stat_read_and_head(repo: Repo) -> None:
    workspace = repo.workspace()
    workspace.prepare(is_dependency_file)
    write(repo.tree_dir, {NOTICES: "new notices\n"})

    assert workspace.stat(NOTICES) == FileStat(regular=True, size=12)
    assert workspace.stat("missing.md") is None
    assert workspace.stat("tools") == FileStat(regular=False)
    assert workspace.read(NOTICES) == b"new notices\n"
    assert workspace.head(NOTICES) == b"old notices\n"
    assert workspace.head("missing.md") is None
    assert workspace.head("tools") is None


def test_an_untracked_directory_in_the_tree_is_fingerprinted(repo: Repo) -> None:
    workspace = repo.workspace()
    workspace.prepare(is_dependency_file)
    nested = repo.tree_dir / "vendor"
    nested.mkdir()
    git(nested, "init", "-q")

    assert workspace.changes()["vendor/"] == "dir"


# -- the handover directory -------------------------------------------------------------------


def test_derived_files_are_added_next_to_the_lock_files_already_handed_over(
    tmp_path: Path,
) -> None:
    handover = tmp_path / "handover"
    write(handover, {"manifest.txt": "uv.lock\n", "files/uv.lock": "lock\n"})

    DirectoryHandover(handover).add({NOTICES: b"notices\n", "apps/mobile/n.json": b"{}\n"})
    DirectoryHandover(handover).add({NOTICES: b"notices again\n"})

    assert read_manifest(handover) == ["uv.lock", NOTICES, "apps/mobile/n.json"]
    assert local_file(handover, NOTICES).read_bytes() == b"notices again\n"
    assert local_file(handover, "apps/mobile/n.json").read_bytes() == b"{}\n"
    assert local_file(handover, "uv.lock").read_bytes() == b"lock\n"


def test_a_handover_directory_is_created_when_there_were_no_lock_files(tmp_path: Path) -> None:
    handover = tmp_path / "handover"

    DirectoryHandover(handover).add({NOTICES: b"notices\n"})

    assert read_manifest(handover) == [NOTICES]
    assert sorted(path.name for path in handover.iterdir()) == ["files", "manifest.txt"]


# -- the command ------------------------------------------------------------------------------


class Generator:
    """Stands in for the shell: writes what the test tells it to into the tree it is run in."""

    files: Mapping[str, str] = {NOTICES: "new notices\n"}
    result = CommandResult(0)
    ran: list[tuple[str, Path, dict[str, str]]] = []  # noqa: RUF012 - reset by the fixture

    def run(
        self, command: str, cwd: Path, timeout_seconds: int, env: Mapping[str, str]
    ) -> CommandResult:
        type(self).ran.append((command, cwd, dict(env)))
        write(cwd, self.files)
        return self.result


@pytest.fixture
def generator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> type[Generator]:
    monkeypatch.setattr("lktec_bot.cli.ShellRunner", Generator)
    monkeypatch.setattr(Generator, "files", {NOTICES: "new notices\n"})
    monkeypatch.setattr(Generator, "result", CommandResult(0))
    monkeypatch.setattr(Generator, "ran", [])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.setenv("LKTEC_BOT_PRIVATE_KEY", "must never reach a generator")
    return Generator


def derive(repo: Repo, tmp_path: Path, ref: str = "trunk") -> tuple[int, str, dict[str, str]]:
    result = CliRunner().invoke(
        app,
        [
            "lockfiles",
            "derive",
            "--workspace",
            str(repo.root),
            "--trusted-ref",
            ref,
            "--tree-dir",
            str(repo.tree_dir),
            "--changed-dir",
            str(tmp_path / "handover"),
        ],
    )
    output = tmp_path / "output.txt"
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    return result.exit_code, result.output, dict(line.split("=", 1) for line in lines)


def test_derive_hands_over_the_generated_file(
    repo: Repo, generator: type[Generator], tmp_path: Path
) -> None:
    commit(repo.root, "bump", {"Directory.Packages.props": "<Project>2.0</Project>\n"})

    code, output, outputs = derive(repo, tmp_path)

    assert code == 0, output
    assert outputs["derived"] == "true"
    assert outputs["decision"] == "generated"
    assert "generated: handed over THIRD-PARTY-NOTICES.md" in output
    assert read_manifest(tmp_path / "handover") == [NOTICES]
    assert local_file(tmp_path / "handover", NOTICES).read_bytes() == b"new notices\n"
    command, cwd, env = generator.ran[0]
    assert (command, cwd) == ("make notices", repo.tree_dir)
    assert "LKTEC_BOT_PRIVATE_KEY" not in env
    assert "GITHUB_OUTPUT" not in env
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("**GENERATED** `lockfiles derive`")
    # The pull request's checkout itself is left alone.
    assert (repo.root / NOTICES).read_text(encoding="utf-8") == "old notices\n"


def test_derive_does_not_use_a_generator_script_the_pull_request_changed(
    repo: Repo, generator: type[Generator], tmp_path: Path
) -> None:
    commit(repo.root, "hostile", {"tools/generate.py": "changed by the pull request\n"})

    code, output, _ = derive(repo, tmp_path)

    assert code == 0, output
    _, cwd, _ = generator.ran[0]
    assert (cwd / "tools/generate.py").read_text(encoding="utf-8") == "trusted generator\n"
    assert "1 file(s) the pull request changes are not dependency manifests" in output


def test_derive_reports_a_failing_generator_and_still_exits_0(
    repo: Repo, generator: type[Generator], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Generator, "result", CommandResult(2, output="::error::boom\r\nline 2\n"))

    code, output, outputs = derive(repo, tmp_path)

    assert code == 0
    assert outputs["derived"] == "false"
    assert outputs["decision"] == "failed"
    assert "::warning title=lktec-bot derived files::generator 'notices' failed" in output
    # What the generator printed is shown, but never as a workflow command of its own.
    assert ": :error::boom" in output
    assert "\n::error::" not in output
    assert not (tmp_path / "handover").exists()


def test_derive_refuses_a_generator_that_writes_another_file(
    repo: Repo, generator: type[Generator], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Generator, "files", {NOTICES: "new\n", ".github/workflows/ci.yml": "evil\n"}
    )

    code, output, outputs = derive(repo, tmp_path)

    assert code == 0
    assert outputs["derived"] == "false"
    assert "wrote outside its declared paths: .github/workflows/ci.yml" in output
    assert not (tmp_path / "handover").exists()


def test_derive_is_idempotent_once_the_head_carries_the_generated_file(
    repo: Repo, generator: type[Generator], tmp_path: Path
) -> None:
    # The second run: the bot's own commit is on the branch, the generator produces the same.
    commit(repo.root, "bot commit", {NOTICES: "new notices\n"})

    code, _, outputs = derive(repo, tmp_path)

    assert code == 0
    assert outputs == {
        "derived": "false",
        "decision": "unchanged",
        "reason": "the derived files already match the pull request head",
    }
    assert not (tmp_path / "handover").exists()


def test_derive_with_a_bad_reference_is_a_usage_error(
    repo: Repo, generator: type[Generator], tmp_path: Path
) -> None:
    code, output, _ = derive(repo, tmp_path, "--evil")

    assert code == 2
    assert "not a plain branch reference" in output
    assert generator.ran == []
