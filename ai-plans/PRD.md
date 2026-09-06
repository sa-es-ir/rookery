# Rookery — Product Requirements

**Status:** living draft — add to it freely
**Last updated:** 6 September 2026

Companion docs: [github-issue-agent-plan.md](github-issue-agent-plan.md) (the
build phases), [PRODUCT-BRIEF.md](PRODUCT-BRIEF.md) (the wider platform
architecture — sandboxes, memory, billing). This file is the requirements list:
what the product must do, and what stands between today's `agent.py` and that.
It intentionally does not restate the architecture in those two.

---

## 1. Vision

A user opens the Rookery UI, pastes the URL of a repo they own, picks which
coding agent should do the work, and installs Rookery on it. From then on,
labelling an issue `claude:trigger` in that repo produces a plan, a
conversation, and — after an approval comment — a draft pull request. No local
setup, no config file, no terminal.

The output is always a draft PR. A human always merges.

## 2. Where we are today

`agent.py` already does the hard half, and does it repo-agnostically:

- `Ctx (repo, issue, path, base)` is threaded through every function; worktrees
  are namespaced per repo, so two repos can share an issue number.
- Sessions are `uuid5(NS, "repo#n/key")` — deterministic, so a re-run resumes.
- Phase labels are the state machine; per-issue state lives in the *target*
  repo under `agent-plans/<n>/` and ships in the PR. Nothing local persists.
- Plan → approve → bounded task loop → advisory review → push → draft PR works
  end to end.

One property is already right and worth protecting: **`claude:trigger` is a
label, and only collaborators can apply labels.** A stranger passing by a public
repo cannot fire the agent. That is the authorization gate; don't replace it
with something weaker.

Everything below is the multi-tenant plumbing that does not exist yet.

---

## 3. Prior art — AgentsRoom

