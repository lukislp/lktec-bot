# The `sync` module

Some repositories commit generated files onto their protected default branch without a pull
request: 13 repositories run a `deploy-bump` job after a release that rewrites the image tag in
their Kubernetes manifests (`k8s/` or `deploy/`, excluding `/flux/`) and pushes
`chore(deploy): image <version> [skip ci]`, and the `status` repository commits generated
`data/` every few minutes. Both push over an SSH deploy key that bypasses the branch ruleset and
sign the commit with a per-repository SSH key (`DEPLOY_SIGNING_KEY`), through the actions
`deploy-key-push` and `signed-bot-commit` of `ci-workflows`.

This module replaces both credentials. The bot commits the files through the GraphQL mutation
`createCommitOnBranch`: **GitHub itself creates and signs the commit**, so it shows as verified,
and the token is narrowed to one repository with `contents:write` only. It ships as

- the verb `commit` ([`application/verbs/commit.py`](../../src/lktec_bot/application/verbs/commit.py)),
- one command, `lktec-bot sync commit`
  ([`application/modules/sync/`](../../src/lktec_bot/application/modules/sync)),
- two reusable workflows, [`deploy-bump.yml`](../../.github/workflows/deploy-bump.yml) (image
  tag bump) and [`sync.yml`](../../.github/workflows/sync.yml) (files a job of the caller
  generated),
- a caller snippet, [`templates/deploy-bump.yml`](../../templates/deploy-bump.yml), and a policy
  snippet, [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml).

## What it does

Both workflows follow the pattern of the [lockfiles module](lockfiles.md): a first job produces
the files and holds **no secret**; a second job, `commit`, holds the App credentials, installs
the bot from the commit of the workflow file it was called at, downloads the files as an
artifact and runs

```sh
lktec-bot sync commit --repository owner/name --changed-dir <handover> \
  [--version V] [--branch B]
```

The handover directory is `manifest.txt` (one repository path per line) plus
`files/<repository path>`. The command, in this order:

1. Reads the handover and refuses the **whole handover** if a path is not a plain relative path
   (including anything under `.github/workflows/`), a listed file is missing, a symlink or
   outside `files/`, or a file is over 10 MiB (exit 1, nothing minted, nothing committed). The
   handover helpers are shared with the lockfiles module.
2. Through `Gate.read` (`contents: read`) reads the policy. `--branch`, when given, must equal
   the policy's `branch`, and every path of the handover must lie under one of `paths` (a
   directory covers everything below it, a file matches exactly). Otherwise `denied` (exit 3),
   with the offending paths listed, and nothing is committed.
3. Resolves the head of the branch (`GET /git/ref/heads/<branch>`) and reads every file at that
   head, comparing it byte for byte. Equal files are dropped; a file missing at the head is an
   addition.
4. Nothing differs: `unchanged`, exit 0.
5. Otherwise runs the `commit` verb (`contents: write`) onto the branch with `expectedHeadOid`
   set to the resolved head, so a push in between makes GitHub refuse the mutation instead of
   the bot overwriting it. The headline is `commit-message` with `{version}` replaced; the body
   lists the files. Decision `committed` with the commit URL.

It writes `result` (`ok`, `denied`, `error`), `decision` (`committed`, `unchanged`, `denied`,
`error`), `reason` and `url` to `$GITHUB_OUTPUT`, and a line to the job summary. `--version`
may only hold letters, digits and `. _ + -`.

### `deploy-bump.yml`

Inputs: `version` (required), `images` (required, an extended regular expression of image
repositories without the tag, for example `ghcr\.io/lukislp/(studylife-ai)`), `paths`
(space-separated directories, default `k8s deploy`), `exclude` (a `grep -E` pattern of paths to
leave alone, default `/flux/`), `branch` (default `main`), `bot-repository`. Secrets: `app-id`,
`private-key`. Outputs: `decision`, `url`.

- `bump` checks out the branch, and for every tracked `*.yaml` / `*.yml` under `paths` that
  mentions one of the images and does not match `exclude` runs
  `sed -i -E "s#(${IMAGES}):[0-9A-Za-z._-]+#\1:${VERSION}#g"`, the loop the 13 repositories
  have today. The changed files go into the handover (artifact `deploy-bump-handover`).
- `commit` runs only when something changed. Its concurrency group is
  `sync-commit-<repository>-<branch>`; a second run waits for the first.

### `sync.yml`

For callers that generate the files themselves. Inputs: `artifact` (required: the name of an
artifact that holds the handover directory), `branch` (default `main`), `version` (optional),
`bot-repository`; the same secrets and outputs. One job: the `commit` job above, with
`actions/download-artifact` of `inputs.artifact`.

