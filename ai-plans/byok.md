# Claude by API key, and owners' own keys (BYOK)

**Status: phases A and B built, C deferred** (5 October 2026). Decisions in §9 taken 5 October 2026.
§10 (wrong label) built 5 October 2026. Spike K-c passed and phase A (§3) built 5 October
2026. K-a passed, K-d failed (no key check on save), 5 October 2026. Phase B (§4) built
5 October 2026: `provider_for()` returns `(name, key id, key)` so the steps can log the id; the
no-key check is `pin()`'s first line; the first key an owner adds is selected. Phase C deferred.

## 1. Goal

1. A new provider, `anthropic`: Claude through an API key, priced by the CLI's own
   `total_cost_usd`. The operator's subscription (`claude`, the CLI's `/login`) stays, but
   only for the operator's own owners.
2. An owner can add API keys for `anthropic`, `deepseek` or `glm` in the portal, keep more
   than one, and pick the one their runs use.

**What stays the same.** The harness stays the `claude` CLI. The provider stays per owner,
never per actor (admin.md). Caps, deny rules, `--strict-mcp-config`, `RUN_AS`: unchanged.

## 2. Facts today

- `PROVIDERS` (`agent.py:55`): `claude` = `{}` (the `/login`); others have `url`, `key`
  (the `.env` var), `models`, `price`. `KEYS` holds the operator's keys, popped from
  `os.environ`.
- `provider_for(repo)` → `provider:<owner>` row, else `provider` row, else `PROVIDER`.
  `claude()` reads it once and passes it to `provider_env()` and `cost_of()`.
- `agent.py:49` pops `ANTHROPIC_API_KEY` at import, so a stale key never beats the `/login`.
- The admin page can set `claude` (the subscription) for any owner. Nothing stops the
  operator's subscription from paying for a stranger's repo.
- The provider key reaches `claude` as `ANTHROPIC_API_KEY` in the process env. Every
  tenant's agent runs as the same `rookery-run` user, so **an agent can read another
  tenant's concurrent `claude` process env** (`/proc/<pid>/environ`). Today that is only the
  operator's key (trade-offs #19). With BYOK it would be a stranger's key.
- The `/login` credentials are in `rookery-run`'s home. Every tenant's agent can read them
  today.

## 3. Phase A: the `anthropic` provider and a subscription for the operator only

### 3.1 The entry

```python
"anthropic": {"url": "https://api.anthropic.com", "key": "CLAUDE_API_KEY"},
```

- No `models`: the CLI's aliases already mean Claude models. `provider_env()` reads
  `p.get("models", {})`.
- No `price`: `cost_of()` already falls back to `total_cost_usd`, which is right for Claude.
- `.env` var `CLAUDE_API_KEY`, not `ANTHROPIC_API_KEY`: `agent.py:49` must keep popping
  that one.

### 3.2 `OWN`

`.env`: `OWN=sa-es-ir,<org>` — the owners that the subscription and the operator's default
provider may pay for. **Unset, `OWN` is `ADMINS`**: the operator's own login is in `ADMINS`
already, so the operator's repos keep running as today with no new `.env` line. An org of
the operator's goes in `OWN` by name.

- `provider_for()` → if the result is `claude` and the owner is not in `OWN`, raise
  `"the subscription is only for the operator's own repos: add a key for <owner> at
  <PUBLIC_URL>/portal.html#keys"`. The job fails with that comment and `rookery:error`.
  Never a fallback to another backend. As built (phase A, no keys page yet) the message is
  "no provider for <owner>: … Ask the operator to set a provider for <owner>."; phase B
  changes it to the keys link.
- `POST /api/admin/provider` refuses `claude` for an owner not in `OWN` (422). The default
  can stay `claude`. It then means "the operator's owners use the subscription, everyone
  else needs a key or an override".
- `admin.html`: the `claude` option is disabled on owners not in `OWN`.
- Startup: log each stored `provider:<owner> = claude` row whose owner is not in `OWN`.

About 15 lines plus tests. This phase can ship alone.

### 3.3 The operator pays for one owner: the admin override

This exists today and stays. On `/admin.html`, the Owners table sets `provider:<owner>`
for one owner, for example `deepseek` for a friend. That owner's runs then use the
operator's `DEEPSEEK_API_KEY`, under the repo's usual caps.

- It is the only way an owner outside `OWN` runs on the operator's money: an explicit
  choice per owner (step 2 in §4.3), never the default.
- Any `ready()` provider except `claude` (§3.2).
- It works for an org too. That is how an org gets a provider, since an org has no keys
  page (§9.1).
