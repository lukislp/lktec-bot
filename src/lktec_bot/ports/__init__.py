# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Ports: the protocols the application layer talks to. Adapters implement them."""

from lktec_bot.ports.github_api import GitHubApi, GitHubApiFactory
from lktec_bot.ports.key_source import AppCredentials, KeySource
from lktec_bot.ports.token_provider import Token, TokenError, TokenProvider

__all__ = [
    "AppCredentials",
    "GitHubApi",
    "GitHubApiFactory",
    "KeySource",
    "Token",
    "TokenError",
    "TokenProvider",
]