The caller's generating job uploads the handover with the directory as the artifact root:

```
manifest.txt            data/status.json
files/data/status.json  <the new content>
```

```yaml
- uses: actions/upload-artifact@<sha> # vX.Y.Z
  with:
    name: status-data
    path: ${{ runner.temp }}/handover   # holds manifest.txt and files/
    include-hidden-files: true
```

followed by a job `uses: lukislp/lktec-bot/.github/workflows/sync.yml@<sha>` with
`needs: <generating job>`, `with: artifact: status-data` and the two secrets.

## Options

Under `modules.sync` in `.github/lktec-bot.yml`. Unknown keys are errors and deny every verb.
The policy is read from the default branch, so enabling the module and changing options takes
effect once merged.

| Option           | Values / default                                                     | Meaning |
| ---------------- | -------------------------------------------------------------------- | ------- |
| `paths`          | list of repository paths, **required, non-empty**                    | Only files under these directories (or exactly these files) may be committed. A path under `.github/workflows/` is rejected when the policy is validated. |
| `branch`         | branch name; default `main`                                          | The branch the commit goes onto. `--branch` must equal it. |
| `commit-message` | one line, at most 200 characters; default `chore(deploy): image {version} [skip ci]` | The headline. `{version}` is replaced by `--version` (empty when not given). |

Verbs of the module: `commit` only.

## Permissions

Every call goes through the gate with its own token, narrowed to one repository:

| Step                                       | Token             |
| ------------------------------------------ | ----------------- |
| policy (every call)                        | `contents: read`  |
| branch head and the files at that head     | `contents: read`  |
| `commit`                                   | `contents: write` |

The `workflows` permission is never requested.

## Setting up a repository

1. **Install the App** on the repository and store `LKTEC_BOT_APP_ID` and
   `LKTEC_BOT_PRIVATE_KEY` as Actions secrets.
2. **The policy file** on the default branch: add the `sync` block of
   [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml) (with `paths` and `enabled: true`)
   to `.github/lktec-bot.yml` and check it with `lktec-bot policy validate`. Merge this
   **before** the workflow first runs.
3. **Make the App a bypass actor of the branch ruleset** (actor type `Integration`, bypass
   mode `always`). The commit lands on the protected branch without a pull request, so without
   the bypass GitHub refuses it. This is a change to the ruleset that the repository owner
   makes; the bot never touches rulesets.
4. **The caller**: for the image bump add the job of
   [`templates/deploy-bump.yml`](../../templates/deploy-bump.yml) to the release chain; for
   generated files call `sync.yml` after the generating job.

### Why the bypass is acceptable

The deploy key it replaces bypasses the ruleset too, and is a long-lived credential that can
push anything anywhere in the repository. The App is narrower in every direction:

- GitHub signs the commit (verified, author the App's bot, committer GitHub), so the
  "signed commits" rule is met without a signing key anyone holds.
- The token lives for the one run and is narrowed to one repository and `contents:write`.
- The `commit` verb refuses `.github/workflows/`, and the App has no `workflows` permission, so
  a bypass cannot be used to change CI definitions.
- The module refuses any file outside the policy's `paths`, a list that is read from the
  default branch and so cannot be widened by the run that uses it.
- `expectedHeadOid` makes a concurrent push win instead of being overwritten.

### An example release chain

```yaml
jobs:
  release:
    uses: lukislp/lktec-bot/.github/workflows/release.yml@<sha> # 2026-11-06
    # ...
  image:
    needs: [release]
    # builds and pushes the image for needs.release.outputs.version
  deploy-bump:
    needs: [release, image]
    if: needs.release.outputs.released == 'true'
    permissions:
      contents: read
      actions: read
    uses: lukislp/lktec-bot/.github/workflows/deploy-bump.yml@<sha> # 2026-11-06
    with:
      version: ${{ needs.release.outputs.version }}
      images: ghcr\.io/lukislp/(studylife-ai)
    secrets:
      app-id: ${{ secrets.LKTEC_BOT_APP_ID }}
      private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
```

The commit carries `[skip ci]`, so it does not start another release run.

## Retire the credentials

After one bump went through the new job, delete from the repository the deploy key used by
`deploy-key-push`, the secret `SEMANTIC_RELEASE_DEPLOY_KEY` (once the
[release module](release.md) is in use as well and nothing else reads it) and the secret
`DEPLOY_SIGNING_KEY`, and remove the deploy key's bypass entry from the ruleset. The SSH
signing key registered on the account can then be removed from the account settings.
