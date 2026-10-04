# Model providers: Claude, DeepSeek, more later

**Status: phase 1 built** (4 October 2026). Spikes S-a, S-b, S-c and S-e were run on
4 October 2026 (results in §4). S-d passed the same day. Next: deploy (DEPLOY.md, "Another model provider").

## 1. Goal

One operator flag picks which backend answers every `claude` call:

```
PROVIDER=claude      # default: the CLI's own /login, exactly as today
PROVIDER=deepseek    # DeepSeek V4 through its Anthropic-compatible endpoint
```

Switching back is `PROVIDER=claude` (or deleting the line) and a restart. Nothing else
changes, and nothing is migrated.

**What stays the same.** The harness stays the `claude` CLI, so every invariant holds as it
is: deny rules, `--strict-mcp-config`, `--setting-sources user`, the plugin with agents and
skills, `--agent`, `STATUS:` parsing, transcripts for `digest()`, and `RUN_AS`. The only
thing that changes is the model behind the CLI.

**Out of scope:**
- Other harnesses (Codex CLI, opencode). That is R6 in `PRD.md`, a different seam: an
  adapter around the CLI, not an env bundle. If it is built, it is a separate `HARNESS`
  flag, so `PROVIDER` keeps meaning "which Anthropic-API backend".
- Per-owner providers and keys (friends bring their own DeepSeek key). That is phase 2
  (§7). Phase 1 is one provider for the whole instance, paid by the operator.

## 2. Why DeepSeek first

- Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`) built for Claude
  Code, so no proxy and no translator.
- 5M free tokens at signup, then about 1/20 of Opus per token.
- About 80% on SWE-bench Verified (aggregator numbers, not ours).

Any other backend with an Anthropic-compatible endpoint (MiniMax, Kimi, GLM, OpenRouter, a
local 9router) is one more table entry. No code changes.

## 3. Design

### 3.1 The table

Module level in `agent.py`, next to the other config:

```python
# Backends behind the `claude` CLI. "claude" is its own /login: no overrides at all.
# Others are an Anthropic-compatible endpoint, the .env var holding its key, and what
# the CLI's model aliases (agents' `model: opus`, tasks' `--model sonnet`, memory's
# haiku) resolve to there.
PROVIDERS = {
    "claude": {},
    "deepseek": {"url": "https://api.deepseek.com/anthropic", "key": "DEEPSEEK_API_KEY",
                 "models": {"opus": "deepseek-v4-pro", "sonnet": "deepseek-flash",
                            "haiku": "deepseek-flash"}},
}
PROVIDER = os.environ.get("PROVIDER", "claude")
```

At import:
- An unknown `PROVIDER`, or a provider whose key var is empty, **fails startup**. A
  misconfigured backend must not quietly fall back to the operator's subscription.
- The key is read into a module variable and **popped from `os.environ`**, the same as
  `GH_TOKEN`. It reaches the CLI only through a per-call `env=`.

The key goes in `ANTHROPIC_API_KEY` (sent as `x-api-key`), which is what DeepSeek's Claude Code
guide uses. Line 48 still pops that var from `os.environ`; it reaches the CLI only through `env=`.

The model map is explicit even though DeepSeek maps `claude-opus*` / `claude-sonnet*` by
itself. That way the mapping is ours, is logged, and doesn't change if DeepSeek changes its
defaults.

### 3.2 `claude()`

One helper builds the overrides:

```python
def provider_env():
    """-> the env overrides for one claude call; {} for the CLI's own login."""
    p = PROVIDERS[PROVIDER]
    if not p:
        return {}
    return {"ANTHROPIC_BASE_URL": p["url"], "ANTHROPIC_API_KEY": PROVIDER_KEY,
            **{f"ANTHROPIC_DEFAULT_{a.upper()}_MODEL": m for a, m in p["models"].items()}}
