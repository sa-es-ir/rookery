# Agent Platform — Product & Architecture Brief

**Status:** design draft
**Author:** Saeed
**Last updated:** 24 August 2026

---

## 1. What this is

A multi-tenant platform where developers define AI agents, connect them to their
GitHub repositories, and have those agents do real work on the codebase —
triggered by an issue, a pull request comment, a schedule, or a button in the
dashboard.

The output of a run is a pull request, not a direct push. A human always merges.

## 2. What this is not

- **Not a memory layer for other people's agents.** Orbit Agents occupies that
  space: it runs no models, holds no keys, and is called *by* Claude Code over
  MCP. This platform is the inverse — we own the run, we hold or broker the
  credentials, we produce the output.
- **Not a chat product.** There is no conversational surface. Agents are
  configured once and then triggered.
- **Not a hosted IDE.** We do not stream a terminal to the user. They see logs,
  diffs and a PR.

## 3. Users

| Who | What they want | Mode |
|---|---|---|
| Solo developer / OSS maintainer | Automate review, cleanup, dependency chores | Managed keys, low friction |
| Small engineering team | Custom agents against a private monorepo | BYOK, own provider bill |
| Power user | Swarms, schedules, custom skill files | BYOK, fine-grained secrets |

The managed tier exists so a new user can click *Run* and see something work
before being asked to create a provider account. That first-run experience is
the single biggest determinant of whether this product survives.

## 4. Core concepts

**Agent** — a named configuration: persona/system prompt, harness, model,
required parameters, allowed repos, secret preset, optional schedule.

**Trigger** — what starts a run. Four kinds:
1. GitHub webhook on `issue_comment` / `issues` / `pull_request`, matched
   against a slash-command (`/code-cleaner`, `/security-review`)
2. Dashboard manual run
3. Quartz schedule
4. API call

**Run** — one agent, one task, one sandbox, one budget. Produces a PR and/or
artifacts.

**Swarm** — N runs fanned out from a single trigger. The dispatcher shards the
task space deterministically where it can; the memory claim ledger is a second
line of defence against two agents doing identical work.

**Secret preset** — a named bundle of secrets scoped to specific agents, so a
job only ever receives the credentials it needs.

**Artifact** — anything the agent produces that isn't code: reports, images,
generated files. Collected from the sandbox and stored in Blob.

## 5. Execution model

This is the decision that shapes everything else.

**We do not implement an agent loop.** We run an existing coding harness
(Claude Code, Cursor CLI) headless inside a sandbox and let it do the agentic
work. We own the sandbox, the queue, the control plane and the dashboard.

*Why:* file editing, git operations, subagents, skills and tool orchestration
all come for free and stay current without us maintaining them. BYOK falls out
naturally — the harness reads a key from its environment.

*Cost:* we lose mid-run control. We cannot cap tokens at turn 12 or inject
operator steering between tool calls. Our levers are wall-clock timeout,
killing the session, and reading usage from the harness's JSON output after the
fact. Budget enforcement therefore *terminates* a run rather than throttling it.

*Open decision:* one harness or several. Each has different flags, output
formats and credential handling, so supporting all of them means an adapter per
harness. Recommendation: ship with one, add a second only on demand.

### Sandbox

Azure Container Apps dynamic sessions, custom container pool. Hyper-V isolated,
prewarmed, scales to zero, GA. This replaces the alternative of building
Firecracker microVMs on autoscaled VMs ourselves — which is roughly a month of
work plus ongoing operational burden for the same isolation guarantee.

**Egress must be locked to GitHub and the provider API and nothing else.** A
prompt-injected repository will otherwise exfiltrate the secrets we just
mounted. This is the security requirement, not a nice-to-have.

## 6. Credential handling

Two paths, both of which avoid long-lived secrets on the execution host.

**Provider keys.** Encrypted at rest with a per-tenant data key; master key in
Azure Key Vault, so a database dump alone is worthless. Validated on save with a
cheap probe call. Never logged, never returned through the API — last four
characters only.

**Delivery to the sandbox.** The queue message carries a *single-use redemption
token*, not the secrets. The runner redeems it at the control plane and receives
only the secrets scoped to that agent's preset, plus a one-hour GitHub App
installation token. Secrets never sit in the queue, never live in the image, and
the blast radius of a leak is one job.

**GitHub access.** A GitHub App with short-lived installation tokens. No stored
user PATs.

**Managed tier.** Our own provider account, metered per tenant, hard daily cap.
A runaway loop on a customer's key is still our reputation, so BYOK runs are
capped too.

## 7. Memory

Postgres with pgvector, scoped per repository. Two jobs:

1. **Context** — what previous runs decided and produced, so agent #40 doesn't
   relitigate what agent #3 settled. Hybrid recall (vector + lexical) with
   trust ranking: operator-confirmed facts outrank agent claims, and
   contradictions are surfaced rather than silently overwritten.
2. **Work claims** — conditional writes that let swarm members avoid
   duplicating each other.

## 8. Reliability

- **Dedupe on GitHub delivery ID.** Webhooks are redelivered.
- **Queue is at-least-once.** Runs need an idempotency key.
- **Reaper.** A Quartz sweep for runs that never phoned home: release claims,
  close out billing, kill orphaned sessions. Jobs die silently and without this
  we bill people for compute they didn't get.
- **Cancel path.** Dashboard → control plane → runner → session terminated.
  Partial work is discarded; no PR is opened from a killed run.

## 9. Stack

| Layer | Choice | Note |
|---|---|---|
| Control plane | ASP.NET Core | |
| Queue | Azure Service Bus | |
| Sandbox | ACA dynamic sessions, custom container | GA, Hyper-V isolated |
| Database | Postgres + pgvector | Single store for state and memory |
| Secrets | Azure Key Vault | Master key + GitHub App private key |
| Artifacts | Azure Blob Storage | SAS upload from runner |
| Scheduling | Quartz.NET, Postgres store, clustered | Also drives the reaper |
| Realtime | SignalR | Log and status fan-out |
| Dashboard | Next.js | |

## 10. Build order

**Phase 1 — one path end to end.** Manual dashboard trigger, one hardcoded
agent, one harness, one sandbox, opens a PR. No swarms, no schedules, no
memory. The goal is to learn cold-start time and cost per run.

**Phase 2 — make it a product.** Agent builder UI, secret presets, GitHub App
install flow, webhook slash-commands, live logs, metering.

**Phase 3 — scale-out.** Swarms, sharding, claim ledger, artifacts, schedules.

**Phase 4 — memory.** Cross-run context and trust ranking. Deliberately last;
it is the most interesting part and the least necessary for a first release.

## 11. Open questions

- Cost and cold-start of a coding harness inside an ACA session. **Nobody has
  publicly done this — spike it before committing the architecture.**
- Whether plan-then-approve should be a per-agent toggle or always on. Skipping
  it is fine for cleanup agents, risky for feature work.
- Harness licensing: whether running Claude Code or Cursor CLI as a hosted
  multi-tenant service is permitted under their terms. **Check before building
  the managed tier.**
- Monorepo handling. Shallow clone of a large repository is slow and a full
  clone is worse; may need sparse checkout driven by the agent's repo scope.
- Concurrency limits per tenant, and what happens to queued runs when a tenant
  hits their cap.