- The owner can still add their own key. Their selected key beats the override.
- An owner that has not installed the App yet can be granted in advance: the POST takes any
  owner name. The row applies when their repos arrive.
- The admin page shows each owner's source: own key, override, default (`OWN` only), or
  none.

## 4. Phase B: owners' own keys

### 4.1 Who manages an owner's keys

- **Personal account:** the signed-in login equals the owner.
- **Organization:** not in this plan (§9.1). An org runs on the operator's choice, set on
  the admin page. `can_manage(login, owner)` is `login.lower() == owner.lower()`, for an
  owner of a repo in `REPOS`.

The key belongs to the owner, not the person who typed it. The person is recorded in `by`.

### 4.2 Storage

`db()`:

```sql
CREATE TABLE IF NOT EXISTS keys (id INTEGER PRIMARY KEY, owner TEXT, provider TEXT,
    label TEXT, secret TEXT, last4 TEXT, added REAL, by TEXT);
```

- The selected key is a `settings` row, `key:<owner>` = `<keys.id>`.
- `secret` is encrypted. The stdlib has no cipher, so it uses the `openssl` CLI that `jwt()`
  already needs: `openssl enc -aes-256-cbc -pbkdf2 -pass env:K -a`, with the master key in
  `env=` for that one call, then HMAC-SHA256 (stdlib) over the ciphertext
  (encrypt-then-MAC). Master key: `KEY_SECRET` in `.env`, popped at import like
  `WEBHOOK_SECRET`. A missing `KEY_SECRET` disables the keys page (the API says so). It
  does not fail startup.
- Why encrypt when `RUN_AS` already keeps the agent out of `rookery.db`: the database is
  copied into `/opt/rookery/backups/` on each deploy, and the agent can read it when
  `RUN_AS` is unset.
- A key is write-only. The API never returns it; the UI shows `provider · label · …last4`.

### 4.3 Which backend a call runs on

`provider_for(repo)` returns `(name, key)` instead of `name`:

1. The owner's selected key (`key:<owner>` → `keys` row) → `(row.provider, decrypted)`.
2. Else the admin override `provider:<owner>` → `(name, KEYS[name])`. The operator pays,
   by an explicit choice for that one owner.
3. Else, **only for an owner in `OWN`**: the default `provider` row, else `PROVIDER`.
4. Else raise `NoKey` (§4.7). An owner outside `OWN` never runs on the operator's default.
5. `claude` for an owner not in `OWN` (an old override row) → raise (§3.2).

The owner's choice beats the admin override: who pays, chooses. `claude()` still reads it
once; `provider_env(name, key)` and `cost_of(out, name)` take the result. The `base` and
`claude` steps log `provider` and `key=<id>` (never the secret). A selected key that was
deleted falls through to step 2.

Switching keys or providers mid-issue is trade-offs #21 as it is.

### 4.4 Getting the key to the CLI

As today: `provider_env(name, key)` puts the key in `ANTHROPIC_API_KEY` in the `env=` of
that one call, through the sudoers `env_keep` line. No sudoers change.

Any tenant's agent can read another tenant's concurrent `claude` env
(`/proc/<pid>/environ`, same `rookery-run` user), and so its key: trade-offs #22. The
upgrade path: a local proxy on `127.0.0.1` that swaps a per-call token for the key, so no
agent's env holds a key (about 50 lines in `web.py`).

### 4.5 API (web.py)

All under the session, POSTs and DELETEs with CSRF. An owner the user cannot manage → 404.

| Route | Does |
|---|---|
| `GET /api/keys` | `{"owners": [{"owner", "selected", "keys": [{"id", "provider", "label", "last4", "added", "by"}]}], "providers": ["anthropic", "deepseek", "glm"]}` for each owner the user manages |
| `POST /api/keys` | `{"owner", "provider", "label", "key"}` → 201. A provider that is unknown or `claude` → 422. Label max 40 characters. Key: 20–300 printable characters, no spaces |
| `DELETE /api/keys/<id>` | removes it; if it was selected, the `key:<owner>` row goes too |
| `POST /api/keys/select` | `{"owner", "id": <id> \| null}`. `null` → back to the operator's choice (or a failure, for an owner not in `OWN`) |

Log lines: `key <id> (<provider>) added/removed/selected for <owner> by <login>`, never the
key.

### 4.6 UI

A **Keys** section in `portal.html` (DESIGN.md tokens, user text by `textContent`):

