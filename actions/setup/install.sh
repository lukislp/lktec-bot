#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

# Install lktec-bot from the repository that holds this action.
#
# Every dependency, the build backend included, is installed with its hash from an export of
# uv.lock made right here, so nothing unpinned is fetched at run time and no generated
# requirements file has to be committed: uv verifies the hashes, and the package itself is
# built without isolation from the already installed backend.
set -euo pipefail

root="$(cd "$ACTION_PATH/../.." && pwd)"
requirements="$(mktemp)"
trap 'rm -f "$requirements"' EXIT
python="$(command -v python)"

uv export --quiet --frozen --no-dev --group build --no-emit-project --hashes \
  --project "$root" -o "$requirements"
uv pip install --quiet --python "$python" --require-hashes --no-deps -r "$requirements"

if [ ! -d "$root/.git" ]; then
  # Used as an action from another repository: GitHub downloads the action without git
  # metadata, so hatch-vcs cannot derive the version. Derive a PEP 440 version from the ref
  # the caller pinned (a tag like v1.2.3, or a commit SHA), never from anything else.
  ref="${ACTION_REF:-}"
  if [[ "${ref#v}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    version="${ref#v}"
  else
    local_segment="$(printf '%s' "${ref:-unknown}" | tr -c 'A-Za-z0-9.' '.' | cut -c1-40)"
    version="0.0.0+${local_segment}"
  fi
  export SETUPTOOLS_SCM_PRETEND_VERSION="$version"
fi

uv pip install --quiet --python "$python" --no-build-isolation --no-deps "$root"
echo "installed lktec-bot $(lktec-bot version)"
