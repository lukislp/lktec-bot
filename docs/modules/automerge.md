# The `automerge` module

Dependabot updates that are routine get approved by the App and armed for auto-merge; GitHub
merges them once the required checks are green. Everything else is left to a person. This is
the behaviour of the shared `dependabot-auto-merge` workflow in `ci-workflows`, ported onto the
bot: same classification, but the credential is a narrowly scoped App token instead of a
personal access token, and nothing here ever merges or bypasses a rule.

It ships as

- two commands, `lktec-bot automerge arm` and `lktec-bot automerge sweep`
  ([`application/modules/automerge/`](../../src/lktec_bot/application/modules/automerge)),
- a reusable workflow, [`.github/workflows/automerge.yml`](../../.github/workflows/automerge.yml),
- a caller template, [`templates/dependabot-auto-merge.yml`](../../templates/dependabot-auto-merge.yml),
  and a policy snippet, [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml).

## What it does

### Classification

The pull request's title and body are read (`classify`, pure and tested against the cases the old
workflow documented). Each dependency update is one of

| Kind     | What it is                                                                         |
| -------- | ---------------------------------------------------------------------------------- |
| `patch`  | same major and minor, a newer patch (`0.13.0` to `0.13.1`, `4.1.1` to `4.1.7`)     |
| `minor`  | same major, a newer minor (`0.13.0` to `0.14.0`, also for `0.x`)                   |
| `major`  | a different major number                                                           |
| `digest` | a Docker digest refresh of an unchanged tag (`` bump python from `78387bc` to `2f17fc0` ``) |

Only the numeric core of a version counts (`3.13-slim` to `3.14-slim` is a minor; a `v` prefix or
a `-rc.1` suffix does not matter). A **group** pull request (`Bump the python group with 3
updates`, members listed as ``Updates `x` from A to B`` in the body) is as severe as its most
severe member. The release notes inside `<details>` blocks are the dependency maintainers' text
and are never read.

Whatever cannot be read with confidence is **manual**, never armed: a title without versions, a
SHA-to-SHA action bump (without backticks it is not a version), a group whose body lists fewer
updates than the title announces.

### Eligibility

A pull request is armed only when all of this holds, checked in this order:

1. it is open and not a draft;
2. its author is exactly `dependabot[bot]`;
3. none of its dependencies is on the `exclude` list (and, with an `exclude` list set, its
   dependencies could be read at all);
4. the update kind is known;
5. it is not a major, unless `majors: auto`;
6. with `workflow-files: manual`, it changes no file under `.github/workflows/`.

### `lktec-bot automerge arm --repository owner/name --number N`

Classifies the pull request and, when eligible, runs the verb `approve` (idempotent per head
commit) and then `enable-automerge` with the configured `method`. It never merges directly. It
writes `result` (`ok`, `denied`, `error`), `decision` (`armed`, `ready`, `skipped`, `denied`,
`error`), `reason` and `url` to `$GITHUB_OUTPUT` and a line to the job summary. A pull request
that is not eligible is `skipped` with the reason, exit 0.

GitHub refuses to enable auto-merge on a pull request that can be merged right now
(`Pull request is in clean status`, also `unstable` and `has_hooks`). The `arm` job runs within
seconds of Dependabot opening the pull request, sometimes before the required checks are
registered; with nothing pending GitHub counts the pull request as mergeable. So
`enable-automerge` retries the mutation up to six times, ten seconds apart, which is long enough
for the checks to show up (`--attempts`, 1 to 6; the sweep passes 1 because it sees the settled
state and would otherwise wait a minute per ready pull request every hour). When it is still refused the verb returns `ok` with
`decision=ready` and the pull request is **ready**: approved by the bot, mergeable now, and
waiting for a person to merge it (the bot has no merge verb). `arm` then reports the decision
`ready`, exit 0, so the `automerge / arm` check is green. A red check there would be a
non-required failure that keeps the pull request `UNSTABLE`, and every sweep would be refused
the same way.

### `lktec-bot automerge sweep --repository owner/name`

Looks at every open, non-draft pull request:

1. **Arm.** A Dependabot pull request without auto-merge is classified and armed exactly as
   `arm` does. This is what catches a pull request the `arm` job never saw (it runs once, at
   creation, under whatever pin the caller had then) or saw under older rules.
   If GitHub still refuses because the pull request can be merged right now, the row says
   `approved; already mergeable, needs a person to merge`, it counts under `ready` (not under
   `errors`) and the sweep leaves one hint through the verb `comment` (marker
   `automerge-ready-<head>`: once per head commit, updated in place if the state changed):
   "lktec-bot approved this pull request, but GitHub does not enable auto-merge on a pull
   request that can be merged right away (state: clean). Please merge it." The hint is posted
   only by the sweep, which sees the settled state, not by `arm`, which runs in the race
   window. A hint that cannot be posted is a warning. The approval is not repeated every hour:
   `approve` is a no-op while the bot's approval is on the current head. Each sweep does retry
   the enable step for such a pull request (about 50 seconds), because it may have been
   refused only for lack of registered checks.
