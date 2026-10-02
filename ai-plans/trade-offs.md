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
