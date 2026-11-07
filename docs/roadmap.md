# Roadmap

What the project intends to work on over the next twelve months (until the end of 2027). It is a
statement of direction from the maintainer ([GOVERNANCE.md](../GOVERNANCE.md)), not a promise with
dates. Priorities change, and the reasons go into the linked issues or pull requests. Requests are
welcome as [issues](https://github.com/lukislp/lktec-bot/issues).

## Phases

| Phase | Content | State |
| --- | --- | --- |
| 0 | Core: policy schema, permission table, gate, token minting, HTTP adapter, CLI, `ping` and the live end-to-end workflow | done |
| 1 | The first verbs and composite actions (`comment`, `approve`, `enable-automerge`, `update-branch`) and the `automerge` module that replaces a personal-access-token workflow | done |
| 2 | `commit`, `release` and `open-pr`; the `lockfiles`, `release` and `sync` modules, so lock files, releases and deploy bumps no longer need a personal token, a deploy key or a signing key | done |
| 3 | Releases of this repository itself (dogfooding), reusable workflows and caller templates, the first pilot repositories | done |
| 4 | **Use it for your own repositories**: setting the App up from a manifest, documentation and templates a stranger can follow, signed release assets, the best-practices badge | done |

## Next twelve months

- **Phase 4 is done** (2026-11-07): App registration from a manifest
  ([docs/register-your-app.md](register-your-app.md)), caller templates for every module, signed
  release assets ([docs/releases.md](releases.md)) and the OpenSSF Best Practices silver badge.
  What is left of it is maintenance: keep the guides in step with the modules.
- **Release mode `pr`**: a reviewed release pull request with changelog and version files, for
  repositories that do not want a tag straight from `main` ([release module](modules/release.md)).
- **More verbs where a module needs them**, each with exactly one App permission and an entry in
  the verb table: `deployment` is listed there but has no handler yet.
  `workflows` is never requested and there is no `merge` verb.
- **Signed release tags.** Today the tag is created by the App through the API and is not signed;
  the signed assets are what to verify. Revisit when GitHub can sign tags created that way.
- **Other token providers** behind the existing `TokenProvider` port (today only the GitHub App
  provider exists).
- Keep the OpenSSF Scorecard at its level or higher, and reach and keep the OpenSSF Best
  Practices passing and silver badges.

## Project continuity

- Keep the mitigations in [GOVERNANCE.md](../GOVERNANCE.md#continuity-what-happens-if-the-maintainer-is-unavailable)
  true: a build anyone can repeat, keyless signing, and no secret a fork would need except its own
  App.
- Look for a second maintainer or a GitHub account successor.

## Maintenance (continuous)

- Dependency updates through Dependabot, merged after green checks. No known vulnerability of
  medium or higher severity stays unfixed for more than 60 days ([SECURITY.md](../SECURITY.md)).
- The test suite stays green and coverage does not drop (`fail_under` in `pyproject.toml`).
- Documentation changes with the code, in the same pull request.