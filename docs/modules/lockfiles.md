# The `lockfiles` module

A Dependabot bump often leaves a lock file stale: Dependabot regenerates the lock file of the
project whose manifest it bumped, but not the lock files of the projects that depend on it
(NuGet records a reference's dependencies in the referencing project's `packages.lock.json`),
and its `uv.lock` output has shipped unparsable files. A locked-mode restore then fails on the
pull request. This module regenerates the lock files with the tool versions CI uses and commits
them onto the pull request branch, after which the normal CI run and the auto-merge proceed.

It replaces the shared `dependabot-lockfiles` workflow of `ci-workflows`, which pushed that
commit with a fine-grained personal access token (`AUTOMERGE_TOKEN`) and signed it with a
per-repository SSH key (`DEPLOY_SIGNING_KEY`). Here the bot makes the commit through the GraphQL
mutation `createCommitOnBranch`: **GitHub itself creates and signs it**, so it shows as verified
(author: the App's bot, committer: GitHub, as for a commit made in the web interface), and the
only permission needed is `contents:write`.

It ships as

- the verb `commit` ([`application/verbs/commit.py`](../../src/lktec_bot/application/verbs/commit.py)),
- one command, `lktec-bot lockfiles commit`
  ([`application/modules/lockfiles/`](../../src/lktec_bot/application/modules/lockfiles)),
- a reusable workflow, [`.github/workflows/lockfiles.yml`](../../.github/workflows/lockfiles.yml),
- a caller template, [`templates/dependabot-lockfiles.yml`](../../templates/dependabot-lockfiles.yml),
  and a policy snippet, [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml).

## What it does

The reusable workflow has two jobs.

1. **`regenerate`** checks out the head commit of a Dependabot pull request and runs the
   ecosystem commands below. It has **no secret**. It collects the lock files that changed
   (tracked files only, and only the known lock file names) into a handover directory,
   `manifest.txt` plus `files/<repo-path>`, and uploads it as an artifact.
2. **`commit`** has the App credentials and never checks out the pull request. It installs the
   bot from the commit of the workflow file it was called at, downloads the artifact and runs

   ```sh
   lktec-bot lockfiles commit --repository owner/name --number N \
     --head-sha <head the files were regenerated for> --changed-dir <handover>
   ```

The command, in this order:

1. Reads `manifest.txt` and refuses the **whole handover** if a path is not a plain relative
   path, or its file name is not a known lock file name (exit 1, nothing minted, nothing
   committed). A listed file that is missing, a symlink or outside `files/` is refused the same
   way.
2. Through `Gate.read` (a token with `contents:read` and `pull_requests:read`), reads the
   pull request and the policy. The pull request must be **open, not a draft, authored by
   exactly `dependabot[bot]`**, with its branch in this repository, and its head must still be
   `--head-sha`. Otherwise the result is `skipped` with the reason, exit 0: a pull request that
   moved on has a run of its own for the new head.
3. Applies the `ecosystems` and `paths` options, then reads every remaining lock file **at the
   pull request head** through the API and compares it byte for byte with the handed-over copy.
   What changed is decided here, not by a list from the first job.
4. Nothing differs: `unchanged`, "nothing to commit", exit 0.
5. Otherwise runs the `commit` verb onto the pull request's head branch with
   `expectedHeadOid` set to that head, so a push in between makes GitHub refuse the mutation
   instead of the bot overwriting it (the step fails; the next `synchronize` run starts over).
   Then it posts one comment on the pull request through the `comment` verb (marker
   `lockfiles`, updated in place on later commits) naming the files. A comment that fails is a
   warning, never the outcome.

It writes `result` (`ok`, `denied`, `error`), `decision` (`committed`, `unchanged`, `skipped`,
`denied`, `error`), `reason` and `url` to `$GITHUB_OUTPUT`, and a line to the job summary.

## The `commit` verb

`lktec-bot commit --repository owner/name --module lockfiles --branch B --message HEADLINE
[--body TEXT | --body-file FILE] [--add REPO_PATH=LOCAL_FILE]... [--delete REPO_PATH]...
[--expected-head OID]`

- The branch must exist. The commit is made on top of `--expected-head`, or on the branch head
  resolved right before the call, and that id is passed as `expectedHeadOid`.
- Each `--add` writes the local file's bytes (base64-encoded by the adapter) to the repository
  path; each `--delete` removes one. A path may appear once.
- A path is **refused** when it is absolute (leading `/` or a drive letter), contains a `..`,
  `.` or empty segment or a backslash, points into `.git`, or lies **under
  `.github/workflows/`**. The last rule is deliberate: the App is never granted the `workflows`
  permission (see [architecture](../architecture.md)), so GitHub would reject such a change
  anyway, and the verb says why before it calls GitHub. A lock file module therefore cannot be
  turned into a way to change CI definitions.
- The result is the commit id and its URL. Permission: `contents:write`.

### Verified live

Run once against this repository, through the real gate with the production adapters and the
App key from an Actions secret, on the scratch branch `scratch/commit-verb-20261106` (deleted
afterwards): the `commit` verb created commit `4b2aa11825bd2e07af29ed909c55569e62287ae6` with one
added file; the API reported `verification.verified: true` (reason `valid`), author
`lktec-bot[bot]`, committer `GitHub` (`web-flow`), the branch head as its only parent.

## Ecosystems

The commands are ported unchanged from `dependabot-lockfiles.yml` of `ci-workflows` (its
current `main`). That workflow supports exactly two ecosystems, so these are the two the module
supports; it never had npm, pnpm or pip-tools branches, and none were added.

| Ecosystem (`ecosystems:`) | Lock file             | Workflow input                | Regeneration                                                                                         |
| ------------------------- | --------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------- |
| `dotnet`                  | `packages.lock.json`  | `dotnet-version`              | for every tracked `packages.lock.json`: `dotnet restore <first *.csproj next to it> --force-evaluate -p:RestoreLockedMode=false` |
| `uv`                      | `uv.lock` (repository root) | `uv-version`, `python-version` | start from the base branch's `uv.lock`, then `uv lock --upgrade-package name==version` for every package Dependabot moved up |

As in the old workflow, an input left empty means the repository has no lock files of that kind
and the steps are skipped. The `uv` step keeps its safeguards: packages Dependabot moved
**down** (its resolver ignores `[tool.uv] override-dependencies`) and the project's own
package are left to the resolver, and the project's own version is pinned to what the base lock
records (`SETUPTOOLS_SCM_PRETEND_VERSION`) so that hatch-vcs versions cannot make every commit
look like a new diff. `UV_PYTHON_DOWNLOADS=never`: the interpreter comes from the runner's tool
cache. The old workflow's pre-checks around `AUTOMERGE_TOKEN` and `DEPLOY_SIGNING_KEY`
(`HAS_TOKEN`, the "PR still open" check, the signing setup, the push-and-rebase loop) have no
counterpart: they existed for the credentials this module removes.

## Why two jobs

The `regenerate` job runs the pull request's code: `uv lock` may build a source distribution,
`dotnet restore` evaluates MSBuild files, both from the branch of a dependency bump nobody has
reviewed. If that job also held the App key, the pull request's code could use it. So:

- `regenerate` references no secret, and its `GITHUB_TOKEN` can only read contents. What it
  can influence is the artifact.
- `commit` holds the secrets and never checks out the pull request. It treats the artifact as
  untrusted: only known lock file names, only plain relative paths, files that really lie inside
  the handover directory, size capped, the result compared with the head through the API, and
  the commit goes through the `commit` verb's own path rules. The worst a hostile artifact can
  do is change the content of a lock file of the very pull request it was made for, which is
  what the regenerating job is trusted with anyway; it cannot add another file or touch a
  workflow.

Both jobs run with harden-runner in block mode; the endpoint lists are in the workflow. A
private NuGet feed has to be added to the `regenerate` list in a fork of the workflow.

## Options

Under `modules.lockfiles` in `.github/lktec-bot.yml`. Unknown keys are errors and deny every
verb. The policy is read from the default branch, so enabling the module and changing options
takes effect once merged.

| Option           | Values / default                                                      | Meaning                                                                                             |
| ---------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `ecosystems`     | list of `uv`, `dotnet`; default `[]` (auto-detect)                    | Which lock files may be committed. Empty: every supported ecosystem whose lock file the first job found changed. |
| `paths`          | list of repository paths; default `[]` (whole repository)             | Restrict to these files or directories (a directory covers everything below it).                    |
| `commit-message` | one line, at most 200 characters; default `chore(deps): refresh lockfiles after Dependabot bump` | The commit headline. `{number}` is replaced by the pull request number. The body lists the files. |

`ecosystems` and `paths` are applied by the `commit` job, from the policy on the default
branch; the first job regenerates whatever it finds, as it must not read policy from the pull
request.

## Permissions

The workflow's own `GITHUB_TOKEN` has `contents: read` (and `actions: read` in the caller and in
the `commit` job, to look up which commit of the workflow the run uses). Every call goes through
the gate with its own token, narrowed to one repository:

