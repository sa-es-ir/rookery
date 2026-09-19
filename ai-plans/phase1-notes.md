# Phase 1 notes — GitHub App

**Date:** 19 September 2026
**Status:** done, tested on the live server. Follows [phase1-handoff.md](phase1-handoff.md);
spec in [PRD-Phases.md](PRD-Phases.md) §2 Phase 1.

## Result

Test run on `sa-es-ir/NudgeMcp` issue #1: trigger → plan → `approve` → draft PR
[#2](https://github.com/sa-es-ir/NudgeMcp/pull/2), about 7 minutes end to end.
The comments, the push and the PR all came from `app/rookery-gha` (`is_bot: true`). The
server's `gh` login was not used.

| Commit | What |
|---|---|
| `c30f5bb` | GitHub App auth, tests, docs |
| `e0318d1` | Start a job on the trigger label only (duplicate-plan fix, below) |

## How auth works now

- `jwt()` builds an RS256 JWT (`iat` now−60s, `exp` now+9min, `iss` = `APP_ID`) and signs
  it with `openssl dgst -sha256 -sign $APP_KEY`, because the stdlib can't sign RSA.
- `token(repo)` sends `GET /repos/{repo}/installation`, then
  `POST /app/installations/{id}/access_tokens`. It caches the result per repo and gets a
  new one once less than 10 minutes remain. Tokens last 1 hour and an implementation can
  run longer, so every `gh`/`git` call asks for a token instead of reusing one from the
  start of the job.
- `auth(repo)` returns an `env=` dict for one subprocess call: `GH_TOKEN` for `gh`, plus
  `GIT_CONFIG_COUNT/KEY_0/VALUE_0` to set `http.https://github.com/.extraheader` to
  `AUTHORIZATION: basic base64(x-access-token:<token>)` for `git`. This keeps the token
  out of the process's arguments and out of `.git/config`.
- Every `gh()` call, `comment()`, `git fetch` in `worktree()`, `git push` and
  `gh pr create` go through `auth()`.
- **The token never goes into `os.environ`.** The `claude` subprocess inherits
  `os.environ`, and the deny rules block `gh` but not `curl` with a token. At import,
  `GH_TOKEN` and `GITHUB_TOKEN` are removed from `os.environ` for the same reason.
- The push goes to `https://github.com/<repo>.git` instead of `origin`, so the App's
  token is used even when origin is an ssh URL. `-u` is gone because nothing needed the
  upstream.
- The PR body is now sent through stdin (`--body-file -`) instead of argv.
- `ME = "rookery-gha[bot]"` is now a constant. The `gh api user` call at startup is gone.
  Instead, startup exits if `APP_ID` or the key file is missing.

New `.env` keys: `APP_ID`, `APP_KEY` (defaults to `./app.pem`). `WEBHOOK_SECRET` is
now the App's webhook secret. `*.pem` is added to `.gitignore`.

## Tests added (`test_agent.py`, still offline)

- The test generates a throwaway key (`openssl genrsa -traditional`) and checks the
  JWT's header and claims. It then verifies the signature with the public key.
- A fake `api` checks that a token is minted once, reused while fresh, and minted again
  when 5 minutes are left.
- `auth()` must produce a git header that decodes to `x-access-token:<token>`. After
  that, `GH_TOKEN` and `GIT_CONFIG_VALUE_0` must still be absent from `os.environ`.
- `opened` + `labeled(claude:trigger)` + `labeled(bug)` must queue exactly one start
  job.

## Bug found during the live test

Creating an issue that already has `claude:trigger` sends both `issues.opened` and
`issues.labeled`. Both arrive before the first job sets a phase label, so both passed
the "no phase label yet" check, and the issue was planned twice (two plan comments). This
was already true before Phase 1. Fix: start a job only on `labeled` when
`p["label"]["name"] == TRIGGER`.

## Server changes

- `/opt/rookery/app.pem` is owned by `rookery` with mode 600. `.env` is also now 600.
- `.env`: `WEBHOOK_SECRET` replaced, `APP_ID=4999655` and
  `APP_KEY=/opt/rookery/app.pem` added. The old file is kept as `.env.bak-pre-app`.
- NudgeMcp is cloned over https (using the App token as a one-off `git -c` header) to
  `/home/rookery/repos/nudgemcp` and added to `repos.json`.
- Llama keeps working: the App is installed on the whole account, so Llama events now
  arrive through the App.

**Gotcha:** piping the secret from PowerShell over `ssh` added CR/BOM bytes. The first
check failed; comparing SHA-256 hashes caught it, and it was fixed with
`sed 's/\r$//'`. Compare the hash after any secret you write to the server this way.

## Docs updated

- `CLAUDE.md`: new "GitHub auth is a GitHub App" section, plus two invariants (token
  only in `env=`, push to the https URL). `openssl` added to the required CLIs.
- `deploy.md`:
  - §0 and §5 rewritten for the App.
  - §3 now copies the key; §4 sets the file modes.
  - §8 clones and checks repos with a `tok` helper.
  - §11 no longer has a per-repo webhook.
  - The bot-ignores-comments gotcha is updated, and the minor gotchas are new.
- `README.md` and `.env.example`: the new keys.

## Known limitations / open items

- **The agent can read `app.pem`.** `claude` runs as the same user that owns the key,
  and the key can mint any token. The deny rules don't cover `openssl` + `curl`. Closing
  this needs the sandbox, or `claude` running as a separate user.
- **The server's `gh` is still logged in** as `rookery-agent` (`~/.config/gh/hosts.yml`).
  The code no longer uses it, but the agent could read that token. Needs a
  `gh auth logout` (not done yet).
- **Llama's old per-repo webhook** now gets 401s (wrong secret). It can be deleted.
- **SSH hardening** (from the handoff) still needs approval.
- Commits in the worktree still use the server's git identity (`rookery-agent`). Only
  the push and the PR are the App's.
- `ME` is hard-coded to `rookery-gha[bot]`; change it if the slug ever changes.
