# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Key sources: environment variables or a file on disk. Nothing lktec-specific is assumed."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from lktec_bot.ports.key_source import AppCredentials

APP_ID_VAR = "LKTEC_BOT_APP_ID"
PRIVATE_KEY_VAR = "LKTEC_BOT_PRIVATE_KEY"
PRIVATE_KEY_FILE_VAR = "LKTEC_BOT_PRIVATE_KEY_FILE"


class KeySourceError(RuntimeError):
    """The App credentials are missing or unusable."""


class EnvKeySource:
    """Reads the App id and the PEM key from the environment.

    The key may be given inline (``LKTEC_BOT_PRIVATE_KEY``, newlines or literal ``\\n``) or as a
    path (``LKTEC_BOT_PRIVATE_KEY_FILE``). The inline variable wins when both are set.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        app_id_var: str = APP_ID_VAR,
        private_key_var: str = PRIVATE_KEY_VAR,
        private_key_file_var: str = PRIVATE_KEY_FILE_VAR,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._app_id_var = app_id_var
        self._private_key_var = private_key_var
        self._private_key_file_var = private_key_file_var

    def load(self) -> AppCredentials:
        app_id = self._env.get(self._app_id_var, "").strip()
        if not app_id:
            raise KeySourceError(f"{self._app_id_var} is not set")
        inline = self._env.get(self._private_key_var, "")
        if inline.strip():
            return AppCredentials(app_id, _normalise_pem(inline))
        path = self._env.get(self._private_key_file_var, "").strip()
        if path:
            return FileKeySource(app_id, Path(path)).load()
        raise KeySourceError(
            f"neither {self._private_key_var} nor {self._private_key_file_var} is set"
        )


class FileKeySource:
    """Reads the PEM key from a file; the App id is passed in."""

    def __init__(self, app_id: str, path: Path) -> None:
        self._app_id = app_id
        self._path = path

    def load(self) -> AppCredentials:
        try:
            pem = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KeySourceError(f"cannot read the private key at {self._path}: {exc}") from exc
        return AppCredentials(self._app_id, _normalise_pem(pem))


def _normalise_pem(pem: str) -> str:
    text = pem.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if "-----BEGIN" not in text:
        raise KeySourceError("the private key is not PEM encoded")
    return text + "\n"
