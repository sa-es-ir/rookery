# Phase 1 handoff — GitHub App

**Date:** 19 September 2026
**Status:** prep done, no code written. The next session implements Phase 1 of
[PRD-Phases.md](PRD-Phases.md).

## Decisions (answers to PRD-Phases.md §1)

| Item | Decision |
|---|---|
| §1.1 licence check | Done — personal projects only. |
| §1.2 decisions | Accepted as written: App only (no fork flow), operator pays with caps, runs on own server invite-only, private repos in. |
| Scope | **Phase 1 only**, live-tested, before Phase 2. |
| Phase 3 caps (later) | 10 runs and $20 per repo per day; pause switch = a `PAUSED` file next to `agent.py`. |

## GitHub App (registered)

| Field | Value |
|---|---|
| App ID | `4999655` |
| Slug | `rookery-gha` — bot login `rookery-gha[bot]`, so `ME = "rookery-gha[bot]"` |
| Owner / install scope | `sa-es-ir`, "Only on this account" |
| Private key (local) | `C:\Users\Saeed\source\Keys-Rookery\rookery-gha.2026-09-19.private-key.pem` (PKCS#1, `BEGIN RSA PRIVATE KEY`) |
| Webhook URL | `http://185.208.172.15:8787/`, content type JSON |
| Webhook secret | New value, **not** the server's current `WEBHOOK_SECRET`. The user has it — never commit it. |
| Permissions | Contents RW, Issues RW, Pull requests RW, Metadata R |
| Events | Issues, Issue comment, Pull request (last one is for Phase 2) |
| Test repo | `sa-es-ir/NudgeMcp` — public, base `main`, installation id `162968573`, no per-repo webhook |

**Verified** by signing a throwaway JWT with `openssl` (the same approach
Phase 1 will use) and calling `GET /app`, `GET /repos/sa-es-ir/NudgeMcp/installation`
and `GET /app/hook/config` — all matched the table above.

The App's first deliveries (`ping`, `installation`) got **401** from the old
agent. That is intended: the secret differs, so the pre-Phase-1 code ignores App
events instead of acting on them with the personal `gh` login.

The JWT recipe that worked (bash, Git for Windows `openssl`):

```bash
b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
h=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
p=$(printf '{"iat":%d,"exp":%d,"iss":"4999655"}' $((now-60)) $((now+540)) | b64)
s=$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -sign "$PEM" -binary | b64)
# Authorization: Bearer $h.$p.$s
```

## Server

| Fact | Value |
|---|---|
| Host | `root@185.208.172.15`, hostname `srv6924501108`, Ubuntu, kernel 6.8, OpenSSH 9.6 |
| App | `/opt/rookery` (`agent.py`, `agents/`, `repos.json`, `work/`), systemd unit `rookery` — **active** |
| Tools | `openssl` 3.0.13, `gh`, `claude`, `python3` all in `/usr/bin` |
| `repos.json` | only `sa-es-ir/Llama` → `/home/rookery/repos/llama`, base `main` |

**SSH from this Windows box:** use Windows OpenSSH, not Git Bash's `ssh`.
`~/.ssh/id_rsa` has a passphrase and is loaded in the Windows `ssh-agent`
service (set to Automatic); Git Bash's `ssh` cannot reach that agent.

```powershell
& "$env:SystemRoot\System32\OpenSSH\ssh.exe" -o BatchMode=yes root@185.208.172.15 '<cmd>'
```

The key is in `/root/.ssh/authorized_keys` (the line ends in a harmless `\r`).

## Open items

- **SSH hardening — not yet approved.** Root is being password-brute-forced
  (`45.148.10.151/152`, `195.178.110.232`) and `PermitRootLogin yes` allows
  passwords. Proposed: `PasswordAuthentication no`, `PermitRootLogin
  prohibit-password`, reload sshd, confirm a second key session works before
  closing the first. Ask the user first.
- **NudgeMcp on the server:** needs a clone and a `repos.json` entry for the
  live test — `repos.json` only goes away in Phase 2.

## Phase 1 plan (agreed, not started)

1. **Code + offline tests** on branch `prd-implementation-phases`:
   - Mint a JWT via the `openssl` CLI, exchange it for a per-job installation
     token, and pass it to `gh`/`git` as `GH_TOKEN` through `env=` only —
     never `os.environ`, so the `claude` subprocess can't read it.
   - `ME` becomes `rookery-gha[bot]`; drop `gh api user` at startup.
   - `git push` authenticates with the installation token, not `gh auth setup-git`.
   - Tests sign with a throwaway key generated in the test; the suite stays offline.
   - Update `CLAUDE.md` and `deploy.md` where they describe `gh auth login` / the personal login.
2. **Copy the key** to `/opt/rookery/app.pem`, owner `rookery`, mode 600.
3. **Server `.env`:** App ID, key path, and the App's webhook secret as `WEBHOOK_SECRET`.
4. **Deploy and live test** on NudgeMcp: trigger → plan → `approve` → draft PR
   authored by `rookery-gha[bot]`, with no `gh auth` needed on the box.
5. Commit per phase.