```

`claude()` passes `env={**os.environ, **provider_env()}` to both `sh()` calls when the
result is non-empty, and `env=None` (as today) when it is empty. So `PROVIDER=claude` runs
the exact command and env it runs now.

The `step()` for each call also records `provider=PROVIDER`, and the `base` step logs it
once per job. That makes it visible in the run log which backend wrote which PR.

### 3.3 Through `sudo` (`RUN_AS`)

`sudo` resets the env, so the overrides never reach `rookery-run`. Add one line to the
sudoers file in DEPLOY.md §5b:

```
Defaults:rookery env_keep += "ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL"
```

`env_keep` passes these variables only when the caller sets them. With `PROVIDER=claude`
none is set, so this line changes nothing for the default.

Rejected: passing the overrides with `claude --settings '{"env": …}'`. That avoids the
sudoers edit, but it puts the key in argv, where `ps` shows it to every user on the box.

### 3.4 Sessions across a switch

Session ids are `uuid5(repo#n/key)`. If the operator switches provider in the middle of an
issue, a reply resumes a thread that the other backend wrote.

**S-c passed in both directions** (4 Oct 2026, CLI 2.1.289): a Claude session with a tool
call resumed on DeepSeek, and the reverse, and each answered from the earlier thread. So
there is no session-key change and no "No conversation found" fallback. Not built.

### 3.5 Cost and caps

**S-b result:** for an unknown model the CLI prices the call as Claude
(`costBasis: "unknown"`, about $5 per million input tokens: 27k input tokens cost $0.136).
DeepSeek is far cheaper, so `USD_CAP` would trip far too early. This is safe, but it wastes
most of the budget. So this section is built.

DeepSeek prices per model, with an off-peak discount of half. We use the **peak** price
(01:00–04:00 and 06:00–10:00 UTC, Mon–Fri): the cap can only trip early, never late, and
there is no clock logic. USD per million tokens, from api-docs.deepseek.com, 4 Oct 2026:

| model | input (cache miss) | input (cache hit) | output |
|---|---|---|---|
| `deepseek-flash` | 0.30 | 0.006 | 1.20 |
| `deepseek-v4-pro` | 1.32 | 0.044 | 3.96 |

The provider entry gets `"price": {model: (in, cache_read, out)}`. `cost_of()` adds
`modelUsage[m]` tokens times the price for each model, when the provider has a `price`. A
model in `modelUsage` with no price fails the job (the same rule as an unknown provider: no
quiet guess). `PROVIDER=claude` keeps using `total_cost_usd` without any change. About 8
lines.

S-c showed that DeepSeek reports `cache_read_input_tokens` (32k of a resumed call), so the
cache-hit price is used in practice.

## 4. Spikes before code

Run locally, `PROVIDER` unset, with the operator's DeepSeek key exported by hand:

Note for re-running: a shell started by Claude Code has its own `ANTHROPIC_API_KEY`, which
beats `/login` and gives a 401. Run the Claude-side calls with `env -u ANTHROPIC_API_KEY`.
`agent.py` pops that var at import, so the server is not affected.

| # | Check | Pass means | Result |
|---|---|---|---|
| S-a | `claude -p "say hi" --output-format json` with the three env vars set, while a `/login` is present | the reply's `modelUsage` names `deepseek-flash` / `deepseek-v4-pro`: the env beats the stored OAuth login. **If not, stop: the design doesn't work.** | **Pass.** `modelUsage` named each model. The CLI warns that claude.ai connectors are disabled because `ANTHROPIC_API_KEY` is set. |
| S-b | the same call: read `total_cost_usd` and `usage` | decides §3.5 | **Claude prices** (`costBasis: unknown`): §3.5 is built. |
| S-c | start a session on Claude, `--resume` it on DeepSeek, and the reverse | decides §3.4 | **Pass, both ways.** §3.4 is not built. |
| S-d | `python agent.py owner/repo <n>` on a test repo, then `approve` | planning, 2–3 tasks, review, and memory each end with a correct `STATUS:` line. **The real quality gate.** Do 3 issues and count marker misses and `rookery:error` runs | **Pass** (4 Oct 2026, issues #10–#12, PRs #13–#15). 33 calls, all `provider=deepseek`, **0 marker misses**: 4 plan-ready, 10 task-done, 3 step-done, 5 check-ok, 2 review-ok, 1 changes-needed (a real catch: committed `.pyc` files), 8 memory-updated. $0.56 in all. The 4 `rookery:error` runs were all `gh` GraphQL `EOF`s on the dev box, not the model; re-running resumed each one. One plan told a task to commit; the agent did not. `RUN_CAP` (30 calls) bound long before `USD_CAP`. |
| S-e | an agent with `model: opus` in its frontmatter | the call hits `deepseek-v4-pro`, not flash | **Pass.** It used `deepseek-v4-pro`. |

## 5. Known limits with a non-Claude provider

- **WebSearch and WebFetch:** they run on Anthropic's side. Expect them to fail on
  DeepSeek. The agent sees a tool error and carries on. No skill depends on them.
- **Prompts were tuned on Claude** (S5, S8 in `skills-and-workflows.md`). A higher rate of
  marker misses is the expected cost. It surfaces as `rookery:error`, never as a wrong
  merge.
- **Data:** with `PROVIDER=deepseek`, every tenant's code goes to DeepSeek. Tenants must be
  told. Log it in `trade-offs.md`.

## 6. Changes

| File | Change |
|---|---|
| `agent.py` | `PROVIDERS`, `PROVIDER`, startup check, key pop, `provider_env()`, `env=` in `claude()`, `provider` in the `claude` and `base` steps, `price` and `cost_of()` (§3.5). About 30 lines |
| `test_agent.py` | `provider_env()` is `{}` for `claude` (the command and env stay as they are today); deepseek gives the URL, the token and all three aliases; an unknown provider and a missing key fail. `cost_of()` with a price, and a model with no price fails. |
| `DEPLOY.md` | §5b sudoers `env_keep` line; `.env` lines `PROVIDER=` and `DEEPSEEK_API_KEY=`; how to switch back |
| `CLAUDE.md` | one Architecture paragraph; extend the token invariant: the provider key is popped like `GH_TOKEN` and only reaches `claude` through `env=` |
| `ai-plans/trade-offs.md` | tenant code to the provider; WebSearch missing |

No portal change in phase 1. The run log already shows step details, so `provider` appears
there without UI work.

## 7. Phase 2: per owner (friends), not now

The same table, chosen per repo owner instead of per instance:
`PROVIDER_<owner>=deepseek` and `DEEPSEEK_API_KEY_<owner>=…` in `.env`, read by
`provider_env(repo)`. An owner with no entry falls back to `PROVIDER` only if the operator
lists them in `OWN`. Otherwise the job fails with "no provider key for <owner>". Same
`env=` path, same sudoers line. The cross-tenant `/proc/<pid>/environ` exposure under one
`rookery-run` user goes to trade-offs. Build it when a friend actually has a key.
