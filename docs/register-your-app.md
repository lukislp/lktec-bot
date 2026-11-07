# Register your own GitHub App

lktec-bot is not tied to one bot identity. Anyone can register their own GitHub App, install it
on the repositories they choose, and run this package with that App's id and private key.
Nothing in the package assumes a particular App name, owner or repository, and **every user
registers their own App: the maintainer's App, id and key are never shared.** For the whole
path from zero see [setup.md](setup.md); this page is the App registration and key handling.

## 1. Create the App from the manifest (recommended)

[`manifest.json`](../manifest.json) in the repository root is a
[GitHub App manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
with exactly the permissions the bot needs and nothing else:

| Manifest field                          | Value                                   | Why                                                            |
| --------------------------------------- | --------------------------------------- | -------------------------------------------------------------- |
| `name`                                  | `REPLACE-ME-my-repo-bot`                | Placeholder; the name must be unique across GitHub.            |
| `url`                                   | this repository                         | Required by GitHub; any URL describing the App will do.        |
| `public`                                | `false`                                 | Only you can install it.                                       |
| `default_permissions.contents`          | `write`                                 | `enable-automerge`, `update-branch`, `commit`, `release`. Tokens are narrowed per verb; the App's maximum is not what a verb gets. |
| `default_permissions.pull_requests`     | `write`                                 | `comment`, `approve`, `enable-automerge`, `update-branch`, `open-pr`. |
| `default_permissions.metadata`          | `read`                                  | Always included by GitHub.                                     |
| `default_permissions.checks`            | `write`                                 | `check`. Not in `manifest.json` yet: add it on the App's settings page and accept it on every installation before running the verb. |
| `issues` (add on the settings page) | `write`                       | `open-issue`, and `comment` on an issue (`--target issue`). The manifest does not ask for it; add it when you use either, and accept it on the installation. |
| `request_oauth_on_install`              | `false`                                 | The bot has no user flow.                                      |
| `redirect_url`                          | `http://localhost/`                     | Where GitHub sends the one-time code (see below).              |
| no `hook_attributes`, no `default_events` | -                                     | The bot runs from workflows; it receives no events and has no webhook. |

**Never grant `workflows`**: the bot never requests it and has no use for it (see
[architecture.md](architecture.md)). Permissions for verbs that do not ship yet (`deployments: write`) can be added on the App's settings page later.

### The flow

The manifest is posted as a form field to GitHub; a plain link cannot carry it.

1. Edit `name` and open [`docs/register-app.html`](register-app.html) in a browser (it is a
   static copy of the manifest in a form; save the file locally if you are reading this on
   GitHub). Enter an organisation name to register the App there, or leave it empty for your
   personal account. Without the page, post the manifest yourself:

   ```html
   <form method="post" action="https://github.com/settings/apps/new?state=RANDOM">
     <input type="hidden" name="manifest" value='{"name": "...", ...}'>
     <button>Register</button>
   </form>
   ```

   For an organisation the action is
   `https://github.com/organizations/<org>/settings/apps/new?state=RANDOM`. `state` is an
   unguessable string you choose; GitHub returns it unchanged.
2. GitHub shows a confirmation page with the manifest's name. Click *Create GitHub App*.
3. GitHub redirects the browser to `redirect_url` with `?code=...&state=...`. With the
   manifest's `http://localhost/` nothing answers, which is fine: the **address bar** holds the
   temporary code. Compare `state` with the one you sent. The code works once and expires after
   one hour.
4. Exchange the code for the App's credentials (the endpoint needs no authentication; `gh` just
   supplies a token):

   ```sh
   gh api -X POST /app-manifests/<code>/conversions > app.json
   jq .id app.json                 # the App ID
   jq -r .pem app.json > bot.pem   # the private key
   ```

   The response carries the App `id`, `slug`, `pem` (the private key), `client_id`,
   `client_secret` and `webhook_secret`. The bot needs only `id` and `pem`: **the client secret
   and webhook secret are not needed** (no OAuth, no webhook). Delete `app.json` right away.
5. Turn the PEM into the secret (the whole file, including the BEGIN and END lines), and the
   id into the second secret, then delete `bot.pem`:

   ```sh
   gh secret set LKTEC_BOT_PRIVATE_KEY --repo owner/name < bot.pem
   gh secret set LKTEC_BOT_APP_ID --repo owner/name --body <app id>
   ```

   For many repositories use organisation secrets (`--org <org> --visibility selected --repos
   ...`). Keep a copy of `bot.pem` only in a password manager. Without `jq`, copy `id` and the
   `pem` value out of `app.json` by hand; the JSON string holds the PEM with `\n` escapes, which
   the bot accepts as well.

### What the manifest cannot do

- **Install the App.** A manifest only registers it; section 2 below installs it on repositories.
- **Choose repositories, set secrets, or touch rulesets.** Those stay manual (for the `sync`
  module the App must also become a bypass actor, see [setup.md](setup.md)).