2. **Keep up to date.** A pull request that has auto-merge enabled, or was opened by anyone but
   Dependabot, and is `BEHIND` its base gets `update-branch`. A Dependabot pull request left
   for a person (a major, an excluded dependency) is not rebuilt on every push: whoever merges
   it updates it. A pull request the sweep armed in this same run is **not** updated: its
   checks are not queued yet, the next tick looks at it.
3. **Unstick.** A Dependabot pull request that has auto-merge enabled and is `DIRTY`
   (conflicting) needs `@dependabot recreate`: Dependabot rebases its own branches only while
   they hold nothing but its commits, so after an `update-branch` it leaves a conflict alone.
   Dependabot takes that command only from people with push access; a GitHub App's comment is
   answered with "Sorry, only users with push access can use that command." So the sweep
   posts a hint through the verb `comment` that names the reason and asks a maintainer to
   comment `@dependabot recreate`; the sweep arms the rebuilt pull request again. The hint is
   an upsert with the marker `automerge-recreate-<head>`: once per head commit, and again when
   the head changed and the pull request still needs a rebuild. The same hint answers a Dependabot pull request (armed, or eligible to be armed) with a
   commit GitHub does not verify (`verification.verified == false`, for example `unsigned`):
   a ruleset that requires signed commits can never merge it, and neither `update-branch` nor an
   approval helps, but a recreated pull request carries a fresh, signed commit, so the hint
   asks for a recreate there too. The sweep reads
   the commits of such a pull request (one extra call, covered by `pull_requests: read`) and
   does nothing else to it in that run. A pull request left for a person, a conflicting one
   (already hinted above) and anyone else's pull request are never touched for this.
4. **Re-approve.** An eligible Dependabot pull request that already has auto-merge enabled gets
   `approve` again, which is a no-op while the bot's approval is on the current head. A head
   that moved (a rebase, `update-branch`) needs a review of its own when the ruleset dismisses
   stale approvals.

GitHub computes the merge state lazily, so `UNKNOWN` is polled (six tries, ten seconds apart).
The result is a summary table with one row per pull request; the exit code is 0, also when a
single step failed (the row says `ERROR ...` and `errors` is in the outputs). The summary line
and the outputs count `armed`, `ready`, `updated`, `recreated` and `errors`
(`armed X, ready N, re-approved ..., updated ..., recreate needed ..., errors ...`). The exit code is 3
when the policy denies the module and 1 when the pull requests cannot be read at all.

An **excluded** Dependabot pull request without auto-merge is left entirely alone. One a
person armed after testing is kept mergeable (update, recreate) but never approved by the bot:
the approval and the merge stay theirs.

## Options

Under `modules.automerge` in `.github/lktec-bot.yml`. Unknown keys are errors and deny every
verb, so a typo never changes behaviour.

| Option           | Values / default            | Meaning                                                              |
| ---------------- | --------------------------- | -------------------------------------------------------------------- |
| `majors`         | `manual` (default), `auto`  | `manual`: a major is never armed. `auto`: it is treated like any update. |
| `exclude`        | list of names, default `[]` | Exact, case-insensitive dependency names that are never approved or armed. A grouped pull request is skipped as a whole when any member is listed. |
| `workflow-files` | `auto` (default), `manual`  | `manual`: a pull request that changes any file under `.github/workflows/` is never armed. |
| `method`         | `squash` (default), `merge`, `rebase` | How GitHub merges once the checks are green. |

## Permissions

The workflow's own `GITHUB_TOKEN` has `contents: read` and `actions: read` and nothing else
(`actions: read` lets the run look up which commit of the reusable workflow it was called at,
to install the bot from exactly that commit; the caller grants both). Every call goes through
the gate with its own token, narrowed to one repository:

| Step                         | Token                                    |
| ---------------------------- | ---------------------------------------- |
| policy (every call)          | `contents: read`                         |
| reading pull requests, files | `pull_requests: read`                    |
| `approve`                    | `pull_requests: write`                   |
| `enable-automerge`           | `contents: write` + `pull_requests: write` |
| `update-branch`              | `contents: write` + `pull_requests: write` |
| `comment` (recreate, ready)  | `pull_requests: write`                   |

The `workflows` permission is never requested (see `docs/architecture.md`).

## Differences to the PAT workflow

