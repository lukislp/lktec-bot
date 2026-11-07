#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

# Assertions of the e2e workflow. $1 is the verb; RESULT, MESSAGE and URL are the action's
# outputs, GH_TOKEN the workflow token (read-only, used for the checks below only).
#
# Bootstrap exception: the gate reads the policy from the default branch, so until
# .github/lktec-bot.yml has landed on main every verb is denied as "missing". That exact
# denial is accepted with a warning and nothing else is; once the file is on main the
# assertion is strict.
set -euo pipefail

verb="$1"
echo "$verb: result=$RESULT message=$MESSAGE url=${URL:-}"

if [ "$RESULT" = denied ] && [[ "$MESSAGE" == *"$POLICY_PATH is missing"* ]]; then
  if gh api "repos/${GITHUB_REPOSITORY}/contents/${POLICY_PATH}" >/dev/null 2>&1; then
    echo "::error::$verb denied as missing although $POLICY_PATH is on the default branch"
    exit 1
  fi
  echo "::warning::$verb denied: $POLICY_PATH is not on the default branch yet (bootstrap)"
  exit 0
fi

[ "$RESULT" = ok ] || { echo "::error::$verb did not run: $RESULT: $MESSAGE"; exit 1; }

case "$verb" in
  ping)
    [[ "$MESSAGE" == "pong from ${GITHUB_REPOSITORY}" ]] || {
      echo "::error::unexpected ping message: $MESSAGE"
      exit 1
    }
    ;;
  comment)
    [[ "${URL:-}" == https://github.com/*#issuecomment-* ]] || {
      echo "::error::comment reported no comment URL: ${URL:-}"
      exit 1
    }
    # Exactly one bot comment on the pull request carries the marker, however often the
    # workflow ran. The author association of the bot's comments is NONE; the login is the
    # App's slug plus [bot], so the user type is what identifies them here.
    count="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
      --jq '[.[] | select(.user.type == "Bot" and (.body | contains("<!-- lktec-bot:e2e -->")))] | length' \
      | paste -sd+ - | bc)"
    [ "$count" = 1 ] || { echo "::error::expected one marked bot comment, found $count"; exit 1; }
    echo "one marked bot comment: $URL"
    ;;
esac
