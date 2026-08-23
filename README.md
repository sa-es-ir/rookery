# github-issue-agent — Phase 1

Open a labelled issue → the agent plans it, asks questions as comments, waits
for `approve`, implements on a branch, opens a draft PR. Plan:
[github-issue-agent-plan.md](github-issue-agent-plan.md).

Everything is `agent.py` (stdlib only) plus the `gh` and `claude` CLIs.

## Where it runs

This repo is the **orchestrator**, not the target. It never runs inside the repo
being worked on. `repos.json` maps each target repo to a local clone:

```json
{ "owner/pilot-repo": { "path": "C:/src/pilot-repo", "base": "main" } }
```

Each issue gets its own `git worktree` under `work/<owner>_<repo>/issue-<n>` on
branch `feature/issue-<n>`, so two repos can share an issue number without
colliding. Your own checkouts are never touched.

## State, in the target repo

```
agent-plans/<n>/
  plan.md        # the approved plan
  tasks.md       # - [ ] checklist; the ORCHESTRATOR ticks, never the agent
  decisions.md   # append-only; agent writes, the next task reads
```

All three are committed and pushed with the change, so they land in the PR.

After approval the orchestrator loops: first unticked task → **a fresh Claude
session** for that task alone → `STATUS: task-done` → tick, `git commit`, next.
Fresh sessions per task are what keeps context (and cost) flat no matter how big
the issue is; `decisions.md` is the only memory that crosses between them.

More than 15 tasks is an error — split the issue. A failed task stops the loop
at `claude:error` with everything before it already committed; fix the plan or
the code and comment `approve` again to resume from that same task.

## Setup

1. `cp .env.example .env` and `cp repos.example.json repos.json`, fill both in.
   `gh auth login` and a global `user.name` / `user.email` must already work —
   the orchestrator commits.
2. Clone each target repo to the `path` you gave it.
3. In **each** repo: settings → Webhooks → add `https://smee.io/<your-channel>`, content type
   `application/json`, the same secret, events: **Issues** and **Issue comments**.
4. Branch-protect `main` — the code refuses to push there, protection is the
   backstop.
5. Run:

```
python agent.py
npx smee-client --url https://smee.io/<your-channel> --target http://127.0.0.1:8787/
```

Labels are created on first start.

## Using it

Add the `claude:trigger` label to an issue. Then follow the `claude:*` label:
`planning` → `awaiting-input` (answer in a comment) → `awaiting-approval`
(comment `approve` / `lgtm` / `go ahead`) → `implementing` → `done`.
`claude:error` means the exception is in the last comment; fix it, remove the
label, re-trigger.

Only logins in `ALLOWLIST` can move an issue along. Approval is a regex
(`agent.py:APPROVE`), never an LLM judgement.

`python test_agent.py` checks the gates.

## Testing without a webhook

Do this first — it exercises worktree → claude → parse → tick → commit → PR
without smee, TLS, or signature verification in the way:

```
python agent.py owner/repo 12          # plan issue 12, as if just labelled
python agent.py owner/repo 12 approve  # run the task loop and open the PR
python agent.py owner/repo 12 use sqlite   # anything else is fed back as a reply
```

Same code path as the webhook (`handle()`); only the transport differs.

## Not built yet

Phases 2–5 of the plan: bug shortcut path, a GitHub App (repos.json covers the
routing, but each repo still needs its webhook wired by hand), OpenSpec,
VPS + queue + alerting. One worker, one job at a time — a long implementation
blocks every other issue.
