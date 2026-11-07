# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_headers import has_header, main

HEADER = "# SPDX-FileCopyrightText: 2026 Lukas Koerber\n# SPDX-License-Identifier: Apache-2.0\n"


def test_header_present_and_absent(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(HEADER + "\nprint('x')\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("print('x')\n", encoding="utf-8")

    assert has_header(good.read_text(encoding="utf-8")) is True
    assert has_header(bad.read_text(encoding="utf-8")) is False


def test_main_reports_missing_headers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "mod.py"
    path.write_text("x = 1\n", encoding="utf-8")

    assert main([str(path)]) == 1
    captured = capsys.readouterr()
    assert f"{path.as_posix()}: missing SPDX copyright and license header" in captured.out


def test_fix_inserts_after_shebang_and_before_docstring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "run.py"
    script.write_text('#!/usr/bin/env python3\n"""Doc."""\nprint("x")\n', encoding="utf-8")

    assert main(["--fix", str(script)]) == 0

    assert script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env python3\n" + HEADER + '\n"""Doc."""\nprint("x")\n'
    )
    assert f"added header: {script.as_posix()}" in capsys.readouterr().out


def test_fix_then_clean_returns_zero(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("x = 1\n", encoding="utf-8")

    assert main(["--fix", str(path)]) == 0
    assert main([str(path)]) == 0
    assert has_header(path.read_text(encoding="utf-8")) is True


def test_version_py_is_skipped(tmp_path: Path) -> None:
    version = tmp_path / "_version.py"
    version.write_text("x = 1\n", encoding="utf-8")

    assert main([str(version)]) == 0
    assert version.read_text(encoding="utf-8") == "x = 1\n"
