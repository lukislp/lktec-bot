# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Where the App's identity comes from. Only the core ever touches the private key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AppCredentials:
    app_id: str
    private_key_pem: str

    def __repr__(self) -> str:  # never leak the key through a traceback or log
        return f"AppCredentials(app_id={self.app_id!r}, private_key_pem='<redacted>')"


class KeySource(Protocol):
    def load(self) -> AppCredentials: ...
