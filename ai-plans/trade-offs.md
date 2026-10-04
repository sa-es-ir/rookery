# Trade-offs to revisit

Decisions taken on purpose with a known cost. Each one says what it gives up and what
would make it worth revisiting. Add an entry when a decision defers something. When one
is revisited, record the outcome under it rather than deleting it.

---

## 1. `verification-before-completion` is off by default

**Decided 2 October 2026** (skills-and-workflows.md §9.5).

A repo with no `.rookery/workflow.json` gets no task skills. A tenant turns the skill on
with `"task": { "skills": ["verification-before-completion"] }`.

- **Gained:** Phase A changes nothing for existing tenants. No extra time or cost per task
  that they didn't ask for.
- **Given up:** a task can still report `STATUS: task-done` without running the repo's
  checks. A false "done" is the costliest failure in the loop: the reviewer and the human
  then trust code that was never run.
- **Revisit when:** run logs or reviews show tasks marked done whose tests fail, or tenants
  who enable it get noticeably fewer `changes-needed` reviews. Turning it on is a one-line
  change to the built-in default, plus a README note, since it changes every tenant's runs.

## 2. The first catalog has 4 skills, not every bold row

**Decided 2 October 2026** (skills-and-workflows.md §2.4, §9.3).

Phase A ships `repo-recon` (plan slot) and `verification-before-completion`,
`systematic-debugging` and `test-driven-development` (task slot). They're the skills that
fit a slot that exists in Phase A.

- **Deferred to Phase B:** `security-review` (check step) and `changelog` (write step).
  Both need the `after` steps, so shipping them earlier would give tenants skills with no
  slot to put them in.
- **Deferred, no phase yet:** language packs, `frontend-design`, `writing-plans` (compare
  it with the planmaker persona first), `performance-review`, `accessibility-review`,
  `docs-writer`, `webapp-testing` (needs browsers on the server, which has no AVX2).
- **Given up:** the most-requested extra review (security) waits for Phase B.
- **Revisit when:** Phase B lands the `after` steps, or a tenant asks for a specific skill.
- **Outcome, 2 October 2026:** Phase B shipped `security-review` (check) and `changelog`
  (write) with the `after` steps. The "no phase yet" list stands.

## 3. `repo-recon` is off by default too

**Decided 2 October 2026** (A1, skills-and-workflows.md §9.5 extended to the plan slot).

The built-in workflow enables no skills in any slot, not only the task slot. A tenant
turns it on with `"plan": { "skills": ["repo-recon"] }`.

- **Gained:** A1 changes nothing for existing tenants, the same promise as §1.
- **Given up:** plans keep lacking a "How to verify" section unless the planmaker writes
  one unprompted, which is what makes §1's false "done" more likely.
- **Revisit when:** §1 is revisited. Turning both on together is cheap: `repo-recon`
  costs one plan call's context, and it gives `verification-before-completion` the
  commands to run.

## 4. The Workflow view has no rate limit or cache

**Decided 2 October 2026** (A2, skills-and-workflows.md §6).

`GET /api/repos/<owner>/<repo>/workflow` runs `steer()`: a `git fetch` of base, the job's
checks, and a throwaway plugin dir, on every request. The page loads it once when opened,
not on the 4-second poll.

- **Gained:** the page always shows what the next job will read, from the same function,
  with no cache to go stale or invalidate.
- **Given up:** a signed-in user who keeps reloading triggers a fetch to GitHub and a
  plugin assembly each time. Only signed-in users who can see the repo can do it, and
  `fetch()`'s per-repo lock serialises it against the repo's own jobs, so the cost is
  server load and GitHub traffic, not a wrong result. A slow fetch can still delay a job's
  `pin()` by holding that lock.
- **Revisit when:** the journal or GitHub's rate limits show repeated workflow fetches, or
  a job start waits on the lock. The cheap fix is a per-repo result cache of ~30 seconds
  keyed by the fetched sha, or skipping the fetch when the last one is under a minute old.

## 5. The `rookery` user's `gh` login may still be on the box

**Deferred since Phase 1, logged 2 October 2026** (phase5-deploy.md "Still open" 1).

The App replaced `gh auth login` in Phase 1, but the old login was never removed from the
server. `gh auth status` as `rookery` should say "not logged in"; until someone checks, it
may not.

