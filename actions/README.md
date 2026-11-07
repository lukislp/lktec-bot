# Composite actions

One action per verb plus a shared `setup`. Every verb action runs the verb through the gate:
it reads the target repository's `.github/lktec-bot.yml` from the default branch, mints a
token narrowed to the verb's permission, runs, revokes the token and writes the result to the
job summary. The App key never leaves the step's environment and is never on a command line.

Pin the actions by commit SHA like every other action (`uses: lukislp/lktec-bot/actions/<name>@<sha> # vX.Y.Z`).
Inside this repository they are used as `./actions/<name>` after a checkout (see
`.github/workflows/e2e.yml`).

## Common inputs and outputs

| Input            | Required | Default                    | Meaning                                                      |
| ---------------- | -------- | -------------------------- | ------------------------------------------------------------ |
| `app-id`         | yes      |                            | The App id (`vars.LKTEC_BOT_APP_ID` or a secret).            |
| `private-key`    | yes      |                            | The App's PEM key (`secrets.LKTEC_BOT_PRIVATE_KEY`).         |
| `module`         | no       | `automerge` (`ping` for ping) | Module asking for the verb; must be enabled in the policy. |
| `repository`     | no       | `${{ github.repository }}` | Target repository as `owner/name`.                           |
| `api-url`        | no       | empty                      | API base URL of a GitHub Enterprise Server.                  |
| `fail-on-denied` | no       | `false`                    | Fail the step on a policy denial instead of reporting it.    |

| Output    | Meaning                                                                 |
| --------- | ----------------------------------------------------------------------- |
| `result`  | `ok`, `denied` or `error`.                                              |
| `message` | One line saying what happened.                                          |
| `url`     | The comment, review or pull request URL when there is one, else empty.  |

A **denied** decision is a result, not a failure: the step succeeds with `result=denied` and
the reason in `message`, so a workflow can branch on it. Set `fail-on-denied: true` to fail
instead. An **error** (token, API, refused draft) always fails the step with `result=error`.
Exit codes of the underlying CLI: 0 ran, 1 error, 2 usage, 3 denied.

## `setup`

Installs the package from the action's own checkout with `uv`. Every dependency and the build
backend are installed with their hashes from an export of `uv.lock` made at run time
(`uv export --frozen --no-dev --group build --no-emit-project --hashes`, then
`uv pip install --require-hashes --no-deps`), so the lock file is the only source of truth and
no generated `requirements.txt` is committed: a Dependabot bump of `uv.lock` is complete on
its own. uv itself is installed by `astral-sh/setup-uv` (SHA-pinned, Astral mirror off).

Inputs: `python-version` (default `3.13`) and `uv-version` (default: the version this
repository tests with, an exact version).

A runner hardened with `step-security/harden-runner` in block mode needs these endpoints for the
setup step: `github.com`, `api.github.com`, `objects.githubusercontent.com`,
`release-assets.githubusercontent.com`, `raw.githubusercontent.com` (setup-uv reads its version
manifest there), `pypi.org` and `files.pythonhosted.org`.

```yaml
- uses: lukislp/lktec-bot/actions/setup@<sha> # vX.Y.Z
```

## `ping`

Proves the whole path without changing anything. Module default `ping`.

```yaml
- id: ping
  uses: lukislp/lktec-bot/actions/ping@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
- run: test "${{ steps.ping.outputs.result }}" = ok
```

## `comment`

Inputs: `number` (required), `target` (`pr`, default, or `issue`), `body` or `body-file`,
`marker`. With a marker the body gets an invisible `<!-- lktec-bot:<marker> -->` tag and later
runs update the bot's own comment that carries it instead of adding another; a comment by
anyone else is never edited. Permission: `pull_requests:write` for `pr`, `issues:write` for
`issue`.

```yaml
- id: comment
  uses: lukislp/lktec-bot/actions/comment@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    number: ${{ github.event.pull_request.number }}
    body-file: report.md
    marker: report
- run: echo "${{ steps.comment.outputs.url }}"
```

## `approve`

