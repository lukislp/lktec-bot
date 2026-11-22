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

A repository can also have **derived files** regenerated in the same run: files it generates
from its dependency graph, such as third-party licence notices, which a dependency bump makes
stale and the repository's own check then fails on (see [Derived files](#derived-files)), and
hash-locked requirements files produced by `uv pip compile` (see
[uv pip compile outputs](#uv-pip-compile-outputs)).

It ships as

- the verb `commit` ([`application/verbs/commit.py`](../../src/lktec_bot/application/verbs/commit.py)),
- three commands, `lktec-bot lockfiles commit`, `lktec-bot lockfiles derive` and
  `lktec-bot lockfiles uv-pip`
  ([`application/modules/lockfiles/`](../../src/lktec_bot/application/modules/lockfiles)),
- a reusable workflow, [`.github/workflows/lockfiles.yml`](../../.github/workflows/lockfiles.yml),
- a caller template, [`templates/dependabot-lockfiles.yml`](../../templates/dependabot-lockfiles.yml),
  and a policy snippet, [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml).

## What it does

The reusable workflow has three jobs.

1. **`source`** looks up which commit of the workflow file the run uses (the run lists the
   workflows it references), so that both other jobs install the bot from there. It runs
   nothing of the pull request and holds no secret.
2. **`regenerate`** checks out the head commit of a Dependabot pull request and runs the
   ecosystem commands below. It has **no secret**. It collects the lock files that changed
   (tracked files only, and only the known lock file names) into a handover directory,
   `manifest.txt` plus `files/<repo-path>`. When the default branch's policy declares
   [derived files](#derived-files), it then installs the bot and runs
   `lktec-bot lockfiles derive`, which adds them to the same directory. When the policy lists
   [`uv pip compile` outputs](#uv-pip-compile-outputs), it also runs
   `lktec-bot lockfiles uv-pip` first, which recompiles them into the same directory. It uploads
   the directory as an artifact.
3. **`commit`** has the App credentials and never checks out the pull request. It installs the
   bot from the commit the `source` job found, downloads the artifact and runs

   ```sh
   lktec-bot lockfiles commit --repository owner/name --number N \
     --head-sha <head the files were regenerated for> --changed-dir <handover>
   ```

The command, in this order:

1. Reads `manifest.txt` and refuses the **whole handover** if a path is not a plain relative
   path of the characters the bot accepts, or the manifest lists more than 1000 files (exit 1,
   nothing minted, nothing committed). A listed file that is missing, a symlink or outside
   `files/`, or larger than 10 MiB, is refused the same way.
2. Through `Gate.read` (a token with `contents:read` and `pull_requests:read`), reads the
   pull request and the policy. The pull request must be **open, not a draft, authored by
   exactly `dependabot[bot]`**, with its branch in this repository, and its head must still be
   `--head-sha`. Otherwise the result is `skipped` with the reason, exit 0: a pull request that
   moved on has a run of its own for the new head.
3. Puts every path through the module's one allow-list
   ([`allowlist.py`](../../src/lktec_bot/application/modules/lockfiles/allowlist.py)) with the
   policy just read from the default branch: a **known lock file name**, or a path listed
   **exactly** under `derived[].paths`, or as a `uv-pip-compile[].output` (at most 2 MiB each). One path that is neither refuses the
   whole handover (`error`, exit 1, nothing committed; only the two read tokens were minted).
4. Applies the `ecosystems` and `paths` options to the lock files, then reads every remaining
   file **at the pull request head** through the API and compares it byte for byte with the
   handed-over copy. What changed is decided here, not by a list from the first job.
5. Nothing differs: `unchanged`, "nothing to commit", exit 0.
6. Otherwise runs the `commit` verb, **one commit for the lock files and the derived files
   together**, onto the pull request's head branch with
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

## Why separate jobs

The `regenerate` job runs the pull request's code: `uv lock` may build a source distribution,
`dotnet restore` evaluates MSBuild files, both from the branch of a dependency bump nobody has
reviewed. If that job also held the App key, the pull request's code could use it. So:

- `regenerate` references no secret, and its `GITHUB_TOKEN` can only read contents. What it
  can influence is the artifact.
- `commit` holds the secrets and never checks out the pull request, and it executes nothing it
  is handed. It treats the artifact as untrusted data: only known lock file names and the
  derived paths the default branch's policy lists, only plain relative paths, files that really
  lie inside the handover directory, size capped, the result compared with the head through the
  API, and the commit goes through the `commit` verb's own path rules. The worst a hostile
  artifact can do is change the content of a lock file, or of a declared derived file, of the
  very pull request it was made for, which is what the regenerating job is trusted with anyway;
  it cannot add another file or touch a workflow.
- `source` finds the bot's commit for both. It needs `actions: read` for that and runs nothing
  else, so the job that runs the pull request's code does not get that permission.

All three jobs run with harden-runner in block mode; the endpoint lists are in the workflow. The
caller can add endpoints to the `regenerate` job only, with the input `allowed-endpoints` (a
private NuGet feed, the npm registry for a generator); the `commit` job's list is fixed.

## Derived files

Some repositories commit files they generate from their dependency graph and fail CI when the
file is stale. The case that motivated this (2026-11-11): two Dependabot pull requests of
studylife bumped OpenTelemetry to 1.21, which pulls in `System.Diagnostics.DiagnosticSource`,
and `test-lint` failed with "THIRD-PARTY-NOTICES.md is stale. Run: python3
tools/check-licences.py --update". The lock files were refreshed by the bot; the notices file
needed a person. With `derived`, the bot regenerates such files in the same run and commits them
together with the lock files.

### The policy

`derived` is a list under `modules.lockfiles`, empty by default (then nothing below happens and
the module behaves exactly as without it):

```yaml
modules:
  lockfiles:
    enabled: true
    derived:
      - name: third-party notices
        run: python3 tools/check-licences.py --update
        paths: [THIRD-PARTY-NOTICES.md]
        timeout-minutes: 5 # optional, 1 to 10, default 5
```

| Key               | Rule                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| `name`            | Required. At most 60 characters, letters, digits, space, `.`, `_`, `-`; unique (case-insensitive). What the job summary calls the generator. |
| `run`             | Required, not blank, at most 2000 characters, no control characters except a line break and a tab. Run as `bash -euo pipefail -c <run>`. |
| `paths`           | Required, 1 to 8 files, each named **exactly**: a plain relative path (no leading `/`, no drive letter, no `..`, `.` or empty segment, no backslash) of letters, digits and `. _ / @ + - space`, no pattern characters (`* ? [ ] { }`), nothing under `.github/` (case-insensitive: the workflows, the policy file `.github/lktec-bot.yml`, Dependabot's configuration), not `.git/`, not a lock file name (lock files have their own rule). A path belongs to one generator only. |
| `timeout-minutes` | Optional, 1 to 10, default 5. All generators together at most 15.                                    |

At most 4 generators. A derived file may be at most 2 MiB. Every rule is checked when the policy
is loaded (`lktec-bot policy validate`), and an invalid entry makes the whole policy invalid,
like any other bad option. The domain rule for a declarable path is `generated_path_problem` in
[`domain/paths.py`](../../src/lktec_bot/domain/paths.py); the decision whether a handed-over
path may be committed is `admit` in
[`allowlist.py`](../../src/lktec_bot/application/modules/lockfiles/allowlist.py), the one place
for lock files and derived files alike, used by both jobs.

### Where the generator runs, and which code it runs

Only in the `regenerate` job (no secret, no App token, read-only `GITHUB_TOKEN`), after the lock
files were refreshed, through `lktec-bot lockfiles derive`:

1. The **commands** come from `.github/lktec-bot.yml` of the **default branch** (`git show
   origin/<default branch>:.github/lktec-bot.yml` of the full-history checkout), never from the
   pull request. An invalid policy, a disabled module or no `derived` entry: `skipped`, nothing
   runs.
2. The **tree** they run in is a separate git worktree of the **merge base** of the pull request
   and the default branch, a commit of the default branch's history. Into it go only the pull
   request's **dependency manifests and lock files** (by file name: `*.csproj`, `*.props`,
   `*.targets`, `packages.lock.json`, `global.json`, `pyproject.toml`, `uv.lock`,
   `requirements*.txt`, `package.json`, `pnpm-lock.yaml`, `yarn.lock`, `go.mod`, `Cargo.toml`
   and the other names in [`ecosystems.py`](../../src/lktec_bot/application/modules/lockfiles/ecosystems.py)),
   including the lock files refreshed in this job. Every other file the pull request changes is
   left at the merge base and named in the job summary. So the generator **scripts** (and
   everything else) are the default branch's, and only the dependency graph is the pull
   request's, which is the point.
3. Each generator runs with that tree as working directory, without standard input, with the
   job's environment minus `GITHUB_TOKEN`, `GH_TOKEN`, the Actions runtime and OIDC request
   tokens (`ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_*`), `LKTEC_BOT_*` and the
   runner's command files (`GITHUB_OUTPUT`, `GITHUB_ENV`, `GITHUB_PATH`, `GITHUB_STATE`,
   `GITHUB_STEP_SUMMARY`), and is killed (with what it started) after its timeout.
4. Afterwards every declared path must exist as a **regular file** (no symlink, no directory) of
   at most 2 MiB, and the generator must not have changed, added or removed **any other file**
   of the tree (files the repository's `.gitignore` covers, such as `obj/` or `node_modules/`,
   are allowed and never handed over).
5. If **all** generators succeeded, the declared files that differ from the pull request head
   are added to the handover directory next to the lock files; if any one failed, ran out of
   time or broke a rule of step 4, **none** is added. The command always exits 0 and writes the
   decision (`generated`, `unchanged`, `skipped`, `failed`) and the reason to the job summary;
   the steps that install the bot and derive are `continue-on-error`, so the lock files are
   handed over and committed either way. Never a partial set of derived files.

The tools on the runner: the caller's .NET SDK (`dotnet-version`) and uv (`uv-version`) when
set, Python 3.13 (the bot's own, first on `PATH`), and whatever the runner image ships (Node.js
with corepack, for example). A generator that needs another host (the npm registry) needs that
host in `allowed-endpoints`.

### What the commit job checks

The `commit` job is the security boundary, and it executes nothing from the pull request or the
artifact. It reads the policy through the API from the default branch, checks that the pull
request is an open, non-draft pull request by `dependabot[bot]` with its branch in the
repository and still at the head the files were made for, admits every handed-over path through
`admit` (a known lock file name, or exactly a `derived[].paths` entry of that policy; regular
files only, no symlink, no traversal, at most 2 MiB per derived file and 10 MiB per file, at
most 1000 files; one refused path refuses everything), compares each file with the pull request
head and commits those that differ in **one** commit, bound to that head through
`expectedHeadOid`. The commit carries file contents only: `createCommitOnBranch` cannot set a
file mode, so an existing file keeps its mode and a new one is a plain non-executable file.

### Residual risks

- **Code from dependencies runs in the `regenerate` job.** A malicious package (or a
  manifest's build logic: MSBuild targets, npm scripts, a Python build backend) can run code
  while the lock files are restored, and again while a generator restores or installs packages.
  It cannot reach a secret (there is none in that job), but it can write anything into the
  artifact. The commit job then still commits only allowed paths of the very pull request, so
  the worst case is wrong content in a lock file or a declared derived file of a Dependabot pull
  request, which the pull request's own CI and review see before it merges. Declare as derived
  only files that are data (notices, inventories), not code that a build executes.
- **The tree is prepared on a runner that has already run the pull request's code.** The
  merge-base tree and the default-branch policy are read from the job's own git checkout, which
  that code could have tampered with. This is why nothing in the `regenerate` job is trusted:
  the commit job re-reads the policy through the API and admits paths on its own.
- **Egress.** The `regenerate` job may reach the package registries on its list (and the
  caller's `allowed-endpoints`). Code running there can send what the job can read (the
  repository's contents, which for a private repository are not public) to those hosts; it has
  no credential to send.
- **The pull request's dependency manifests are used as they are.** A maintainer commit on a
  Dependabot branch that changes a manifest is part of the pull request's graph; one that
  changes anything else is not used by the generators (and named in the summary).

### Rollout order

A policy with `derived` or `uv-pip-compile` is **invalid for every bot release that does not know the key**: unknown
options make the whole policy invalid and deny every verb of the repository. The bot that reads
the policy is the one pinned in the caller workflows (`dependabot-lockfiles.yml`,
`dependabot-auto-merge.yml`, ...). So, per repository:

1. Release lktec-bot with this feature.
2. Move **every** caller workflow of the repository that pins lktec-bot to that release (the
   fleet's pin bump).
3. Only then add `derived` or `uv-pip-compile` to `.github/lktec-bot.yml` on the default branch, checked with
   `lktec-bot policy validate .github/lktec-bot.yml` of the new release.

### Worked example

studylife's notices file is produced by `tools/check-licences.py`, which restores the server's
projects (`dotnet restore` writes `obj/project.assets.json`), reads every package's licence from
the NuGet cache and rewrites `THIRD-PARTY-NOTICES.md`. Its caller already sets
`dotnet-version`, so the policy gets:

```yaml
    derived:
      - name: third-party notices
        run: python3 tools/check-licences.py --update
        paths: [THIRD-PARTY-NOTICES.md]
```

On the next Dependabot bump the `regenerate` job refreshes `packages.lock.json`, prepares the
merge-base tree with the pull request's `*.csproj`, `Directory.Packages.props` and the refreshed
lock files, runs the script there (its `obj/` output is ignored by `.gitignore`) and hands over
`THIRD-PARTY-NOTICES.md` when it differs from the pull request head. The `commit` job admits it
because the default branch's policy lists it and commits it together with the lock files:
`chore(deps): refresh lockfiles after Dependabot bump`. That commit triggers the workflow again;
the second run produces the same files, finds them equal to the head and stops with "nothing to
commit". If the script fails (a licence the policy does not allow, say), the job summary says
`failed: generator 'third-party notices' failed with exit code 1; no derived file is handed
over`, the lock files are committed alone, and the pull request's own check stays red for a
person to look at.

## uv pip compile outputs

Some repositories commit a hash-locked requirements file generated by `uv pip compile`, for a
Docker build or a fuzzing image, and install it with `pip install --require-hashes`. Dependabot
bumps a package in that file, but its regeneration can drop pins that `uv pip compile
--generate-hashes` would have kept. The case that motivated this (mowdeck, PR #436):
Dependabot bumped `coverage` in `.clusterfuzzlite/requirements.txt` and lost the transitive
`setuptools` pin, so the install failed and the nightly fuzzing run broke. With `uv-pip-compile`
the bot runs `uv pip compile` again on the pull request branch and commits the result, the same
way it regenerates `uv.lock`.

### The policy

`uv-pip-compile` is a list under `modules.lockfiles`, empty by default (then nothing below
happens). **It is opt-in per file**: `requirements.txt` is not a lock file name, so it is not
admitted by name, and only the outputs listed here are ever regenerated or committed.

```yaml
modules:
  lockfiles:
    enabled: true
    uv-pip-compile:
      - input: .clusterfuzzlite/requirements.in
        output: .clusterfuzzlite/requirements.txt
```

| Key      | Rule                                                                                         |
| -------- | -------------------------------------------------------------------------------------------- |
| `input`  | Required. The file `uv pip compile` reads, named exactly.                                    |
| `output` | Required. The generated file, named exactly; the only path of the entry the bot may commit.  |

Both paths follow the rules of a declared derived path (`generated_path_problem` in
[`domain/paths.py`](../../src/lktec_bot/domain/paths.py)): plain relative paths of letters,
digits and `. _ / @ + - space`, no pattern characters, nothing under `.github/` (so never a
workflow, the policy file or Dependabot's configuration), not `.git/`, not a lock file name. The
two must differ. At most 8 entries; an output is listed once and is not also a `derived` path;
an input is not generated by the policy. Every rule is checked when the policy is loaded, and an
invalid entry makes the whole policy invalid.

### Which command runs

The command is **not** taken from the policy but from the output file's own header, the line
after `# This file was autogenerated by uv via the following command:`. That line is text of the
pull request, so it is never run as written. The parser
([`compile_command.py`](../../src/lktec_bot/application/modules/lockfiles/compile_command.py),
pure) accepts exactly

```
uv pip compile <input> [flags] -o <output>
```

with these flags, each at most once, in any order: `--generate-hashes`, `--universal`,
`--python-version X` (digits and dots, such as `3.13`), `--python-platform X` (such as
`x86_64-manylinux_2_28`), and `-o` or `--output-file` with the output. Every word may only use
letters, digits and `. _ / @ + = : -`. A shell operator, a quote that does not close, a flag
outside the list, a flag given twice, `--flag=value`, a second input, or an input or output that
is not exactly the configured path is refused. The file is then **skipped with the reason** in
the job summary (and the other configured files still run); a file without a header is skipped
the same way. What runs is the validated word list in its recorded order (so uv writes the same
header back), started as `<uv> <words>` **without a shell**.

### Where it runs

Only in the `regenerate` job (no secret), after the lock files were collected and before the
derived-file generators, through `lktec-bot lockfiles uv-pip`:

1. The files to compile come from `.github/lktec-bot.yml` of the **default branch**, read the
   same way as for `derive`. No `uv-pip-compile` entry, an invalid policy or a disabled module:
   `skipped`, nothing runs.
2. The command compiles **in place in the pull request's checkout**, so uv sees the pull
   request's input file and uses its current output as the preferred pins (a package Dependabot
   bumped stays bumped, a pin it dropped is added back). The input and the output must be
   regular files (no symlink).
3. It runs with the job's environment minus the credentials, as in the `derive` step, and with a
   timeout of 5 minutes per file.
4. The workflow runs the **uv it installed at `uv-version`**: the path is looked up right after
   that installation and handed over with `--uv`, because the bot's own setup later puts the
   bot's uv first on `PATH`. The workflow installs uv and a Python for the step when the policy
   mentions `uv-pip-compile`, also in a repository without a `uv.lock`. `uv-version` must be set
   in the caller (empty: the step is skipped). `--python-version` and `--python-platform` in the
   header decide what is resolved, not the runner's Python.
5. A result of at most 2 MiB that differs from the pull request's file is added to the handover
   next to the lock files. The command always exits 0 and writes `uv-pip` (`true` or `false`),
   the decision (`generated`, `unchanged`, `skipped`, `failed`) and the reason; the steps are
   `continue-on-error`, so the lock files are committed either way.

### What the commit job checks

The `commit` job is unchanged in kind. `admit` accepts, besides a known lock file name and a
`derived[].paths` entry, **exactly an `output` of `uv-pip-compile`** of the policy it read through
the API from the default branch, with a cap of 2 MiB. The path goes through every existing check
first (`repo_path_problem`: absolute, `..`, `.git`, `.github/workflows/`) and the declared-path
rule again. A `requirements.txt` that the policy does not list, or any other path, refuses the
whole handover, as before; so does the same path in a repository without the option. The file is
compared with the pull request head and committed in the same single commit as the lock files.
No new permission, no new token.

### Residual risks

As for derived files: `uv pip compile` can build a source distribution to read its metadata, so
code of a dependency can run in the `regenerate` job. It has no secret, and what it can change is
the content of the declared output of the very pull request, which the pull request's own CI sees
before it merges. The input file comes from the pull request and is not validated beyond its
path; a Dependabot branch with a changed `.in` file is resolved as it is.

## Options

Under `modules.lockfiles` in `.github/lktec-bot.yml`. Unknown keys are errors and deny every
verb. The policy is read from the default branch, so enabling the module and changing options
takes effect once merged.

| Option           | Values / default                                                      | Meaning                                                                                             |
| ---------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `ecosystems`     | list of `uv`, `dotnet`; default `[]` (auto-detect)                    | Which lock files may be committed. Empty: every supported ecosystem whose lock file the first job found changed. |
| `paths`          | list of repository paths; default `[]` (whole repository)             | Restrict to these files or directories (a directory covers everything below it).                    |
| `commit-message` | one line, at most 200 characters; default `chore(deps): refresh lockfiles after Dependabot bump` | The commit headline. `{number}` is replaced by the pull request number. The body lists the files. |
| `derived`        | list of generators (`name`, `run`, `paths`, `timeout-minutes`); default `[]` (none) | Files generated from the dependency graph, regenerated and committed with the lock files; see [Derived files](#derived-files). Needs a release that knows the key in every caller pin first ([rollout order](#rollout-order)). |
| `uv-pip-compile` | list of `input` and `output` paths; default `[]` (none)             | `uv pip compile` outputs to recompile and commit; opt-in per file, see [uv pip compile outputs](#uv-pip-compile-outputs). Needs a release that knows the key in every caller pin first ([rollout order](#rollout-order)). |

`ecosystems` and `paths` are applied by the `commit` job to the lock files, from the policy on
the default branch; the first job regenerates whatever it finds, as it must not read policy
from the pull request. `derived` and `uv-pip-compile` are read from the default branch by both jobs: by the first to
know what to run (from its git checkout, fetched from GitHub before any code of the pull
request runs), by the `commit` job through the API to decide what may be committed.

## Permissions

The workflow's own `GITHUB_TOKEN` has `contents: read` (and `actions: read` in the caller and in
the `source` job, to look up which commit of the workflow the run uses; the `commit` job no
longer needs it). Every call goes through
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
   A derived-file generator that needs a host outside the fixed list goes into
   `allowed-endpoints`.
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
