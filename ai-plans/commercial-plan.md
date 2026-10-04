# Rookery — from a working tool to a product people pay for

**Status:** plan, nothing built. The decisions in §7 come first.
**Written:** 4 October 2026

Companion docs: [PRD.md](PRD.md) (requirements), [PRD-Phases.md](PRD-Phases.md) (phases 1–6;
the milestones here are **M0–M7** so the numbers don't collide),
[PRODUCT-BRIEF.md](PRODUCT-BRIEF.md) (the wider platform), [trade-offs.md](trade-offs.md)
(the security debts that M1 and M3 close). When a recommendation here is accepted as a
deferral, it goes into trade-offs.md, as usual.

---

## 1. Where we start

**Built, and worth keeping as it is:** the GitHub App, onboarding with no restart, approval
by write access, per-repo caps, parallel repos, the portal (run log, cancel, workflow
editor, notes), PR review on request, and repo memory. That is 1,578 + 397 lines of stdlib
Python, with a 1,228-line test suite. It runs on one VPS behind Cloudflare Flexible and is
deployed with `scp`.

**What stands between that and taking a stranger's money:**

| Gap | Today | Why it blocks selling |
|---|---|---|
| **Model licence** | `claude` runs on the operator's claude.ai login. `agent.py` pops `ANTHROPIC_API_KEY` on purpose. | Anthropic: *"Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products"*. A hosted product needs an API key, under the Commercial Terms. |
| **Tenant isolation** | `claude` runs as the user that reads `app.pem`, `rookery.db`, every tenant's clone and every tenant's notes | One prompt-injected issue can act as the App on every installation (trade-offs #7, #14, #15) |
| **Known holes** | trade-offs #5 (`gh` login), #6 (SSH), #8 (port 80), #9 (Flexible) | Each one is a single step from losing the box or a tenant's data |
| **Old CLI and models** | The box has no AVX2, so the CLI is pinned at 2.1.110 and `opus`/`sonnet` resolve to 4.6 models (DEPLOY.md, "AVX2") | Customers compare it with tools on current models, and we can't take CLI security fixes |
| **Tenancy and money** | One global `RUN_CAP`/`USD_CAP`, applied per repo. No account, no plan, no payment. | There is nothing to sell yet |
| **Durability** | Queued jobs are only in memory. A restart drops them with no row and no comment. A running job becomes `interrupted`, and its issue stays on its phase label with no comment. | A paying user's trigger disappears without a trace |
| **No limit on parallel jobs** | Each repo gets a worker. Nothing caps the number of `claude` processes on the box. | 30 repos triggered at once means 30 CLIs plus their test runs, and the box runs out of memory |
| **Operations** | Deploys by `scp`, no CI, no staging, no backups of `rookery.db` or the notes, no alerting. The service can't take webhooks for about 20 s after a restart. | Every incident gets found by a customer |
| **Operator view** | The `PAUSED` file, `journalctl`, and SQLite run by hand | No way to manage accounts, refunds or abuse |

---

## 2. The gate before any code: can this be sold? (M0)

The licence finding changes the economics, not only the config:

- **Managed tier = the operator's API key.** We pay real money per token. `total_cost_usd`
  stops being a nominal figure and becomes our cost of goods. The caps already work in
  dollars, so they now protect real money. The CLI computes that figure at API list
  prices, so the costs already in `rookery.db` are usable for pricing.
- **The key is readable by the agent until M3.** It's in the `claude` process's env. M3's
  metering proxy takes it out of the agent's reach. Until then, set a spend limit on the
  Anthropic Console workspace as a backstop.
- **Branding:** "Powered by Claude" is allowed. The product must not present itself as
  Claude Code. The landing page passes this today; keep it that way.
- **GitHub Marketplace isn't the first billing channel.** Paid plans need a verified
  publisher organization and at least 100 installations. Plan it for later, for discovery
  (M7).
- **Rookery's own licence.** The repo is private and has no licence, while
  `docs/self-hosting.md` tells readers to deploy from it. Decide (§7) whether self-hosting is
  a product.

Pull the real numbers before choosing prices. The box has no `sqlite3` CLI, so run this
with `python3`:

```sql
SELECT j.act, COUNT(DISTINCT j.id) AS jobs,
       ROUND(SUM(json_extract(s.detail, '$.usd')) / COUNT(DISTINCT j.id), 2) AS usd_per_job
FROM jobs j JOIN steps s ON s.job = j.id AND s.what = 'claude'
WHERE j.status = 'ok' GROUP BY j.act;
```

---

## 3. Area by area

### 3.1 Security

Three layers, cheapest first.

1. **Close the known holes (M1).** trade-offs #5, #6, #8 and #9 are each an hour or less.
   They're written up already.
2. **Least privilege on the box (M1).**
   - Run `claude` as a second user (`rookery-run`) that can't read `/opt/rookery`, `.env`,
     `app.pem` or `rookery.db`. This is the small fix for #7. Tenants' clones are still
     shared under that one user; M3 closes that.
   - Mint **repo-scoped installation tokens**: `POST /app/installations/{id}/access_tokens`
     takes `repositories` and `permissions`. A leaked token then reaches one repo, not the
     whole installation.
   - Load secrets with systemd `LoadCredential=`, not from a readable `.env`.
   - Add nginx `limit_req` on `/auth/*` and `/api/*`.
   - Move the portal's inline `<script>` into `portal.js` and send a strict CSP.
3. **A sandbox per `claude` call (M3), the real fix.** It mounts only this issue's
   worktree, the job's plugin dir and the repo's session dir. The root filesystem is
   read-only. Memory, CPU, PIDs, disk and wall-clock time are capped. There's no host
   network: egress goes through an allowlist (the metering proxy, package registries) and
   **not GitHub**, because the orchestrator does every git and `gh` call. The model key
   never enters the sandbox (§3.9, M3).

Also needed before the public launch:

- an outside pen test;
- `/.well-known/security.txt` and a security page that says how code is isolated (teams buy
  on this);
- a one-page incident runbook;
- a retention policy: logs kept N days, clones and notes deleted on uninstall (done today),
  and run-log rows deleted too (not done today).

### 3.2 User management

**GitHub stays the identity provider.** No passwords, no invitations, no Rookery-side teams.
Roles already exist on GitHub; we read them, we don't copy them.

| Role | Can | Comes from |
|---|---|---|
| Viewer | see repos, runs, workflow, notes | the user's installations ∩ `REPOS` (today) |
| Writer | approve, reply, cancel, propose config, clear notes | `can_write()` (today) |
| Billing owner | change the plan, pay, see invoices and usage | org admins: `GET /orgs/{org}/memberships/{user}`, which needs the App's *Members: read* permission. Existing installs must accept the new permission. For a personal account, the user. |
| Operator | everything in §3.5 | an `OPERATORS` list of GitHub logins in `.env`, each with 2FA. Never derived from a GitHub permission. |

New pieces:

- an `accounts` table, one row per installation account (user or org);
- handle `github_app_authorization` (revoked) by deleting that user's sessions;
- a "refresh my repos" button (signing in again does this today);
- per-account data export and deletion.

**Not building:** email and password sign-in, SAML (GitHub Enterprise's SSO already gates
org members), seats.

### 3.3 Subscriptions and billing

- **Unit: usage, not seats.** Our cost is model spend per run, and seats don't follow it.
  Each plan has a monthly **included credit**, a **hard cap** (today's behaviour: stop
  before the next call, and the next `approve` resumes), and **prepaid top-ups**.
  No metered overage: no surprise bills, and no metering integration with the payment
  provider.
- **Shape (to test with the beta, not final):**
  - Trial: 14 days, a small credit, 3 repos.
  - Team: monthly fee with included credit, N repos, concurrency 2.
  - Business: more credit, concurrency 4, Opus available in more slots.

  Set the numbers from §2's query. Price = expected credit use × margin + platform fee.
- **Provider: a Merchant of Record** (Paddle or Lemon Squeezy, for example). It handles
  VAT and sales tax, invoices and card updates worldwide. Integration is a hosted checkout
  link plus one webhook, HMAC-signed, so stdlib is enough. Use Stripe only if your company
  can register for tax itself. Which providers can serve your company's country is a §7
  question.
- **States:** `trial → active → past_due → paused`, then `cancelled`.
  - **Paused** means no new `claude` call. A task already running finishes and is
    committed, and nothing is deleted (see the invariants in §8).
  - **Cancelled** keeps the data for 30 days, then purges it like an offboard.
- **Limits per account, from the plan:** monthly credit, a daily cap (against runaways),
  concurrency, number of repos, and models allowed per slot. `RUN_CAP`/`USD_CAP` stay as the
  instance ceiling, which self-hosters use as they do today.
- **Ledger:** append-only.
  - `runs` gains account, job, model and tokens.
  - A `credits` table holds the grants: plan renewals, top-ups, operator goodwill.
  - Balance = grants − spend. No row is ever updated.

### 3.4 Observability

**For the operator:**

- **Logs:** structured JSON lines with account, repo, job and issue on every line. This is a
  stdlib `logging` formatter. The `[threadName]` repo tag already exists.
- **`GET /metrics`:** Prometheus text format (stdlib, localhost only). Expose:
  - jobs by kind and status;
  - job and call duration;
  - dollars by account;
  - queue age per repo;
  - running jobs;
  - cap hits;
  - webhook 4xx/5xx;
  - GitHub rate-limit headroom;
  - Anthropic 429s and 5xx.
- **One hosted vendor** for uptime checks, logs, alert rules and a public status page
  (Better Stack, for example). Grafana Cloud if you want dashboards. The Aspire dashboard
  already on the box is in-memory and has no alerting, so it isn't for production.
- **Alerts:**
  - a webhook 5xx;
  - job failure rate above its baseline;
  - queue age over 10 min;
  - disk over 80 %;
  - an account spending N× its 7-day average (abuse or a runaway);
  - backup heartbeat missed;
  - Anthropic errors.

**For the customer:**

- the run log, already built;
- a live log over SSE in place of the 4-second poll (stdlib can stream it);
- a usage page: spend by repo, day and job, and credit left.

**For the product:**

- the activation funnel: installed → first trigger → first plan → first approve → draft PR →
  **PR merged**;
- the north-star metric: merged PRs per account per week, and **cost per merged PR**. The
  `pull_request` closed event already arrives (it drives `drop_worktree()`); it only needs
  `merged` recorded.

### 3.5 Operator console (super admin)

`ui/admin.html` and `/api/admin/*`, both behind `OPERATORS`, both in the same server. No
second app.

| View | Shows | Actions |
|---|---|---|
| Overview | running and queued jobs, today's spend, revenue this month, error rate | global pause (the `PAUSED` file stays as the kill switch) |
| Accounts | plan, status, repos, 30-day spend vs revenue (margin), last activity | approve from the waitlist, change plan, override limits, grant credit, pause, suspend, delete data |
| Jobs | every tenant's jobs, with filters | open the log, cancel |
| Deliveries | failed webhook deliveries (from `GET /app/hook/deliveries`) | redeliver |
| System | disk, queue ages, sandbox health, CLI version, backup age, rate-limit headroom | — |
| Audit | every operator action, and every time an operator opened a tenant's log | — |

"View as account" is read-only and audited. An operator who reads a tenant's run log is
reading their issue titles and errors, so it's logged like a write.

### 3.6 Backend

**Keep Python, keep the shape. No rewrite to ASP.NET or Next.js** (PRODUCT-BRIEF §9).

- A rewrite costs about two months and gives customers nothing.
- It throws away the test suite that guards every invariant in CLAUDE.md.
- The hard problems here are isolation, money and operations, not the language.

Revisit only if a team that can't work in Python joins.

**Split by concern as code lands, not in advance:**

- `billing.py` for accounts, plans, the ledger and the provider webhook;
- `/api/admin/*` stays in `web.py`'s `route()`;
- a `sandbox.py` from M3.

`agent.py` stays the pipeline.

**The stdlib rule** ("adding a dependency changes what this project is") needs an explicit
decision (§7). Proposal:

- No pip dependencies in the orchestrator through M5.
- External **services and binaries** are allowed: Docker with gVisor, Litestream, the
  payment provider, the monitoring vendor.
- The first pip dependency is expected at M6 (a Postgres driver) or M7 (encryption for
  hosted BYOK). Both have a named trigger.

### 3.7 Frontend

| Page | State |
|---|---|
| Landing | exists |
| Docs | exist. Add pricing, billing, security and FAQ pages. |
| Pricing | new |
| Onboarding | new, and the page that decides conversion. Install → back on the portal signed in (the Setup URL already does this) → a checklist that ticks itself from the run log: repo live, labels made, first issue labelled, plan, approve, PR. "Create a demo issue" opens a pre-filled GitHub new-issue URL, so we make no API write. |
| Portal | exists. Add Usage, Billing and Account settings. |
| Admin | new (§3.5) |
| Status | at the vendor |
| Terms, Privacy, DPA, AUP | new |

Stay on vanilla JS with no build step and follow DESIGN.md. Split `portal.html`'s script
into one file per view before it grows past ~1,000 lines. A framework only earns its place
when two people edit the UI at the same time.

### 3.8 Desktop app

**Recommendation: don't build a native one.** PRD §3 already found that the desktop cockpit
is AgentsRoom's product, and that ours is the reverse: headless, with the repo as the
interface. A native app means a second codebase, signing, auto-update and three operating
systems, for a product whose UI is a GitHub issue thread.

What people mean by "desktop", delivered more cheaply:

1. **A PWA:** a manifest plus a service worker. The portal becomes installable and gets
   web push for "plan ready" and "PR opened". About a day of work.
2. **Bring your own compute (M7):** the M6 runner, packaged so customers can run it on
   their own machine or CI with their own model key. This is the honest local story, and
   it's also BYOK with no customer keys stored by us.

If a native shell is ever really needed, wrap the portal in Tauri, not Electron.

### 3.9 Scalability

**Where it breaks, in order:**

1. **Memory and CPU per job.** A CLI plus the repo's own test runs can take GBs.
   - M1: a global `MAX_JOBS` semaphore around `handle()`, sized from measurement.
   - M2: per-account concurrency from the plan.
2. **The box itself.** No AVX2 means no current CLI. **The first hosting move is a better
   box, not more boxes (M1).** A dedicated-vCPU VPS that exposes AVX2 (DEPLOY.md lists
   providers) can probably run 8–16 jobs at once; measure it. Jobs are bursty and gated by
   human approval, so that's likely the first 50–100 paying teams.
3. **Disk.**
   - Partial clones (`git clone --filter=blob:none`).
   - Reap the worktrees of issues idle for 30 days, but only if `unpushed()` is clean, as
     today.
   - Prune run-log rows older than 180 days.
4. **Anthropic rate limits** are per organization. Request tier increases early, and
   schedule fairly across accounts (round-robin, not FIFO) so one big account can't starve
   the others.
5. **One box = one failure.** This is M6, when the trigger fires.

**Three kinds of local state pin a job to its box.** M6 has to move each of them off it:

| State | Today | Off the box |
|---|---|---|
| The plan, between plan and `approve` | uncommitted files in a local worktree | stored by the control plane (they're small), restored by whichever runner gets the job |
| Claude session transcripts (resume, memory digests) | `~/.claude/projects/*/<sid>.jsonl` | object storage, keyed by session id, restored before `--resume` |
| Task commits before the PR opens | local until the push at the end | push the branch after each task commit, so any runner resumes from GitHub (this changes what users see: the branch appears early) |

Stay on SQLite (WAL, replicated by Litestream) until the control plane needs a second node.
Only then move to Postgres. Kubernetes and a message broker aren't needed at any milestone
here: runners can long-poll the control plane over HTTPS.

### 3.10 Other things a sellable product needs

- **Legal:**
  - a company entity;
  - Terms of Service, including that AI-written code is reviewed by the customer before
    they merge it;
  - a Privacy Policy;
  - a DPA;
  - a subprocessor list (Anthropic, GitHub, Cloudflare, the host, the payment provider, the
    monitoring vendor);
  - an Acceptable Use Policy.

  Start from templates, then pay for an hour of a lawyer's time.
- **"How we handle your code" page.** Code goes to Anthropic's API under the Commercial
  Terms. Clones are deleted on uninstall. Nothing is kept past the retention policy.
  Teams decide on this page.
- **Reliability (M1):**
  - a durable queue: a `jobs` row with status `queued` at `submit()`, re-queued at startup;
  - interrupted jobs get a comment and `rookery:error`, so `approve` visibly resumes them;
  - at startup, redeliver the deliveries that failed while the service was down;
  - nightly offsite backups of `rookery.db` and `memory/`, with one restore actually tested.
- **Delivery (M1):**
  - GitHub Actions runs `test_agent.py` on Ubuntu and Windows for every PR;
  - one deploy command: release dirs plus a symlink, tests on the box, a health check,
    rollback = the previous symlink;
  - a staging App on `staging.` with its own systemd unit;
  - every CLI upgrade is tested there first.
- **Support:** a support address, plus "Report a problem" in the portal that attaches the
  run id.
- **Abuse:**
  - trial limits per GitHub account, and refuse trials from new accounts;
  - sandbox CPU and time limits (crypto mining);
  - suspension under the AUP.
- **Margin control:** Opus in `planmaker`/`reviewer` is the expensive part of a run. Make
  the model per slot a plan limit, not only a frontmatter choice.

---

## 4. Milestones

Two launch gates: a **paid private beta** after M2, and a **public launch** after M5.
Every milestone keeps CLAUDE.md's invariants and adds asserts to `test_agent.py`.

### M0 — Can it be sold (3–5 days of work, more calendar time)

- Production runs on an Anthropic API key, with a Console spend limit as a backstop. The
  code change is small: the `ANTHROPIC_API_KEY` pop and its comment go, and the key reaches
  `claude` per call.
- The §7 decisions are recorded.
- The §2 query has run, and there's a week of cost numbers per job type.
- Apply to a Merchant of Record now: approval takes calendar time.
- Legal drafts.

**Done when:** no tenant's run uses a claude.ai login, and §7 is decided.

### M1 — Harden it and make it operable (1–1.5 weeks)

- **Security:**
  - trade-offs #5, #6, #8 and #9;
  - #7's small fix (`rookery-run`);
  - repo-scoped tokens, `LoadCredential=`, `limit_req`, CSP.
- **Hosting:** a new box with AVX2, the current CLI and current models. Staging on the
  same box, with a second App.
- **Reliability:**
  - the durable queue;
  - interrupted jobs commented;
  - failed deliveries redelivered at startup;
  - `MAX_JOBS`;
  - backups plus one tested restore;
  - run-log pruning.
- **Delivery:** CI, the one-command deploy, rollback.
- **Observability, the basics:** `/healthz`, an external uptime check, an alert on
  `FAILED` / `dispatch error` lines.

**Done when:**

- the agent, asked to, can't read `app.pem`, `.env` or `rookery.db`;
- a deploy is one command and gated on tests;
- killing the service with jobs queued loses no trigger and leaves no issue stuck in
  silence;
- production runs the current CLI.

### M2 — Accounts, plans, and a paid design-partner beta (1–2 weeks)

- **Accounts and the gate:**
  - the `accounts` table, created on `installation`;
  - the App becomes public;
  - an install from an account not on the invite list is recorded as `pending` and not
    onboarded (no clone, no labels) until an operator approves it.
- **Limits:**
  - limits per account from its plan;
  - per-account concurrency and per-account pause;
  - the credit ledger.
- **Operator tools, the minimum:** `admin.html` with Accounts (approve, plan, credit,
  pause, suspend) and the audit table.
- **Payment, kept manual:** a Merchant of Record payment link per plan, matched to the
  account by an operator. The webhook automation waits for M4.
- **The beta:** 5–10 design-partner teams and a weekly call. Measure merge rate, cost per
  merged PR, and time to first PR.

**Done when:** an invited org installs, gets approved, runs within its plan's limits and
pays, and an uninvited install stays inert.

### M3 — Isolation: a sandbox per call and a metering proxy (2–3 weeks)

**Blocks public sign-up.**

- **Spike first (2 days):**
  - gVisor (`runsc`) under Docker on the new box: cold start, memory per job, the CLI
    inside.
  - Compare with Anthropic's Managed Agents self-hosted sandbox.
  - Pick one.
- **The container** is as in §3.1, with one image that has the common toolchains (Node,
  Python, .NET, Go, Java) and a pinned CLI, rebuilt in CI. A repo-provided image or
  devcontainer comes later.
- **Egress:** a deny-by-default network. Its only ways out are an allowlisting CONNECT
  proxy (package registries) and the metering proxy.
- **Metering proxy (stdlib):**
  - The sandbox gets `ANTHROPIC_BASE_URL` pointing at the proxy and a per-job token. The
    proxy adds the real key, so **the key never enters the sandbox**.
  - It reads `usage` from each response, including streamed `message_delta` events, prices
    it and records it per account. That makes it the billing source of truth, with
    `total_cost_usd` as a cross-check.
  - It refuses requests past the budget. This ends `check_caps()`'s "one call can overshoot"
    ceiling.
- **Unblocked by the sandbox:**
  - fork PR reviews (trade-offs #14);
  - notes isolation (#15): the notes are mounted read-only, and only the orchestrator
    writes them.
- **`SANDBOX=0`** keeps today's path, for local development on Windows.
- **Red-team checklist as tests:** read `app.pem`, read another tenant's clone,
  `curl` an arbitrary host, read the model key, fork-bomb, fill the disk, run for 2 h. All
  must fail safely.

**Done when:** the red-team checklist fails safely on production, and an outside pen test
has no open high findings.

### M4 — Self-serve: sign-up, checkout, onboarding (about 2 weeks)

- Pricing page, hosted checkout, and the provider webhook that sets plan and status.
- The provider's customer portal for cards and invoices.
- Trial credit, the dunning states, the usage page.
- Emails at 80 % and 100 % of credit (stdlib `smtplib` or the provider API).
- The onboarding checklist and "Create a demo issue".
- Legal pages live. Terms accepted at first sign-in, with version and time recorded.
- Account export and deletion.

**Done when:** a stranger goes from the landing page to a draft PR and a paid plan with no
operator action, and a failed payment pauses them without losing work.

### M5 — Operator console and observability (1–2 weeks)

- **Operators:** the rest of §3.5 (Jobs, Deliveries, System, view-as).
- **Telemetry:** `/metrics`, logs at the vendor, the alert rules, the status page.
- **Customers and product:** SSE live logs, merged-PR tracking, the funnel.

**Done when:**

- the operator learns of an outage, a stuck queue, a cost spike or a missed backup from an
  alert before any customer reports it;
- "what did account X cost and pay last month" is one page.

**Public launch = M0–M5 done.**

### M6 — Scale out (3–4 weeks, only when triggered)

**Trigger:** any one of these:

- p95 queue wait over 10 min at peak for two weeks;
- the box above 70 % memory at peak;
- a customer who needs an uptime promise.

**The split:** a control plane (webhook, API, portal, scheduler, metering proxy, token
minting; `app.pem` lives only here) and N stateless runners.

**The runner protocol:**

- an HTTPS long-poll claim with a runner key;
- a lease and a heartbeat;
- a reaper that re-queues expired leases;
- the runner gets a 1-hour repo-scoped token.

**The rest:**

- Move the three kinds of local state off the box (§3.9).
- A clone cache per runner.
- Fair scheduling across accounts.
- Postgres if the control plane needs a second node.

**Done when:** killing a runner mid-task costs at most that task, and adding a runner adds
throughput with no code change.

### M7 — Expansion, each item on demand

| Item | Ask for it when |
|---|---|
| Bring your own compute (packaged M6 runner) | Customers want their own key or hardware, or self-hosting is a product (§7) |
| PWA with web push | Users ask for notifications outside GitHub |
| Hosted BYOK (KMS-encrypted keys, injected by the metering proxy, never in a sandbox) | A customer won't use our key and won't run a runner |
| Second harness (PRD-Phases Phase 6) | A paying customer names one |
| Linear and Jira as issue sources | Design partners track work outside GitHub Issues |
| Scheduled agents (PRODUCT-BRIEF) | Repeated requests for recurring chores |
| GitHub Marketplace listing | 100 installs and a verified publisher org |
| SOC 2 Type I, data residency | A deal needs it |

**Not on any milestone:** a native desktop app, a rewrite, our own password sign-in,
Kubernetes, microservices, metered overage billing, Postgres before M6, and more than one
harness before someone asks.

---

## 5. Estimates

One person, focused. Coding is the fast part. The calendar goes to approvals (the payment
provider, legal), live testing, and design-partner feedback.

| Milestone | Estimate | What makes it slow |
|---|---|---|
| M0 Can it be sold | 3–5 days | Legal and the payment provider's approval |
| M1 Harden and operate | 6–8 days | Moving the box, the deploy pipeline, testing a restore |
| M2 Accounts and beta | 6–8 days | Recruiting design partners |
| M3 Sandbox and meter | 10–15 days | The spike, toolchains in the image, streaming through the proxy, the pen test |
| M4 Self-serve | 8–10 days | Billing state edge cases, onboarding polish |
| M5 Console and observability | 6–8 days | Choosing alert thresholds that page for real problems only |
| **To public launch** | **~8–11 weeks** | |
| M6 Scale out | 15–20 days | Moving session state and plans off the box |

---

## 6. Risks

1. **The core loop is becoming a commodity.** GitHub's Copilot coding agent (assign an
   issue, get a draft PR), Anthropic's `claude-code-action`, OpenAI Codex and Google Jules
   all do issue → PR. Check where each one is now before setting prices.

   What Rookery has that, as far as I know, they don't package together:
   - plan-then-approve, with the plan committed into the PR;
   - hard spend caps per repo;
   - a workflow steered by the repo and its owner, read from base;
   - a fresh session per task.

   The M2 beta exists to find out whether that is worth paying for. Run it before M3–M5
   take two months.
2. **Margin.** Cost per issue varies a lot, and Opus planning dominates it. The caps bound
   the loss; the price must cover the median, not the best case.
3. **Toolchain fit in the sandbox.** A repo whose tests need a database or Docker can't run
   them inside it. Tasks then report done without verification (trade-offs #1). Say so in
   the docs. Repo-provided images come later.
4. **CLI churn.** Flags and binaries change, as the AVX2 break showed. Pin the CLI, and run
   every upgrade through staging first.
5. **One operator, one box,** until M6. Promise no SLA in v1, and keep a public status
   page.
6. **Liability for AI-written code.** Covered by the Terms, and by the rule that a human
   always merges.

---

## 7. Decisions needed from you

| # | Decision | Recommendation | Why |
|---|---|---|---|
| 1 | Who pays for the model | **Our API key for hosted plans.** BYOK only through bring-your-own-compute (M7). | The first run must just work. We store no customer keys. |
| 2 | Payment provider | **A Merchant of Record** | No tax registrations for a solo founder. Depends on which providers serve your company's country. |
| 3 | Rookery's licence and self-hosting | **Closed SaaS now.** Self-hosting becomes the bring-your-own-compute offer or an enterprise licence later. Take `docs/self-hosting.md` off the public docs until then. | A public self-host guide to a private repo confuses buyers |
| 4 | The stdlib rule | **No pip dependencies through M5. External services and binaries are allowed.** | Keeps the code what it is, and still allows the sandbox and backups |
| 5 | Sandbox technology | **Spike gVisor and Managed Agents' self-hosted sandbox, then pick** | Cold start and memory per job decide it, not opinion |
| 6 | Hosting | **A new AVX2 VPS now, with staging on it** | It unblocks the current CLI and models. One box is enough until M6. |
| 7 | Pricing unit | **Plan with included credit + hard cap + prepaid top-ups** | It follows our cost, and nobody gets a surprise bill |
| 8 | Beta before self-serve | **Yes: paid design partners right after M2** | Proof of willingness to pay before 6+ weeks of M3–M5 |
| 9 | Push the branch after every task (M6) | **Decide at M6** | It changes what users see; it isn't needed before runners are stateless |

---

## 8. Invariants to add to CLAUDE.md as each one is built

- **The model API key never enters a sandbox.** The metering proxy adds it. (M3)
- **`app.pem` lives only where tokens are minted.** Every other component gets
  repo-scoped, short-lived tokens. (M1, M6)
- **Billing never strands work.** A paused or cancelled account gets no new `claude` call.
  A started task finishes and is committed. Nothing pushed or local is deleted before the
  retention rules say so, and `unpushed()` still guards every deletion. (M2)
- **Every operator action is audited**, and so is every operator read of a tenant's data.
  (M2)
- **Operators are an explicit list,** never derived from a GitHub permission. (M2)
- **Limits are checked before every call and enforced during it** by the meter. (M3)

---

Sources:
- [Agent SDK overview — claude.ai login note, branding, Commercial Terms](https://code.claude.com/docs/en/agent-sdk/overview)
- [GitHub Marketplace — requirements for listing an app](https://docs.github.com/en/apps/github-marketplace/creating-apps-for-github-marketplace/requirements-for-listing-an-app)
- [GitHub Marketplace — pricing plans](https://docs.github.com/en/apps/github-marketplace/selling-your-app-on-github-marketplace/pricing-plans-for-github-marketplace-apps)