- **Carry the key without the conversion step.** The private key exists only in the conversion
  response (or by generating one on the App's page). If you skip `redirect_url` or lose the
  code, generate a key on the settings page, as below.
- **Pick a free name.** If the name is taken GitHub asks you for another one on its page.

### Alternative: pre-filled query string

Without the manifest, a pre-filled form works too (replace `name` and `url`; for an
organisation use `https://github.com/organizations/<org>/settings/apps/new?...`):

```
https://github.com/settings/apps/new?name=my-repo-bot&url=https://github.com/you/your-repo&webhook_active=false&public=false&contents=write&pull_requests=write
```

Keep "Where can this GitHub App be installed?" at *Only on this account* and submit. Then, on
the App's settings page, note the **App ID** and under *Private keys* click *Generate a private
key*; GitHub downloads a `.pem` once and it cannot be downloaded again.

Store the key in a secret store (a password manager, or the *Secrets* of GitHub Actions); never
commit it (the repository `.gitignore` ignores `*.pem`) and delete the downloaded file once it
is stored. A key can be revoked and regenerated on the same page at any time.

## 2. Install the App on selected repositories

*Install App* in the left menu, pick your account, choose **Only select repositories** and
tick the repositories that will use the bot. Each of those repositories additionally needs
`.github/lktec-bot.yml` with at least one module enabled; without the file every verb is
denied, even on repositories the App is installed on.

## 3. Give the bot its identity

The core reads the App credentials from the environment:

| Variable                     | Content                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| `LKTEC_BOT_APP_ID`           | The App ID (a number).                                         |
| `LKTEC_BOT_PRIVATE_KEY`      | The PEM text. Real newlines or literal `\n` both work.         |
| `LKTEC_BOT_PRIVATE_KEY_FILE` | Alternatively, a path to the `.pem` file.                      |
| `LKTEC_BOT_API_URL`          | Optional. API base URL of a GitHub Enterprise Server instance. |
| `LKTEC_BOT_TOKEN_PROVIDER`   | Optional. `app` (default); an unknown name is an error.        |
| `LKTEC_BOT_CALLER_TOKEN`     | Optional. A token used only to read the policy file.           |

The reusable workflows take both as Actions *secrets* (LKTEC_BOT_APP_ID, LKTEC_BOT_PRIVATE_KEY, see [setup.md](setup.md)). For a job that runs the CLI itself, the App ID may be a *variable* instead and the key a *secret*:

```yaml
jobs:
  ping:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
      - uses: astral-sh/setup-uv@<sha> # vX.Y.Z
      - run: uv sync --locked
      - run: uv run lktec-bot ping --repository "${{ github.repository }}" --module ping
        env:
          LKTEC_BOT_APP_ID: ${{ vars.LKTEC_BOT_APP_ID }}
          LKTEC_BOT_PRIVATE_KEY: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
```

The repository's `.github/lktec-bot.yml` needs `ping: {enabled: true}` under `modules` for
this. The job summary then shows `OK ping on you/your-repo (ping): pong from you/your-repo`,
or the reason it was denied (exit code 3).

Without a checkout of this repository, use the composite actions instead (pinned by commit
SHA; see [`actions/README.md`](../actions/README.md)):

```yaml
      - uses: lukislp/lktec-bot/actions/setup@<sha> # vX.Y.Z
      - uses: lukislp/lktec-bot/actions/ping@<sha> # vX.Y.Z
        with:
          app-id: ${{ vars.LKTEC_BOT_APP_ID }}
          private-key: ${{ secrets.LKTEC_BOT_PRIVATE_KEY }}
```

## 4. What happens on every run

1. The core signs a short-lived JWT (RS256, 9 minutes, `iss` = App ID) with the private key.
2. It looks up the installation for the target repository
   (`GET /repos/{owner}/{repo}/installation`).
3. It mints an installation token for that one repository, narrowed to the verb's permission
   (`POST /app/installations/{id}/access_tokens` with `repositories` and `permissions`), and
   checks that the permissions and repositories GitHub reports for the token are no more than
   that; in Actions the token value is masked (`::add-mask::`) right away.
4. The verb runs. The token is revoked (`DELETE /installation/token`) whether or not it
   succeeded; a revoke that fails is reported as a warning, the result stands.

Installation tokens expire after one hour on their own; revoking them early is defence in
depth, not the only safeguard.

## Rotating or revoking

- **Rotate the key:** generate a new one on the App page, update the secret, then delete the
  old key on the same page. There is no downtime: both keys are valid until you delete one.
- **Pull the plug on one repository:** set `enabled: false` in its `.github/lktec-bot.yml`, or
  uninstall the App from that repository.
- **Pull the plug everywhere:** delete the private key on the App page.
