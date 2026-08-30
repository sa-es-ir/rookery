# GitHub Issue Agent — Build Plan

An agent pipeline triggered by GitHub issues: a developer opens an issue, the
agent reads it, asks clarifying questions as issue comments, produces a plan,
waits for explicit human approval, then implements on a branch and opens a
draft PR.

> **Portability note.** This document is intended to move into its own repo.
> It is written to be self-contained — every reference to an external
> artifact uses a full absolute path rather than a path relative to whatever
> repo this file currently sits in.

**Status:** Phase 1 not started. Option A (self-hosted) is the chosen path;
Option B (Anthropic-hosted) is documented below as the deliberate
alternative — revisit it if Phase 1 proves more work than the control is
worth.

---

## Prior art — VSDD

This design borrows its shape from VSDD (Verified Spec-Driven Development),
an existing pipeline in the `core_ai` repo. Full paths:

| Artifact | Absolute path |
| --- | --- |
| Methodology skill | `C:\Users\Saeed\source\repos\core_ai\plugins\vsdd\skills\spec-driven-development\SKILL.md` |
| Agent definitions (11) | `C:\Users\Saeed\source\repos\core_ai\plugins\vsdd\agents\` |
| Plugin README (overview of agents, skills, standards, ADRs) | `C:\Users\Saeed\source\repos\core_ai\plugins\vsdd\README.md` |
| Engineer-facing runner guide | `C:\Users\Saeed\source\repos\core_ai\docs\vsdd-runner-guide.md` |
| The orchestrator itself | `C:\Users\Saeed\source\repos\core_ai\scripts\vsdd-runner.ps1` |
| Manual fallback workflow | `C:\Users\Saeed\source\repos\core_ai\docs\vsdd-interactive-runbook.md` |

Three VSDD ideas carry over directly, and are the reason the design below
looks the way it does:

1. **Author → gate → human approval → execute.** VSDD crystallises a spec,
   verifies it adversarially, pauses for an explicit human `yes`, then
   executes phase by phase. This pipeline does the same with an issue in
   place of a design document.
2. **State lives on disk, not in a session.** VSDD phases each run in a
   fresh agent session and communicate through files
   (`.vsdd/run-state.json`, `spec.md`, `tasks.yaml`). Here, **GitHub itself
   is the state store** — labels are the phase marker, comments are the
   handover surface. That makes state visible to the whole team and
   overridable by hand.
3. **Structured markers over prose parsing.** VSDD agents emit lines like
   `COMPONENT_NAME:` and `PR_ID:` that the runner parses deterministically.
   This pipeline uses `STATUS: questions` / `STATUS: plan-ready` /
   `STATUS: implemented` the same way, rather than asking the orchestrator
   to infer outcome from free text.

VSDD's adversarial verifier gate between every phase is carried over only in
part. A **reviewer agent** now runs once, after the task loop and before the
push, and is **advisory**: its findings go into the PR body and a
`changes-needed` verdict sets `claude:error`, but the branch is always pushed
and the draft PR is always opened. Work is never stranded in a local worktree,
and the human reading the PR is already where the findings are.

Two stronger gates are deliberately not built — see
[Reviewer escalation](#reviewer-escalation-future) below.

---

## The two options

Both options solve the same problem — "something must be listening when a
GitHub issue is opened, and it must be able to run Claude Code against a
repo." They differ in who operates the listener.

### Option A — Self-hosted (chosen)

You run a webhook receiver and orchestrator yourself: on your laptop during
Phase 1 (with a smee.io tunnel standing in for a public endpoint), on a VPS
from Phase 5 onward. It invokes the `claude` CLI headless (`claude -p`) in a
git worktree per issue and drives GitHub through the API.

**Why it was chosen**

- **Full control of the state machine.** The label lifecycle, the approval
  regex, the per-repo config, the triage rules — all ordinary code you can
  read, test, and change. Nothing depends on a hosted feature behaving the
  way you hoped.
- **Deterministic security gates.** Approval detection, commenter
  allowlisting, and "never push to `main`" are enforced by the orchestrator
  in code — not by prompt instructions or a platform's policy surface.
- **Arbitrary local tooling.** Phase 4's OpenSpec integration, custom
  triage, repo-specific build/test commands, and any future verifier agent
  all run wherever you decide.
- **Multi-repo routing on your terms.** Phase 3's per-repo config is a
  dictionary in your code.

**What it costs**

- You own the uptime, the TLS certificate, the secrets, the log rotation,
  the restart-on-crash, and the alerting when a job fails at 2am.
- VPS cost from Phase 5 onward.
- Materially more code than Option B before the first working demo.

### Option B — Anthropic-hosted cloud routine (the alternative)

Claude Code supports **routines** (scheduled/triggered cloud agents) that can
be wired to a **webhook trigger** — via the `RemoteTrigger` tool's
`create_webhook_trigger` action, or the `/schedule` skill. You name the event
source (a GitHub repository), the events to fire on (e.g. `issues.opened`), a
structured filter, and the routine the event should fire. Anthropic hosts the
run; there is no server, no tunnel, no TLS, and no process to keep alive.

**Why it is attractive**

- **Near-zero infrastructure.** No VPS, no smee.io, no systemd unit, no
  secrets management, no uptime burden.
- **Possibly free session continuity.** The tooling notes that a routine
  posting into an existing session adds to that session rather than creating
  a new run — which, if it holds for issue-comment follow-ups, gives the
  ask-a-question / developer-replies / resume loop without you building the
  `repo#issue → session id` map that Option A needs.