| Step                                   | Token                             |
| -------------------------------------- | --------------------------------- |
| policy (every call)                    | `contents: read`                  |
| reading the pull request and the files | `contents: read` + `pull_requests: read` |
| `commit`                               | `contents: write`                 |
| `comment`                              | `pull_requests: write`            |

The `workflows` permission is never requested by this module (only `update-branch` holds it, see
`docs/architecture.md`).

## Dependabot and the new commit

Dependabot rebases its own pull requests only while the branch holds nothing but its own
commits. After the bot's commit it leaves the branch alone, so a base branch that moves on
later makes the pull request `BEHIND` or `DIRTY`. `BEHIND` is handled by the automerge sweep's
`update-branch`. For `DIRTY` the sweep leaves a hint asking a maintainer to comment
`@dependabot recreate` (Dependabot does not take commands from a GitHub App); Dependabot then
rebuilds the pull request from the current base (dropping the bot's commit), and the lockfiles
run for the new head commits again. That is the same recovery the old workflow relied on, so the
[automerge module](automerge.md) should be set up first or alongside.

The bot's commit starts the workflows of the pull request (a push with an App token does,
unlike `GITHUB_TOKEN`), including the lockfiles workflow itself: the second run finds the lock
files equal to the head and stops with "nothing to commit". A second commit is only made when a
regeneration really differs, so the version pin of the `uv` step matters.

