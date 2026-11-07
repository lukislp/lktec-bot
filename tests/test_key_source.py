# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from lktec_bot.adapters.key_source import EnvKeySource, FileKeySource, KeySourceError

PEM = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"


def test_env_inline_key() -> None:
    source = EnvKeySource({"LKTEC_BOT_APP_ID": " 42 ", "LKTEC_BOT_PRIVATE_KEY": PEM})

    credentials = source.load()

    assert credentials.app_id == "42"
    assert credentials.private_key_pem == PEM + "\n"


def test_env_inline_key_with_escaped_newlines() -> None:
    escaped = PEM.replace("\n", "\\n")
    source = EnvKeySource({"LKTEC_BOT_APP_ID": "42", "LKTEC_BOT_PRIVATE_KEY": escaped})

    assert source.load().private_key_pem == PEM + "\n"


def test_env_key_file(tmp_path: Path) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(PEM + "\n", encoding="utf-8")
    source = EnvKeySource({"LKTEC_BOT_APP_ID": "42", "LKTEC_BOT_PRIVATE_KEY_FILE": str(key_file)})

    assert source.load().private_key_pem == PEM + "\n"


def test_inline_key_wins_over_file(tmp_path: Path) -> None:
    source = EnvKeySource(
        {
            "LKTEC_BOT_APP_ID": "42",
            "LKTEC_BOT_PRIVATE_KEY": PEM,
            "LKTEC_BOT_PRIVATE_KEY_FILE": str(tmp_path / "missing.pem"),
        }
    )

    assert source.load().private_key_pem == PEM + "\n"


def test_custom_variable_names() -> None:
    source = EnvKeySource(
        {"MY_APP": "7", "MY_KEY": PEM},
        app_id_var="MY_APP",
        private_key_var="MY_KEY",
    )

    assert source.load().app_id == "7"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({}, "LKTEC_BOT_APP_ID is not set"),
        ({"LKTEC_BOT_APP_ID": "42"}, "neither LKTEC_BOT_PRIVATE_KEY nor"),
        ({"LKTEC_BOT_APP_ID": "42", "LKTEC_BOT_PRIVATE_KEY": "not a key"}, "not PEM"),
    ],
)
def test_env_errors(env: dict[str, str], fragment: str) -> None:
    with pytest.raises(KeySourceError, match=fragment):
        EnvKeySource(env).load()


def test_file_source_missing_file(tmp_path: Path) -> None:
    with pytest.raises(KeySourceError, match="cannot read"):
        FileKeySource("42", tmp_path / "nope.pem").load()


def test_env_source_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LKTEC_BOT_APP_ID", "99")
    monkeypatch.setenv("LKTEC_BOT_PRIVATE_KEY", PEM)

    assert EnvKeySource().load().app_id == "99"
