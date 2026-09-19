# Phase 2 notes — onboarding without a restart

**Date:** 19 September 2026
**Status:** done, tested on the live server. Spec in [PRD-Phases.md](PRD-Phases.md)
§2 Phase 2; follows [phase1-notes.md](phase1-notes.md).

## Result

Live test on `sa-es-ir/rookery-throwaway` (public, created for this, base `main`):

| Time | Event |
|---|---|
| 11:23:16 | Repo added to the installation → `installation_repositories.added` → clone started |
| 11:23:19 | `live, base main`, 8 `claude:*` labels created. No restart. |
| 11:23:29 | Issue #1 labelled `claude:trigger` → `job: start` |
| 11:24:05 | Plan comment from `rookery-gha[bot]`, `claude:awaiting-approval` |
| 11:26:03 | Repo removed from the installation → clone (252K) and worktree dir (32K) deleted |

After removal, an `lgtm` comment on the issue did nothing, because GitHub stops sending
the repo's events.

| Commit | What |
|---|---|
| `2c82d6e` | Onboarding from installations, retention, tests, docs |

## Decisions (from the session brief)

- Clones at `$WORKSPACE/repos/<owner>_<repo>`. `repos.json` is gone. The base is
  `repository.default_branch`.
- The repo list is rebuilt from the App's installations at startup, and
  `installation` / `installation_repositories` events keep it current.
- Retention: when an issue's PR closes, its worktree is removed. When a repo leaves the
  installation, its clone and worktrees are removed. Both refuse (log + skip) if any branch
  has unpushed commits.
- App repository access is **Only select repositories**: 95 repos on the account, 4 in
  the installation before the test. Changing that selection needs a user token authorized
  for the App, so the operator does it in the UI. `gh` can't.

## How it works

- `REPOS` is `{"owner/name": base}` and exists only in memory. `sync()` calls
  `GET /app/installations`, mints an installation token for each, then pages
  `GET /installation/repositories` and calls `onboard(full_name, default_branch)` for each
  repo. Pagination (`api_all`) is per_page=100.
- `onboard()` clones over https with `auth()` (the header never lands in `.git/config`)
  if the dir is missing, creates the labels (as startup did before), and sets
  `REPOS[repo]`. When an event triggers it, the base comes from `GET /repos/{repo}`, since
  installation payloads don't carry `default_branch`.
- `enqueue()` for installation events: an added repo goes into `REPOS` immediately (base
  `""`) and an `onboard` job is queued. Because the queue is FIFO, a trigger that arrives
  during the clone runs after the clone finishes and is not dropped. A removed repo is popped
  at once, so its events are ignored from then on, and an `offboard` job is queued.
  `installation.created/deleted` use `repositories`, and `installation_repositories` uses
  `repositories_added/removed`.
- `pull_request.closed`: if the author is `ME` and the head ref is exactly
  `feature/issue-<n>`, a `drop-worktree` job is queued. The author check matters, because a
  fork PR named `feature/issue-5` must not delete our worktree.
- `unpushed(clone, ref)` = `git rev-list -1 ref --not --remotes`, so it needs no network
  (after offboarding, the App can't reach the repo anyway). The push goes to a URL,
  which updates no tracking ref, so `implement()` now runs
  `git update-ref refs/remotes/origin/<branch>` right after the push. Without that, every
  finished issue would look unpushed and nothing would ever be freed.
- Uncommitted files (a plan awaiting approval) do **not** block removal. The spec says
  unpushed *commits*, and the plan text is also in the issue comment.
- `worker()` now catches everything `handle()` lets out. Before this, if the error comment
  itself failed (likely for a job queued just before its repo was removed), the worker
  thread died and the service went deaf while systemd still showed it `active`.

## Tests added (`test_agent.py`, still offline)

- `ctx()` builds the clone path from `WORKSPACE` and the base from `REPOS`.
- All four installation event shapes: repos are added and dropped immediately, and
  onboard/offboard jobs are queued in order. A trigger on a removed repo is ignored.
- `pull_request.closed`: a bot PR on `feature/issue-3` queues a job. Another author's PR and
  `feature/issue-5-extra` don't.
- Real git in a temp dir (a bare origin, a clone, two worktrees, one with an unpushed commit):
  `offboard` refuses and `drop_worktree` keeps that one but removes the clean one. After a
  push to the URL plus `update-ref`, `offboard` frees everything. This also covers the
  read-only object files on Windows (`rmtree` with `onexc`).
- `sync()` over a fake API: 103 repos across two pages, with the default branch passed through.

## Server changes

- The service was stopped, then `agent.py` / `test_agent.py` installed (the old one kept as
  `agent.py.bak-pre-phase2`). `repos.json` → `repos.json.bak-pre-phase2`.
- Existing clones moved: `/home/rookery/repos/{llama,nudgemcp}` →
  `/opt/rookery/work/repos/sa-es-ir_{Llama,NudgeMcp}`, then `git worktree repair` on the
  three live worktrees (Llama 110/112, NudgeMcp 1). "`.git` file broken" is repair
  reporting what it fixed. Afterwards the links point both ways and `status` works.
- NudgeMcp `feature/issue-1` was pushed by Phase 1 without a tracking ref. `ls-remote`
  confirmed `13d8b5a` is on GitHub, then `update-ref` recorded it. Llama's branches
  already had tracking refs from the old `push -u origin`.
- The empty leftover `work/sa-es-ir_llama` (lowercase) was removed.
- At startup `sync()` found **four** repos, not two: **detester** and **Mantis** are also in
  the installation. They are now cloned into `work/repos/` and have the `claude:*` labels.
  Drop them from the installation if that wasn't intended; that now also frees the disk.
- `/home/rookery/repos/` is now empty and unused.

## Known limitations / open items

- **Removal is event-driven only.** A repo removed while the service is down keeps its
  clone. Startup doesn't prune dirs that aren't in the installation list. The fix is a pass in
  `sync()`, but the `<owner>_<repo>` dir name isn't reversible (repo names can contain `_`),
  so it would compare dir names, not repo names.
- **Onboarding waits in the single queue**, so a repo added during a long implementation
  goes live when that implementation ends. Its triggers are queued, not lost. Phase 4's
  per-repo workers fix this.
- A repo with no commits can't be planned (no `origin/<base>`). Documented in deploy.md §8.
- A default-branch rename is only picked up at the next restart.
- `installation.suspend` is not handled (tokens fail until unsuspended).
- The per-PR-close worktree removal is covered offline but was not exercised live (the test
  didn't go through approve → PR). It uses the same `unpushed()` check that the live
  offboard exercised.
- Phase 1 open items still stand: the agent can read `app.pem`, the server's `gh` is still
  logged in, Llama's old per-repo webhook, SSH hardening.
- `sa-es-ir/rookery-throwaway` still exists on GitHub (issue #1 open). Delete it when you
  like: `gh repo delete sa-es-ir/rookery-throwaway` (needs the `delete_repo` scope).