[agentsroom.dev](https://agentsroom.dev/) is the closest thing shipping, and it
is worth being precise about where it overlaps, because it answers some of our
open questions and dodges others entirely.

**What it is.** A native desktop app (macOS/Windows/Linux), free for 3 projects
/ 6 parallel agents, €7.99/mo Pro. 100% local, BYOK — "not a wrapper that
resells tokens". It spawns each provider's CLI natively: Claude Code, Codex,
Antigravity, Copilot CLI, Cursor, Aider, OpenCode, Grok, Mistral Vibe, Ollama,
Kimi, Amp, Devin, Codebuff — 14+, switchable per agent and mid-conversation,
with multi-account quota auto-switch. The rest is a cockpit: agent grid with
live status, split view, 14 built-in roles plus 260+ community ones, agent
fork/morph/messaging, per-agent diff review, voice, mobile companion, secret
manager, SSH and DB connections.

**Where it overlaps us:**

| Ours | Theirs |
|---|---|
| R6 pluggable harness | Shipped, 14+ providers, per-agent, BYOK per provider |
| `reviewer` role | "Agent teams" — dev writes, a separate QA agent reviews |
| `decisions.md` | "Project memory" — decisions/pitfalls the agents write themselves |
| Issue → PR | "Public Backlog" — a feedback portal where each ticket becomes an agent that codes it |
| Webhook trigger | "Triggers" — paste a webhook URL into GitHub/Slack/CI, fires on `pull_request opened`, build broken |
| Remote execution | "Cloud agents" — a throwaway cloud VM runs the task, commits, pushes a branch |

**Where it is not our product — and this is the part that matters:**

1. **There is no repo URL to paste.** A project is a folder on your machine;
   their webhook trigger fires *your own desktop app* over a relay. R1–R5, R7
   and R8 — the whole multi-tenant half of this document — do not exist for
   them, because there is no tenancy. That is how they sidestep the GitHub App,
   the write-access problem, per-tenant billing and the queue.
2. **No approval state machine.** No plan → `awaiting-approval` → `approve`
   comment → implement. Review is a diff pane a human reads in the app.
3. **State lives in the app, not in the repo.** Our `agent-plans/<n>/`
   committed into the PR is genuinely different.
4. **You have to install and run it.** Ours is headless; the repo is the
   interface.

**What we take from this.** Their weakest area — the GitHub-side workflow — is
our strongest, and vice versa. If the product we want is the cockpit, we are
~1,164 releases behind an app with a mobile companion and a community agent
store, and we would be rebuilding it. If the product we want is *GitHub-native
and headless* — label an issue, a plan appears in the thread, comment
`approve`, a draft PR shows up, nothing installed anywhere — that is still
open, and `agent.py` already does it.

**Design rule that follows: nothing in this PRD justifies building a desktop
UI.** The UI in R8 is a control plane (register a repo, pick a harness, watch a
run), not a terminal cockpit.

---

## 4. Requirements

### R1 — Self-serve repo onboarding

Paste a repo URL in the UI; the repo is live without a restart.

Today `REPOS` is read from `repos.json` at import (`agent.py:31`), labels are
created once at startup (`agent.py:415`), and `enqueue()` drops any repo not in
the map (`agent.py:397`). Registration must become a runtime operation: persist,
create the `claude:*` labels, start accepting webhooks.

Small — the smallest item on this list. Do it after R2, since the GitHub App
installation payload supplies the repo and the installing user for free.

### R2 — GitHub App instead of a shared webhook secret and a personal `gh` login

**The load-bearing requirement. R3, R4 and part of R5 collapse into it.**

Today: one global `WEBHOOK_SECRET` (`agent.py:35`) that every tenant would have
to be handed in order to paste it into their own webhook settings, and one `gh`
login (`ME`, `agent.py:414`) that authors every PR for every user.

A GitHub App fixes all three at once: the user clicks *Install*, GitHub
registers the webhook itself (nothing to copy-paste), the installation token
grants write access to their repo, and PRs are authored by the app rather than
by a person.

Requires replacing the `gh` shell-outs with authenticated API calls, or minting
a short-lived installation token into `GH_TOKEN` per job.

### R3 — Write access to a repo we don't own

`implement()` pushes to `origin` and opens a same-repo PR (`agent.py:337-341`).
On a repo we don't own we have neither the push right nor the branch.

R2 solves this for anyone who installs the App. For anyone who won't, the
fallback is fork-based: `gh repo fork --clone` (origin = fork, upstream =
parent), fetch the base from `upstream`, and
`gh pr create --repo <upstream> --head <fork-owner>:<branch>`.

`keep_plans_only()` and `assert branch != c.base` hold unchanged in both paths.

### R4 — Repo provisioning without a pre-existing local clone

`worktree()` (`agent.py:108`) assumes `c.path` is an existing clone. Onboarding
must clone (or fork-and-clone) on first use. Two lines for the clone case; the
fork case is R3.

Also needs a retention policy: N tenants' clones and worktrees on one disk is
unbounded growth. Decide when a worktree is reaped.

### R5 — Per-repo identity, approval and cost

Three things that are global today and must become per-repo:

- **Approval.** `ALLOW` is one env-var list (`agent.py:36`), so a login
  allowlisted for one repo can approve on any other. Must be scoped to the repo
  — default to its collaborators, or the installing user.
- **Whose account pays.** Every run currently bills the operator's claude.ai
  session. BYOK vs managed tier is a product decision, not a code one, but
  nothing works multi-tenant until it is answered.
- **Caps.** There is no quota, rate limit or budget. One registered repo can
  drain the account. Needs a per-repo run cap and a kill switch, independent of
  `MAX_TASKS` (which bounds one issue, not one tenant).

### R6 — Pluggable agent harness (user picks Claude, Codex, …)

The user chooses per repo — or per issue — which coding agent runs their tasks:
Claude Code, OpenAI Codex CLI, Cursor CLI, Aider, others.

`claude()` (`agent.py:120`) is the single hard-coded harness today. It has to
become an adapter interface.

**The model to copy is AgentsRoom's: spawn each provider's CLI natively — no
wrapper, no proxy, no token resale.** An adapter shells out to the provider's
own headless command, and the user's key or subscription goes straight to that
provider. This is not just an architecture preference; it is what makes BYOK
fall out for free and keeps us out of the business of reselling tokens (R5).

What each adapter must supply:

| Concern | Today (Claude Code) | Adapter must provide |
|---|---|---|
| Invocation | `claude -p <prompt>` | headless one-shot run of a prompt in a cwd |
| Output | `--output-format json` | plain text out, plus token/cost if available |
| Session continuity | `--session-id` / `--resume` with a `uuid5` | resume a thread, or an explicit "no resume" fallback |
| Publishing guardrail | `--disallowed-tools Bash(git push:*) Bash(gh:*)` | **an equivalent deny mechanism, or the adapter is rejected** |
| Roles | `--agents` JSON + `--agent`, from `agents/*.md` | map planmaker/reviewer to its own persona format |
| Model | `--model sonnet` for tasks; `model:` frontmatter for roles | per-role and per-task model selection |
| Auth | claude.ai login, `ANTHROPIC_API_KEY` popped | its own key, per tenant |

Constraints that stay harness-independent — they are the contract, not
implementation detail:

- `STATUS:` markers are **parsed, never inferred** (`MARKER`). A harness that
  can't be made to emit a bare marker line on its own line is not supported.
- The orchestrator ticks checkboxes and commits; the agent never publishes. A
  harness with no deny-rule equivalent cannot be trusted with the task loop —
  the sandbox in PRODUCT-BRIEF.md §5 is the alternative containment, not an
  optional extra.
- Planning writes `agent-plans/<n>/` and nothing else, enforced after the fact
  by `keep_plans_only()` — this one already works for any harness.

Open: **fallback vs strict.** If a user's chosen harness fails or is
unavailable, do we fail the issue or fall back to another? Recommendation: fail,
and name the harness that failed. Silent substitution makes runs irreproducible
and bills the wrong account.

Sequencing note: ship one harness well and add the second when a real user asks.
`PRODUCT-BRIEF.md` §5 already flags "one harness or several" as an open
decision, and every adapter is permanent maintenance against someone else's CLI
flags. AgentsRoom carries 14 of them, which is a feature for a local cockpit
where the user installs the CLI themselves — for us every adapter is also a
binary we have to install, sandbox and keep current on the host. Two done
properly beats fourteen listed on a landing page.

Deliberately **not** required: switching harness mid-conversation (their
headline feature). Our unit of work is a fresh session per task with
`decisions.md` as the only carry-over, so there is no long conversation to
preserve. Per-repo, or at most per-issue, selection is enough.

### R7 — Concurrency

One worker drains `jobs` one at a time (`worker()`), which is deliberate and
correct for one user. Multi-tenant, a single 15-task implementation stalls every
other tenant's issues behind it.

Needs a queue with per-repo rather than global serialization: one job at a time
*per repo*, N repos in parallel. Phase 5 in the build plan.

### R8 — UI and control-plane API

`ui/` is static HTML with nothing to talk to; `agent.py` exposes exactly one
endpoint, the webhook, guarded by an HMAC (`Hook`). The UI needs an
authenticated API: register a repo, list repos, pick a harness, view run status,
see logs, cancel a run.

User auth (GitHub OAuth is the obvious choice, since the App install flow is
already GitHub) does not exist in any form today.

### R9 — Observability

The only run record today is the issue's comment thread plus stdout. A user
whose run failed at task 9 of 12 should see that in the UI without reading a
journal on someone else's server.

---

## 5. Open questions

- Who pays for the model — BYOK, managed tier, or both? Blocks R5.
- Fork-based flow: support it at all, or is "install the App" the only supported
  path? App-only is materially less code.
- Second harness: on demand, or as a launch differentiator?
- Private repos: in or out for v1?
- Where do runs execute — the operator's box (today), or a sandbox per run
  (PRODUCT-BRIEF.md §5)? A stranger's repo running arbitrary code on a shared
  host is a real problem the moment onboarding is self-serve.

## 6. Explicitly out of scope

- A chat surface. Agents are triggered, not conversed with.
- A hosted IDE or streamed terminal. Logs, diffs, and a PR.
- Auto-merge. A human always merges.

## 7. Parking lot

_Add here; anything that graduates becomes an R-item above._
