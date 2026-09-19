# Phase 3–4 notes — approval and caps per repo, parallel repos

**Date:** 19 September 2026
**Status:** done, tested on the live server. Spec in [PRD-Phases.md](PRD-Phases.md)
§2 Phases 3–4; follows [phase2-notes.md](phase2-notes.md).

## Result

Live test on `sa-es-ir/rookery-throwaway` (issue #2, 3 tasks) and
`sa-es-ir/rookery-throwaway-2` (new, public, issue #1), both added to the installation
and onboarded in parallel (each cloned on its own worker thread, both live at 11:47:19).

| Time | Event |
|---|---|
| 11:47:33 | throwaway#2 `job: start` → plan-ready at 11:47:57 (1 run, $0.074 recorded) |
| 11:48:17 | `RUN_CAP=3` added to `.env`, restart |
| 11:48:41 | `approve` from `sa-es-ir` (admin) → passes the write check → `job: continue`, implementing |
| 11:48:49 | throwaway-2#1 `job: start` **while throwaway#2 implements**, planning 11:48:51 |
| 11:48:57 | throwaway#2 task 1 done + committed |
| 11:48:58 | Redelivered webhook `151f4500…` → `duplicate delivery … ignored` |
| 11:49:06 | throwaway#2 task 2 done + committed; next call refused: `hit its daily cap: 3/3 runs, $0.21/$20.00` → comment + `claude:error`. Task 3 untouched, tree clean. |
| 11:49:09 | Redelivered throwaway-2's `claude:trigger` delivery `136c64e0…` → `duplicate delivery … ignored`, no second job |
| 11:49:19 | throwaway-2#1 plan-ready, awaiting-approval |
| 11:49:31 | `RUN_CAP` removed from `.env` (default 10), restart |
| 11:50:06 | `approve` again (see the 502 below) → resumed at task 3 → review-ok → draft PR [#3](https://github.com/sa-es-ir/rookery-throwaway/pull/3) by `app/rookery-gha`, `claude:done` |

Usage after the test, read back from `rookery.db` across two restarts: throwaway
(5 runs, $0.34) = plan + 3 tasks + review; throwaway-2 (1 run, $0.08). The refused call
is not recorded — the cap check runs before anything is spent.

The two jobs overlapped (throwaway-2 planned 11:48:49–11:49:19 while throwaway ran
11:48:41–11:49:08). Under Phase 2's single queue, throwaway-2's `job: start` could not
have been logged before 11:49:08.

| Commit | What |
|---|---|
| `4b38214` | Phase 3: approval and caps per repo |
| `1a49039` | Phase 4: parallel repos |
| `3c47fd0` | Worker threads named after their repo, shown in every log line |
| `5066a43` | deploy.md: read usage with python3 (the box has no `sqlite3` CLI) |

## Decisions (from the session brief)

- Approval **and** replies need write/maintain/admin on *that* repo. This replaces `ALLOWLIST`.
- A run is **one `claude` call**. The caps are 10 runs and $20 per repo per **UTC** day.
- Cost is `total_cost_usd` from `claude --output-format json`, stored in
  `$WORKSPACE/rookery.db` with one connection per call.
- Pause: a `PAUSED` file next to `agent.py`.
- A cap hit mid-implementation **finishes the current task and stops**.
- Parallelism: one queue and worker per repo, created lazily. Onboard and offboard go
  through the repo's queue.
- Dedupe is on `X-GitHub-Delivery`, in memory and bounded.
- Offboarding **stops the worker once its queue drains**.
- The non-collaborator approve test was **offline only**.

## How it works

- `can_write(repo, user)`: `GET /repos/{repo}/collaborators/{user}/permission` with the
  repo's installation token (the App's Metadata: read covers it). It returns true for
  `admin` or `write`; maintain shows up as `write` in that field. Any HTTP error denies.
  `enqueue()` checks it last for `issue_comment`, after the `ME` filter and the phase
  label, so most comments never make the API call.
- `check_caps(repo)` runs at the top of `claude()`. It raises on `PAUSED` or on
  `runs >= RUN_CAP or usd >= USD_CAP` for today (UTC, `time.gmtime`). The raise goes
  through the normal `handle()` error path: an issue comment, then `claude:error`.
  `implement()` commits every task before the next `claude()` call, so a cap stops
  between tasks, and `approve` resumes at the first unticked one.
- `claude()` records one row `(repo, day, usd)` in a `finally`, so failed calls count as
  runs. A failed call's cost is recorded as 0 because it is lost with its stdout. The
  "session already in use" retry is still one run.
- `RUN_CAP` / `USD_CAP` can be overridden in `.env`. Both are global values applied per
  repo.
- `submit(job)` puts the job on `queues[repo]` under `qlock` and starts the repo's worker
  on its first job. After an `offboard` job, the worker exits under the same lock if its
  queue is empty. A job submitted at that moment either lands before the check, so the
  worker keeps going, or after the delete, so it gets a fresh worker.
- `fresh(delivery)` holds the last 1000 ids in an `OrderedDict`. Only the HTTP thread
  touches it, since `HTTPServer` serves one request at a time. The check runs after the
  HMAC check and after the 204. **Live-confirmed: a redelivery carries the same GUID**
  (the deliveries API lists it with `redelivery: true`).
- Log lines show `[threadName]`, which is the repo on a worker, so interleaved `#n` lines
  stay attributable.

## Tests added (`test_agent.py`, still offline)

- `can_write` over a fake permission API: admin/write pass, `read` fails, admin on one
  repo is nothing on another, and a 404 denies. Through `enqueue()`, only the
  collaborator's `approve` is queued; a non-collaborator's, another repo's admin's and
  `ME`'s are dropped.
- Caps in a temp SQLite DB: the run cap, the dollar cap, one repo's spend not counting
  against another, yesterday's rows ignored, and `PAUSED`.
- `claude()` with a fake `sh`: a successful call records its cost, a failed call is
  recorded as a run, and at the cap the next call raises before `sh` is ever invoked.
- Real threads with a fake `handle`: a blocked job on `o/slow` does not delay `o/fast`,
  order within a repo is kept, `offboard` stops only that repo's worker, and a re-added
  repo gets a fresh one.
- `fresh()`: a duplicate is rejected, a missing id is always fresh, and the bound evicts
  the oldest id.

## Server changes

- Backups: `agent.py.bak-pre-phase3`, `test_agent.py.bak-pre-phase3`, `.env.bak-pre-phase3`.
- `ALLOWLIST` was removed from `.env` because nothing reads it now. `RUN_CAP=3` was added
  for the test and then removed, so the defaults (10, $20) are live.
- `/opt/rookery/work/rookery.db` is created by the service (owner `rookery`). Read it
  as the `rookery` user: `sudo -u rookery python3 -c "import agent; print(agent.usage('OWNER/REPO'))"`
  from `/opt/rookery`.
- New repo `sa-es-ir/rookery-throwaway-2` (public, README only). It and
  `rookery-throwaway` are now in the installation, so six repos are live.

## Found during the live test

- **Webhooks are lost across a restart.** Startup `sync()` takes about 20s (it mints a
  token, then clones and labels each of six repos) before the server listens. A comment made
  in that window got a **502**, and GitHub does not retry on its own. The fix used here:
  App settings → Recent Deliveries → Redeliver, or `POST /app/hook/deliveries/{id}/attempts`.
  That delivery's GUID was never seen, so dedupe let it through, and it resumed the job. Binding
  before `sync()` would not fix this alone, because `enqueue()` drops repos not yet in
  `REPOS`. Accepted for now; restart when nothing is expected.
- Picking "the newest `issues.labeled` delivery" to redeliver picked the bot's own
  `claude:planning` label. That was harmless, since `enqueue()` ignores non-trigger labels,
  but it is worth knowing when choosing a delivery to replay.

## Known limitations / open items

- **A cap is checked before a call**, so one call can overshoot `USD_CAP` by its own cost.
  A 15-task implementation is 17 runs (plan + 15 + review), which is more than the default
  10/day. It stops, and `approve` the next UTC day resumes it. Raise `RUN_CAP` if that is
  too tight.
- The `PAUSED` file and the non-collaborator `approve` were only tested offline, as
  agreed. The same `check_caps()` / `can_write()` paths ran live.
- A repo's worker stopping after `offboard` was only tested offline. The test didn't
  remove a repo.
- The permission lookup runs on the HTTP thread, like `labels_of()` already did. A slow
  API call delays the next delivery, but the 204 has already been sent.
- Phase 1–2 open items still stand: the agent can read `app.pem`, the server's `gh` is
  still logged in, Llama's old per-repo webhook, SSH hardening, and no pruning of
  clones for repos removed while the service was down.
- Test leftovers on GitHub: `rookery-throwaway` #1 (awaiting approval), #2 + draft PR #3,
  and `rookery-throwaway-2` #1 (awaiting approval). Delete both repos when you like:
  `gh repo delete sa-es-ir/rookery-throwaway{,-2}` (needs the `delete_repo` scope).

## Next steps

Phases 1–4 are done. Per [PRD-Phases.md](PRD-Phases.md) §5, the next step is **not**
Phase 5. It is running Rookery with a few invited users, and only then deciding on Phase 5
(API/UI), the sandbox and Phase 6, based on whether people want it.

**Before anyone else uses the box.** These open security items have carried over since Phase 1:

1. **Log out the server's `gh`** (`gh auth logout` as `rookery`). The code no longer
   uses it, but the agent can read that token. Takes a minute.
2. **SSH hardening:** `PasswordAuthentication no`, `PermitRootLogin prohibit-password`.
   Root is being password-brute-forced. Needs the operator's approval; confirm a second key
   session works before closing the first.
3. **Delete Llama's old per-repo webhook.** It only produces 401s now.
4. **`app.pem` is readable by the agent.** `claude` runs as the user that can read the App
   key, which can mint a token for any installed repo. The real fix is the sandbox. The
   smaller fix is running `claude` as a separate user, which should happen before
   strangers are invited.

**For the invited trial:**

- The App is installable "only on this account". Invited users need either their repos
  under this account, or the App switched to "any account" plus an allowlist of accounts
  that may install it (the invite-only gate from §1.2, not built yet).
- Check `RUN_CAP` against real issues. 10 calls/day stops anything over 8 tasks
  (plan + tasks + review) mid-run until the next UTC day.
- Delete the throwaway repos: `gh repo delete sa-es-ir/rookery-throwaway{,-2}`.

Suggested order: 1 and 3 now (trivial), then 2 and 4 once approved and planned.
