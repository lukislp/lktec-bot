# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``automerge`` module: classify Dependabot pull requests, arm the routine ones and keep
open pull requests mergeable.

``options`` and ``classify`` are pure and imported by the module registry; ``service`` drives
the gate and is imported by the command line only, which keeps the import graph acyclic.
"""
