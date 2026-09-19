# Rookery — PRD Implementation Phases

**Status:** Phases 1–4 built and live-tested (notes: [phase1-notes.md](phase1-notes.md),
[phase2-notes.md](phase2-notes.md), [phase3-4-notes.md](phase3-4-notes.md)). Next: §5 step 3,
an invited trial, after the security items in phase3-4-notes.md "Next steps".
Phases 5–6 not started.
**Last updated:** 19 September 2026

Companion docs: [PRD.md](PRD.md) (the requirements this file sequences),
[github-issue-agent-plan.md](github-issue-agent-plan.md) (the original build
plan), [PRODUCT-BRIEF.md](PRODUCT-BRIEF.md) (the wider platform architecture).

The phase numbers below are specific to this file. They do not line up with the
phases in `github-issue-agent-plan.md`.

---

## 1. Before Phase 1

### 1.1 Check the harness licence first

Confirm that Claude Code's terms allow running it as a hosted service for other
people's repos (PRODUCT-BRIEF.md §11). If they don't, most of the work below
loses its purpose. It is about an hour of reading, so do it before writing code.

### 1.2 Decisions

The PRD's open questions (§5) change what gets built. Recommendation for each:

| Question | Recommendation | Why |
|---|---|---|
| Fork-based flow, or GitHub App only? | **App only** | Drops R3's fork fallback entirely. The PRD already notes it is materially less code. |
| Who pays for the model? | **Operator pays in v1, hard per-repo caps** | Bring-your-own-key means storing keys encrypted, and the stdlib has no symmetric cipher. Deferred to Phase 6. |
| Where do runs execute? | **Operator's server, invite-only onboarding** | Strangers' repos on a shared host need the sandbox first. An allowlist of accounts that may install the App is the cheap gate until then. |
| Private repos in v1? | **In** | The installation token covers them at no extra cost. |

### 1.3 What "GitHub App only" means for users

It does **not** take users off github.com. A GitHub App is how integrations run
on GitHub itself; Dependabot, Renovate and CodeRabbit all work this way.

- **Install:** the user opens Rookery's App page, clicks *Install*, and picks
  repos. GitHub then delivers that repo's events to Rookery. No webhook secret
  to copy, no settings to paste.
- **Daily use is unchanged:** label an issue, read the plan in the comments,
  reply `approve`, get a draft PR. Comments and PRs show the App's bot name
  instead of a personal account.
- **Not a desktop or mobile app.** "App" is GitHub's name for this kind of
  integration.

"App only" means there is no second way in for people who won't install it —
the fork flow in R3, where Rookery forks the repo, pushes to its fork and opens
a cross-fork PR. The only users that rules out are those whose owner refuses, or
whose organization forbids, third-party Apps. If those users matter for v1, add
the fork flow to Phase 1.

---

## 2. Phases

Every phase keeps the invariants in `CLAUDE.md` and adds asserts to
`test_agent.py`.

### Phase 1 — GitHub App

**Covers:** R2 and most of R3. Every later phase assumes it.

- Mint a short-lived installation token per job and give it to `git` and `gh`
  as `GH_TOKEN`. The personal `gh` login (`ME`, `agent.py:414`) goes away.
- The App's JWT is RS256, which the stdlib cannot sign. Shell out to the
  `openssl` CLI, the same way `gh` and `claude` are already external CLIs.
- Keep the token **out of the `claude` subprocess environment**. The deny rules
  block `gh` and `git push`; they do not stop an agent reading an env var and
  calling the API with `curl`.
- `ME` becomes the App's bot login (`<slug>[bot]`), so the bot still can't
  approve its own plans.
- `WEBHOOK_SECRET` stays, but it is now the App's single secret, owned by the
  operator and never handed to tenants.

**Done when:** an issue on a repo with the App installed goes from trigger to
plan to `approve` to a draft PR authored by the App, with no `gh auth` on the
box.

### Phase 2 — Onboarding without a restart

**Covers:** R1 and R4.

- Handle `installation` and `installation_repositories` events: clone into the
  workspace, take the base from `repository.default_branch`, create the
  `claude:*` labels (today done once at startup, `agent.py:415`).
- At startup, rebuild the repo list from the App's installations and delete
  `repos.json` (`agent.py:31`). GitHub stays the source of truth, so nothing new
  is stored locally.
- `enqueue()` (`agent.py:397`) checks the live installation list instead of the
  file.