- **Fastest path to "does this idea even work?"** — a single pilot repo
  could be wired up in an afternoon.

**Why it was not chosen as the primary path**

- **Less control at the gates.** Approval detection, commenter
  allowlisting, and branch-protection behaviour become a matter of prompting
  and platform configuration rather than code you own.
- **Custom logic is harder to place.** Deterministic triage from issue-form
  fields, OpenSpec scaffolding, and per-repo build commands are awkward
  when the orchestration lives in a hosted routine.
- **Unverified assumptions.** The session-continuity behaviour above is
  inferred from the tool documentation, not yet tested for this use case.

**When to revisit it.** If Phase 1 stalls on infrastructure rather than on
pipeline logic, spend a day wiring one repo through Option B first. It is
also a legitimate permanent answer for simple repos even if Option A wins
for the complex ones — the two can coexist.

---

## Architecture (Option A)

- **Webhook receiver** — small HTTP service that receives GitHub webhook
  events, validates the HMAC signature, filters for the target repo(s) and
  the trigger condition, and enqueues work.
- **Session mapping** — `repo#issue → claude session id`, stored locally, so
  separate webhook-triggered invocations can resume the same Claude Code
  session (`claude --resume=<id>`) instead of starting cold each time.
- **State machine via GitHub labels** — `claude:planning` →
  `claude:awaiting-input` → `claude:awaiting-approval` → `claude:implementing`
  → `claude:done` (plus `claude:error`). Visible to the team, overridable by
  hand.
- **Git worktree per issue** — each issue gets its own branch
  (`feature/issue-<n>`) and working directory, so concurrent issues cannot
  clobber each other.
- **Claude Code invoked headless** (`claude -p`, non-interactive), emitting
  the `STATUS:` markers described above so the orchestrator parses outcome
  rather than inferring it.

**Guardrails — apply from day one, not deferred to a later phase:**

- The agent never runs `git push` or `gh pr create` itself; only the
  orchestrator does, after the agent reports success via a structured marker.
- The agent never commits to `main` — always inside the per-issue worktree
  branch. Enforce this with GitHub branch protection as well, not only with
  prompt instructions.
- Triggering requires explicit opt-in (a dedicated label or an exact mention
  phrase). Never react to every issue opened.
- Only comments from an allowlisted set of users (repo collaborators) drive
  state transitions.

---

## Phase 1 — Local machine, single repo, feature issues only

No VPS. Goal: prove the mechanism end to end before spending money on
hosting.

### 1.1 Prerequisites

- smee.io client (`npx smee-client`) forwarding GitHub webhook deliveries to
  `localhost`. GitHub cannot deliver to a private address; the tunnel is what
  makes a laptop-hosted Phase 1 possible.
- A webhook plus a fine-grained PAT on the one pilot repo. A full GitHub App
  is Phase 3 — unnecessary for a single repo.
- `claude` CLI installed and authenticated locally.
- A "Feature" issue template (GitHub issue form) requiring title and
  description, plus a `claude:trigger` label or exact mention phrase for
  explicit opt-in.

### 1.2 Webhook receiver skeleton

- Minimal HTTP server (stack undecided — see Open Decisions) listening on
  `localhost`, fed by the smee.io tunnel.
- Verify `X-Hub-Signature-256` against the webhook secret.
- `issues.opened`: if the trigger label or phrase is present, enqueue a
  "start planning" job.
- `issue_comment.created`: if the commenter is allowlisted, is not the bot,
  and the issue currently carries a `claude:*` label, enqueue a "continue"
  job carrying the comment body.

### 1.3 Job runner / orchestrator

A single in-process worker, one job at a time. No queue framework yet —
Phase 1 volume does not justify one.

**"start planning" job**

1. `git worktree add` a new branch `feature/issue-<n>` from `main`.
2. Invoke `claude -p` headless with the issue title and body, instructed to
   produce a plan (a plain `PLAN.md` for now — see Phase 4 for OpenSpec), to
   ask questions if anything is unclear, and to end with a `STATUS:` marker.
3. Save the returned Claude session id into the `repo#issue → session` map.
4. Questions present → post them as an issue comment and label
   `claude:awaiting-input`. No questions → post the plan summary and label
   `claude:awaiting-approval`.

**"continue" job, label = `claude:awaiting-input`**