- One card per owner the user manages.
- A list of keys: a radio to select, provider, label, `…last4`, added by and when, a
  remove button. An extra radio row "Rookery's default" (the operator's choice), disabled
  with a hint for an owner not in `OWN` that has no override.
- An add form: provider select, label, a `type=password` key field, Save.
- A line that says where the code goes: "Runs on this key send your code to <provider>."

### 4.7 No key

An owner outside `OWN` with no selected key and no admin override triggers an issue:

- `handle()` calls `provider_for(c.repo)` right after `begin()`, before `pin()` and before
  `plan()` sets `rookery:planning`. So no fetch, no clone work, no `claude` call, no cap
  spent. The same check runs for `continue` and `pr-review` jobs.
- It raises `NoKey`, and the normal failure path posts the comment and sets
  `rookery:error`:

  > Agent error: no API key for **<owner>**. Add one at <PUBLIC_URL>/portal.html#keys and
  > select it. Then remove the `rookery:error` and `rookery:trigger` labels and add
  > `rookery:trigger` again.

  Both labels: `enqueue()` starts a plan only on a `labeled` event for `rookery:trigger`
  and only with no `PHASES` label on the issue.
- On a PR (`pr-review`), the failure path is `gh pr comment`, never a label (the
  invariant). The comment says to add a key, then add `rookery:pr-review` again.
- No memory job follows: the failed job has no `claude` step, so `digest()` is empty and
  `remember()` skips it.

### 4.8 Caps

`RUN_CAP` and `USD_CAP` stay as they are, also on an owner's own key: they protect the box
too, not only the operator's money. A cap per owner set by the owner: not now.

## 5. Phase C: the subscription out of the tenants' reach — deferred

**Not in this change: trade-offs #23.** The `/login` credentials are in `rookery-run`'s
home, so any tenant's agent can read the operator's subscription token. `OWN` (§3.2) stops
Rookery from *spending* the subscription on others; it does not stop an agent from
*taking* it. The fix, when it is built:

- A second sandbox user, `RUN_AS_OWN` (for example `rookery-own`), holds the `/login`.
  `rookery-run` has no login.
- `claude()` runs as `RUN_AS_OWN` when the backend is `claude`, else as `RUN_AS`.
- Both users are in the shared worktree group (`shared()`), so ticking and committing work
  the same.
- DEPLOY.md §5b: create the user, move the `/login`, one more sudoers rule.

About 5 lines in `agent.py`, most of the work is on the server.

## 6. Spikes before code

| # | Check | Pass means |
|---|---|---|
| K-a | `claude -p "say hi" --output-format json` with `ANTHROPIC_API_KEY` set while a `/login` is present (run with `env -u` for the shell's own key) | `modelUsage` names Claude models, `total_cost_usd` matches the API price, no prompt about the key in `-p`. **Passed** 5 October 2026, local CLI 2.1.289 (the server pins 2.1.110: recheck there before deploy): `init` says `apiKeySource: ANTHROPIC_API_KEY` with the `/login` present, `modelUsage` is `claude-haiku-4-5-20251001`, `total_cost_usd` 0.01570155 equals the Haiku 4.5 list price of its tokens to the last digit, and `cost_of()` returns it. stderr warns that claude.ai connectors are off (no harm: `--strict-mcp-config`) |
| K-c | `openssl enc -aes-256-cbc -pbkdf2 -pass env:K -a` round trip on the server's OpenSSL | the same bytes come back. **Passed** 5 October 2026 on OpenSSL 3.2.1 (local) and 3.0.13 (server). Use `-a -A` both ways: without `-A` the base64 wraps at 64 characters |
| K-d | `GET <url>/v1/models` with each key | if all three answer 200 for a good key and 401 for a bad one, `POST /api/keys` checks a key before it saves it. Else no check: a bad key fails the first job with the provider's error. **Failed** 5 October 2026: anthropic 200 / 401 (`x-api-key`); deepseek 404 for a good key (no `/v1/models` on its Anthropic endpoint); glm 200 for a bad key. So no check on save |

## 7. Changes

| File | Change |
|---|---|
| `agent.py` | `anthropic` entry, `OWN`, `KEY_SECRET`, `keys` table, `seal()`/`unseal()` (openssl + HMAC), `provider_for()` → `(name, key)`, `provider_env(name, key)`, startup logs, `OWN` defaulting to `ADMINS`, the `wrong-label` job (§10) |
| `web.py` | `/api/keys*` (§4.5), `can_manage(login, owner)`, `OWN` check in `/api/admin/provider` |
| `ui/portal.html` | the Keys section |
| `ui/admin.html` | `claude` disabled for owners not in `OWN`; show which owners use their own key |
| `test_agent.py` | resolution order (own key > override > default; deleted key falls through; `claude` outside `OWN` raises); seal/unseal round trip and a tampered tag fails; `/api/keys*` (404 for an owner not managed, 422 for `claude`, the key never in any response); `claude()` passes the owner's key, not the operator's, in `env`; an owner outside `OWN` with no key or override fails the job before `pin()`, with the portal link, and a PR gets no label |
| `docs/keys.md` | new, STE: add a key, select it, what each provider receives |
| `docs/workflow.md` | the wrong-label comment and the no-key failure, where the labels are described |
| `DEPLOY.md` | `.env`: `CLAUDE_API_KEY`, `OWN`, `KEY_SECRET` |
| `CLAUDE.md` | Providers paragraph; invariants: an owner's key reaches `claude` only through `env=`, like the operator's; keys are encrypted and write-only; the subscription only for `OWN` |
| `ai-plans/trade-offs.md` | #22–#24, logged with this plan |

## 8. Trade-offs

Logged in `ai-plans/trade-offs.md`:

- **#22** An owner's key is in the agent's process env, readable by any tenant's agent.
- **#23** The operator's `/login` is readable by any tenant's agent (phase C deferred).
- **#24** Keys only for personal accounts; the master key is on the same box; caps still
  apply on an owner's own key; no key check on save if K-d fails.

## 9. Decisions (5 October 2026)

1. **Organizations:** personal accounts only. An org runs on the operator's choice.
2. **The key in the env, as today**, not a proxy: trade-offs #22.
3. **Phase C deferred:** trade-offs #23.
4. **No key → `rookery:error`** with a comment that links to the keys page (§4.7).
5. **A wrong label gets a comment** (§10).

## 10. A wrong label gets a comment

Independent of keys. It can ship first.

Today a label on the wrong kind of item does nothing and says nothing:

| Label | On | Today | Then |
|---|---|---|---|
| `rookery:trigger` | a PR (`pull_request` `labeled`) | ignored | comment + remove the label |
| `rookery:pr-review` | an issue (`issues` `labeled`) | ignored | comment + remove the label |

- `enqueue()` submits a `wrong-label` job (sender not `ME`). `handle()` runs it at the top,
  like `drop-worktree`: no `begin()`, no run log, no `claude` call, no cap.
- On a PR it uses `gh pr comment` and `gh pr edit --remove-label`, never `gh issue`
  (the invariant). On an issue, `comment()` and `gh issue edit --remove-label`.
- The label is removed so that adding the right one later is a clean `labeled` event, and
  the item does not look like work is in progress.
- No `may()` check: only people with triage access can add a label, and the reply spends
  nothing.
- Comments (module-level templates, STE):

  > `rookery:trigger` plans an issue. It does nothing on a pull request, so I removed it.
  > To get a review of this pull request, add `rookery:pr-review` or mention
  > @rookery-agent.

  > `rookery:pr-review` reviews a pull request. It does nothing on an issue, so I removed
  > it. To plan this issue, add `rookery:trigger`.

- Tests: each row of the table gives one job and the right `gh` calls; the right label on
  the right item gives no `wrong-label` job; the bot's own label event gives none.
- About 20 lines. Not covered: assigning `rookery-agent` to a PR (still ignored).

## 11. The current setup after this change

| What | After |
|---|---|
| `.env` `PROVIDER`, `DEEPSEEK_API_KEY`, `ZAI_API_KEY` | unchanged. They are the operator's keys, used for `OWN` owners and for admin overrides |
| The `/login` on the server | unchanged. Used only for owners in `OWN` |
| The stored default (`provider` row) | unchanged, but it applies only to owners in `OWN` |
| A `provider:<owner>` override for a non-`claude` provider | unchanged: that owner keeps running on the operator's key |
| A `provider:<owner> = claude` row for an owner not in `OWN` | that owner's jobs fail with the §3.2 message. Startup logs each one. Change it on the admin page before the deploy |
| An owner not in `OWN` with no override | **their jobs start to fail** with the no-key comment (§4.7). Before the deploy: check the admin page's Owners table, and add an override for each owner that should keep running on the operator's money |
| New `.env` lines | `KEY_SECRET` (needed only for the keys page), `CLAUDE_API_KEY` (optional), `OWN` (optional, defaults to `ADMINS`) |
| `KEY_SECRET` | back it up with `.env`. If it is lost, every stored owner key is unreadable and must be added again. `unseal()` failing is a `NoKey`-like failure with "add the key again" |
| Server | no sudoers change, no new user (phase C is deferred) |
