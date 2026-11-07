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
  open-issue)
    case "${DECISION:-}" in
      opened | updated | reopened | unchanged) ;;
      *)
        echo "::error::unexpected open-issue decision: ${DECISION:-}"
        exit 1
        ;;
    esac
    [ -n "${NUMBER:-}" ] || { echo "::error::the action reported no issue number"; exit 1; }
    # Reading one issue is consistent right after a write; the list is not (a freshly opened
    # or reopened issue takes a few seconds to show up in it). So the reported issue is checked
    # directly, and the list only guards against a second marked issue accumulating, with a
    # short retry until the fresh one has appeared.
    issue="repos/${GITHUB_REPOSITORY}/issues/${NUMBER}"
    state="$(gh api "$issue" --jq .state)"
    type="$(gh api "$issue" --jq .user.type)"
    labelled="$(gh api "$issue" --jq '[.labels[].name] | index("e2e-check") != null')"
    marked="$(gh api "$issue" --jq '(.body // "") | contains("<!-- lktec-bot:e2e -->")')"
    if [ "$state" != open ] || [ "$type" != Bot ] || [ "$labelled" != true ] || [ "$marked" != true ]; then
      echo "::error::issue #$NUMBER is not the open, labelled, marked bot issue (state=$state type=$type labelled=$labelled marked=$marked)"
      exit 1
    fi
    count=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      all="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues?state=all&labels=e2e-check&per_page=100" \
        --jq '.[] | select(.pull_request == null and .user.type == "Bot" and ((.body // "") | contains("<!-- lktec-bot:e2e -->"))) | .number')"
      count="$(printf '%s' "$all" | grep -c . || true)"
      [ "$count" -ge 1 ] && break
      sleep 3
    done
    [ "$count" = 1 ] || { echo "::error::expected exactly one marked bot issue, found $count: $all"; exit 1; }
    [ "$all" = "$NUMBER" ] || { echo "::error::the marked issue is #$all, the action reported #$NUMBER"; exit 1; }
    echo "one marked bot issue, open: ${URL:-}"
    ;;
  check)
    # created on the first run for a commit, updated when the run differs, unchanged on a
    # re-run of the same commit.
    case "${DECISION:-}" in
      created | updated | unchanged) ;;
      *) echo "::error::unexpected check decision: ${DECISION:-}"; exit 1 ;;
    esac
    # Exactly one check run of that name on the commit, and it is the one the verb reported.
    # The name is the bot's alone: no other App or workflow job publishes it.
    runs="$(gh api "repos/${GITHUB_REPOSITORY}/commits/${GITHUB_SHA}/check-runs?check_name=lktec-bot%20e2e" \
      --jq '[.check_runs[] | {url: .html_url, app: .app.slug}]')"
    count="$(jq 'length' <<<"$runs")"
    [ "$count" = 1 ] || { echo "::error::expected one check run, found $count: $runs"; exit 1; }
    [ "$(jq -r '.[0].url' <<<"$runs")" = "$URL" ] || {
      echo "::error::the check run on the commit is not the one the verb reported ($URL): $runs"
      exit 1
    }
    echo "one check run by $(jq -r '.[0].app' <<<"$runs"): $URL"
    ;;
esac
