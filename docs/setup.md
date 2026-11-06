# Set up lktec-bot for your own repositories

This is the path from zero: no App, no policy, no workflows yet. At the end the bot runs under
**your own** GitHub App on the repositories you choose. The identity rule: every user registers
their own App. Nothing in this repository carries the maintainer's App, id or key, and a
workflow that you call from `lukislp/lktec-bot` only ever receives the credentials **you** hand
it as secrets.

## 1. Decide where the workflows come from

The reusable workflows and composite actions live in `lukislp/lktec-bot`. Two ways to use them:

- **Straight from `lukislp/lktec-bot`** (the default). Callers reference
  `lukislp/lktec-bot/.github/workflows/<module>.yml@<sha>`. Pin the **commit of a release
  tag**, never a branch: take the newest tag from the
  [releases page](https://github.com/lukislp/lktec-bot/releases), resolve it to its commit
  (`git ls-remote https://github.com/lukislp/lktec-bot 'refs/tags/vX.Y.Z^{}'`; for a lightweight
  tag drop the `^{}`), and write the tag next to the sha as a comment:
  `uses: lukislp/lktec-bot/.github/workflows/automerge.yml@<40-hex-sha> # vX.Y.Z`. Let
  Dependabot's `github-actions` ecosystem move the pin.
- **A fork.** Fork the repository, reference `<you>/lktec-bot/...@<sha>` in your callers and pass
  `bot-repository: <you>/lktec-bot` to every reusable workflow. The input defaults to
  `lukislp/lktec-bot`. A reusable workflow cannot read its own repository from the `github`
  context, so it finds the commit it was called at in the run's list of referenced workflows
  and installs the bot from there, trusting only an entry of the repository named in
  `bot-repository`. Without the input a fork's workflow would find no trusted entry and fail
  instead of running unknown code.

## 2. Register your App from the manifest

Follow [register-your-app.md](register-your-app.md): post [`manifest.json`](../manifest.json)
to GitHub (the helper page [`register-app.html`](register-app.html) does it), exchange the
returned code for the App id and private key, and keep only those two. The manifest asks for
exactly `contents: write`, `pull_requests: write` and `metadata: read`, no events and no
webhook.

## 3. Install the App

On the App's settings page choose *Install App*, pick your account and **Only select
repositories**. Tick the repositories that will use the bot. A repository that the App is not
installed on gets nothing, and so does one without a policy file (step 5).

## 4. Set the two Actions secrets

In each repository (or as organisation secrets shared with selected repositories):

```sh
gh secret set LKTEC_BOT_APP_ID --repo owner/name --body <app id>
gh secret set LKTEC_BOT_PRIVATE_KEY --repo owner/name < bot.pem
```

Both are ordinary Actions secrets, not variables: the reusable workflows declare them as
`secrets: app-id` and `secrets: private-key`. Dependabot-triggered runs read **Dependabot**
secrets, not Actions secrets, unless the caller uses `pull_request_target` (the automerge and
lockfiles templates do, on purpose; see the module docs).

## 5. Write the policy file

Create `.github/lktec-bot.yml` on the default branch, starting from
[`templates/lktec-bot.yml`](../templates/lktec-bot.yml). Keep only the modules you use and set
`enabled: true` there. The bot reads the policy from the **default branch**, never from a pull
request, and an unknown module, verb or option makes the whole file invalid: every verb is then
denied. Check it locally before you merge:

```sh
uv run lktec-bot policy validate .github/lktec-bot.yml
```

A minimal first file enables the harmless `ping` module:

```yaml
version: 1
modules:
  ping:
    enabled: true
```

## 6. Add the callers, per module

Copy the template for each module into `.github/workflows/`, replace `<sha>` with the commit
from step 1 (and the trailing comment with the tag), and read the module's page for its options.

| Module      | Caller template                                                         | Module page                                  |
| ----------- | ----------------------------------------------------------------------- | -------------------------------------------- |
| `automerge` | [`dependabot-auto-merge.yml`](../templates/dependabot-auto-merge.yml)   | [automerge.md](modules/automerge.md)         |
| `lockfiles` | [`dependabot-lockfiles.yml`](../templates/dependabot-lockfiles.yml)     | [lockfiles.md](modules/lockfiles.md)         |
| `release`   | [`release.yml`](../templates/release.yml)                               | [release.md](modules/release.md)             |
| `sync`      | [`deploy-bump.yml`](../templates/deploy-bump.yml) (a job in the release chain) | [sync.md](modules/sync.md)            |

Every caller needs

```yaml
permissions:
  contents: read
  actions: read # the reusable workflow reads which commit of itself the run uses
```

at the workflow level (and on a calling job when the job sets its own `permissions`). Without
`actions: read` the workflow cannot tell which commit of itself it runs and stops, by design,
instead of falling back to a branch.

## 7. The first run

1. Merge the policy file (step 5) **before** any caller.
2. Optionally prove the whole path first: a caller job that runs the `ping` action (see
   [actions/README.md](../actions/README.md)) ends with `OK ping on owner/name (ping): pong ...`
   in the job summary, or with the reason for a denial (exit code 3: policy; 1: error).
3. Merge a caller. `automerge` and `lockfiles` wake up on the next Dependabot pull request (or
   an hourly sweep); `release` on the next push to the default branch that carries a `feat` or
   `fix` commit; `workflow_dispatch` runs the ones that have it right away.
4. A result of `denied` in a job summary is not a failure of the bot: it names what the policy
   or the gate refused. `error` is a failure; the message says where.

## Pitfalls we learned

- **Rulesets must not require code-owner review.** The bot's approval is a normal review by a
  different identity than the pull request author, which satisfies *Require approvals*. It never
  counts as a code owner review: the App is not (and cannot be usefully) listed in `CODEOWNERS`,
  so a rule *Require review from Code Owners* would block every Dependabot pull request forever.
- **A policy block needs a caller that knows the module.** When a repository adds a block for a
  module to `.github/lktec-bot.yml`, **every** caller of lktec-bot in that repository must
  already be pinned to a release that knows the module. A caller pinned to an older release
  validates the file with its older schema, finds an unknown module, treats the file as invalid
  and denies every verb, including the module that worked yesterday. Bump all pins first (or in
  the same pull request that adds the block, with the pins merged before the policy takes
  effect on the default branch).
- **`sync` needs the App as a bypass actor.** The commit lands on the protected branch without a
  pull request, so the branch ruleset must list the App under *Bypass list* (actor type
  `Integration`, mode `always`). Only the repository owner can change rulesets; the bot never
  touches them. It is acceptable because GitHub signs the commit, the token is narrowed to one
  repository and `contents: write`, the verb refuses `.github/workflows/`, and the module
  refuses any file outside the policy's `paths`, which is read from the default branch. Details
  and the comparison with a deploy key: [sync.md](modules/sync.md#why-the-bypass-is-acceptable).
  Modules that use pull requests (`automerge`, `lockfiles`, `release` in mode `tag`) need no
  bypass.
- **Dependabot cooldown and the bot's own bumps.** If `.github/dependabot.yml` sets a `cooldown`
  for the `github-actions` ecosystem, add the repository you pin the workflows from
  (`lukislp/lktec-bot`, or your fork) to its `exclude` list, so a new bot release is proposed
  at once instead of after the cooldown (security updates are not delayed by a cooldown, normal
  version updates are):

  ```yaml
  - package-ecosystem: github-actions
    directory: /
    cooldown:
      default-days: 7
      exclude:
        - lukislp/lktec-bot
  ```
- **`actions: read` in every caller** (step 6), and on a calling job that sets its own
  `permissions`.
- **Rate limit.** The primary REST limit is 5,000 requests per hour per installation, plus 50
  per repository above 20 (for non-Enterprise installations; Enterprise Cloud organisations
  get 15,000). It is shared by all repositories the App is installed on, not per repository. A
  bot run costs a handful of requests; the hourly `automerge` sweep scales with the number of
  open Dependabot pull requests. The adapter waits out short limits and reports
  `rate limit remaining: N` in the summary; see [architecture.md](architecture.md).
- **The bot never merges and never touches `.github/workflows/`.** Auto-merge lets GitHub merge
  when the required checks are green; a Dependabot pull request that changes workflow files is
  armed or left to a person by the `workflow-files` option of `automerge`.

## Rotating or removing the bot

See "Rotating or revoking" in [register-your-app.md](register-your-app.md): rotate the key
without downtime, set `enabled: false` in one repository's policy, or delete the key to stop the
bot everywhere.