The workflow finds the commit it was called at among the workflows the run references and only
trusts an entry of the repository in its `bot-repository` input (default `lukislp/lktec-bot`,
change it only in a fork).

## Setting up a repository

1. **Install the App** on the repository and store `LKTEC_BOT_APP_ID` and
   `LKTEC_BOT_PRIVATE_KEY` as *Actions* secrets (the workflow runs on `pull_request_target`,
   which sees Actions secrets; see [automerge](automerge.md) if they are already there for it).
2. **The policy file** on the default branch: add the `lockfiles` block of
   [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml) to `.github/lktec-bot.yml` and
   check it with `lktec-bot policy validate .github/lktec-bot.yml`. Merge this **before** the
   workflow first runs.
3. **The caller workflow**: copy
   [`templates/dependabot-lockfiles.yml`](../../templates/dependabot-lockfiles.yml) to
   `.github/workflows/dependabot-lockfiles.yml`, replace `<sha>` with a commit of this
   repository and set `dotnet-version` and/or `uv-version` (the CI pin) as the old caller did.
   The triggers are the old caller's (`opened`, `synchronize`, `reopened`), as
   `pull_request_target`, because the App secrets are Actions secrets.
4. **Order of the two caller workflows.** Merge the policy first, then in one pull request
   add the new `dependabot-lockfiles.yml` and remove the old caller of `ci-workflows`, so two
   workflows never push to the same branch. The `dependabot-auto-merge.yml` caller (automerge
   module) is independent: on every `synchronize` caused by the bot's commit its `arm` job
   approves the new head, which is what a ruleset that dismisses stale approvals needs.
5. **Retire the credentials.** After one Dependabot pull request went through, the repository
   can delete the secret `DEPLOY_SIGNING_KEY` and, once the automerge module is set up too and
   nothing else uses it, `AUTOMERGE_TOKEN` (both as Actions and as Dependabot secrets). The
   SSH signing key registered on the account can then be removed from the account settings.

## Dogfooding

This repository uses the workflow itself
([`.github/workflows/dependabot-lockfiles.yml`](../../.github/workflows/dependabot-lockfiles.yml),
uv only). The `uses:` line pins a commit of `main`. Move it by hand when a change to the reusable
workflow or to the modules should take effect here: the bot is installed from the pinned commit, so
a stale pin fails on policy keys that commit does not know yet.