- **The bot approves and arms; GitHub merges.** The old sweep also retried a direct merge with an
  admin token. Here there is no `merge` verb and no bypass actor: a pull request merges when the
  ruleset is satisfied the ordinary way (required checks green, the bot's approval counting
  where a review is required).
- **Identity.** Actions are the App's (`<app>[bot]`), visible as such in the audit log, not the
  owner's. Pushes and comments by the App do start workflows, unlike `GITHUB_TOKEN`.
- **No long-lived secret with write access.** The App key is the only secret; tokens live for
  one verb.
- **Workflow files.** Pull requests that touch `.github/workflows/` follow `workflow-files`
  (default `auto`, as before). The App can never edit workflow files itself.
- **Opt-in per repository.** No policy file on the default branch, or `enabled: false`: every
  call is denied and the workflow fails visibly.
- **Groups and `exclude`** are read from the pull request body the same way in both jobs; the
  old `arm` job read GitHub's `fetch-metadata`, the old sweep only the title.
- **Digest refreshes** are recognised by the title alone (both hashes in backticks), whatever
  the ecosystem, as the old sweep did.
- **Re-approval** in the sweep only for eligible pull requests; the old sweep approved every
  Dependabot pull request that had auto-merge.
- The old sweep commented `@dependabot recreate` every hour; Dependabot ignores that from a
  GitHub App, so this one leaves a hint for a maintainer, once per head commit.

## Setting up a repository

1. **Install the App** on the repository (see [register-your-app](../register-your-app.md)).
   Under *Settings > General*, allow auto-merge and the merge method you chose.
2. **Two secrets**, repository or organization level: `LKTEC_BOT_APP_ID` (the App id) and
   `LKTEC_BOT_PRIVATE_KEY` (the PEM). Store both as *Actions* secrets (the hourly and push runs
   read those) **and** as *Dependabot* secrets: a run that Dependabot itself triggers sees only
   Dependabot secrets, while a run triggered by the App's own `update-branch` sees the regular
   ones. A missing secret fails the run visibly; nothing falls back to another credential.
3. **The policy file** on the default branch: copy
   [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml) to `.github/lktec-bot.yml`, set
   the options. Check it with `lktec-bot policy validate .github/lktec-bot.yml`. Merge this to
   the default branch **before** the workflow runs: the bot reads the policy from there only.
4. **The caller workflow**: copy [`templates/dependabot-auto-merge.yml`](../../templates/dependabot-auto-merge.yml)
   to `.github/workflows/dependabot-auto-merge.yml` and replace `<sha>` with a commit of this
   repository. If the repository's default branch is not `main`, change the `push` trigger.
5. **Retire the PAT.** Once the first sweep and one Dependabot pull request went through,
   delete the `AUTOMERGE_TOKEN` secret and remove the old caller of `ci-workflows`'
   `dependabot-auto-merge.yml`.
6. **Ruleset.** Keep the bypass list as small as it can be; the bot does not need to be on it.
   If the ruleset requires an approving review, the bot's approval counts as one (it is a
   different identity from the pull request author). It does **not** count as a code owner
   review: the App is never listed in `CODEOWNERS`, so with *require review from code owners*
   enabled an armed pull request waits for a person. Turn that setting off (or have no
   `CODEOWNERS` file) in a repository where the bot's approval is meant to be the one review.
   For the OpenSSF Scorecard this costs nothing: the code owner review sits in branch-protection
   tier 4 next to *two* required reviewers, so on its own it never scores. Leave *require
   approval of the most recent reviewable push* off as well where the [lockfiles](lockfiles.md)
   module runs: the bot pushes a commit there and then approves, which that setting forbids.

## How the workflow installs the bot

A reusable workflow cannot use `./actions/setup` (that would be the caller's checkout) and
cannot read its own repository or commit from the `github` context. The run object lists the
workflows it references with their repository and commit; the job reads that
(`referenced_workflows`), checks out that repository at that commit into `.lktec-bot/` and runs
`./.lktec-bot/actions/setup`. So the pin in the caller (`automerge.yml@<sha>`) decides exactly
which code runs, and the job fails instead of falling back to a branch when it cannot tell. Only
an entry of the repository in the `bot-repository` input (default `lukislp/lktec-bot`, change it
only in a fork) is trusted, so a workflow of another repository is never mistaken for it. The
pull request's own code is never checked out: the job runs on `pull_request_target`, with
secrets.

## Dogfooding

This repository uses the workflow itself
([`.github/workflows/dependabot-auto-merge.yml`](../../.github/workflows/dependabot-auto-merge.yml)).
The `uses:` line pins a commit of `main`. Move it by hand when a change to the reusable workflow
or to the modules should take effect here: the bot is installed from the pinned commit, so a stale
pin fails on policy keys that commit does not know yet.
