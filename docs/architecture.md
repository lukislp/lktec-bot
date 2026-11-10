# Architecture

lktec-bot is a small hexagonal core. The domain decides, the application orchestrates, the
adapters talk to GitHub. Modules and add-ons never touch the App key or the GitHub API
directly: they call verbs, and every verb goes through one gate.

## Layers

```
src/lktec_bot/
  domain/        pure models and decisions - no I/O, no environment, no GitHub
    policy.py        shape of .github/lktec-bot.yml (version, modules, enabled), Decision
    permissions.py   Verb, Permission, the canonical verb -> permission table
    paths.py         which repository paths the bot may write (no absolute, no .., no workflows)
  ports/         protocols the application layer talks to
    github_api.py    GitHubApi (get_file_content, create_review, enable_auto_merge, create_commit_on_branch, ..., close)
    token_provider.py TokenProvider.token_for(owner, repo, permissions) -> Token, TokenError
    key_source.py    KeySource.load() -> AppCredentials (app id + private key PEM)
  adapters/      implementations of the ports
    _http_common.py  base URL, API version, User-Agent, header builder shared by both clients
    github_http.py   httpx REST + GraphQL client with the retry policy described below
    app_token.py     RS256 App JWT -> installation lookup -> narrowed installation token
    key_source.py    EnvKeySource (LKTEC_BOT_APP_ID / LKTEC_BOT_PRIVATE_KEY[_FILE]), FileKeySource
  application/   the gate, the module registry and the verbs
    gate.py          Gate.run(VerbRequest) -> VerbResult; Gate.read for module reads; owner/repo validation
    modules/         module registry (__init__.py): name -> allowed verbs + options model; authorize()
      automerge/     the automerge module: options.py, classify.py (pure), service.py (arm, sweep)
      lockfiles/     the lockfiles module: options.py, ecosystems.py (pure), service.py (commit)
      release/       the release module: options.py, commits.py, notes.py (pure), service.py (run)
      sync/          the sync module: options.py (pure), service.py (commit)
      _handover.py   the handover directory (manifest.txt + files/) shared by lockfiles and sync
    verbs/           one file per verb: @register(Verb.X, grants=..., options=...) on the handler
      _common.py     --number parsing and the draft/closed refusal shared by the PR verbs
  cli.py         typer entry point; verb subcommands are built from the verb registry, module
                 commands (automerge arm / sweep) are written by hand
  summary.py     markdown to $GITHUB_STEP_SUMMARY, result/message/url to $GITHUB_OUTPUT,
                 ::add-mask:: for minted tokens
  text.py        sanitising and markdown escaping for everything that is printed
actions/         composite actions (setup + one per verb), see actions/README.md
tools/           repository checks run by CI (strict YAML, action pinning)
```

Dependency direction is inward only: `adapters -> ports <- application -> domain`. The domain
imports nothing from the other layers; the application imports ports and domain, never an
adapter. The CLI is the only place where adapters are wired together (`cli.build_gate`).

## The gate

Every verb runs through `Gate.run`, which is the only path that holds a token. The decision
order is fixed and every step fails closed:

1. **Handler.** The verb must have a registered handler, else `error`.
2. **Module known.** The module must exist in the module registry (`application/modules.py`),
   else `denied` - before anything is minted or read.
