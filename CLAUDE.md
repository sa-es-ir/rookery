# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
python test_agent.py                       # the whole test suite (asserts, no framework, no network)
python agent.py                            # webhook server on 127.0.0.1:$PORT (default 8787)
npx smee-client --url https://smee.io/<ch> --target http://127.0.0.1:8787/
python agent.py owner/repo 12              # dry run: plan issue 12 as if just labelled
python agent.py owner/repo 12 approve      # dry run: run the task loop, push, open the draft PR
python agent.py owner/repo 12 use sqlite   # dry run: anything else is fed back as a developer reply
```

There is no build, no lint, no dependency install — stdlib only, plus the `gh`, `claude`
and `openssl` CLIs on PATH. The dry-run forms go through the same `handle()` as the webhook; only the
transport differs, so use them instead of standing up smee to test a change.

`.env` is gitignored and required at import time (`agent.py` reads
`WEBHOOK_SECRET`/`ALLOWLIST` from the environment at module level). `test_agent.py` sets both
env vars _before_ `import agent` for that reason — keep that ordering if you add tests.

## Architecture

One file, `agent.py`. Two threads: `Hook` (HTTP, verifies the HMAC, never blocks) puts jobs on
`jobs`; a single daemon `worker()` drains them one at a time. One job at a time is deliberate —
a long implementation blocks every other issue.

**This repo is the orchestrator, never the target.** `REPOS` maps `owner/name` → base branch
and is never written to disk: `sync()` rebuilds it at startup from the App's installations
(`default_branch` is the base), and `installation` / `installation_repositories` events
keep it current — `enqueue()` adds or drops the repo at once and queues an `onboard` /
`offboard` job, so a trigger that arrives while the clone is still being made is queued
behind it. Clones live at `$WORKSPACE/repos/<owner>_<repo>`; `Ctx` (repo, issue number,
clone path, base branch) is threaded through every function. Each issue gets its own
`git worktree` at `$WORKSPACE/<owner>_<repo>/issue-<n>` on `feature/issue-<n>`, so two
repos can share an issue number.

**Retention.** A closed PR from the bot on `feature/issue-<n>` removes that worktree
(`drop_worktree()`); a repo leaving the installation removes its clone and all its
worktrees (`offboard()`). Both refuse — log and skip — when `unpushed()` finds a branch
with commits on no remote-tracking ref. The push goes to a URL, which updates no such
ref, so `implement()` records `refs/remotes/origin/<branch>` itself right after it.

**State lives on GitHub and in the target repo, not here.** The `claude:*` labels _are_ the
state machine (`PHASES`), advanced only via `set_phase()`, which removes stale phase labels.
Per-issue files live in `agent-plans/<n>/` inside the target repo — `plan.md`, `tasks.md`,
`decisions.md` — and are committed and pushed with the change so they land in the PR. Nothing
local persists between runs: Claude session ids are `uuid5(NS, "repo#n/key")`, so re-running
the same issue resumes the same session.

Flow: label `claude:trigger` → `plan()` (`claude:planning`) → either `claude:awaiting-input`
(a reply re-enters `plan(resume=True)`, same session) or `claude:awaiting-approval` → an
allowlisted `approve` comment → `implement()` → `claude:reviewing` → `claude:done` /
`claude:error`.

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
- **Approval is `APPROVE`, a regex — never an LLM judgement**, and only from a login in
  `ALLOWLIST`. `ME` (the App's bot login) is filtered out so the bot can't approve itself.
- **The GitHub token never enters `os.environ`**, only a per-call `env=`: the `claude`
  subprocess inherits `os.environ`, and the deny rules stop `gh`, not `curl` with a token.
  `GH_TOKEN`/`GITHUB_TOKEN` are popped at import for the same reason.
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
  worktree; `changes-needed` only sets `claude:error`. Escalating it to a blocking gate or a
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
  ignores repos absent from `REPOS`.
- **Retention never deletes unpushed commits.** `drop_worktree()`/`offboard()` check
  `unpushed()` first and keep everything if it finds any. `worker()` catches everything
  `handle()` lets out, so one bad job cannot kill the loop.
- Every exception in `handle()` becomes an issue comment plus `claude:error` — the loop never
  dies silently.

## Style

Stdlib only; adding a dependency changes what this project is. Prompts are module-level
`.format()` templates (`PLAN`, `REPLY`, `TASK`) — they are the interface to Claude, so treat
their wording as code. `sh()` raises on non-zero; pass `check=False` for the cases (like
`gh label create` on an existing label) where failure is expected.

Phases 1–2 of [ai-plans/PRD-Phases.md](ai-plans/PRD-Phases.md) (GitHub App, onboarding
without a restart) are built; phases 3–6 are not.

For designing UI pages you must use @DESIGN.md file and follow the rules/patterns there.
