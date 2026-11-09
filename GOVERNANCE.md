# Governance

lktec-bot is a **single-maintainer project**. This file says who decides what, how decisions
are made, and what happens when the maintainer is unavailable. It describes how the project works
today, not a larger team.

## Roles

| Role | Who | Responsibilities |
| --- | --- | --- |
| **Maintainer** | [@lukislp](https://github.com/lukislp) (Lukas Koerber), the only person with write and admin access | Sets the direction and the [roadmap](docs/roadmap.md). Reviews and merges pull requests. Triages issues and security reports ([SECURITY.md](SECURITY.md)). Decides which permission a new verb may ask for. Enforces the [Code of Conduct](CODE_OF_CONDUCT.md). Administers the repository, the GitHub App that the project's own workflows use, and the App's private key (an Actions secret, [assurance case](docs/assurance-case.md)). |
| **Contributor** | Anyone who opens an issue or a pull request | Follows [CONTRIBUTING.md](CONTRIBUTING.md) and the Code of Conduct. Contributors need no write access: every change arrives as a pull request. |
| **Automation** (no human authority) | Dependabot, the `dependabot-*` workflows, the release module of this very bot | Dependabot proposes dependency updates as pull requests. The bot approves routine ones and enables auto-merge; GitHub merges once the required checks are green. The release workflow tags and publishes releases. None of them can bypass the required checks on `main`; the bot has no `merge` verb and only `update-branch` holds the `workflows` permission, to bring the base branch into a pull request ([architecture](docs/architecture.md#why-only-update-branch-gets-workflows)). |

There are no other roles: no co-maintainers, no committee, no release managers. A contributor with
sustained, high-quality contributions can be offered co-maintainer access (see
[Becoming a maintainer](#becoming-a-maintainer)), and this file is updated when that happens.

## How decisions are made

- **Day-to-day changes** (bug fixes, dependency updates, small features) are decided in the pull
  request: the required checks must be green and a code owner approves.
- **Bigger changes** (a new verb, a new module, a policy schema change, a new permission) start as
  an issue so the direction is agreed before the work starts. A new verb begins by naming the one
  GitHub App permission it needs. The maintainer makes the final call and states the reason in
  the issue.
- **Permission changes are the most sensitive decision.** The verb table in
  `src/lktec_bot/domain/permissions.py` is the single reviewed place; the `workflows`
  permission belongs to `update-branch` alone (decided 2026-11-09, a test pins it), no other
  verb may get it, and there is no `merge` verb. A change that widens what the App may
  do is described in the pull request body, including the migration for repositories that have
  opted in.
- **Breaking changes** (policy schema, verb signatures, action inputs) are marked in the title
  (`feat!:` or a `BREAKING CHANGE:` footer), so the release notes list them.
- **Security decisions** follow [SECURITY.md](SECURITY.md) and the
  [assurance case](docs/assurance-case.md), which lists the accepted residual risks.

## Releases

Releases are automated: every push to `main` is looked at by the bot's own release module
([`.github/workflows/self-release.yml`](.github/workflows/self-release.yml)); the Conventional
Commit types since the last tag decide whether and how the version moves. The tag is created by
the App through the API and carries no signature; the release assets (source distribution and
wheel) are signed keyless and carry build provenance ([docs/releases.md](docs/releases.md)). Only the
latest release is supported.

## Continuity: what happens if the maintainer is unavailable

The bus factor is **1**, and this file does not pretend otherwise. No second person works on the
project or has admin access day to day. What exists instead:

- **The code and the history are public.** Anyone can fork the repository (Apache-2.0). A fork
  keeps the full history, and every published release stays downloadable from GitHub Releases.
- **Nothing needed to build lives only with the maintainer.** The package builds with `uv build`
  from this repository, two builds of the same commit are byte-identical
  ([docs/releases.md](docs/releases.md#reproducible-builds)), and the release assets are signed
  keyless with Sigstore, bound to the workflow identity, so no private signing key is needed. The
  CI workflows run unchanged in a fork. What a fork needs of its own is a GitHub App (register it
  from the documented manifest, [docs/register-your-app.md](docs/register-your-app.md)) and its
  key: the App key of this project is not transferable, and every repository that opted in has to
  install the new App.
- **Consumers keep working.** A repository that pins the actions or the reusable workflows by
  commit SHA keeps running the pinned code; nothing calls home. What stops is updates and fixes,
  and the App's own installation tokens keep working as long as the App exists.
- **A trusted person can take over the accounts.** The maintainer keeps what the project needs in
  a password manager: the GitHub account with its recovery codes, the App's registration and its
  private key, and the bestpractices.dev entry. A trusted person has emergency access to that
  vault with a waiting period of a few days, together with written instructions. If the
  maintainer cannot continue, that person gets access well within a week and can then answer or
  close issues, rotate or delete the App key, transfer the repository, publish a release with the
  existing workflow, or archive the project. The person's name and the credentials are
  deliberately not published. The arrangement is reviewed once a year.
- **No GitHub account successor or second owner yet.** There is no organisation account and no
  second person with admin rights. One will be added once a suitable person is found.
- **If the maintainer stops responding** (no reaction to issues, pull requests or security reports
  for more than 30 days, with no announcement), treat the project as unmaintained: fork it,
  register your own App and keep pinning by SHA.

## Becoming a maintainer

There is no formal application. A contributor who has had several non-trivial pull requests
merged, has reviewed others' changes and has shown good judgement about permissions and security
can be invited by the maintainer. A new maintainer gets write access first, and admin access only
after a while. The new role, and any change to the decision process, is recorded in this file by
pull request.

## Code of Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). The maintainer enforces it.

## Changes to this document

This file changes like any other: by pull request, merged by the maintainer.