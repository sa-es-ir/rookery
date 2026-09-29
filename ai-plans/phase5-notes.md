# Phase 5 notes — control-plane API, portal, run log, cancel

**Date:** 19 September 2026
**Status:** built and tested locally (Windows, 127.0.0.1, the real App), then **deployed** on
29 September 2026 to https://rookery.codesimple.dev ([phase5-deploy.md](phase5-deploy.md)). Spec: [PRD-Phases.md](PRD-Phases.md) §2 Phase 5, brief in
[phase5-prompt.md](phase5-prompt.md). Follows [phase3-4-notes.md](phase3-4-notes.md).
Branch `phase5-local` off `prd-implementation-phases`.

## Result

All four "done when" items held in a real browser (Playwright) against the real App, on
`sa-es-ir/rookery-throwaway-2` issue #2 ("Phase 5 test: textkit helpers", 4 tasks):

| Time | Event |
|---|---|
| 16:14:28 | Local instance up with `REPOS_ONLY=sa-es-ir/rookery-throwaway-2`: onboarded that repo only |
| 16:14:52 | Plan job submitted in-process → plan-ready 16:15:32, `claude:awaiting-approval` |
| 16:15:47 | `approve` (in-process), `RUN_CAP=3` → tasks 1–2 committed; task 3 refused by the cap → comment + `claude:error` |
| 16:23:06 | `sa-es-ir signed in (6 repos in their installations)` — the portal listed **one**: installations ∩ `REPOS` |
| — | Run 2 in the portal: **"Failed at task 3 of 4"**, the log task by task, the cap error in full |
| 16:24:13 | Cap raised to 10, `approve` again → resumed at task 3 (tasks 1–2 not re-run) |
| 16:24:23 | **Cancel run** clicked in the portal 6 s into task 3 → `claude` killed, job over the same second |
| 16:24:23–26 | Issue #2: comment "Cancelled from the Rookery UI by @sa-es-ir." + `claude:error`; run 3 shows "Cancelled at task 3 of 4" |

After the cancel the worktree was clean at task 2's commit (the killed session had not
written yet), and no `claude` process outlived it. Usage read back: 4 runs, $0.53 — the
killed call counts as a run at $0 (its cost is lost with its stdout, as for any failed call).
`python test_agent.py` passes offline.

| Commit | What |
|---|---|
| `69da5e2` | Run log: `jobs` + `steps` in `rookery.db` |
| `1be3771` | `web.py`: API, GitHub login, sessions, static `ui/`, `REPOS_ONLY` |
| `12ca2d0` | Cancel |
| `256973e` | `ui/portal.html` wired to the API |
| `9800f88` | Docs |

## Decisions (brief placeholders, as resolved)

- Branch `phase5-local`. No smee: the App has one webhook URL (the server), so a local
  instance would receive nothing without a second App or a repo webhook. Jobs were started
  **in-process** by a scratch driver calling the same `submit()`/`handle()`; no trigger label
  or `approve` comment was ever put on GitHub, so the server's copy ignored the issue (it only
  saw its own bot's labels and comments, which it filters).
- Server-side session row, **8h**, keyed by sha256 of the cookie. HttpOnly, SameSite=Lax,
  `Secure` when `PUBLIC_URL` is https.
