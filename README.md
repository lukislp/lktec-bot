# lktec-bot

[![CI](https://github.com/lukislp/lktec-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/lukislp/lktec-bot/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lukislp/lktec-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lukislp/lktec-bot)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)

GitHub App bot core: a small set of least-privilege verbs (comment, approve, enable auto-merge,
update branch, later commits, releases, checks, issues and deployments) that run under a GitHub
App identity instead of a personal account. Every verb reads the target repository's
`.github/lktec-bot.yml` policy, mints a short-lived installation token narrowed to the one
permission it needs, runs, and revokes the token. A repository that has no policy file gets
nothing.

The package is generic: register your own GitHub App from the documented manifest and run the
same code. No bot name, owner, repository or environment is hard-coded.

## Why

Automation that merges, releases or deploys usually runs as a person: a fine-grained personal
access token with admin bypass, or a deploy key that pushes release commits. Those credentials
are long-lived, broader than any single job needs, and invisible in the audit log as anything
but the owner. lktec-bot replaces them with an App that

- is opt-in per repository through a committed policy file,
- holds the key in one place (the core) and hands modules only results,
- asks for exactly one permission per verb and revokes the token when the verb is done,
- never requests the `workflows` permission and has no `merge` verb (branch protection merges).

## Quick start

```sh
uv sync
uv run lktec-bot version
uv run lktec-bot policy validate .github/lktec-bot.yml
uv run lktec-bot policy check --file .github/lktec-bot.yml --module automerge --verb approve
```

A policy file that enables the `automerge` module and the end-to-end `ping` check:

```yaml
version: 1
modules:
  automerge:
    enabled: true
    exclude: []
    majors: manual
    workflow-files: auto
  ping:
    enabled: true
```

Each module may only run the verbs registered for it (`automerge`: approve, enable-automerge,
update-branch, comment; `lockfiles`: commit, comment; `release`: release; `sync`: commit; `ping`: ping), and only the options it declares; anything else makes
the file invalid and denies every verb.

To run a verb against a real repository, register an App ([docs/register-your-app.md](docs/register-your-app.md)),
install it on the repository, export `LKTEC_BOT_APP_ID` and `LKTEC_BOT_PRIVATE_KEY`, then:

```sh
uv run lktec-bot ping --repository owner/name --module ping
```

`ping` changes nothing; it proves the whole path (policy, narrowed token, token scope check,
revoke) and writes the result to the job summary when run in GitHub Actions. Exit codes: 0 ran,
1 error, 2 usage, 3 denied by policy. `LKTEC_BOT_API_URL` points the bot at a GitHub Enterprise
Server; `LKTEC_BOT_TOKEN_PROVIDER` selects the token provider (`app`, the default and only one
so far).

## Documentation

- [docs/architecture.md](docs/architecture.md): layers, the gate's decision order, the module
  registry, the verb/permission table, how a verb is added (one file), exit codes, provider
  selection, why there is no merge verb and why `workflows` is never requested.
- [docs/register-your-app.md](docs/register-your-app.md): registering your own App, where to
  store the key, environment variables, rotation.
- [docs/modules/automerge.md](docs/modules/automerge.md): the automerge module, its options, the
  differences to the personal-access-token workflow and how to set up a repository.
- [docs/modules/lockfiles.md](docs/modules/lockfiles.md): the lockfiles module and the `commit`
  verb: signed bot commits that replace a personal access token and a signing key.
- [docs/modules/release.md](docs/modules/release.md): the release module, the `release` verb,
  how semantic-release maps onto it, release chains and how to set up a repository.
- [docs/modules/sync.md](docs/modules/sync.md): the sync module: generated files committed onto a
  protected branch through the App (deploy bump, status data), the bypass-actor setup and why it
  is acceptable.
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Verbs and actions

The phase-1 verbs ship: `ping`, `comment` (with an optional marker to update the bot's own
earlier comment instead of adding one), `approve`, `enable-automerge` and `update-branch`, and
`commit` (a commit on a branch that GitHub signs; CLI only so far), `release` and `open-pr`. Each
verb except `commit` is a CLI subcommand and a composite action under [`actions/`](actions/README.md):

```yaml
- uses: lukislp/lktec-bot/actions/setup@<sha> # vX.Y.Z
- uses: lukislp/lktec-bot/actions/enable-automerge@<sha> # vX.Y.Z
  with:
    app-id: ${{ vars.LKTEC_BOT_APP_ID }}
    private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
    number: ${{ github.event.pull_request.number }}
    method: squash
```

Every action reports `result` (`ok`, `denied`, `error`), `message` and `url`; a policy denial
does not fail the step unless `fail-on-denied: true`. The `E2E` workflow runs the bot against
this repository on every pull request (ping, and one marked comment per PR that later runs
update).

## Modules

The [`automerge` module](docs/modules/automerge.md) ports the Dependabot auto-merge workflow
onto the bot: `lktec-bot automerge arm` classifies a Dependabot pull request (patch, minor,
digest refresh: routine; major: a person) and has the App approve it and enable auto-merge;
`lktec-bot automerge sweep` arms what was missed, updates behind branches and asks Dependabot
to recreate conflicting pull requests and ones with an unsigned commit. It ships as a reusable workflow
([`.github/workflows/automerge.yml`](.github/workflows/automerge.yml)) with a caller template
([`templates/dependabot-auto-merge.yml`](templates/dependabot-auto-merge.yml)). The bot never
merges: GitHub does, once the required checks are green.

The [`lockfiles` module](docs/modules/lockfiles.md) regenerates the lock files of a Dependabot
pull request in a job without secrets and commits them with the `commit` verb in a second job.
The commit is created through GraphQL `createCommitOnBranch`, so GitHub signs it: no personal
access token and no signing key. Reusable workflow
([`.github/workflows/lockfiles.yml`](.github/workflows/lockfiles.yml)) and caller template
([`templates/dependabot-lockfiles.yml`](templates/dependabot-lockfiles.yml)).

The [`release` module](docs/modules/release.md) replaces semantic-release and its per-repository
SSH deploy key: `lktec-bot release run` works out the next version from the Conventional Commits
since the last tag, renders the notes in semantic-release's style and creates the tag and the
GitHub release with the `release` verb (`contents: write`, an installation token narrowed to the
repository). Reusable workflow ([`.github/workflows/release.yml`](.github/workflows/release.yml),
with a `dry-run` input and `version` output for release chains) and caller template
([`templates/release.yml`](templates/release.yml)). Mode `tag` only; a release pull request
(`pr`) is planned. The verb `open-pr` (open or update a pull request) and `commit --from` (create
the branch first) ship with it.

The [`sync` module](docs/modules/sync.md) commits generated files onto a protected branch
through the App: today the image-tag deploy bump after a release in the Kubernetes repositories
(`k8s/` or `deploy/`), and the generated `data/` commits of the status page. It replaces the SSH
deploy key plus `DEPLOY_SIGNING_KEY` push of the `deploy-key-push` and `signed-bot-commit`
actions of `ci-workflows`: `lktec-bot sync commit` compares the handed-over files with the
branch head and commits the ones that differ with the `commit` verb. Because the commit lands on
the protected branch without a pull request, the App must be a bypass actor (actor type
Integration, mode always) of the branch ruleset. That is acceptable because GitHub signs the
commit, the token is narrowed to one repository, the verb refuses `.github/workflows/`, and the
module refuses any file outside the `paths` of the policy. Reusable workflows
([`.github/workflows/deploy-bump.yml`](.github/workflows/deploy-bump.yml) and
[`.github/workflows/sync.yml`](.github/workflows/sync.yml)) and caller template
([`templates/deploy-bump.yml`](templates/deploy-bump.yml)). The deploy bump can carry extra files, such as a coverage badge, into the same commit (input `handover-artifact`).

## Status: phase 2, pre-release

Policy schema, permission table, gate, token minting, HTTP adapter, CLI, the phase-1 verbs and
their composite actions, the `release` and `open-pr` verbs, and the automerge, lockfiles,
release and sync modules, all tested against fakes and mocked HTTP, plus the live end-to-end workflow.

Deliberately not in this repository yet:

- **Release mode `pr`** (a reviewed release pull request with changelog and version files).
- **Releases and tags of this repository.** No version has been published; `hatch-vcs` derives
  the version from tags once they exist. Until then pin the actions by commit SHA. The release
  module's first pilot is `lukislp/mowdeck-hacs`.

## License

Apache-2.0, see [LICENSE](LICENSE).

## Project

- [SECURITY.md](SECURITY.md): reporting a vulnerability, response targets, verifying a release.
- [CONTRIBUTING.md](CONTRIBUTING.md): how changes get in, tests, coding style.
- [GOVERNANCE.md](GOVERNANCE.md): roles, decisions, continuity.
- [docs/roadmap.md](docs/roadmap.md): phases 0-4 and the next twelve months.
- [docs/assurance-case.md](docs/assurance-case.md): threat model, trust boundaries, residual risks.
- [docs/releases.md](docs/releases.md): signed release assets and reproducible builds.

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lukislp/lktec-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lukislp/lktec-bot)
