# The `release` module

A push to the default branch is turned into a tag and a GitHub release, by the App. The next
version is worked out from the Conventional Commits since the last tag, exactly as
semantic-release does it with the commit analyzer, the release notes generator and the GitHub
plugin; the credential that pushes the tag is not a per-repository SSH deploy key any more but
an installation token for this one repository, narrowed to `contents: write`, minted for the
duration of one API call.

It ships as

- the command `lktec-bot release run`
  ([`application/modules/release/`](../../src/lktec_bot/application/modules/release)),
- the verb `release` (create the release, and with it the tag),
- a reusable workflow, [`.github/workflows/release.yml`](../../.github/workflows/release.yml),
- a caller template, [`templates/release.yml`](../../templates/release.yml), and a policy
  snippet, [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml).

Only **mode `tag`** exists in this version. Mode `pr` (see [below](#what-mode-pr-will-add)) is
planned; the policy file is rejected when it asks for it.

## What it does

### `lktec-bot release run --repository owner/name [--sha SHA] [--dry-run]`

1. **The head.** Through the gate (`Gate.read`, a token narrowed to `contents: read`) the head of
   the configured `branch` is resolved. When `--sha` is given and the branch has moved on since
   (the head is another commit), the run is `skipped` with exit 0: the push that moved it has a
   run of its own.
2. **The last tag.** The tags matching `tag-format` are listed
   (`git/matching-refs/tags/<prefix>`, paginated). The part where `{version}` stands must be a
   strict `X.Y.Z` (no leading zeros, no prerelease, no build metadata); everything else is
   ignored. The highest version wins. `compare/<tag>...<head>` must say `ahead` or `identical`;
   `diverged` or `behind` is an error (the history does not contain the last release). The
   commits come from that comparison, page by page (`per_page=100`, the compare maximum, de-duplicated by sha); when GitHub reports more
   commits than could be read the run fails instead of releasing from a partial list.
   With no matching tag at all, every commit of the branch up to the head counts and the first
   release has the version `first-version`.
3. **The bump.** Each commit is parsed and classified (below); the highest bump wins. Nothing
   releasable is the decision `none` with exit 0.
4. **The notes** are rendered in semantic-release's style (below).
5. **The release.** The verb `release` (token narrowed to `contents: write`) creates the GitHub
   release `tag`, named like the tag, with the notes as its body, as the latest release, at the
   head commit.

With `--dry-run` everything up to and including the notes happens, the version and the notes are
printed, and no write token is minted.

Outputs (`$GITHUB_OUTPUT`) and exit codes are the same for the real run and the dry run:

| Output     | Values                                                                                  |
| ---------- | --------------------------------------------------------------------------------------- |
| `result`   | `ok`, `denied`, `error`                                                                 |
| `decision` | `released`, `would-release` (dry run only), `none`, `skipped`, `denied`, `error`        |
| `version`  | the version, empty when there is nothing to release                                     |
| `tag`      | the tag, empty when there is nothing to release                                         |
| `url`      | the release's URL (real run only)                                                       |
| `reason`   | one line saying why; `message` carries the same text                                    |

Exit codes: 0 for `released`, `would-release`, `none` and `skipped`, 1 for `error`, 3 for
`denied`. A summary line is written to the job summary. A denied run therefore fails the job of
the reusable workflow, unlike the composite actions, which succeed with `result=denied` unless
`fail-on-denied` is set.

### Commits

The first line of a commit message is matched with `^(\w+)(?:\(([^)]*)\))?(!)?: (.+)$`
(type, scope, breaking mark, subject). A commit is **breaking** when the header has the `!` or a
line of the body starts with `BREAKING CHANGE:` or `BREAKING-CHANGE:` (the lines up to the next
blank line belong to the note). Merge commits (`Merge pull request`, `Merge branch`) and headers
that do not match contribute nothing. A squash-merge commit is analysed by its headline, like
semantic-release does; its trailing ` (#123)` stays in the subject and becomes a pull request link
in the notes.

The bump of a commit is the **first** rule of `rules` whose conditions all match (`type`,
`scope`, `breaking`; an omitted condition matches anything), else the defaults:

| Commit                    | Bump  |
| ------------------------- | ----- |
| breaking                  | major |
| `feat`                    | minor |
| `fix`, `perf`, `revert`   | patch |
| anything else             | none  |

`release: none` in a rule stops the search: the commit contributes nothing, whatever the
defaults would say. Version arithmetic is plain semver, also below `1.0.0`.

### Release notes

```
## [1.4.0](https://github.com/owner/name/compare/v1.3.2...v1.4.0) (2026-11-06)

### Features

* **api:** add the endpoint ([#42](https://github.com/owner/name/pull/42)) ([abcdef1](https://github.com/owner/name/commit/abcdef1...))

### BREAKING CHANGES

* **api:** the old endpoint is gone
```

The headline links to the comparison with the previous tag (the first release links to the tag's
tree) and carries the date (UTC). A commit is listed when its type has a heading in `sections`
(by default `feat: Features`, `fix: Bug Fixes`, `perf: Performance Improvements`,
`revert: Reverts`) or when it moves the version through a rule; the latter without a heading go
under "Other changes". Each `BREAKING CHANGE` note, or the subject of a commit marked with `!`
only, is listed under `BREAKING CHANGES`. The text is a pure function of the commits: the same
input gives the same notes. The notes carry the commit subjects as written, like semantic-release
did (no markdown escaping), so an `@mention` in a commit subject is a mention in the release.

### `lktec-bot release --repository owner/name --module release --tag T --sha SHA --name TITLE`

The verb, usable on its own (`--body` or `--body-file`, `--latest/--no-latest` (default
latest), `--prerelease`). It calls `POST /repos/{o}/{r}/releases` with
`target_commitish = SHA`, and GitHub creates the lightweight tag at that commit when the tag does
not exist. Idempotent, and a tag is never moved:

| State                                            | Result                         |
| ------------------------------------------------ | ------------------------------ |
| release exists, tag points at `SHA`              | `ok`, decision `unchanged`     |
| tag points at `SHA`, no release                  | release created, `created`     |
| neither exists                                   | release and tag created        |
| tag points at another commit                     | `error`, nothing is written    |
| release exists but the tag does not              | `error`                        |

A prerelease is never marked as the latest release. Outputs: `result`, `decision`
(`created`, `unchanged`, `denied`, `error`), `message`, `url`, `tag`. The composite action is
[`actions/release`](../../actions/README.md).

## Options

Under `modules.release` in `.github/lktec-bot.yml`. Unknown keys are errors and deny every verb.

| Option         | Values / default              | Meaning                                                                 |
| -------------- | ----------------------------- | ----------------------------------------------------------------------- |
| `mode`         | `tag` (default and only value) | `pr` is rejected with a message saying it is not available yet          |
| `tag-format`   | `"v{version}"`                | The tag name; `{version}` exactly once, letters, digits and `. _ / @ -` around it |
| `branch`       | `main`                        | The branch releases are cut from (set it for `master`)                  |
| `first-version`| `"1.0.0"`                     | The version when no tag matches `tag-format` yet                        |
| `rules`        | `[]`                          | List of `{type?, scope?, breaking?, release: major\|minor\|patch\|none}`, a rule needs at least one condition |
| `sections`     | `{}`                          | Commit type to heading, merged over the defaults                        |

## How today's semantic-release setup maps

Reference: the `release.yml` of `ci-workflows`, its `semantic-release` action and the
`.releaserc.json` of the repositories.

| semantic-release                                              | Here                                                                 |
| ------------------------------------------------------------- | -------------------------------------------------------------------- |
| `@semantic-release/commit-analyzer`                           | `commits.py`: header pattern, breaking notes, rules, defaults        |
| `releaseRules` (`breaking`, `type`, `scope`, `release`)       | `rules` (`release: false` is `release: none`)                        |
| `@semantic-release/release-notes-generator`                   | `notes.py`, same layout; `sections` for the headings                 |
| `@semantic-release/github`                                    | the `release` verb: tag and release in one API call                  |
| `tagFormat: "v${version}"`                                    | `tag-format: "v{version}"`                                           |
| `branches: ["main"]`                                          | `branch: main`                                                       |
| no changelog file, no version file, no `@semantic-release/git`| the same: nothing is committed (that is mode `pr`)                   |
| deploy key, to push past the tag ruleset                      | none: the ruleset forbids deleting and moving tags, not creating them; the App creates the tag through the API |
| the retry when the branch moved ("behind the remote")         | not needed, see below                                                |

A typical `.releaserc.json` becomes

```yaml
modules:
  release:
    enabled: true
    rules:
      - {breaking: true, release: minor}
      - {type: refactor, release: patch}
      - {type: fix, scope: ci, release: none}
```

Differences worth knowing: several matching `releaseRules` give semantic-release the **highest**
release, here the **first** matching rule decides (put the specific rules first); `scope` and
`type` match exactly, no globs; `revert` is a patch by default, as in semantic-release's Angular
rules; a `1.0.0` first release is
`first-version`, not a prompt.

### Why the race retry is gone

semantic-release checks out the branch, works out the version and pushes the tag; when another
push landed in between, the push fails and the old workflow diagnoses and retries. Here nothing is
pushed from a checkout. The run is given the commit of its own push (`--sha ${{ github.sha }}`)
and compares it with the branch head when it reads: if the branch moved on, the run is `skipped`
and the run of the newer push (which sees both pushes' commits) cuts the release. If the branch
moves *after* that read, the release is still made at the commit that was analysed and the newer
run finds the new tag as its starting point. A second run that races the first onto the same
version finds the tag at the same commit and the release verb answers `unchanged`; if the tag
points elsewhere the run fails and moves nothing.

## Release chains

Some repositories need the next version **before** the tag exists: build an image tagged with it,
scan it, deploy it, and release at the end (a `get-version` job, the build jobs, a
`semantic-release` job). The reusable workflow serves that directly. It has the outputs
`released` (`"true"` when a release was made or, with `dry-run`, would be made), `decision`,
`version`, `tag` and `url`:

```yaml
jobs:
  version:
    uses: lukislp/lktec-bot/.github/workflows/release.yml@<sha> # vX.Y.Z
    with:
      dry-run: true
    secrets:
      app-id: ${{ secrets.LKTEC_BOT_APP_ID }}
      private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}

  build:
    needs: version
    if: needs.version.outputs.released == 'true'
    # ... build and push an image tagged ${{ needs.version.outputs.version }}

  release:
    needs: [version, build]
    if: needs.version.outputs.released == 'true'
    uses: lukislp/lktec-bot/.github/workflows/release.yml@<sha> # vX.Y.Z
    secrets:
      app-id: ${{ secrets.LKTEC_BOT_APP_ID }}
      private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
```

The first call replaces the `get-version` job; the last one replaces the
`semantic-release` job. The second run recomputes the version from the same head and refuses
(`skipped`) when the branch moved on in the meantime, exactly like the "behind the remote" guard
of today; the next push's chain releases then. Because the version comes from the same commits,
the tag the last job creates is the one the image was built for.

Runs of the release workflow queue up per branch (`concurrency` group
`lktec-bot-release-<repository>-<ref>`, nothing is cancelled while it runs). A queued run that a
newer one replaces is cancelled by GitHub, which is fine: the newer push's run releases. Without
this, two pushes close together could let the older run create its tag after the newer run had
already released.

The workflow finds the commit it was called at among the workflows the run references, and only
trusts an entry of the repository in its `bot-repository` input (default `lukislp/lktec-bot`;
change it only in a fork). A chain that still references another repository's `release.yml` is
therefore never mistaken for this one.

## Permissions

The workflow's own `GITHUB_TOKEN` has `contents: read` and `actions: read` (`actions: read` lets
the run look up which commit of the reusable workflow it was called at, to install the bot from
exactly that commit; the caller grants both). Every call goes through the gate with its own
token, narrowed to one repository:

| Step                                          | Token                |
| --------------------------------------------- | -------------------- |
| policy (every call)                           | `contents: read`     |
| head, tags, compare, commits (`release run`)  | `contents: read`     |
| `release` (tag and GitHub release)            | `contents: write`    |

No `workflows` permission is ever requested (see [architecture](../architecture.md)): a release
does not need to change a workflow file, and a bot that could would be able to change its own
gate.

## Setting up a repository

1. **Install the App** on the repository (see [register-your-app](../register-your-app.md)).
2. **Two secrets**: `LKTEC_BOT_APP_ID` and `LKTEC_BOT_PRIVATE_KEY`, repository or organization
   level, as *Actions* secrets (a push run reads those).
3. **The policy file** on the default branch: add the `release` block of
   [`templates/lktec-bot.yml`](../../templates/lktec-bot.yml) to `.github/lktec-bot.yml`, set
   `enabled: true`, `branch` (for `master`), `tag-format` (as in `.releaserc.json`'s `tagFormat`),
   and translate the `releaseRules` into `rules`. Check it with
   `lktec-bot policy validate .github/lktec-bot.yml`. Merge it **before** the workflow runs: the
   bot reads the policy from the default branch only.
4. **The caller workflow**: copy [`templates/release.yml`](../../templates/release.yml) to
   `.github/workflows/release.yml` (replacing the old caller of `ci-workflows`) and replace
   `<sha>` with a commit of this repository.
5. **Try it**: run the workflow by hand (*Run workflow*) or open a pull request that adds a
   `dry-run: true` job; the log and job summary show the version and the notes.
6. **Retire the deploy key.** Once the first release went through, delete the
   `SEMANTIC_RELEASE_DEPLOY_KEY` secret, remove the deploy key in *Settings > Deploy keys*, and
   delete `.releaserc.json`.
7. **Keep the tag ruleset as it is.** It forbids deleting and moving tags, which is what the
   bot relies on; it does not forbid creating them.

## Dogfooding

This repository releases itself with the module
([`.github/workflows/self-release.yml`](../../.github/workflows/self-release.yml), policy block in
[`.github/lktec-bot.yml`](../../.github/lktec-bot.yml)): every push to `main` that carries a
`feat` or `fix` commit becomes a tag `vX.Y.Z` and a GitHub release, which is what callers pin.
The line starts at 0.1.0 (`first-version`) while the modules are being rolled out. The `uses:`
line pins a commit of `main`, like the other dogfood callers; move it by hand when a change to
the reusable workflow or to the module should take effect here.

## What mode `pr` will add

A second mode, not built yet: instead of tagging on every push, the module keeps one release pull
request up to date (the changelog and the version files computed from the commits, opened and
updated with the verb `open-pr` and signed commits from the `commit` verb with `--from`), and
publishes the tag and the release when that pull request is merged. It is for repositories that
want a reviewed changelog or a version in a file. `open-pr` and `commit --from` ship now so
that it, and add-ons, can be built on them; `open-pr` is not part of any module's verb set yet.

## Limits

- No prereleases: a tag whose version part has a `-rc.1` suffix is ignored, and the module never
  creates one. (The verb can mark a release as a prerelease; the module does not.)
- No maintenance branches: one `branch`, one line of versions.
- No monorepo: one `tag-format` per repository; a repository that releases several packages needs
  one that distinguishes them (and a module instance per package, which does not exist).
- The commit list is bounded by what the compare API serves; a history of more than the API's
  ceiling since the last tag fails the run instead of releasing from part of it.
- Draft releases, assets and release discussions are not created.
