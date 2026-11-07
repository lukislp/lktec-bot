# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Adapters: the implementations of the ports that talk to the real world."""

from lktec_bot.adapters._http_common import API_VERSION, DEFAULT_BASE_URL, USER_AGENT
from lktec_bot.adapters.app_token import AppTokenProvider, InstallationToken, build_app_jwt
from lktec_bot.adapters.github_http import GitHubHttpError, HttpGitHubApi
from lktec_bot.adapters.key_source import EnvKeySource, FileKeySource, KeySourceError

__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "USER_AGENT",
    "AppTokenProvider",
    "EnvKeySource",
    "FileKeySource",
    "GitHubHttpError",
    "HttpGitHubApi",
    "InstallationToken",
    "KeySourceError",
    "build_app_jwt",
]
