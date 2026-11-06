# Releases

How a release is made, how to verify one, and how far the build is reproducible.

## How a release is made

[`.github/workflows/self-release.yml`](../.github/workflows/self-release.yml) runs on every push to
`main`. It calls the reusable [`release.yml`](../.github/workflows/release.yml), the bot's own
[release module](modules/release.md): the Conventional Commit types since the last tag decide
whether the version moves (`feat:` minor, `fix:` patch, `!` or a `BREAKING CHANGE:` footer major;
0.x line while the modules are rolled out). The tag `vX.Y.Z` and the GitHub release with its notes
are created by the lktec-bot GitHub App through the GitHub API. **The tag is not signed.**

When a release was made, the job `publish-assets` builds the source distribution and the wheel
at the tag with `uv build --no-build-isolation` (the backend from `uv.lock`), signs each keyless with Cosign, attaches a GitHub build provenance
attestation and uploads everything to the release:

| File | What it is |
| --- | --- |
| `lktec_bot-<version>.tar.gz` | source distribution |
| `lktec_bot-<version>-py3-none-any.whl` | wheel |
| `<file>.sigstore.json` | Sigstore bundle (signature, certificate, transparency log entry) per file |
| `provenance.intoto.jsonl` | GitHub build provenance (SLSA) for both files |

Consumers use the composite actions and reusable workflows by commit SHA (`uses: ...@<sha> # vX.Y.Z`);
the assets are for people who install the package from a download and for audits.

## Verifying a release

The signing identity is the workflow on `main`:
`https://github.com/lukislp/lktec-bot/.github/workflows/self-release.yml@refs/heads/main`, issuer
`https://token.actions.githubusercontent.com`.

```bash
cosign verify-blob --bundle lktec_bot-<version>-py3-none-any.whl.sigstore.json \
  --certificate-identity https://github.com/lukislp/lktec-bot/.github/workflows/self-release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  lktec_bot-<version>-py3-none-any.whl

gh attestation verify lktec_bot-<version>-py3-none-any.whl -R lukislp/lktec-bot
```

The same for the `.tar.gz`. A file whose signature does not verify against this identity must
not be used; report it ([SECURITY.md](../SECURITY.md)). The tag itself cannot be verified by
signature: check that the commit it points at is the one the provenance names
(`gh attestation verify ... --format json` shows the source commit).

## Reproducible builds

What is built: the sdist and the wheel of this one package (`uv build`, backend hatchling with
hatch-vcs). Inputs: the commit (hatch-vcs takes the version from the tag, so a build from the tag
has the exact version), `pyproject.toml`, `uv.lock` (the backend is locked in the `build` group)
and the tool versions pinned in the workflows (uv by version, the actions by commit SHA).

**Result.** Two clean builds of the same commit are byte-identical, wheel and sdist, with the backend from `uv.lock` (`--no-build-isolation`, which is what the release and the check use) and with an isolated build that resolves the newest backend (the measurements below; the hashes were the same for the locked-backend build with `SOURCE_DATE_EPOCH`). Measured on
2026-11-07 at the tag `v0.3.0` in a clean `git worktree`, uv 0.12.24, CPython 3.13, Windows 11:

| Run | `SOURCE_DATE_EPOCH` | wheel sha256 (first 12) | sdist sha256 (first 12) |
| --- | --- | --- | --- |
| 1 | unset | `059407d8a2a6` | `23101430ec77` |
| 2 | unset | `059407d8a2a6` | `23101430ec77` |
| 1 | commit time | `fd252a091ca6` | `5a3607a5a3ff` |
| 2 | commit time | `fd252a091ca6` | `5a3607a5a3ff` |

Why the two settings differ: hatchling writes a fixed timestamp (2020-02-02) into the archives
when `SOURCE_DATE_EPOCH` is unset, and the commit time when it is set. The release job sets it to
the commit time, so a rebuild with `SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"` matches the
released files.

The job `reproducible-build` in [`ci.yml`](../.github/workflows/ci.yml) builds twice from the same
checkout on every pull request and fails on any difference (it is not a required check).

By hand:

```sh
git checkout v<version>
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
uv sync --locked --group build
uv build --no-build-isolation --out-dir /tmp/build-a
rm -rf dist build
uv build --no-build-isolation --out-dir /tmp/build-b
sha256sum /tmp/build-a/* /tmp/build-b/*
```

**Limits, honestly:**

- The working tree must be clean. hatch-vcs appends a date for a tree with modified tracked files
  (`+dYYYYMMDD`), which changes the version string and the files. An untracked or ignored
  file that hatch includes (sdist includes tracked and not-ignored files) changes the sdist.
- Build on Linux or macOS. Windows gave identical results for repeated builds here, but file modes
  in the sdist can differ between platforms; the check in CI runs on Linux.
- A much later rebuild resolves `hatchling` and `hatch-vcs` from `uv.lock` of the tag, so it
  matches as long as those versions stay downloadable from PyPI; uv itself is a tool version and
  a different uv could write different metadata.
- The Sigstore bundles and the provenance are new on every signing run and are not reproducible
  by design; compare the wheel and sdist, not the bundles.