- `ThreadingHTTPServer` (a login's GitHub calls must not stall webhooks); `fresh()` got a lock.
- Cancel needs **write+** (`can_write()`, the same check as `approve`).
- CSRF: per-session token in `X-CSRF-Token` on every POST.
- Static files from `ui/` (flat whitelist). Split: `agent.py` + `web.py`; no `db.py` — the
  schema stayed in `agent.db()`.

## How it works

- **Run log.** `handle()` calls `begin()` only once it knows it will plan, reply or
  implement, so a no-op comment makes no row. `step()` appends; it is a no-op outside a job.
  Steps: `phase`, `task` (i, of, text), `claude` (key, status, usd), `tick`, `commit` (sha),
  `push`, `pr` (url), `error` (message). `finish()` sets ok / failed / cancelled; startup marks
  leftover `running` rows `interrupted`. The API derives "task i of N", cost and the last error
  from the steps.
- **Cancel.** `sh(track=repo)` starts the process under `alock` and registers it in
  `active[repo]`; `cancel()` takes the same lock, sets the flag and kills the tree
  (`taskkill /T /F` on Windows, `killpg` on POSIX, where tracked calls get their own session).
  A flagged job can't start another call (`sh` and `check_caps()` both refuse). `handle()`
  replaces the killed process's noise with "Cancelled from the Rookery UI by @login".
- **Login.** `/auth/login` sets a `state` cookie and redirects to GitHub; `/auth/callback`
  checks it, exchanges the code, lists `/user/installations` → their repositories with the
  user's token, stores the list in the session and drops the token.
- **`agent.py` as `__main__`** re-imports itself as `agent` before `main()`, so `web.py`'s
  `import agent` shares `REPOS`, `active` and the queues instead of a second copy.

## Tests added (`test_agent.py`, offline)

- Run log: a 3-task implementation failing at task 2 through the real `handle()` and git →
  one `failed` job, steps in order, "2 of 3", error logged and commented, nothing written outside a job.
- Login: redirect carries a state that must come back in the cookie (forged / missing → 400);
  callback with faked GitHub → HttpOnly/SameSite=Lax cookies, 8h; no token or raw session id in
  the DB, none in `os.environ`.
- Visibility: installations ∩ `REPOS`; another repo's run is a 404 in list, detail and cancel.
- CSRF on POST, logout ends the session server-side, expiry → 401.
- Static: `/`, `styles.css` served; `agent.py`, `../`, `%2e%2e`, `.env`, subpaths → 404.
- Webhook over a real socket on `web.Handler`: bad HMAC 401, redelivery answered but not acted on.
- Cancel: a real 60 s subprocess killed via the API in well under 10 s; carol (read-only)
  gets 403; status `cancelled`, the comment, `claude:error`; a second cancel → 409; a job
  cancelled between tasks refuses its next call.
- `REPOS_ONLY` filters `sync()`.

## Found along the way

- The Bash tool's heredocs mangle backslashes (`\n` became real newlines in generated Python);
  edit scripts went through files instead.
- The Windows console logs `—` as `�`; the data itself is fine (the issue body arrived intact).
- The GitHub App settings page may not show "Expire user authorization tokens"; it doesn't
  matter here, since the user token is dropped after login and the 8h limit is ours.

## Known limitations / open items

- **Deployed behind Cloudflare Flexible** (29 Sep 2026): the Cloudflare → server hop is
  plain http. The upgrade to Full (strict) is in [phase5-deploy.md](phase5-deploy.md).
- **Repo list is fixed per session** (8h). Losing access on GitHub is not seen until the next
  sign-in; cancel is still re-checked live with `can_write()`.
- **Cancel between the last claude call and the push** (during push/PR) doesn't stop the
  push — nothing is running to kill, and the work reaching GitHub is the right outcome anyway.
- **A cancelled task can leave partial edits** in the worktree if the agent had written before
  the kill; `approve` then resumes on top of them, as it already did after any failed task.
- Polling every 4 s while the tab is visible; no push channel.
- The user token isn't revoked after use; it expires on its own (8h where expiry is enabled).
- `rookery.db` grows without pruning (`jobs`, `steps`; sessions are swept at each login).
- Phase 1–4 open items still stand (phase3-4-notes.md "Next steps"), notably `app.pem`
  readable by the agent — which now also reads `rookery.db`; that is why it holds no tokens
  and only hashed session ids.

## Leftovers

- GitHub: `rookery-throwaway-2` issue #2 at `claude:error` with the plan, the cap error and the
  cancel comment. Branch `feature/issue-2` exists only locally (never pushed).
- Local: `work/` holds the clone, the issue-2 worktree and `rookery.db` (all gitignored);
  `app.pem` and the OAuth values are in the local `.env`. A second App private key was
  generated for this machine — delete it in the App settings when done testing locally.
- App settings: the callback `http://127.0.0.1:8787/auth/callback` stays until removed.
