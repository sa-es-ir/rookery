<p align="center">
  <img src="rookery.png" alt="Rookery" width="320">
</p>

<h1 align="center">Rookery</h1>

<p align="center">
  <em>Label an issue. Get a draft PR.</em>
</p>

<p align="center">
  <img alt="Python 3" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-stdlib%20only-2ea44f">
  <img alt="Status: phase 1" src="https://img.shields.io/badge/status-phase%201-blue">
</p>

---

Rookery watches your repos for a `claude:trigger` label. When one lands it plans
the issue, asks its questions as issue comments, waits for a human `approve`,
then implements the plan task by task on a branch and opens a draft PR.

```
claude:trigger → planning → awaiting-input ⇄ awaiting-approval → implementing → done
```

Everything is one file, `agent.py` — Python stdlib only, plus the `gh` and
`claude` CLIs on your PATH. The full roadmap lives in
[github-issue-agent-plan.md](github-issue-agent-plan.md).

## Quick start

```bash
cp .env.example .env            # WEBHOOK_SECRET, ALLOWLIST
cp repos.example.json repos.json
python agent.py owner/repo 12   # dry run: plan issue 12 without a webhook
```

## Requirements

| Needs | Why |
| --- | --- |
| `gh`, authenticated | reads issues, posts comments, opens the PR |
| `claude` CLI | plans and implements |
| `git` with a global `user.name` / `user.email` | the orchestrator commits |
| Python 3.10+ | no packages to install |

## How it works

### This repo is the orchestrator, never the target

Rookery never runs inside the repo it is working on. `repos.json` maps each
target repo to a local clone:

```json
{ "owner/pilot-repo": { "path": "C:/src/pilot-repo", "base": "main" } }
```

Each issue gets its own `git worktree` under `work/<owner>_<repo>/issue-<n>` on
branch `feature/issue-<n>`, so two repos can share an issue number without
colliding. Your own checkouts are never touched.

### State lives in the target repo

```
agent-plans/<n>/
  plan.md        # the approved plan
  tasks.md       # - [ ] checklist — the ORCHESTRATOR ticks, never the agent
  decisions.md   # append-only; the agent writes, the next task reads
```

All three are committed and pushed with the change, so they land in the PR.
Nothing persists locally between runs.

### One task, one session

After approval the orchestrator loops: take the first unticked task → start a
**fresh Claude session** for that task alone → require `STATUS: task-done` →
tick the checkbox, `git commit`, next.

Fresh sessions per task keep context — and cost — flat no matter how large the
issue is. `decisions.md` is the only memory that crosses between them.

- More than **15 tasks** is an error: split the issue.
- A failed task stops at `claude:error` with everything before it already
  committed. Fix the plan or the code, comment `approve` again, and the loop
  resumes at that same task.

## Setup

1. Fill in `.env` and `repos.json`.
2. Clone each target repo to the `path` you gave it.
3. In **each** target repo: *Settings → Webhooks →* add
   `https://smee.io/<your-channel>`, content type `application/json`, the same
   secret, events **Issues** and **Issue comments**.
4. Branch-protect `main`. The code refuses to push there; protection is the
   backstop, not the primary guard.
5. Run the server and point smee at it:

   ```bash
   python agent.py
   npx smee-client --url https://smee.io/<your-channel> --target http://127.0.0.1:8787/
   ```

Labels are created on first start.

## Using it

Add the `claude:trigger` label to an issue, then follow the `claude:*` label:

| Label | Meaning |
| --- | --- |
| `claude:planning` | writing the plan |
| `claude:awaiting-input` | a question is in the comments — answer it |
| `claude:awaiting-approval` | comment `approve` / `lgtm` / `go ahead` |
| `claude:implementing` | working through `tasks.md` |
| `claude:done` | draft PR is open |
| `claude:error` | the exception is the last comment; fix, unlabel, re-trigger |

Only logins in `ALLOWLIST` can move an issue along, and approval is a regex
(`agent.py:APPROVE`) — never an LLM judgement. The agent itself is blocked from
`git push` and `gh`; it never publishes.

## Testing without a webhook

The dry-run forms go through the same `handle()` as the webhook — only the
transport differs. Use them instead of standing up smee:

```bash
python agent.py owner/repo 12              # plan the issue, as if just labelled
python agent.py owner/repo 12 approve      # run the task loop, push, open the PR
python agent.py owner/repo 12 use sqlite   # anything else is fed back as a reply
python test_agent.py                       # the gates, no network
```

## Not built yet

Phases 2–5 of the plan: the bug shortcut path, a GitHub App (`repos.json` covers
routing, but each repo still needs its webhook wired by hand), OpenSpec, and
VPS + queue + alerting.

One worker, one job at a time — deliberately. A long implementation blocks every
other issue.
