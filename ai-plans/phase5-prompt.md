Implement Phase 5 of ai-plans/PRD-Phases.md (control-plane API, UI, run log) —
LOCAL DEVELOPMENT ONLY. No deployment, no changes to the server, no ssh.
Read first: ai-plans/phase3-4-notes.md (what Phases 3–4 built, open items),
ai-plans/PRD.md R8/R9, CLAUDE.md, DESIGN.md, ui/portal.html + ui/styles.css.

Scope: Phase 5 only, on a new branch <phase5-local | prd-implementation-phases>
off prd-implementation-phases. Code + offline tests, committed in steps (run log →
API + auth → cancel → UI). Update CLAUDE.md/README.md; leave deploy.md alone except
a "not deployed yet" note. Run it locally against the real App and verify in a
browser (Playwright). Write ai-plans/phase5-notes.md at the end.

Local setup:
- agent.py runs on this Windows box on 127.0.0.1; webhooks arrive via
  <smee (npx smee-client) | none — dry-run CLI only>. Use a local .env and a local
  WORKSPACE; never touch /opt/rookery.
- A local instance must not fight the server for the same repos: use only
  sa-es-ir/rookery-throwaway-2 <and pause the server's copy via its PAUSED file — ask
  me first | accept that both see its webhooks and don't trigger issues there>.

Phase 5 decisions:
- Run log: each run appends its steps (job start, each claude call with key/status/
  cost, task ticks, commit, push, PR, error) to rookery.db, tables alongside `runs`.
  The UI shows "failed at task 9 of 12" from this, not from the journal.
- Auth: GitHub OAuth login through the existing App (user-to-server token,
  callback http://127.0.0.1:<PORT>/auth/callback — I add it in App settings; tell me
  the exact URL). Session = <HMAC-signed cookie, stdlib | server-side session row in
  rookery.db>, <8h | 24h> expiry, HttpOnly, SameSite=Lax. Secure flag off locally only.
- Who sees what: a user's repos = their own App installations' repos
  (GET /user/installations/{id}/repositories with their token) ∩ REPOS.
  Nothing is visible without login.
- API (JSON, same process, same HTTPServer — <switch to ThreadingHTTPServer | keep
  single-threaded>): list repos, runs per repo, one run's log, cancel a run,
  today's usage vs caps. Cancel needs <write+ on that repo | the same check as
  viewing>.
- Cancel: track the running `claude` subprocess per repo (Popen instead of run in
  that path) and kill it; the job ends like any failure (comment + claude:error),
  and `approve` resumes at the first unticked task, as today.
- CSRF: state-changing endpoints are POST and require <a per-session token header |
  SameSite=Strict on the cookie is enough>.
- UI: wire ui/portal.html to the API (vanilla JS, fetch), styled per DESIGN.md.
  Served by agent.py as static files <from ui/ | inlined>.
- File layout: <keep one agent.py | split into agent.py + web.py (+ db.py)> —
  the PRD expects a split here; keep the dry-run CLI and test_agent.py working.
- Stdlib only, as ever. No framework, no JS build step.

Invariants: every one in CLAUDE.md still holds — especially the token never
entering os.environ (user OAuth tokens included), and APPROVE staying a regex on
comments (the UI must not add an approve button unless I say so).

Done when, locally: I log in with GitHub, see only my repos, see a run's log task
by task (including a failed one), cancel a running job and watch the issue get the
comment + claude:error, and test_agent.py passes offline.