- **Gained:** nothing. It's a leftover, not a choice.
- **Given up:** if the login is there, `~/.config/gh/hosts.yml` holds a long-lived personal
  token the agent can read. The deny rules stop `gh`, not `cat` plus `curl`, and that token
  reaches every repo the account can, not just the installed ones.
- **Revisit when:** next time anyone is on the server. The fix is one command
  (`su - rookery -c 'gh auth logout'`) and nothing in `agent.py` depends on the login.

## 6. SSH still accepts root password logins

**Deferred since 19 September 2026, logged 2 October 2026** (phase5-deploy.md "Still open" 2).

Root on `185.208.172.15` is being password-brute-forced. Login is by key in practice, but
`PasswordAuthentication no` and `PermitRootLogin prohibit-password` aren't set.

- **Gained:** no risk of locking ourselves out by editing `sshd_config` on the only box.
- **Given up:** the server is one guessed root password away from being lost, along with
  `app.pem`, the Claude credentials and every tenant's clone.
- **Revisit when:** now, before the link goes to anyone else. Open a second key session and
  confirm it works before closing the first, then reload `sshd`.

## 7. The agent can read `app.pem`

**Deferred since Phase 1, logged 2 October 2026** (phase5-deploy.md "Still open" 4,
DEPLOY.md step 5 "Known ceiling").

`claude` runs as `rookery`, the same user that reads the App's private key to mint tokens.

- **Gained:** one service user, one systemd unit, no `sudo` between the orchestrator and
  the agent.
- **Given up:** a prompt-injected agent can sign its own JWT with `openssl` and act as the
  App on every installation, not just the repo it was working on. The key outranks any
  token minted from it, and the deny rules don't cover `openssl` + `curl`. Since #18 that
  includes Workflows RW: CI that runs with a repo's secrets.
- **Revisit when:** before onboarding anyone outside the invite list. The small fix is to
  run `claude` as a second user that can't read `/opt/rookery`; the real fix is the sandbox.
- **Outcome, 4 October 2026: the small fix is live** (DEPLOY.md §5b, `RUN_AS`). `claude`
  runs as `rookery-run` via sudo; checked on the server that it can't read `app.pem`,
  `.env`, `rookery.db`, the notes or `rookery`'s home, and gets none of `.env` in its
  environment (before, `WEBHOOK_SECRET` was in it). The clone's `.git` stays read-only to
  it, since its hooks and config run when the orchestrator commits. Still open: tenants'
  worktrees share one user (the sandbox), and #18's workflow push needs no key at all.

## 8. Port 80 on the origin answers anyone

**Deferred since the Phase 5 deploy (29 September 2026), logged 2 October 2026**
(phase5-deploy.md "Still open").

nginx listens on port 80 for every address, not only Cloudflare's.

- **Gained:** nothing to maintain when Cloudflare changes its IP ranges.
- **Given up:** anyone who finds the IP reaches the same app over plain http, around
  Cloudflare. Session cookies and the OAuth code then cross the internet in cleartext, and
  Cloudflare's rate limiting and bot rules don't apply.
- **Revisit when:** with §6 or §9. nginx `allow`/`deny` for Cloudflare's published ranges,
  or `ufw`, closes it.

## 9. Cloudflare → origin is plain http (SSL mode Flexible)

**Decided 29 September 2026** (phase5-deploy.md, "Upgrade path: Flexible → Full (strict)").

The browser → Cloudflare hop is HTTPS; Cloudflare → nginx is http on port 80. Chosen over
a tunnel, Caddy or certbot as the least moving parts.

- **Gained:** no certificates on the box, nothing to renew, a plain nginx config.
- **Given up:** cookies, OAuth codes and webhook payloads cross the Cloudflare → server hop
  unencrypted. An https redirect on the origin can never be added while Flexible holds (it
  loops).
- **Revisit when:** §8 is done, or anyone outside the invite list signs in. The upgrade is a
  Cloudflare Origin Certificate on nginx plus Full (strict), already written up in
  phase5-deploy.md.

## 10. Owner defaults have no `CLAUDE.md`, and `after` is replaced whole

**Decided 2 October 2026** (Phase C, skills-and-workflows.md §1.2).

`<owner>/.rookery` supplies `workflow.json`, `agents/` and `skills/`. A root `CLAUDE.md` there
is not read, and a repo that sets `after` replaces the owner's steps rather than adding to
them.

