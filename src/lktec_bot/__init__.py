# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""lktec-bot: a GitHub App identity plus least-privilege verbs, opt-in per repository."""

try:
    from lktec_bot._version import __version__
except ImportError:  # pragma: no cover - only hit on a source tree that was never built
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