1. Resume the saved session with the new comment as input.
2. More questions → post them, stay `awaiting-input`. Plan finalised → post
   the summary, move to `claude:awaiting-approval`.

**"continue" job, label = `claude:awaiting-approval`**

1. Comment matches an approval regex (`/^(approve|lgtm|go ahead)/i`) from an
   allowlisted user → resume the session with "implement the approved plan",
   label `claude:implementing`.
2. Otherwise → treat it as further feedback, resume with it, stay
   `awaiting-approval`.

Approval detection is a regex, not an LLM judgement call — this gate is what
stands between a comment and real commits.

**Implementation completion**

1. The orchestrator — never the agent — runs `git push` and
   `gh pr create --draft`, referencing the issue (`Closes #<n>`).
2. Comment the PR link on the issue and label `claude:done`.

### 1.4 Definition of done

Open a "Feature" issue in the pilot repo → get asked a clarifying question as
a comment → answer it → see a plan comment → reply "approve" → receive a
draft PR. All running from your laptop through the smee.io tunnel.

---

## Phase 2 (future) — Bug / small-fix shortcut path

- Add a `type: bug` issue template.
- Shortcut flow: skip the plan-and-approval phase entirely and go straight to
  implementation (still allowed one clarifying question if genuinely
  blocked), then open the PR. The human reviews the PR itself instead of a
  separate plan-approval step.

## Phase 3 (future) — Multi-repo support

- Replace the pilot's ad-hoc PAT with a GitHub App installed per target repo
  — scoped install tokens and built-in webhook management.
- Receiver routes by repo full name to per-repo config (base branch, issue
  templates, allowed labels).
- Each repo gets its own workspace directory and worktree namespace on the
  host. Job queue keyed by `repo#issue`, still one worker per issue in
  flight.

## Phase 4 (future) — OpenSpec integration

- Replace the ad-hoc `PLAN.md` with an OpenSpec change proposal under
  `openspec/changes/<slug>/`.
- The plan phase produces the proposal; the approval gate becomes "OpenSpec
  proposal approved"; implementation consumes the proposal the way VSDD's
  executor consumes `tasks.yaml`.

## Phase 5 (future) — Move to a server

Infrastructure only. Do this once the pipeline logic is proven locally — not
before.

- Buy the VPS. Replace the smee.io tunnel with a public endpoint (reverse
  proxy plus TLS) receiving GitHub webhooks directly.
- Run the orchestrator as a systemd service (or a container) with
  restart-on-failure.
- Replace the single in-process worker with a real queue (a SQLite-backed
  table, or Redis plus a job library) only if the per-repo/per-issue lock
  proves insufficient in practice. Do not build this ahead of the need.
- Add failure alerting (for example a Slack ping on job failure) — nobody is
  watching a terminal any more.
- Harden secrets handling: webhook secret and GitHub App private key in a
  secrets manager or a restrictive-permission `.env`, never committed.
- Log retention and rotation for per-issue Claude session logs, mirroring
  VSDD's `.vsdd/logs/`.

---

## Reviewer escalation (future)

The reviewer ships advisory. Both stronger forms were considered and rejected
for now; the reasons are the trigger for revisiting them.

**Blocking gate.** `STATUS: changes-needed` stops before `git push`: findings
post as an issue comment, phase goes to `claude:error`, no PR is opened.
Commenting `approve` again re-runs from the first unticked task.

*Why not yet:* the branch then exists only as a local worktree on the
orchestrator host. Nothing on GitHub shows the work happened, and if the
laptop is wiped the commits are gone. Advisory keeps the artifact durable and
still flags the issue. Revisit if draft PRs from failed reviews turn out to be
noise a team actually has to triage.

**Advisory plus a fix loop.** On `changes-needed`, append the findings to
`tasks.md` as new tasks, re-enter the implement loop, then review again.

*Why not yet:* it adds a second convergence loop to a design whose invariants
are all about bounded, provable termination — `MAX_TASKS` checked every
iteration, every tick strictly reducing the open count. A reviewer that can
add tasks breaks "every tick strictly reduces the open count", so it needs its
own separate bound (a max number of review rounds) and a rule for what happens
when a fix round makes the review worse. That is a real design, not an
increment. Revisit once there is data on how often reviews come back
`changes-needed` at all.

## Open decisions to revisit

- Webhook receiver / orchestrator stack — Node, Python, or Go. Undecided.
- Trigger convention — dedicated label, `@claude` mention, or both.
- Whether the Phase 1 plan artifact stays a plain `PLAN.md` or jumps
  straight to OpenSpec (Phase 4 pulls forward if it turns out to be cheap).
- ~~Whether to add a VSDD-style adversarial verifier gate before the PR, or
  leave human PR review as the only gate.~~ Decided: an advisory reviewer
  agent runs before the push; see [Reviewer escalation](#reviewer-escalation-future)
  for the two stronger gates that were not built.