- **Gained:** one rule for a slot (key by key) and one for `after` (whole list), and no
  second text competing with the repo's own `CLAUDE.md` inside the 64 KB argument.
- **Given up:** an owner cannot push house rules into every repo's system prompt except
  through `instructions` per slot, and a repo that wants the owner's security check plus
  one of its own has to repeat the owner's step.
- **Revisit when:** an owner asks for shared conventions across repos, or for `after` steps
  every repo must keep. The first is an owner `CLAUDE.md` appended before the repo's under
  the same cap; the second needs a merge rule (append, dedupe by agent).

## 11. Owner defaults need the App on `<owner>/.rookery`, and silently don't apply without it

**Decided 2 October 2026** (Phase C).

`org_of()` uses the owner repo only while it is in `REPOS`: installed, and let in by
`REPOS_ONLY`. That also makes it a normal target repo, with its clone and labels.

- **Gained:** the same clone, fetch, token and pinning as every other input, and no second
  way to read GitHub.
- **Given up:** an owner who creates `<owner>/.rookery` but doesn't add it to the
  installation gets no defaults and no error. The Workflow view shows it (no "Owner
  defaults" fact, no owner badge), but a run doesn't. A local instance with `REPOS_ONLY`
  ignores the owner's defaults unless it lists that repo too.
- **Revisit when:** someone is confused by it. A check for the repo via the installation
  token, warning in the Workflow view when it exists but isn't installed, closes it.

## 12. The portal edits only `workflow.json` and agents, and one PR per submit

**Decided 2 October 2026** (Phase C, skills-and-workflows.md §6).

`CONFIG_FILE` allows `workflow.json` and `agents/<name>.md`. Skills, `CLAUDE.md` and
`.claude/` are edited on GitHub as before. Every "Open pull request" opens a new PR on a new
`rookery/config-<hex>` branch; nothing updates an open one. The catalog's "Add to …" covers
plan/task/review skills; a check/write skill needs an `after` step written by hand.

- **Gained:** a small, allowlisted write surface whose every file `steer()` already knows
  how to validate, and no branch state to track between submits.
- **Given up:** two edits a minute apart are two PRs, and an abandoned one stays open until
  someone closes it (its branch too). Overriding a built-in role starts from a blank
  template, not the built-in persona.
- **Revisit when:** tenants open several config PRs in a row, or ask to edit skills here.
  Reusing the user's open `rookery/config-*` PR, and serving the built-in persona text to
  the editor, are the next steps.

## 13. A PR opened by a re-run carries only that job's sections

**Decided 2 October 2026** (B/C smoke test, skills-and-workflows.md §8).

`implement()` builds the PR body from what ran in this job: tasks ticked now, `after` steps
run now, this review. A job that failed after some tasks or write steps, approved again,
opens the PR without their sections; their commits are on the branch. And when the PR is
already open, the re-run posts its sections as a PR comment, while the issue comment still
says they are "in the PR body".

- **Gained:** no state carried between jobs to rebuild a body; the commits are the record.
- **Given up:** the PR description can miss the summary of work done in an earlier attempt.
- **Revisit when:** a reviewer misses it. Each task's and write step's reply could go in its
  commit body, and the PR body be rebuilt from `git log`; the issue comment's wording is one line.

## 14. PR review refuses pull requests from forks

**Decided 3 October 2026** (pr-review.md §7).

`review_pr()` stops with "Rookery does not review pull requests from forks yet" when
`isCrossRepository` is true.

- **Gained:** the reviewer, which runs with Bash under `bypassPermissions`, only reads code
  pushed by someone with write access: the trust the issue flow already relies on. A fork's
  diff can carry injected instructions, and #7 means an injected agent can act as the App on
  every installation.
- **Given up:** open-source repos cannot ask Rookery to review contributor PRs, which is
  where a review helps most.
- **Revisit when:** #7 is fixed (a second user for `claude`, or the sandbox). Then a fork PR
  can be fetched the same way, `refs/pull/<n>/head` already covers it.

## 15. Any agent on the box can change any repo's notes

**Decided 3 October 2026** (repo-memory.md §3.8).

Repo notes are files in `$WORKSPACE/memory/`, owned by the same user that runs `claude`.
An agent working on repo A can edit the notes of repo B, and B's next run reads them in its
system prompt.

- **Gained:** notes with no new storage, no new user and no new service.
- **Given up:** tenant isolation for the notes. This adds to #7; it is not a new class of
  problem, since the same agent can already read `app.pem`.
- **Revisit when:** #7 is fixed (a second user for `claude`, or the sandbox). The same fix
  covers the notes.
- **Outcome, 4 October 2026: fixed with #7.** `memory/` is mode 700 and owned by `rookery`;
  `rookery-run` can neither read nor write it. Notes still reach the agent through its
  system prompt, so #16 stands.

## 16. Repo notes steer runs without a human review

**Decided 3 October 2026** (repo-memory.md §3.8).

A haiku call writes the notes from a run's transcripts, and the next run reads them. No
person reviews them first, unlike `CLAUDE.md` and `.rookery/`, which change only by a merge.

- **Gained:** the notes stay current with no work for anyone.
- **Given up:** "what steers the agent is reviewed" now has an exception. The limits: the
  notes come only from runs that a person with write access started, they are per repo, the
  prompt says the code and `CLAUDE.md` win, and the portal shows them and can clear them.
- **Revisit when:** a bad note causes a bad run. Then: hand edits in the portal, or notes
  that change only by a PR, like config.

## 17. A PR review's findings become repo notes

**Decided 3 October 2026** (seen in prod on sa-es-ir/Mantis-Frontend#110).

`handle()` submits a `memory` job after a `pr-review` job as after any other job. The
reviewer's findings are about one diff, so `REMEMBER` records them as "Known Traps": the
rich-text length bug, the double polling interval, a missing `@types/dompurify`. When the
PR fixes them or closes, the notes are stale, and every later job reads them as advice.
A later review can raise them again on a PR that does not touch that code.

- **Gained:** one rule for all jobs. A review also finds real repo facts (layout, stack,
  conventions), and those notes are good.
- **Given up:** notes that hold only lasting facts. The cost of a bad note is a wrong
  finding or a wasted task, not a failed job.
- **Revisit when:** next change to the memory flow. Then: no `memory` job after a
  `pr-review` (smallest change), or a `REMEMBER` rule for pr-review jobs that records repo
  facts and never findings about the diff.

## 18. The App asks for Claude's GitHub App permissions, not only what it uses

**Decided 4 October 2026.**

The App now has Actions RW, Checks RW, Discussions RW, Workflows RW and Commit statuses R
on top of Contents, Issues and Pull requests RW and Metadata R. Webhooks (repository hooks),
which Claude's App also asks for, stays off. `agent.py` calls
none of the new APIs. Workflows does change one behaviour: a branch that edits
`.github/workflows/*` now pushes, where before GitHub refused it and the run ended in
`rookery:error`.

- **Gained:** a task can change CI. Later features (read CI results, re-run a workflow,
  post check runs) need no new permission, so installs don't have to accept one again.
- **Given up:** least privilege. #7 now covers writing workflows (they run with the repo's
  secrets). The install screen asks for
  more than Rookery does. Worse, and not fixed by #7's fixes: the orchestrator's own push
  now carries any workflow edit the agent made, and a `push`-triggered workflow runs the
  version on the pushed branch, with the repo's secrets, before a human reads the PR.
- **Revisit when:** #7 is fixed, or a tenant questions the install screen. Then: drop what
  is still unused, keeping Workflows only if tasks are meant to edit CI.

## 19. A non-Claude `PROVIDER` sends every tenant's code to that backend

**Decided 4 October 2026.** (`ai-plans/providers.md`)

With `PROVIDER=deepseek`, every `claude` call goes to DeepSeek. That includes each tenant's
code, issues and CLAUDE.md. The choice is per instance, not per tenant.

- **Gained:** each call costs about 1/20 of Opus, with no proxy and no change to the harness.
- **Given up:**
  - Tenants choose nothing. They must be told before the switch.
  - WebSearch and WebFetch run on Anthropic's side, so expect them to fail. The agent gets a tool error and continues.
  - The prompts were tuned on Claude, so expect more marker misses (`rookery:error`).
  - The key is in the agent's process env. A prompt-injected agent can read it with `env`
    and send it out. The `/login` credentials file has the same exposure today.
  - `cost_of()` uses peak prices, so `USD_CAP` trips up to 2x early in off-peak hours.
- **Revisit when:** a friend brings their own key (phase 2 in the plan: per owner), or S-d
  shows a marker-miss rate that is too high.
