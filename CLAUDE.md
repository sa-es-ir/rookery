# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
python test_agent.py                       # the whole test suite (asserts, no framework, no network)
python agent.py                            # webhook + portal on 127.0.0.1:$PORT (default 8787)
npx smee-client --url https://smee.io/<ch> --target http://127.0.0.1:8787/
python agent.py owner/repo 12              # dry run: plan issue 12 as if just labelled
python agent.py owner/repo 12 approve      # dry run: run the task loop, push, open the draft PR
python agent.py owner/repo 12 use sqlite   # dry run: anything else is fed back as a developer reply
```

There is no build, no lint, no dependency install — stdlib only, plus the `gh`, `claude`
and `openssl` CLIs on PATH. The dry-run forms go through the same `handle()` as the webhook; only the
transport differs, so use them instead of standing up smee to test a change.

`.env` is gitignored and required at import time (`agent.py` reads
`WEBHOOK_SECRET` from the environment at module level). `test_agent.py` sets it
_before_ `import agent` for that reason — keep that ordering if you add tests.

## Architecture

Two files. `agent.py` is the pipeline; `web.py` is everything HTTP on one
`ThreadingHTTPServer`: the webhook (`Handler.do_POST` on `/`, verifies the HMAC, drops a
repeated `X-GitHub-Delivery` via `fresh()`, never blocks on a job), GitHub sign-in, the JSON
API and the static `ui/` files. `route()` is the whole non-webhook app as a function of
(method, url, headers), which is how `test_agent.py` drives it. `agent.py`'s `__main__`
re-imports itself as `agent` before `main()`, so `web.py`'s `import agent` is the same
module, not a second copy. The webhook hands jobs to `submit()`, which puts each on its repo's
own queue in `queues`; the repo's daemon `worker()` is started lazily on its first job. Repos
run in parallel, but within a repo it is still one job at a time — deliberate, since two jobs
on one issue would race on its worktree and labels. A worker whose last job was `offboard`
exits under `qlock`, so a job submitted at that moment gets a fresh worker, never a dead one.

**This repo is the orchestrator, never the target.** `REPOS` maps `owner/name` → base branch
and is never written to disk: `sync()` rebuilds it at startup from the App's installations
(`default_branch` is the base), and `installation` / `installation_repositories` events
keep it current — `enqueue()` adds or drops the repo at once and queues an `onboard` /
`offboard` job on that repo's queue, so a trigger that arrives while the clone is still
being made is queued behind it. Clones live at `$WORKSPACE/repos/<owner>_<repo>`; `Ctx` (repo, issue number,
clone path, base branch) is threaded through every function. Each issue gets its own
`git worktree` at `$WORKSPACE/<owner>_<repo>/issue-<n>` on `feature/issue-<n>`, so two
repos can share an issue number.

**Retention.** A closed PR from the bot on `feature/issue-<n>` removes that worktree
(`drop_worktree()`); a repo leaving the installation removes its clone and all its
worktrees (`offboard()`). Both refuse — log and skip — when `unpushed()` finds a branch
with commits on no remote-tracking ref. The push goes to a URL, which updates no such
ref, so `implement()` records `refs/remotes/origin/<branch>` itself right after it.

**State lives on GitHub and in the target repo, not here.** The `rookery:*` labels _are_ the
state machine (`PHASES`), advanced only via `set_phase()`, which removes stale phase labels.
Per-issue files live in `agent-plans/<n>/` inside the target repo — `plan.md`, `tasks.md`,
`decisions.md` — and are committed and pushed with the change so they land in the PR. Nothing
local persists between runs: Claude session ids are `uuid5(NS, "repo#n/key")`, so re-running
the same issue resumes the same session.

Flow: label `rookery:trigger` → `plan()` (`rookery:planning`) → either `rookery:awaiting-input`
(a reply re-enters `plan(resume=True)`, same session) or `rookery:awaiting-approval` → an
`approve` comment from someone with write access → `implement()` → `rookery:reviewing` → `rookery:done` /
`rookery:error`.

**Named roles are `AGENTS`, passed as `--agents`/`--agent`.** `planmaker` runs `plan()`,
`reviewer` runs the review pass; implementation tasks deliberately run as _no_ agent — the
default harness plus the deny rules is the point. They live in `agents/*.md` (root, in the
native Claude Code agent format, loaded by `parse_agent()` at import) rather than
`.claude/agents/*.md`: `claude` runs with cwd set to the _target_ repo's worktree, so
definitions in this repo would never be discovered and writing them into the target would
pollute it — the JSON is transport, the files are the source. Root, not `.claude/`, so
Claude Code does not load them as subagents for anyone working _on_ Rookery.
Both roles declare `model: opus` in their frontmatter; agentless task invocations pass
`--model sonnet` in `claude()` — a bad plan costs fifteen tasks of wrong code, grinding
through those tasks does not need the same model.

`implement()` is the heart: loop the first unticked task in `tasks.md` → **a fresh Claude
session per task** (`key=f"task-{i}"`) → require `STATUS: task-done` → tick the checkbox →
`git commit` → next. Fresh sessions keep context and cost flat regardless of issue size;
`decisions.md` is the only memory that crosses between tasks. A failure raises with everything
before it already committed, so commenting `approve` again resumes at that same task.

**Run log.** `handle()` opens a `jobs` row per plan/reply/implement (`begin()`), and the
pipeline appends `steps` (phase, task i of N, each claude call with key/status/cost, tick,
commit, push, PR, error) via `step()`, which is a no-op outside a job. `finish()` sets
ok/failed/cancelled; a restart marks leftovers `interrupted`. `active[repo]` is the job a
worker is running — one per repo.

**Cancel.** `sh(track=repo)` registers the process in `active[repo]` under `alock`;
`cancel()` sets the flag and kills the tree (`killpg` / `taskkill /T`) under the same lock,
so a cancel can't land between the check and the start. `check_caps()` refuses further
calls, and the job fails through the normal path with a "Cancelled … by @login" comment.

**Portal auth.** GitHub OAuth through the App (`CLIENT_ID`/`CLIENT_SECRET`, callback
`PUBLIC_URL/auth/callback`, a `state` cookie). The user's token lists their installations'
repos once at login and is dropped; the session row (keyed by sha256 of the cookie, 8h)
keeps that list. Visible = that list ∩ `REPOS`. Signing in again replaces the session with a
fresh list; the App's Setup URL is `/auth/login`, so an install does that by itself. POSTs
need the session's `X-CSRF-Token`.
Cancel needs `can_write()`, the same check as `approve`. `REPOS_ONLY` limits an instance.

**GitHub auth is a GitHub App, not a login.** `jwt()` signs an RS256 JWT with `openssl`
(`APP_ID`, `APP_KEY` from `.env`); `token(repo)` exchanges it for an installation token,
cached and re-minted with under 10 minutes left, since an implementation outlives the 1h
token. `auth(repo)` turns it into an `env=` for one `gh`/`git` call — `GH_TOKEN` plus a
`GIT_CONFIG_*` https auth header. There is no `gh auth login` on the box.

## Invariants — do not soften these

- **The orchestrator ticks checkboxes and commits; the agent never does.** `tick()` rewrites one
  byte by offset so task text is never disturbed. The TASK prompt tells the agent not to commit;
  `--disallowed-tools Bash(git push:*) Bash(gh:*)` enforces it (deny rules beat
  `bypassPermissions`). The agent never publishes.
- **Approval is `APPROVE`, a regex — never an LLM judgement**, and only from a commenter with
  write, maintain or admin on _that_ repo (`can_write()`, the collaborator-permission API with
  the repo's installation token; a failed lookup denies). Replies need the same. `ME` (the App's
  bot login) is filtered out so the bot can't approve itself.
- **Caps are checked before every `claude` call** (`check_caps()`): `RUN_CAP` calls and
  `USD_CAP` dollars per repo per UTC day (defaults 10 and $20), plus the operator's `PAUSED`
  file next to `agent.py`. Every call — failed ones too — is recorded with its
  `total_cost_usd` in `$WORKSPACE/rookery.db`, one sqlite3 connection per call, so a restart
  keeps the day's count. A cap raises like any other error (comment + `rookery:error`); mid
  implementation that means the current task is committed and the next one never starts.
  Caps are per tenant; `MAX_TASKS` is per issue — keep them independent.
- **The GitHub token never enters `os.environ`**, only a per-call `env=`: the `claude`
  subprocess inherits `os.environ`, and the deny rules stop `gh`, not `curl` with a token.
  `GH_TOKEN`/`GITHUB_TOKEN` are popped at import for the same reason, and `CLIENT_SECRET`
  in `web.py`. A user's OAuth token lives only inside `callback()`: never stored, never in
  the env.
- **`rookery.db` is readable by the agent** (same user, same workspace), so it holds nothing
  replayable: session rows are keyed by the cookie's sha256, and no token is stored.
- **The portal never approves.** No approve endpoint, no approve button; `APPROVE` stays a
  regex on issue comments. Visibility comes from the user's own installations; anything
  that steers a repo (cancel) needs `can_write()`. Someone else's run is a 404, not a 403.
- **User-written text is never `innerHTML`** in `ui/portal.html` — issue titles and errors
  go in through `textContent` (`h()`).
- **The push goes to `https://github.com/<repo>.git`**, not `origin`, so it carries the
  App's token even when the clone's origin is ssh.
- **Planning writes `agent-plans/<n>/` and nothing else**, enforced by `keep_plans_only()`
  after every `plan()` call, not by the PLAN prompt's wording. It reverts rather than raises,
  so the awaiting-input reply loop can call `plan()` repeatedly without accumulating stray
  edits. It parses `git status --porcelain -uall` — `-uall` matters, since plain `--porcelain`
  collapses a first-ever `agent-plans/` to one untracked-dir entry that the prefix test would
  then delete.
- **The review is advisory and runs once**, after the task loop and before the push. The
  branch is pushed and the draft PR opened either way, so work is never stranded in a local
  worktree; `changes-needed` only sets `rookery:error`. Escalating it to a blocking gate or a
  fix loop is specced in the plan doc — a fix loop needs its own bound, because a reviewer
  that appends tasks breaks "every tick strictly reduces the open count".
- **`STATUS:` is parsed, never inferred.** `MARKER` requires the marker alone on its line; a
  missing marker is an error, not a guess. The marker is stripped from the text before it is
  posted as a comment.
- **`MAX_TASKS = 15`**, checked every iteration; the loop is bounded by `range(MAX_TASKS + 1)`
  with a `for/else` that raises on non-convergence. Every tick strictly reduces the open count.
- **Never push to base**: `assert branch != c.base` before `git push`; branch protection on the
  target repo is the backstop, not the primary guard.
- Webhook signature is checked with `hmac.compare_digest` before anything else; `enqueue()`
  ignores repos absent from `REPOS`. Deliveries are deduped on `X-GitHub-Delivery` (last 1000,
  in memory — a restart forgetting them is accepted); the phase-label checks in `enqueue()`
  remain the guard against a redelivery after that.
- **Retention never deletes unpushed commits.** `drop_worktree()`/`offboard()` check
  `unpushed()` first and keep everything if it finds any. `worker()` catches everything
  `handle()` lets out, so one bad job cannot kill the loop.
- Every exception in `handle()` becomes an issue comment plus `rookery:error` — the loop never
  dies silently.

## Style

Stdlib only; adding a dependency changes what this project is. Prompts are module-level
`.format()` templates (`PLAN`, `REPLY`, `TASK`) — they are the interface to Claude, so treat
their wording as code. `sh()` raises on non-zero; pass `check=False` for the cases (like
`gh label create` on an existing label) where failure is expected.

Phases 1–5 of [ai-plans/PRD-Phases.md](ai-plans/PRD-Phases.md) (GitHub App, onboarding
without a restart, approval and caps per repo, parallel repos, portal/API/run log) are
built and deployed (https://rookery.run, see [DEPLOY.md](DEPLOY.md)); phase 6 is
not built.

For designing UI pages you must use @DESIGN.md file and follow the rules/patterns there.
