# Admin panel: users and providers per owner

Status: built 5 October 2026.

## Goal

An unlinked page at `/admin.html` where an operator can:

1. See every GitHub user who has signed in to the portal.
2. Set the default provider (`claude`, `deepseek`, `glm`) for all owners.
3. Override the provider for one owner. An owner without an override uses the default.

## Facts today

- Nothing records sign-ins. A `sessions` row is deleted at logout and swept 8h after it
  expires (`new_session()`). Older sign-ins are gone; only live rows can seed the new table.
- `PROVIDER` is fixed at import. `agent.py:66` keeps only the selected key and pops the
  others into nothing.
- `claude()` reads `PROVIDER` twice (`provider_env()`, then `cost_of()`). A switch between
  the two reads prices the call with the wrong table and fails it.

## Decisions

- **"User" for a provider = the repo owner** (`repo.split("/")[0]`): the account or org that
  installed the App. That is the tenant level that caps, `<owner>/.rookery` and visibility
  already use. Never the person who triggered a step: one issue has several actors, and the
  provider would change inside one issue. An org is its own owner: `acme/*` needs an
  override for `acme`, not for a member.
- **Admins = `ADMINS` in `.env`**, comma-separated GitHub logins. Not in the list or not
  signed in → every `/api/admin*` route is a 404, like someone else's run. `admin.html` is a
  static file with no data; the API is the guard.
- **A provider is selectable only if its key is in `.env`** (`claude` needs none). The API
  refuses the others, so no setting can make every run fail.
- **A change takes effect at the next `claude` call**, even mid-job. Each task is a fresh
  session, and every `claude` step logs its own provider.

## Changes

### agent.py

- `KEYS = {name: os.environ.pop(p["key"], "") ...}` keeps every key, still out of
  `os.environ`. `PROVIDER_KEY` goes.
- `PROVIDER` stays as the `.env` default and the fallback.
- `db()`: two new tables.
  - `settings (key TEXT PRIMARY KEY, value TEXT)`. Row `provider` = the default;
    row `provider:<owner>` = an override.
  - `users (login TEXT PRIMARY KEY, first REAL, last REAL, logins INTEGER)`.
- `provider_for(repo)` → the override row for the owner, else the `provider` row, else
  `PROVIDER`. One sqlite read per call.
- `provider_env(name)` and `cost_of(out, name)` take the provider as an argument.
  `claude()` calls `provider_for(c.repo)` once and passes the result to both and to its
  `step()`. `pin()` logs it in the `base` step.
- Startup check: `provider_env()` for the `.env` default, as now, plus a log line for each
  stored row whose key is missing (a key removed from `.env` after the row was set). Such a
  row fails that owner's jobs at the call, with the existing "needs <KEY> in .env" error,
  never a fallback to another backend.

### web.py

- `ADMINS = set(...)` from `.env`; `admin(s)` = `s and s["login"] in ADMINS`.
- `callback()`: upsert `users` (insert first/last/1, or set last and add 1 to logins).
- `db()` migration: `INSERT OR IGNORE INTO users SELECT login, MIN(expires) - TTL, ...
  FROM sessions GROUP BY login`, once, so people signed in now are not missed.
- `GET /api/admin` →
  ```json
  {"default": "claude",
   "providers": [{"name": "claude", "ready": true}, {"name": "glm", "ready": false, "key": "ZAI_API_KEY"}],
   "overrides": {"sa-es-ir": "claude"},
   "owners": [{"owner": "sa-es-ir", "repos": 3}],
   "users": [{"login": "sa-es-ir", "first": 0, "last": 0, "logins": 12, "live": true, "repos": 3}]}
  ```
  `owners` comes from `REPOS`, plus any owner that has an override but no repo now.
- `POST /api/admin/provider {"owner": null | "<owner>", "provider": "<name>" | null}`,
  with CSRF.
  - `owner: null` sets the default. `provider: null` is refused there.
  - `owner: "<o>"`, `provider: null` deletes the override.
  - A provider that is unknown or not ready → 422. An owner off `[\w.-]+` → 400.
  - Log: `provider for <owner|default> -> <name> by <login>`.

### ui/admin.html

DESIGN.md tokens, `styles.css`. Fetches `/api/me` for the CSRF token and `/api/admin`;
on 404, shows "Not found" and nothing else. All user text through `textContent`.

- **Default provider** card: one radio per provider; a not-ready one is disabled and shows
  the missing key.
- **Owners** table: owner, repo count, a select whose first option is
  "Default (<current default>)". A change POSTs at once and shows the saved state.
- **Users** table: login, first seen, last seen, sign-ins, live session, repos. A user who
  is also an owner shows that owner's provider.

### Tests (test_agent.py)

- `provider_for()`: default from `.env`, the stored default, an override, a deleted
  override falling back.
- `claude()` reads the provider once: a switch after `provider_env()` still prices with
  the first provider.
- `/api/admin*`: 404 when signed out and when not in `ADMINS`; 403 on POST without CSRF;
  422 for a not-ready provider; 400 for a bad owner; setting and deleting an override.
- `callback()` upsert: a second sign-in adds to `logins`, keeps `first`.

### Docs

- CLAUDE.md: Providers (default + per-owner rows, `provider_for()`, the read-once rule),
  Portal auth (`ADMINS`, the 404 rule), a line in the invariants that an admin route is a
  404 for a non-admin.
- DEPLOY.md: `ADMINS`, and every provider key the operator wants selectable in `.env`.
- `docs/`: nothing. The admin page is operator-only.

## Trade-offs (to log in trade-offs.md when built)

- **A resumed session can cross backends.** An override changed while an issue is in
  awaiting-input resumes the plan session on another provider, with the other model's
  turns (thinking blocks) in the transcript; the new API may reject them. The job then
  fails with a comment; nothing is silent. Fix if it happens: add the provider to the
  `uuid5` key, at the cost of a fresh plan session.
- **Sign-ins before this ships are lost**, except live sessions.
- **`rookery.db` is writable by the agent when `RUN_AS` is unset** (local runs), so an agent
  could change a provider row. The same is true of the caps today; with `RUN_AS` (the
  server) it cannot read the file.

## Not in scope

- Per-repo overrides: add a `provider:<owner>/<repo>` row and one more lookup when needed.
- Banning users, roles, admin-editable caps, a pause switch: `PAUSED` and `.env` cover it.
- An audit table: switches go to the log.