Inputs: `number` (required), optional `body` or `body-file`. Submits an `APPROVE` review;
refuses drafts and closed pull requests (`result=error`); does nothing when the bot already
approved the current head. Permission: `pull_requests:write`.

```yaml
- uses: lukislp/lktec-bot/actions/approve@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    number: ${{ github.event.pull_request.number }}
```

## `enable-automerge`

Inputs: `number` (required), `method` (`squash`, default, `merge` or `rebase`). Already
enabled with the same method is `ok`; with another method it is an error. The repository must
allow auto-merge (*Settings > General > Allow auto-merge*) and the pull request must not be
mergeable yet (otherwise GitHub answers "clean status" and the verb reports an error).
Permission: `contents:write` plus `pull_requests:write` (GitHub refuses the mutation with
`pull_requests:write` alone; verified live).

```yaml
- uses: lukislp/lktec-bot/actions/enable-automerge@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    number: ${{ github.event.pull_request.number }}
    method: squash
```

## `update-branch`

Inputs: `number` (required). Asks GitHub to merge the base branch into the pull request
branch; `message` says `accepted` or `already up to date`. Permission: `contents:write` plus
`pull_requests:write`.

```yaml
- uses: lukislp/lktec-bot/actions/update-branch@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    number: ${{ github.event.pull_request.number }}
```

## `release`

Inputs: `tag`, `sha` and `name` (required), `body` or `body-file`, `latest` (`true`, default) and
`prerelease` (`false`, default), `module` (default `release`). Publishes a GitHub release; when
the tag does not exist GitHub creates it at `sha`. A release that exists with its tag at `sha` is
`unchanged`; a tag at another commit is an error and is never moved. Outputs besides the common
ones: `decision` (`created`, `unchanged`, `denied`, `error`) and `tag`. Permission:
`contents:write`. The module that works out the version is the reusable workflow
[`release.yml`](../docs/modules/release.md), not an action.

```yaml
- id: release
  uses: lukislp/lktec-bot/actions/release@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    tag: v1.2.3
    sha: ${{ github.sha }}
    name: v1.2.3
    body-file: notes.md
- run: echo "${{ steps.release.outputs.url }}"
```

## `open-pr`

Inputs: `module` (required: no shipped module may run `open-pr` yet), `head`, `base` and `title`
(required), `body` or `body-file`, `draft` (`false`, default). Opens a pull request from `head`
into `base`, or changes the open one to the given title, body (only when given) and draft state,
where they differ. Outputs besides the common ones: `decision` (`opened`, `updated`, `unchanged`,
`denied`, `error`) and `number`. Permission: `pull_requests:write`.

```yaml
- id: pr
  uses: lukislp/lktec-bot/actions/open-pr@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    module: my-addon
    head: release/next
    base: main
    title: "chore: release"
    body-file: pr.md
```

## `open-issue`

Inputs: `module` (required: the `lockfiles`, `release` and `sync` modules may run it), `title`
and `body` or `body-file` (required), `marker`, `labels` (comma-separated, each must exist in
the repository), `reopen` (`true`, default). Opens an issue. With a `marker` the body gets an
invisible `<!-- lktec-bot:<marker> -->` tag and later runs update the issue the bot opened with
that tag instead of opening another: the title, the body and the labels it lacks are changed where
they differ (labels are never removed), nothing differs: no write, a closed issue is reopened
(`reopen: "false"` opens a new one instead). An issue by anyone else is never touched. Outputs
besides the common ones: `decision` (`opened`, `updated`, `reopened`, `unchanged`,
`denied`, `error`) and `number`. Permission: `issues:write`.

```yaml
- id: issue
  uses: lukislp/lktec-bot/actions/open-issue@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    module: release
    title: "deploy bump: image tag not found"
    body-file: problem.md
    marker: deploy-bump
    labels: ci, deploy
```

## Forks

A `pull_request` run from a fork has no secrets, so the verb actions cannot mint a token
there. Skip the job when `github.event.pull_request.head.repo.fork` is true, as the e2e
workflow does.
