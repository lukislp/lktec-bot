# lktec-bot

[![CI](https://github.com/lukislp/lktec-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/lukislp/lktec-bot/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lukislp/lktec-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lukislp/lktec-bot)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/16420/badge)](https://www.bestpractices.dev/projects/16420)
[![CodeQL](https://github.com/lukislp/lktec-bot/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/lukislp/lktec-bot/security/code-scanning)
[![Release](https://img.shields.io/github/v/release/lukislp/lktec-bot)](https://github.com/lukislp/lktec-bot/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
[![Built on GitHub Apps](https://img.shields.io/badge/built%20on-GitHub%20Apps-181717?logo=github)](https://docs.github.com/en/apps)

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
- requests the `workflows` permission for the `update-branch` and `merge` verbs alone; the
  `merge` verb exists only for a pull request GitHub refuses to arm because it is already
  mergeable, and never bypasses branch protection (GitHub's auto-merge is the normal mechanism).

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
merge, update-branch, comment; `lockfiles`: commit, comment; `release`: release; `sync`: commit, open-issue, check; `ping`: ping), and only the options it declares; anything else makes
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
  selection, the merge verb and its limits and why only `update-branch` and `merge` get
  `workflows`.
- [docs/setup.md](docs/setup.md): set the bot up for your own repositories from zero, with the pitfalls.
- [docs/register-your-app.md](docs/register-your-app.md): registering your own App from a manifest, where to
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
earlier comment instead of adding one), `approve`, `enable-automerge`, `merge` (CLI only; for pull requests GitHub will not arm) and `update-branch`, and
`commit` (a commit on a branch that GitHub signs; CLI only so far), `release`, `open-pr`, `open-issue` (open an issue, or update the bot's own marked one) and `check` (a check run on a commit, created or updated). Each
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
`lktec-bot automerge sweep` arms what was missed, updates behind branches and asks a maintainer
to recreate conflicting pull requests and ones with an unsigned commit (Dependabot does not take
commands from an App). It ships as a reusable workflow
([`.github/workflows/automerge.yml`](.github/workflows/automerge.yml)) with a caller template
([`templates/dependabot-auto-merge.yml`](templates/dependabot-auto-merge.yml)). GitHub merges, once
the required checks are green. The bot merges itself only with the opt-in option `merge-ready`,
and only a pull request GitHub refused to arm because it is already mergeable.

The [`lockfiles` module](docs/modules/lockfiles.md) regenerates the lock files of a Dependabot
pull request in a job without secrets and commits them with the `commit` verb in a second job.
The commit is created through GraphQL `createCommitOnBranch`, so GitHub signs it: no personal
access token and no signing key. Optionally (`derived` in the policy) the same run regenerates
files the repository derives from its dependency graph, such as third-party notices: the
generators come from the default branch's policy and run only in the job without secrets, and
the commit job admits nothing but lock files and the exact paths the policy lists. Reusable
workflow ([`.github/workflows/lockfiles.yml`](.github/workflows/lockfiles.yml)) and caller
template ([`templates/dependabot-lockfiles.yml`](templates/dependabot-lockfiles.yml)).

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
module refuses any file outside the `paths` of the policy. It can optionally report the bump as a
check run on the release commit (policy option `check`, off by default). Reusable workflows
([`.github/workflows/deploy-bump.yml`](.github/workflows/deploy-bump.yml) and
[`.github/workflows/sync.yml`](.github/workflows/sync.yml)) and caller template
([`templates/deploy-bump.yml`](templates/deploy-bump.yml)). The deploy bump can carry extra files, such as a coverage badge, into the same commit (input `handover-artifact`).

## Status

Released: the current version is v0.3.0 (see the [releases](https://github.com/lukislp/lktec-bot/releases)).
The policy schema, permission table, gate, token minting, HTTP adapter, CLI, the verbs and their
composite actions, and the `automerge`, `lockfiles`, `release` and `sync` modules are in
production use across the maintainer's repositories, tested against fakes and mocked HTTP and by
the live end-to-end workflow. This repository releases itself through its own `release` module.

Not in this repository yet: release mode `pr` (a reviewed release pull request with changelog and
version files).

## Use it for your own repositories

The workflows and actions work for any repository, under your own GitHub App:

- [docs/setup.md](docs/setup.md): the path from zero (pin the workflows, register the App, install
  it, set the two secrets, policy file, callers, first run) and the pitfalls we learned.
- [docs/register-your-app.md](docs/register-your-app.md): registering the App from
  [`manifest.json`](manifest.json), turning the returned key into `LKTEC_BOT_PRIVATE_KEY`,
  rotation.

The identity rule: every user registers their own App. Nothing in this repository ever carries
the maintainer's App id or private key, and a workflow only ever sees the credentials you hand it
as secrets.

## License

Apache-2.0, see [LICENSE](LICENSE).

## Project

- [SECURITY.md](SECURITY.md): reporting a vulnerability, response targets, verifying a release.
- [CONTRIBUTING.md](CONTRIBUTING.md): how changes get in, tests, coding style.
- [GOVERNANCE.md](GOVERNANCE.md): roles, decisions, continuity.
- [docs/roadmap.md](docs/roadmap.md): phases 0-4 and the next twelve months.
- [docs/assurance-case.md](docs/assurance-case.md): threat model, trust boundaries, residual risks.
- [docs/releases.md](docs/releases.md): signed release assets and reproducible builds.
