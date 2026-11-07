# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``release`` module: cut a tag and a GitHub release from Conventional Commits.

``options`` and ``commits`` (parsing, release rules) and ``notes`` (rendering) are pure; ``options``
is imported by the module registry. ``service`` drives the gate and is imported by the command
line only, which keeps the import graph acyclic.
"""