- **Retention:** remove an issue's worktree when its PR is closed; remove the
  clone when the App is uninstalled.

**Done when:** installing on a fresh repo makes it live, and uninstalling frees
the disk.

### Phase 3 — Approval and caps per repo

**Covers:** R5.

- **Approval:** only commenters with write, maintain or admin permission on
  *that* repo can approve, looked up via the collaborator-permission API. This
  replaces the global `ALLOWLIST` (`agent.py:36`). The `APPROVE` regex is
  unchanged.
- **Cost:** record each run's cost, which `claude --output-format json`
  already reports, in SQLite (stdlib).
- **Caps:** a daily cap per repo on runs and on cost, plus an operator pause
  switch. Hitting a cap comments on the issue and sets `claude:error`. Caps are
  independent of `MAX_TASKS`, which bounds one issue, not one tenant.
- Stored in SQLite so a restart does not reset a daily cap.

**Done when:** a collaborator on one repo can't approve on another, and a repo
over its cap stops.

### Phase 4 — Parallel repos

**Covers:** R7.

- One queue and worker per repo: N repos in parallel, still one job at a time
  within a repo.
- Dedupe webhooks on `X-GitHub-Delivery`, because GitHub redelivers.
- Small. It only matters once Phase 2 lets many repos in.

**Done when:** a long implementation on one repo does not delay planning on
another.

### Phase 5 — Control-plane API, UI, run log

**Covers:** R8 and R9.

- **Auth:** GitHub OAuth login with a signed session cookie. A user's repo list
  comes from their own App installations, so GitHub decides who sees what.
- **API:** list repos, run status, run log, cancel. Cancel means tracking each
  running subprocess so it can be killed.
- **Run log:** each run appends its steps to the Phase 3 database, which is
  what lets a user see "failed at task 9 of 12" without reading server logs.
- **UI:** connect `ui/portal.html` to the API, styled per `DESIGN.md`.
- `http.server` is not built to face the internet: run it behind a reverse
  proxy with TLS. Expect `agent.py` to split into a few files here.

**Done when:** a user logs in, sees their repos and a failed run's log task by
task, and cancels a running job.

### Phase 6 — Second harness, on demand only

**Covers:** R6, plus bring-your-own-key from R5.

- Extract an adapter from `claude()` (`agent.py:120`) only when a real user
  wants another CLI. The PRD recommends the same.
- Reject any harness that can't deny publishing or can't emit a bare `STATUS:`
  line.
- Per-tenant keys land here, since that is when the operator stops paying.
  Needs encrypted key storage, which is outside the stdlib.

---

## 3. Estimates

Rough. Writing the code is the fast part; the calendar goes to live testing —
registering the App, real webhook deliveries, and real runs where each task
takes minutes of model time.

| Phase | Estimate | What makes it slow |
|---|---|---|
| 1: GitHub App | 2–3 days | Registering the App, token signing, removing the `gh` login everywhere |
| 2: Onboarding | 1–2 days | Testing install and uninstall events on real repos |
| 3: Approval and caps | 2 days | Cap behaviour across restarts |
| 4: Parallel repos | 1 day | Little; the change is small |
| 5: API and UI | 5–8 days | Login, sessions, security of a public API, cancel, UI wiring |
| 6: Second harness | 3–5 days | Deferred until someone asks |
| **Phases 1–5** | **~2–3 weeks focused** | Excludes the sandbox and Phase 6 |

---

## 4. Assessment and risks

- **Phases 1–4 are good work.** They fit the current design, keep every
  invariant and stay small. Phase 1 is worth doing even if nothing else is:
  it removes the personal token and makes PRs come from a bot.
- **Phase 5 changes what the project is.** A 434-line script becomes a public
  web service with login, sessions and a database. The stdlib can do it, but it
  needs a reverse proxy in front and the single file will stop being readable.
- **Biggest risk: licensing** (§1.1). It sits outside every phase and can void
  most of them.
- **Second risk: the sandbox.** The agent has shell access, so a malicious
  instruction in a stranger's repo runs on the operator's server. Invite-only is
  fine for early users; it is not fine for a public launch. The sandbox
  (PRODUCT-BRIEF.md §5) blocks opening onboarding beyond the invite list.

## 5. Suggested order

1. Check the licence.
2. Build Phases 1–4.
3. Run them with a few invited users.
4. Only then decide on Phase 5, the sandbox, and Phase 6 — with evidence that
   people want the product before a week goes into the UI.
