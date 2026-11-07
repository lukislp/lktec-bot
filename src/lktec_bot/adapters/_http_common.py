# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Constants and headers shared by the HTTP adapters, so that neither imports the other."""

from __future__ import annotations

from lktec_bot import __version__

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = f"lktec-bot/{__version__}"
ACCEPT = "application/vnd.github+json"


def github_headers(authorization: str) -> dict[str, str]:
    """The header set every GitHub call carries; ``authorization`` is the full header value."""
    return {
        "Authorization": authorization,
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
