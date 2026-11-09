# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
https://github.com/lukislp/lktec-bot/security/advisories/new

If you cannot use GitHub, email the maintainer at koerber.lukas@t-online.de with "SECURITY" in the
subject line instead.

Do not open a public issue, discussion or pull request for a security problem. If you are not sure
whether something is a vulnerability, report it privately anyway.

A useful report has the affected version or commit, how the bot is used (reusable workflow,
composite action, CLI), steps to reproduce and the impact you expect. Never include a real App
private key or token; if one leaked, revoke it first.

## What happens after you report

lktec-bot has one maintainer ([GOVERNANCE.md](GOVERNANCE.md)), so these are targets one person can
actually meet:

| Step | Target |
| --- | --- |
| Acknowledgement of the report | within **3 days** |
| First assessment (accepted, needs more information, or not a vulnerability, with a CVSS severity) | within **14 days** |
| Fix released for **critical** and **high** severity | within **30 days** of acceptance |
| Fix released for **medium** and **low** severity | within **60 days**, or with the next regular release |

The process:

1. **Triage.** The maintainer reproduces the issue in the private advisory, agrees on a severity
   with the reporter and decides whether it is in scope (below).
2. **Fix.** The fix is developed in the advisory's private fork or on a branch that does not reveal
   the vulnerability. It comes with a regression test ([CONTRIBUTING.md](CONTRIBUTING.md#tests))
   and ships through the normal release pipeline.
3. **CVE and advisory.** For an accepted vulnerability, the maintainer requests a CVE through GitHub
   (GitHub is a CVE Numbering Authority). The advisory is published once the fixed release is out.
   It names the fixed version, and the release notes link to it.
4. **Disclosure.** The default is coordinated disclosure: the details go public with the advisory,
   once the fix is released or after 90 days, whichever comes first. A different date can be
   agreed with the reporter.
5. **Credit.** Reporters are credited by name or handle in the advisory, unless they ask not to be.

If a target slips, the reporter is told why and gets a new date. If you get no acknowledgement
within 7 days, comment on the advisory again or send an email.

## Scope

This package holds a GitHub App's private key in memory, mints installation tokens and acts on
repositories with them. **In scope:** anything in this repository that could let a verb obtain a
permission it does not need, act on a repository that has not opted in through
`.github/lktec-bot.yml`, keep a token alive longer than the verb, write a path it must refuse,
or leak the private key or a token through logs, summaries or error messages; the composite
actions, reusable workflows and templates, and the release workflow of this repository.

The policy file lives in the target repository and is read on every run, so a pull request that
changes it is a change to what the bot may do. Review such changes like code.

**Out of scope here:** vulnerabilities in GitHub itself, in a dependency (Dependabot tracks
those; report them upstream), and findings that need an already compromised runner, repository
owner account or App key. The [assurance case](docs/assurance-case.md) lists the accepted
residual risks.

## Supported versions

Only the latest tagged release and the `main` branch receive security fixes. Consumers are moved
by Dependabot (or bump their SHA pin by hand); there are no maintenance branches.

## Verifying a release

Releases come only from the release workflow on `main` ([`self-release.yml`](.github/workflows/self-release.yml)).
The tag `v<version>` is created by the lktec-bot GitHub App through the GitHub API and carries no
signature of its own. What to verify are the release assets: the source distribution and the
wheel are signed keyless with Sigstore (Cosign) and carry a GitHub build provenance
attestation. Each asset has a Sigstore bundle (`<file>.sigstore.json`) next to it, and
`provenance.intoto.jsonl` holds the provenance. The signing identity is
`https://github.com/lukislp/lktec-bot/.github/workflows/self-release.yml@refs/heads/main` with the
issuer `https://token.actions.githubusercontent.com`:

```bash
cosign verify-blob --bundle lktec_bot-<version>-py3-none-any.whl.sigstore.json \
  --certificate-identity https://github.com/lukislp/lktec-bot/.github/workflows/self-release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  lktec_bot-<version>-py3-none-any.whl
gh attestation verify lktec_bot-<version>-py3-none-any.whl -R lukislp/lktec-bot
```

More: [docs/releases.md](docs/releases.md). The composite actions and reusable workflows are used
by commit SHA, not from a download: pin the SHA of a release tag and let Dependabot move it.

## Security baseline

- How the requirements are met, the trust boundaries and the accepted residual risks are in the
  [assurance case](docs/assurance-case.md).
- Secrets never go into the repository. The App's private key lives in Actions secrets (or a
  file or environment variable of the operator), `*.pem` is git-ignored, and secret scanning with
  push protection is on.
- Every verb asks for exactly one narrowed installation token and revokes it afterwards; the
  `workflows` permission is requested by `update-branch` alone and by no other verb
  ([architecture](docs/architecture.md#why-only-update-branch-gets-workflows)).