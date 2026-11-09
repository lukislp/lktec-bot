# Security assurance case

This page argues why lktec-bot's security requirements are met. It covers the threat model, the
trust boundaries, how the secure design principles are applied, how common implementation
weaknesses are countered and which risks are knowingly accepted. It cites the source files that
enforce each point; the [architecture](architecture.md) has the detail. How to report a
vulnerability is in [SECURITY.md](../SECURITY.md).

Last full review: **2026-11-07** (maintainer, see [Review record](#review-record)).

## Claim

> A repository that has not opted in through `.github/lktec-bot.yml` gets nothing. For a
> repository that has, each verb acts with a short-lived installation token narrowed to that one
> repository and to the permissions of that verb, never more than the verb table lists, and the
> token is revoked when the verb ends. The App's private key and the tokens do not appear in
> logs, summaries or error messages, and code from a pull request never runs in a job that holds
> the key.

It does **not** claim: protection against an attacker who controls the runner, the repository
owner's account or the App's private key; the security of GitHub itself; or that a policy file the
owner committed is wise (the bot only enforces what the file says).

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| The App's private key | Actions secret `LKTEC_BOT_PRIVATE_KEY` of each repository that calls the bot (or a file or environment variable of the operator); in memory only while a verb runs | With it, anyone can mint tokens for every installation of the App |
| Installation tokens | memory of the running verb; minted per verb, revoked in a `finally` | Act on one repository with the verb's permissions |
| The repositories' contents, branches, releases | GitHub | What the verbs write to |
| The policy file `.github/lktec-bot.yml` | the target repository's default branch | Decides what the bot may do there |
| The release assets of this project | GitHub Releases | Supply-chain compromise of consumers |

## Actors and threat sources

- **Pull request author** (including a Dependabot pull request whose dependency is hostile): can
  change files in the pull request, including lock files, workflow files and the policy file in
  the PR branch.
- **Anonymous GitHub user**: can open issues and pull requests on a public repository, nothing
  else.
- **Hostile or malformed API response**: GitHub error bodies, rate-limit headers.
- **Supply-chain attacker**: a tampered dependency, action or release asset.
- **Operator mistakes**: a policy file that enables too much, a key stored in the wrong place.

## Trust boundaries and controls

| # | Boundary | Controls (source) |
| --- | --- | --- |
| TB1 | Pull request code to the job that holds the key | The reusable workflows split every module in two jobs. Whatever runs a pull request's code (`regenerate` in [`lockfiles.yml`](../.github/workflows/lockfiles.yml)) has no secrets and a read-only token and hands over only a directory of files. The job with the key (`commit`, `arm`, `sweep`) runs on `pull_request_target` without checking out the pull request's code (`persist-credentials: false`, the workflow comments say so at each checkout), and it installs the bot from a pinned commit of this repository, never from the PR. [`automerge.yml`](../.github/workflows/automerge.yml) acts only for `dependabot[bot]` and `lockfiles.yml` only for non-draft Dependabot pull requests whose head lives in the same repository. |
| TB2 | The handover from the untrusted job | [`_handover.py`](../src/lktec_bot/application/modules/_handover.py) and the module services treat everything as untrusted: only known lock file names, plain relative paths ([`domain/paths.py`](../src/lktec_bot/domain/paths.py)), files of at most 10 MiB, every path matched with an anchored pattern; one bad path refuses the whole handover and nothing is committed. |
| TB3 | Which repository and which action | [`gate.py`](../src/lktec_bot/application/gate.py): the gate reads `.github/lktec-bot.yml` from the **default branch** of the target (never from a pull request head), with a token narrowed to `contents:read`. A missing, invalid or non-enabling policy denies. The module must be known and the verb must be in the module's verb set. An unknown option or module name makes the whole file invalid ([`domain/policy.py`](../src/lktec_bot/domain/policy.py), [`application/modules/__init__.py`](../src/lktec_bot/application/modules/__init__.py)). |
| TB4 | Which permissions a verb gets | [`domain/permissions.py`](../src/lktec_bot/domain/permissions.py) is the single table. The token is minted for `repositories=[repo]` and exactly the verb's grants ([`adapters/app_token.py`](../src/lktec_bot/adapters/app_token.py)). What GitHub reports for the minted token must be a subset of what was requested plus `metadata:read`, and its repository list exactly the target; otherwise the token is revoked and the run fails (`Gate._scope_problem`). A handler whose grants differ from the table cannot be registered. The `workflows` permission is requested by `update-branch` alone (its merge of the base branch is a workflow write for GitHub whenever the base changed a workflow file; a test pins that no other verb gets it), and there is no `merge` verb. |
| TB5 | What the `commit` verb may write | `createCommitOnBranch` through GraphQL: GitHub creates and signs the commit, no signing key or personal token exists. Paths must be relative, normalised, not under `.git`, and not at or under `.github/workflows/` (compared case-insensitively) ([`domain/paths.py`](../src/lktec_bot/domain/paths.py)); the `sync` module additionally refuses any file outside the `paths` of the policy ([`sync/options.py`](../src/lktec_bot/application/modules/sync/options.py)). `expectedHeadOid` makes a concurrent push fail instead of being overwritten; no ref is ever force-updated and a tag is never moved. |
| TB6 | Writing to a protected branch (`sync`) | The commit lands without a pull request, so the App must be a **bypass actor** (type Integration, mode always) of the branch ruleset. That is accepted because the token is narrowed to one repository, the verb refuses `.github/workflows/`, the module refuses paths outside the policy, GitHub signs the commit and the ruleset's signature requirement still applies. The bypass is per repository and listed in [`docs/modules/sync.md`](modules/sync.md). |
| TB7 | The key, tokens and output | The key is loaded by a `KeySource` ([`adapters/key_source.py`](../src/lktec_bot/adapters/key_source.py)) only when a token is needed. `InstallationToken.__repr__` prints no value; minted tokens are masked with `::add-mask::` ([`summary.py`](../src/lktec_bot/summary.py)). Everything printed passes through [`text.py`](../src/lktec_bot/text.py): one line, no `::` workflow commands, markdown escaped. |
| TB8 | The network | Only `https://api.github.com` (or the operator's GitHub Enterprise Server URL from `LKTEC_BOT_API_URL`); certificates are verified (no `verify=False` anywhere); the retry policy never retries a POST that may have taken effect ([`adapters/github_http.py`](../src/lktec_bot/adapters/github_http.py)). CI runners run with `step-security/harden-runner` and an egress allow list. |
| TB9 | Dependencies and actions | `uv.lock` is committed; the composite action installs from an export of it with hashes (`actions/setup/install.sh`); every `uses:` is pinned to a commit SHA, enforced by `tools/check_yaml.py` in CI; Dependabot, dependency review and the OpenSSF Scorecard run on the repository. |

## Secure design principles

| Principle | How it is applied |
| --- | --- |
| Least privilege | One narrowed token per verb with the verb's permissions only, for one repository; `permissions: contents: read` as the default of every workflow; `workflows` only for `update-branch`, for no other verb. |
| Fail closed | Missing or invalid policy, unknown module or option, unknown token scope, a broader token than asked for, a failed policy read, an unreported actor: all are `denied` or `error`, never a fallback to broader behaviour. |
| Economy of mechanism | A small hexagonal core: the domain decides, adapters talk to GitHub, every verb is one file and goes through one gate ([architecture](architecture.md)). |
| Complete mediation | `Gate.run` is the only path that holds a token; modules and add-ons call verbs and never touch the key or the API. |
| Open design | Everything is public; the security argument rests on the key and GitHub's ruleset, not on obscurity. |
| Separation of privilege | Untrusted code and the key are in different jobs; the merge is done by GitHub under branch protection, not by the bot. |
| Defence in depth | Policy check, token narrowing, scope verification after minting, path rules, GitHub's own permission model, branch rulesets and required signatures. |
| Psychological acceptability | Policy is one small YAML file in the repository; a denial is a readable result in the job summary, not a stack trace. |

## Common implementation weaknesses

| Weakness | Counter |
| --- | --- |
| Injection into the shell or workflow commands | No shell is built from untrusted text: workflow inputs are passed through `env:`, never interpolated into `run:`. Output is sanitised so a message cannot form `::set-env` or `::add-mask::` ([`text.py`](../src/lktec_bot/text.py)). |
| Path traversal | `repo_path_problem` refuses absolute paths, drive letters, backslashes, control characters, empty, `.` and `..` segments, `.git` and the workflows directory. |
| Name and URL injection | Owner and repository names must match `[A-Za-z0-9_.-]+` completely (`fullmatch`), not `.` or `..`. All validators that check a whole value are anchored with `\Z`, because `$` also accepts a trailing newline; a test pins this (`tests/test_properties.py`). |
| Secrets in logs | Tokens are never part of a message; `repr` hides values; the key is loaded only when a token is needed and never printed; a GitHub error body is truncated and sanitised before it is shown. |
| Confused deputy through the policy | The policy comes from the default branch, not from the pull request. |
| Race conditions | `expectedHeadOid`, concurrency groups in the workflows, tags created only if absent and never moved. |
| Unsafe deserialisation | `yaml.safe_load` only (`tools/check_yaml.py` uses a `SafeLoader` subclass); the options model of every module must forbid unknown keys (the registry refuses to register one that does not). |
| Vulnerable dependencies | Locked and hash-installed, monitored by Dependabot and dependency review. |

## Input validation

Untrusted input reaches the bot in four places. Each has an explicit allow-list check and a
rejection test:

1. **Command line and workflow inputs** (`--repository`, `--module`, `--number`, `--sha`, tags,
   branches, markers): validated in the gate and in the verbs (`parse_repository`,
   `validate_name`, the anchored patterns of `verbs/comment.py`, `verbs/release.py`,
   `verbs/commit.py`, the module options). Invalid input is a usage error, exit code 2.
2. **The policy file**: parsed with `yaml.safe_load` and validated against the module registry;
   anything unknown denies everything (`tests/test_policy.py`).
3. **The handover from an untrusted job**: see TB2 (`tests/test_lockfiles.py`, `tests/test_sync.py`).
4. **GitHub API responses**: the shapes the bot relies on are checked before use; a token response
   without a token, a missing scope report or a missing actor fails closed
   (`tests/test_app_token.py`, `tests/test_github_http.py`).

Pure input rules also have property-based tests with hypothesis (`tests/test_properties.py`):
an accepted path is always plain, relative and outside the workflows directory; an accepted name
has only the allowed characters; sanitised text is one line without `::`.

## Cryptography

The only cryptography is the App authentication that GitHub prescribes: a JSON Web Token signed
with the App's RSA private key using RS256, valid for at most 10 minutes (backdated 60 seconds,
lifetime 9 minutes), sent as a bearer token to `POST /app/installations/{id}/access_tokens`
(`build_app_jwt` in [`adapters/app_token.py`](../src/lktec_bot/adapters/app_token.py)).

- It is done by **PyJWT with the `cryptography` package** (`pyjwt[crypto]`, both free software).
  There is **no custom cryptography** in the project: no own primitive, no own padding, no hashing
  of passwords (the bot stores none), no random number generation of its own.
- RS256 (RSASSA-PKCS1-v1_5 with SHA-256) with a 2048-bit or longer key is what GitHub generates
  and accepts. No broken algorithm (MD5, SHA-1,
  DES, RC4) is used anywhere, and the algorithm is fixed in the code, never taken from input.
- Transport is TLS 1.2 or later with certificate verification, through httpx and the runner's
  OpenSSL defaults; nothing disables verification.
- The private key is a credential, not source: it comes from an environment variable or a file at
  run time, `*.pem` is git-ignored, and [register-your-app.md](register-your-app.md) documents
  rotation (generate a new key, replace the secret, delete the old key).
- **Algorithm agility:** the algorithm is dictated by GitHub's App authentication, which accepts
  only RS256, so the project cannot offer a choice. If GitHub adds another algorithm, `build_app_jwt`
  is the single place to change.

## Residual risks (accepted)

- **The App key is a single point of compromise.** Whoever holds it can mint tokens for every
  repository the App is installed on, with every permission of the App. Mitigations: the key is
  an Actions secret that jobs running pull request code never see; the App's permission set is
  the union of the verb table, which since 2026-11-09 includes `workflows:write` (for
  `update-branch`), so a leaked key could change workflow files on any installation; per-verb
  token narrowing means only a run of `update-branch` ever holds that permission; keys can be
  rotated at any time; installing the App on fewer repositories reduces the blast radius. A
  leaked key is revoked by deleting it in the App's settings.
- **The `sync` bypass.** The App can write to a protected branch without a pull request in the
  repositories that opted into `sync` (TB6). Limited by path allow-list, the workflows refusal
  and signed commits, but it is a bypass of review by design.
- **A policy file merged by mistake** widens what the bot may do. Branch protection and code
  owner review apply to it like to code ([SECURITY.md](../SECURITY.md#scope)).
- **GitHub itself.** Rulesets, signatures and token narrowing are GitHub's behaviour; the bot
  verifies what GitHub reports for a token, but cannot detect a GitHub bug.
- **Unsigned tags.** The tag of a release is created by the App through the API and carries no
  signature; the signed release assets and their provenance are what to verify
  ([releases](releases.md)).
- **Single maintainer.** See [GOVERNANCE.md](../GOVERNANCE.md#continuity-what-happens-if-the-maintainer-is-unavailable).

## Review record

| Date | Reviewer | Scope |
| --- | --- | --- |
| 2026-11-07 | maintainer | First full review of this page against the code on `main` (gate, verbs, paths, workflows, release pipeline) |
| 2026-11-09 | maintainer | `update-branch` gets the `workflows` permission (TB4, least privilege, App key residual risk); no other verb has it |

The page is reviewed when a verb, a permission or a workflow trust boundary changes, and at least
once a year.