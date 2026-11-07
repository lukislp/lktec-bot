#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

# Run one verb from a composite action. The verb name is the only argument; everything else
# comes from INPUT_* variables the action sets from its inputs, and the App identity from
# LKTEC_BOT_APP_ID / LKTEC_BOT_PRIVATE_KEY, which never appear on a command line.
#
# Exit codes of lktec-bot: 0 ran, 1 error, 2 usage, 3 denied by policy. A denial is a normal
# result (output `result=denied`) and does not fail the step unless `fail-on-denied` is true.
set -uo pipefail

verb="$1"
if ! command -v lktec-bot >/dev/null 2>&1; then
  echo "::error::lktec-bot is not installed; run the setup action (actions/setup) first"
  exit 2
fi

args=(--repository "${INPUT_REPOSITORY:?repository input is required}")
args+=(--module "${INPUT_MODULE:?module input is required}")
case "$verb" in
  ping) ;;
  comment)
    args+=(--target "${INPUT_TARGET:-pr}" --number "${INPUT_NUMBER:?number input is required}")
    if [ -n "${INPUT_BODY_FILE:-}" ]; then
      args+=(--body-file "$INPUT_BODY_FILE")
    else
      args+=(--body "${INPUT_BODY:-}")
    fi
    [ -n "${INPUT_MARKER:-}" ] && args+=(--marker "$INPUT_MARKER")
    ;;
  approve)
    args+=(--number "${INPUT_NUMBER:?number input is required}")
    if [ -n "${INPUT_BODY_FILE:-}" ]; then
      args+=(--body-file "$INPUT_BODY_FILE")
    elif [ -n "${INPUT_BODY:-}" ]; then
      args+=(--body "$INPUT_BODY")
    fi
    ;;
  enable-automerge)
    args+=(--number "${INPUT_NUMBER:?number input is required}" --method "${INPUT_METHOD:-squash}")
    ;;
  update-branch)
    args+=(--number "${INPUT_NUMBER:?number input is required}")
    ;;
  release)
    args+=(
      --tag "${INPUT_TAG:?tag input is required}"
      --sha "${INPUT_SHA:?sha input is required}"
      --name "${INPUT_NAME:?name input is required}"
    )
    if [ -n "${INPUT_BODY_FILE:-}" ]; then
      args+=(--body-file "$INPUT_BODY_FILE")
    elif [ -n "${INPUT_BODY:-}" ]; then
      args+=(--body "$INPUT_BODY")
    fi
    if [ "${INPUT_LATEST:-true}" = "true" ]; then args+=(--latest); else args+=(--no-latest); fi
    if [ "${INPUT_PRERELEASE:-false}" = "true" ]; then args+=(--prerelease); fi
    ;;
  check)
    args+=(
      --sha "${INPUT_SHA:?sha input is required}"
      --name "${INPUT_NAME:?name input is required}"
      --title "${INPUT_TITLE:?title input is required}"
      --status "${INPUT_STATUS:-completed}"
    )
    if [ -n "${INPUT_SUMMARY_FILE:-}" ]; then
      args+=(--summary-file "$INPUT_SUMMARY_FILE")
    else
      args+=(--summary "${INPUT_SUMMARY:-}")
    fi
    if [ -n "${INPUT_TEXT_FILE:-}" ]; then
      args+=(--text-file "$INPUT_TEXT_FILE")
    elif [ -n "${INPUT_TEXT:-}" ]; then
      args+=(--text "$INPUT_TEXT")
    fi
    # The conclusion only belongs to a completed check run; the input defaults to success.
    if [ "${INPUT_STATUS:-completed}" = "completed" ]; then
      args+=(--conclusion "${INPUT_CONCLUSION:-success}")
    fi
    [ -n "${INPUT_DETAILS_URL:-}" ] && args+=(--details-url "$INPUT_DETAILS_URL")
    [ -n "${INPUT_EXTERNAL_ID:-}" ] && args+=(--external-id "$INPUT_EXTERNAL_ID")
    [ -n "${INPUT_ANNOTATIONS_FILE:-}" ] && args+=(--annotations-file "$INPUT_ANNOTATIONS_FILE")
    ;;
  open-pr)
    args+=(
      --head "${INPUT_HEAD:?head input is required}"
      --base "${INPUT_BASE:?base input is required}"
      --title "${INPUT_TITLE:?title input is required}"
    )
    if [ -n "${INPUT_BODY_FILE:-}" ]; then
      args+=(--body-file "$INPUT_BODY_FILE")
    elif [ -n "${INPUT_BODY:-}" ]; then
      args+=(--body "$INPUT_BODY")
    fi
    if [ "${INPUT_DRAFT:-false}" = "true" ]; then args+=(--draft); fi
    ;;
  open-issue)
    args+=(--title "${INPUT_TITLE:?title input is required}")
    if [ -n "${INPUT_BODY_FILE:-}" ]; then
      args+=(--body-file "$INPUT_BODY_FILE")
    else
      args+=(--body "${INPUT_BODY:-}")
    fi
    [ -n "${INPUT_MARKER:-}" ] && args+=(--marker "$INPUT_MARKER")
    IFS=',' read -r -a labels <<<"${INPUT_LABELS:-}"
    for label in "${labels[@]+"${labels[@]}"}"; do
      label="${label#"${label%%[![:space:]]*}"}"
      label="${label%"${label##*[![:space:]]}"}"
      [ -n "$label" ] && args+=(--label "$label")
    done
    if [ "${INPUT_REOPEN:-true}" = "true" ]; then args+=(--reopen); else args+=(--no-reopen); fi
    ;;
  *)
    echo "::error::unknown verb '$verb'"
    exit 2
    ;;
esac

lktec-bot "$verb" "${args[@]}"
code=$?
if [ "$code" -eq 3 ] && [ "${INPUT_FAIL_ON_DENIED:-false}" != "true" ]; then
  echo "::notice::$verb was denied by policy; the step succeeds because fail-on-denied is not set"
  exit 0
fi
exit "$code"
