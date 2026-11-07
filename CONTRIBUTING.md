# Contributing to lktec-bot

Thanks for taking the time. This repository holds the core of a GitHub App bot: the policy
schema, the gate, the token minting and the verbs. Modules and add-ons that use the verbs live
elsewhere; keep this package small and generic (nothing specific to one owner, bot name or
repository is hard-coded here, everything comes from configuration or arguments).

## How changes get in

The maintainer aims to answer new issues and pull requests within 14 days.

1. Open an issue first for anything bigger than a typo, so the direction can be agreed before
   you spend time on it. A new verb starts with naming the one GitHub App permission it needs.
2. Fork the repository (or branch, if you have write access) and make your change on a branch.
3. Open a pull request against `main`.
4. `main` is protected: a PR merges only after `lint` and `test` are green, the branch is up to
   date with `main`, and a code owner has approved. Nobody pushes to `main` directly, not even
   the maintainer. Commits on `main` carry verified signatures: sign yours if you can (SSH or GPG);
   an unsigned commit from a fork is squash-merged by the maintainer with a recorded bypass after
   the review, so a missing signature does not block a contribution.

## What a pull request needs

- **Conventional Commits.** Commit and pull-request titles follow Conventional Commits
  (`feat:`, `fix:`, `docs:`, `ci:`, and so on); they drive semantic versioning once releases
  are switched on. Squash-merge keeps the PR title as the commit message.
- **Green checks.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests` and `uv run pytest` must pass; CI runs exactly these (see [Tests](#tests) and [Coding style](#coding-style)).
- **Architecture.** Read [`docs/architecture.md`](docs/architecture.md) first. Domain code does
  no I/O, adapters implement ports, every verb goes through the gate, and a verb never asks for
  more than the permission listed in `lktec_bot/domain/permissions.py`. The `workflows`
  permission is never requested.
- **Pin every action by SHA.** New or bumped `uses:` references carry the full commit SHA with
  the human-readable version as a trailing comment, for example
  `uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`.
- **Say what breaks.** A changed policy schema or verb signature affects every repository that
  opted in. Describe the migration in the PR body.

## Tests

- **New behaviour comes with tests.** A feature or a change of behaviour adds tests next to the code it changes, in `tests/`. A pull request that adds behaviour without a test is asked to add one.
- **Every defect fix comes with a regression test.** A fix of a defect in the bot's behaviour (`src/`, `actions/`) adds a test that fails without the fix and passes with it. Security fixes always get one. Exempt:
  - dependency updates (`fix(deps)`, Dependabot pull requests, lock file refreshes): the existing suite and the required checks cover them instead;
  - fixes without testable behaviour (documentation, CI or build configuration, templates): the pull request says why no test was added and how the fix was verified.
- **Input is bounded.** A new option, path or name gets a validator (anchored with `\Z`, not `$`) and a test for the rejection; `tests/test_properties.py` holds property-based tests (hypothesis) for the pure input rules.
- **No network.** Adapters are tested against mocked HTTP (respx) and the verbs against fakes (`tests/fakes.py`); the suite needs no GitHub App.

`uv run pytest` runs the suite with branch coverage; `fail_under` in `pyproject.toml` is the floor (coverage is 100 % today) and a pull request is expected not to lower it.

## Coding style

- **Python 3.13**, [PEP 8](https://peps.python.org/pep-0008/) as checked by ruff: the rule sets `E`, `F`, `I`, `UP`, `B`, `SIM`, `RUF` and `S` (flake8-bandit), line length 100, formatted with `ruff format`. Type hints everywhere, checked by mypy in strict mode (also for the tests). The configuration is in `pyproject.toml`; it is the style guide. A `# noqa` or `# type: ignore` names the rule and says why.
- **Workflows and shell:** every `uses:` is pinned to a commit SHA (`tools/check_yaml.py` enforces it), workflows start from least-privilege `permissions`.
- **Headers:** every Python and shell file starts with `# SPDX-FileCopyrightText: <year> <holder>` and `# SPDX-License-Identifier: Apache-2.0` after the shebang (`tools/check_headers.py` enforces it; `--fix` adds them).
- **Everything:** `.editorconfig` (UTF-8, LF, final newline), English, comments that explain why rather than what.

The required check `lint` runs ruff, ruff format, mypy, the YAML check and the header check, so a pull request with a lint warning cannot merge.

## Developer Certificate of Origin

By contributing you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change, or otherwise have the right to submit it under the project's licence (Apache-2.0). Sign off every commit with `git commit -s`, which adds a `Signed-off-by: Your Name <email>` line. Dependency and bot pull requests are exempt. No check enforces the sign-off today. Cryptographic signatures are welcome but not required from a fork (see above).

## Local setup

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/) are the only requirements. No GitHub
App is needed to run the test suite: adapters are tested against mocked HTTP.

## Security issues

Governance and how decisions are made: [GOVERNANCE.md](GOVERNANCE.md); planned work: [docs/roadmap.md](docs/roadmap.md).

Please do not open a public issue for a vulnerability - use the private reporting path described
in [SECURITY.md](SECURITY.md). The [Code of Conduct](CODE_OF_CONDUCT.md) applies to every
interaction in this repository.
