# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Short-lived, narrowed tokens for one repository."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class TokenError(RuntimeError):
    """A provider could not resolve, mint or revoke a token.

    This is the one exception a provider is expected to raise. The gate turns it into an
    ``error`` result with its message; anything else a provider raises is treated as a bug and
    reported as such.
    """


class Token(Protocol):
    """An installation access token. ``revoke`` is idempotent and never raises after success."""

    @property
    def value(self) -> str: ...

    @property
    def permissions(self) -> Mapping[str, str] | None:
        """The permissions GitHub reports for the token, or ``None`` when the provider does not
        know them. The gate fails closed on ``None``: a token of unknown scope never runs a verb.
        """
        ...

    @property
    def repositories(self) -> frozenset[str] | None:
        """The repository names the token is scoped to, or ``None`` when unknown."""
        ...

    @property
    def actor(self) -> str | None:
        """The login the token acts as (``<app-slug>[bot]`` for an App), or ``None`` when the
        provider does not know it. Verbs that look for their own earlier work (``comment`` with
        a marker) need it; they fail closed without it.
        """
        ...

    def revoke(self) -> None: ...


class TokenProvider(Protocol):
    def token_for(self, owner: str, repo: str, permissions: Mapping[str, str]) -> Token:
        """Mint a token valid for exactly ``owner/repo`` with exactly ``permissions``."""
        ...

    def close(self) -> None:
        """Release the provider's connections. Idempotent."""
        ...
