# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.text import escape_markdown, neutralise_commands, sanitize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("two\nlines", "two lines"),
        ("crlf\r\nand\rcr", "crlf and cr"),
        ("::set-env name=A::b", ": :set-env name=A: :b"),
        ("a\n::warning::x", "a : :warning: :x"),
        ("Foo::Bar", "Foo: :Bar"),
        ("", ""),
    ],
)
def test_sanitize_makes_one_harmless_line(text: str, expected: str) -> None:
    assert sanitize(text) == expected
    assert "\n" not in sanitize(text)
    assert "::" not in sanitize(text)


def test_neutralise_commands_only_touches_line_starts() -> None:
    assert neutralise_commands("::a::b\nx ::c\n::d") == ": :a::b\nx ::c\n: :d"
    assert neutralise_commands("fine\n") == "fine\n"


def test_escape_markdown_escapes_structure_and_collapses_lines() -> None:
    assert escape_markdown("`code` *b* _i_ [l](u) <b> | ~s~ # \\\n::x") == (
        "\\`code\\` \\*b\\* \\_i\\_ \\[l\\](u) \\<b\\> \\| \\~s\\~ \\# \\\\ : :x"
    )
    assert escape_markdown("module 'automerge' is not declared in .github/lktec-bot.yml") == (
        "module 'automerge' is not declared in .github/lktec-bot.yml"
    )