3. **Read the policy.** `.github/lktec-bot.yml` is fetched from the *default branch* of the
   target repository (never from a pull request head, so a PR cannot bring its own
   permissions) with a token narrowed to `contents:read`, or with the caller's token when
   `LKTEC_BOT_CALLER_TOKEN` is set (for example the workflow's `GITHUB_TOKEN`). The file is
   validated against the registry: an unknown module name, or an option its module does not
   take, makes the whole file invalid and denies everything. A 404 on this read is retried
   twice (after 2 s and 5 s) before the file counts as missing, because GitHub can answer 404
   briefly for a file that exists; a retry that finds it adds a warning to the result. Only
   this read retries, other file reads do not.
4. **Decide.** `authorize(policy, module, verb)` returns a `Decision` with a readable reason:
   missing file, invalid file, module not declared, module not enabled, verb not in the
   module's verb set - each one `denied`.
5. **Mint.** A second installation token is minted for exactly one repository
   (`repositories=[repo]`) and exactly the verb's permissions (`permissions_for(verb)`).
6. **Check the token.** What GitHub reports for the token must be a subset of what was
   requested plus `metadata:read`, and its repository list must be exactly the target. A
   token that is broader, or whose scope the provider cannot report, is revoked and the run
   is an `error`. The same check runs on the policy-read token.
7. **Run.** The handler gets a `GitHubApi` bound to that token and the request with `actor`
   filled in (the login the token acts as, `<app-slug>[bot]`, taken from the installation
   lookup); the client is closed afterwards. A verb that looks for its own earlier work
   (`comment` with a marker, `approve`'s idempotency check) matches on that login and fails
   closed when the provider could not report it.
8. **Revoke.** Every token is revoked whether or not its user succeeded. A failed revoke is
   retried once and then reported as a warning (`token not revoked ...`) on the result; it
   never replaces the result.

The outcome is always a `VerbResult` (`ok`, `denied` or `error`, with a message, an optional
URL and warnings). A denial is a normal result rendered into the job summary, not an
exception.

Errors are told apart: a `TokenError` from the provider becomes `could not mint ...` with the
provider's message, anything else a provider raises is reported as an unexpected error with
its type, and nothing the gate catches ever turns into a traceback.

When `GITHUB_ACTIONS` is set, `::add-mask::<token>` is printed for every minted token right
after minting (`summary.mask_secret`, wired into the gate by `cli.build_gate`), so the runner
redacts the value from every later log line. Every message the CLI prints or writes to the
summary goes through `text.sanitize`: line breaks collapse to spaces and `::` becomes `: :`,
so no message can form a workflow command; `to_markdown` additionally escapes markdown.

Two tokens per run is deliberate: it keeps the verb token free of `contents:read` when the
verb does not need it, which is what makes "the minted permissions equal the verb's
permission and nothing more" a testable invariant (see `tests/test_gate.py`).

### Exit codes

| Code | Meaning                                                      |
| ---- | ------------------------------------------------------------ |
| 0    | the verb ran (`policy check`: the policy allows it)           |
| 1    | error (token, API, invalid file for `policy validate`)         |
| 2    | usage or configuration (bad `--repository`, unknown provider) |
| 3    | denied by policy or by the module registry                    |

A workflow that wants to treat a denial as a soft failure can test for `3`. The composite
actions do exactly that: a denial is `result=denied` on a successful step unless
`fail-on-denied: true`.

### Outputs

Every verb command prints `<outcome>: <message>` (and `url: <url>` when there is one), writes
the markdown line to `$GITHUB_STEP_SUMMARY`, and appends `result`, `message` and `url` to
`$GITHUB_OUTPUT` when that variable is set, each collapsed to one line. The composite actions
expose those three as their outputs.

### Environment

| Variable                   | Meaning                                                              |
| -------------------------- | -------------------------------------------------------------------- |
| `LKTEC_BOT_APP_ID`, `LKTEC_BOT_PRIVATE_KEY[_FILE]` | the App identity (see `docs/register-your-app.md`) |
| `LKTEC_BOT_TOKEN_PROVIDER` | which provider mints tokens; `app` (default) is the only one today. An unknown name is a clear error, exit 2. |
| `LKTEC_BOT_API_URL`        | API base URL for GitHub Enterprise Server, default `https://api.github.com` |
| `LKTEC_BOT_CALLER_TOKEN`   | optional token used only to read the policy file                     |

There is no command-line flag for any token: flags end up in process listings and shell
history, environment variables of a workflow step do not.

## The module registry

A module is the caller of a verb (`--module automerge`). The policy file in the target
repository enables a module; the registry bounds what an enabled module may do:

| Module      | Verbs                                              | Options                                      |
| ----------- | -------------------------------------------------- | -------------------------------------------- |
| `automerge` | `approve`, `enable-automerge`, `update-branch`, `comment` | `majors: manual \| auto`, `exclude: [str]`, `workflow-files: auto \| manual`, `method: squash \| merge \| rebase` (see [modules/automerge.md](modules/automerge.md)) |
| `lockfiles` | `commit`, `comment`                                | `ecosystems: [uv \| dotnet]`, `paths: [str]`, `commit-message: str` (see [modules/lockfiles.md](modules/lockfiles.md)) |
| `release`   | `release`                                          | `mode: tag`, `tag-format: str`, `branch: str`, `first-version: str`, `rules: [{type, scope, breaking, release}]`, `sections: {type: heading}` (see [modules/release.md](modules/release.md)) |
| `sync`      | `commit`                                         | `paths: [str]` (required), `branch: str`, `commit-message: str` (see [modules/sync.md](modules/sync.md)) |
| `ping`      | `ping`                                             | none                                         |

An enabled module cannot run a verb outside its set, no matter what the policy file says.
Options are validated with a pydantic model that forbids unknown keys; a module without an
options model accepts no options at all. The domain only knows the shape of the file
(`version`, `modules`, `enabled` as a strict boolean); which modules exist and what they take
is application knowledge, so the domain stays free of module names.

Adding a module is one `register_module(name, verbs, OptionsModel)` call in
`application/modules/__init__.py`; a module with logic of its own gets a package next to it
(`modules/automerge/`) whose pure parts (options, classification) the registry imports and
whose service (the part that drives the gate) only the command line imports.

### Module commands and `Gate.read`

A module that decides *what* to do needs to read before it writes (which pull requests exist,
what they change). It does that through `Gate.read(owner, repo, module, permissions, reader)`:
the module must be known and enabled in the policy, the reader gets a client bound to a token
narrowed to exactly `permissions` (`pull_requests: read` for `automerge`, plus `contents: read` for `lockfiles`, `contents: read` for `release` and `sync`; scope-checked and
revoked like a verb token) plus the validated policy to read its own options, and nothing it
does may write. Every write is a verb and goes through `Gate.run`, each with its own narrowed
token, so a module command never holds more than one permission at a time and the permission
footprint of every write stays the one reviewed in the verb table. A denial of either is the
same `denied` result and exit code 3.

## The verb table

`lktec_bot/domain/permissions.py` is the canonical list: it maps every planned verb to its
GitHub App permission, even the ones that do not ship yet, so the permission footprint of the
whole project is reviewed in one place:

| Verb               | Permission(s)                            |
| ------------------ | ---------------------------------------- |
| `ping`             | `metadata:read`                          |
| `comment`          | `pull_requests:write` or `issues:write`, by target |
| `approve`          | `pull_requests:write`                    |
| `enable-automerge` | `contents:write` + `pull_requests:write` |
| `update-branch`    | `contents:write` + `pull_requests:write` + `workflows:write` |
| `commit`           | `contents:write`                         |
| `open-pr`          | `pull_requests:write`                    |
| `release`          | `contents:write`                         |
| `check`            | `checks:write`                           |
| `open-issue`       | `issues:write`                           |
| `deployment`       | `deployments:write`                      |

The shipped verbs are `ping`, `comment`, `approve`, `enable-automerge`, `update-branch`, `commit`,
`release`, `open-pr`, `open-issue` and `check`. `tests/test_verbs.py` pins the set of shipped verbs;
adding one updates that line on purpose. No shipped module may run `open-pr` yet: it is for the release
module's planned `pr` mode and for add-ons, and a module gets it by adding it to its verb set. The same
holds for `open-issue` and `check`, which the `lockfiles`, `release` and `sync` modules may run but none
calls yet.

| Verb               | Options                                   | Behaviour                                                                 |
| ------------------ | ----------------------------------------- | ------------------------------------------------------------------------- |
| `ping`             | none                                      | changes nothing; exercises policy, token, scope check and revoke          |
| `comment`          | `--target pr\|issue --number N --body\|--body-file [--marker ID]` | posts a comment; with a marker, appends `<!-- lktec-bot:ID -->` and on later runs updates the bot's own comment carrying it (matched on the actor login, never on the marker alone); unchanged body is a no-op |
| `approve`          | `--number N [--body\|--body-file]`        | `APPROVE` review; refuses drafts and closed PRs (`error`); no second review for a head the bot already approved |
| `enable-automerge` | `--number N --method squash\|merge\|rebase` | GraphQL `enablePullRequestAutoMerge`; refuses drafts and closed PRs; already enabled with the same method is `ok` (`decision=unchanged`), with another method an `error`; a pull request GitHub says is mergeable now is retried six times, then `ok` with `decision=ready` |
| `update-branch`    | `--number N`                              | `PUT .../update-branch` with the current head sha; 202 is `ok`, GitHub's 422 "no new commits" is `ok` with a message, anything else an `error` |
| `commit`           | `--branch B [--from BRANCH] --message HEADLINE [--body\|--body-file] [--add PATH=FILE]... [--delete PATH]... [--expected-head OID]` | GraphQL `createCommitOnBranch`: one commit that GitHub signs (verified; author is the App's bot, committer is GitHub); the branch must exist, unless `--from` names a branch to create it from (`POST git/refs` at that branch's head, only when the branch is missing; an existing branch is never moved and no ref is force-updated); `expectedHeadOid` is `--expected-head` or the head resolved right before, so a concurrent push fails the mutation instead of being overwritten; refuses absolute paths, `..`, `.git` and everything under `.github/workflows/` (the `commit` token carries no `workflows` permission) |
| `release`          | `--tag T --sha SHA --name TITLE [--body\|--body-file] [--latest\|--no-latest] [--prerelease]` | `POST releases` with `target_commitish`: GitHub creates the lightweight tag at `SHA` when it does not exist. Release exists and the tag points at `SHA`: `unchanged`; the tag exists at another commit: `error`, a tag is never moved. Outputs `decision` (`created`, `unchanged`, `error`), `url`, `tag` |
| `open-pr`          | `--head B --base B --title T [--body\|--body-file] [--draft]` | upsert: an open pull request `head` into `base` gets its title, body (only when given) and draft state changed to what was asked, only where they differ (the draft state through GraphQL, REST cannot toggle it); none exists: one is opened. Outputs `decision` (`opened`, `updated`, `unchanged`, `error`), `url`, `number` |
| `open-issue`     | `--title T --body\|--body-file [--marker ID] [--label L]... [--reopen\|--no-reopen]` | opens an issue; with a marker it is an upsert keyed on the invisible `<!-- lktec-bot:ID -->` tag in the body **and** the actor login as author (the issues the bot opened are listed with `creator=`, pull requests skipped; several matches: the oldest is used). A match gets the title, body and labels it lacks changed in one `PATCH` (labels are only added, never removed); nothing differs: no write; a closed one is reopened, or with `--no-reopen` a new one is opened. Labels must exist (an unknown one is an `error` with GitHub's message). Outputs `decision` (`opened`, `updated`, `reopened`, `unchanged`, `error`), `url`, `number` |
| `check`            | `--sha SHA --name N --title T --summary TEXT\|--summary-file F [--text\|--text-file] [--status queued\|in_progress\|completed] [--conclusion C] [--details-url URL] [--external-id ID] [--annotations-file F]` | upsert of a check run keyed on (commit `SHA`, `N`, this App): `GET commits/SHA/check-runs?check_name=N`, keep the runs whose `app.slug` is the bot's login without `[bot]` (the App id is not known to verbs; a run of another App is never touched), take the highest id; none: `POST check-runs`; one that differs in status, conclusion, title, summary, text (and details URL or external id when given): `PATCH check-runs/ID`; equal: `unchanged`, no write. `--conclusion` is required for `completed` and refused otherwise; `action_required` needs `--details-url`. Annotations (JSON array, at most 500) go 50 per request, the rest as further updates; a run with annotations always updates, and GitHub appends them. Outputs `decision` (`created`, `updated`, `unchanged`, `error`), `url`, `id` |

Every result carries the relevant URL (comment, review, pull request or release). Nothing a verb
prints or stores contains a token. A verb may report more `$GITHUB_OUTPUT` keys than `result`,
`message` and `url` through `VerbResult.outputs` (`decision`, `tag`, `number`, `id`); for `release`,
`open-pr`, `open-issue` and `check`, `decision` is the outcome itself when the gate stops the run before
the handler.

The command `lktec-bot release run` (the release module) and the verb `lktec-bot release` share a
name; click cannot hold a group and a command under one name, so the module command is
registered as `release-run` and the root group rewrites `release run` to it.

Verb handlers declare their grants next to the code (`@register(Verb.X, grants=...)`), and
`register` refuses a handler whose grants differ from the table. The table is the source of
truth; the declaration on the handler is the reviewable copy. Both have to change for a verb's
footprint to change.

`commit` is the GraphQL mutation `createCommitOnBranch`. GitHub creates the commit itself, so it is
signed by GitHub with no signing key and no personal access token, and needs `contents:write`
only. The adapter base64-encodes the added files; the path rules live in `domain/paths.py` and
are shared with the `lockfiles` and `sync` modules, which check everything a job that ran a pull request's
code hands over against them. The mutation is a POST and is never retried after a 5xx: the first
attempt may have created the commit.

`enable-automerge` is a GraphQL mutation (`enablePullRequestAutoMerge`). The adapter resolves
the pull request's node id from `owner/repo/number` first; the port never exposes node ids.
GitHub documents the mutation under the pull-request permission, but a token narrowed to
`pull_requests:write` alone is refused with `Resource not accessible by integration`
(verified live against `lukislp/lktec-bot#4`); with `contents:write` added the mutation
succeeds. The table row and the registration carry both, like `update-branch`, which also
schedules a write to the branch.

### Why there is no `merge` verb

Merging is what branch protection is for. The bot enables auto-merge; GitHub merges once the
required checks and reviews are green. A `merge` verb would be a bypass of exactly the rules
the repository owner configured, and the App would need to be a bypass actor, which is what
this project exists to get rid of. The ruleset keeps `RepositoryRole admin` as the only bypass
actor, and only through a pull request.

### Why only `update-branch` gets `workflows`

The `workflows` permission lets a token create or update `.github/workflows/*`. The bot is
driven from those workflows; a verb that could rewrite them could change its own gate, so no
verb that writes content of its own gets it.

`update-branch` is the one exception. It only asks GitHub to merge the base branch into a pull
request branch, and GitHub treats that merge as a workflow write whenever the base changed a
file under `.github/workflows/` (a merged actions bump, a pin of the shared or the bot's own
workflows). Without the permission it answers `403 refusing to allow a GitHub App to create or
update workflow ... without workflows permission`, and every routine Dependabot pull request
stays behind its base. The content of that merge is the base branch, which is already reviewed
and already on the protected branch; the verb cannot supply workflow text of its own.

`Permission.WORKFLOWS` exists for this one grant. `tests/test_permissions.py` asserts that it is
in the grants of `update-branch` and of no other verb, and `tests/test_verbs.py` asserts the same
for the registrations, so a later change cannot spread it. The `commit` verb additionally refuses
every path under `.github/workflows/` (`domain/paths.py`), and the `lockfiles`, `release` and
`sync` modules never ask for the permission. A pull request that touches workflow files is made
by a person and reviewed as such.

Residual risk: the App itself now carries `workflows: write`, so a leaked private key could
change workflow files in every repository the App is installed on. The key is the existing
single point of compromise ([assurance case](assurance-case.md)); the per-verb token narrowing
means only a run of `update-branch` ever holds the permission, and no job that runs pull request
code sees the key.

## Adding a verb

1. Make sure the verb and its permission are in the table in `domain/permissions.py` (and in
   the row above); every planned verb already is.
2. Write the handler in `application/verbs/<name>.py`, one file, nothing else to register:

   ```python
   @register(
       Verb.APPROVE,
       grants=frozenset({Grant(Permission.PULL_REQUESTS, "write")}),
       options=(Option("number", "Pull request number.", kind="int", required=True),),
   )
   def approve(api: GitHubApi, request: VerbRequest) -> VerbResult:
       url = api.create_review(request.owner, request.repo, number_of(request), "APPROVE")
       return VerbResult(VerbOutcome.OK, "approved", url=url)
   ```

   `load_verbs()` discovers the file with `pkgutil`; the CLI builds the subcommand from the
   registry: `--repository` and `--module` always, `--target pr|issue` when `grants` is a
   mapping from `Target` to grant set (`comment`), and one flag per declared `Option`. An
   option is declarative (`kind` is `int`, `str`, `choice` with `choices`, or `text`, which
   becomes `--name` plus `--name-file`); parsed values arrive in `VerbRequest.args` under the
   option's name. The registry knows nothing about typer.
3. Add the verb to the verb set of every module that may call it (`application/modules.py`).
4. Add a gate test with the fakes in `tests/fakes.py` asserting the minted permissions equal
   `permissions_for(Verb.APPROVE)` and nothing more, and update the shipped-verbs line in
   `tests/test_verbs.py`.

If the handler needs a `GitHubApi` method that does not exist yet, add it to the port and to
the httpx adapter together, with a respx test.

## The HTTP adapter's retry policy

- 5xx: retried for `GET`, `HEAD`, `PUT` and `DELETE` only. `POST` and `PATCH` are not: the
  first attempt may have succeeded before the answer was lost, and a second comment or review
  is not something a retry may create.
- 403/429 secondary rate limit (`Retry-After`, exhausted `X-RateLimit-Remaining`, or the
  words in the body): retried for every method, because GitHub rejected the request before
  acting on it.
- The primary rate limit (5,000 requests per hour) is per installation, not per repository: the
  App has one installation per account, so every repository it is installed on shares one
  budget, and a migration burst across many of them can exhaust it. It resets only at
  `X-RateLimit-Reset`, so exponential back-off cannot help. A 403/429 without `Retry-After`
  that says "API rate limit exceeded" (or carries `X-RateLimit-Remaining: 0`) plus a reset
  header is treated as primary: when the reset is at most `max_rate_limit_wait_seconds` away
  (600 by default) the adapter logs one line to stderr, sleeps until the reset, and retries
  once; otherwise it raises `GitHubHttpError` at once, naming the reset time (UTC). A run that
  fails on it is safe to re-run after the reset: sweeps are idempotent and a release run
  re-reads everything. The adapter keeps the last `X-RateLimit-Remaining` it saw
  (`last_rate_limit`); the gate hands it to the CLI, which appends `rate limit remaining: N`
  to the final line of `automerge arm`, `automerge sweep`, `lockfiles commit`, `sync commit` and
  `release run`.- Transport errors (connection dropped, timeout): wrapped into `GitHubHttpError`; retried once
  for idempotent methods.
- A `Retry-After` above the back-off limit (30 s by default) fails the call instead of being
  capped: the job would otherwise wait for a limit it cannot see.
- GraphQL answers that are not objects, carry `errors`, have no `data`, or hold a null node
  on the path the adapter needs all become `GitHubHttpError` with a message naming the node.

Both clients are closed after use (`contextlib.closing` in the gate for the API client, in the
CLI for the gate, which closes the token provider).

## How a module or add-on calls a verb

A module is a workflow (or a small program) that decides *when* something should happen; the
core decides *whether it may* and *with what token*. A module never sees the key:

```yaml
- run: uv run lktec-bot ping --repository "${{ github.repository }}" --module ping
  env:
    LKTEC_BOT_APP_ID: ${{ vars.LKTEC_BOT_APP_ID }}
    LKTEC_BOT_PRIVATE_KEY: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
```

From Python, build a `Gate` and call `run(VerbRequest(...))`; the fakes in `tests/fakes.py`
show the ports a test double has to satisfy.

The target repository opts in with `.github/lktec-bot.yml`:

```yaml
version: 1
modules:
  automerge:
    enabled: true
    exclude: ["some-package"]   # dependency names the module leaves alone
    majors: manual              # manual | auto
  ping:
    enabled: true               # only needed to run the end-to-end check
```

Unknown top-level keys, unknown module names and unknown module options are errors, so a typo
never opens a door. `lktec-bot policy validate <file>` and
`lktec-bot policy check --file <file> --module m --verb v` run the same code the gate runs.
