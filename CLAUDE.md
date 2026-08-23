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

There is no build, no lint, no dependency install — stdlib only, plus the `gh` and `claude`
CLIs on PATH. The dry-run forms go through the same `handle()` as the webhook; only the
transport differs, so use them instead of standing up smee to test a change.

`.env` and `repos.json` are gitignored and required at import time (`agent.py` reads
`WEBHOOK_SECRET`/`ALLOWLIST` from the environment at module level). `test_agent.py` sets both
env vars *before* `import agent` for that reason — keep that ordering if you add tests.

## Architecture

One file, `agent.py`. Two threads: `Hook` (HTTP, verifies the HMAC, never blocks) puts jobs on
`jobs`; a single daemon `worker()` drains them one at a time. One job at a time is deliberate —
a long implementation blocks every other issue.

**This repo is the orchestrator, never the target.** `repos.json` maps `owner/name` → local
clone path + base branch; `Ctx` (repo, issue number, clone path, base branch) is threaded
through every function. Each issue gets its own `git worktree` at
`work/<owner>_<repo>/issue-<n>` on `feature/issue-<n>`, so two repos can share an issue number.

**State lives on GitHub and in the target repo, not here.** The `claude:*` labels *are* the
state machine (`PHASES`), advanced only via `set_phase()`, which removes stale phase labels.
Per-issue files live in `agent-plans/<n>/` inside the target repo — `plan.md`, `tasks.md`,
`decisions.md` — and are committed and pushed with the change so they land in the PR. Nothing
local persists between runs: Claude session ids are `uuid5(NS, "repo#n/key")`, so re-running
the same issue resumes the same session.

Flow: label `claude:trigger` → `plan()` (`claude:planning`) → either `claude:awaiting-input`
(a reply re-enters `plan(resume=True)`, same session) or `claude:awaiting-approval` → an
allowlisted `approve` comment → `implement()` → `claude:done` / `claude:error`.

`implement()` is the heart: loop the first unticked task in `tasks.md` → **a fresh Claude
session per task** (`key=f"task-{i}"`) → require `STATUS: task-done` → tick the checkbox →
`git commit` → next. Fresh sessions keep context and cost flat regardless of issue size;
`decisions.md` is the only memory that crosses between tasks. A failure raises with everything
before it already committed, so commenting `approve` again resumes at that same task.

## Invariants — do not soften these

- **The orchestrator ticks checkboxes and commits; the agent never does.** `tick()` rewrites one
  byte by offset so task text is never disturbed. The TASK prompt tells the agent not to commit;
  `--disallowed-tools Bash(git push:*) Bash(gh:*)` enforces it (deny rules beat
  `bypassPermissions`). The agent never publishes.
- **Approval is `APPROVE`, a regex — never an LLM judgement**, and only from a login in
  `ALLOWLIST`. `ME` (our own `gh` login) is filtered out so the bot can't approve itself.
- **`STATUS:` is parsed, never inferred.** `MARKER` requires the marker alone on its line; a
  missing marker is an error, not a guess. The marker is stripped from the text before it is
  posted as a comment.
- **`MAX_TASKS = 15`**, checked every iteration; the loop is bounded by `range(MAX_TASKS + 1)`
  with a `for/else` that raises on non-convergence. Every tick strictly reduces the open count.
- **Never push to base**: `assert branch != c.base` before `git push`; branch protection on the
  target repo is the backstop, not the primary guard.
- Webhook signature is checked with `hmac.compare_digest` before anything else; `enqueue()`
  ignores repos absent from `repos.json`.
- Every exception in `handle()` becomes an issue comment plus `claude:error` — the loop never
  dies silently.

## Style

Stdlib only; adding a dependency changes what this project is. Prompts are module-level
`.format()` templates (`PLAN`, `REPLY`, `TASK`) — they are the interface to Claude, so treat
their wording as code. `sh()` raises on non-zero; pass `check=False` for the cases (like
`gh label create` on an existing label) where failure is expected.

Phases 2–5 (bug shortcut path, GitHub App, OpenSpec, VPS + queue) are unbuilt and specified in
[github-issue-agent-plan.md](github-issue-agent-plan.md).
