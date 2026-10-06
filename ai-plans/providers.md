# Model providers: Claude, DeepSeek, GLM, more later

**Status: phase 1 built** (4 October 2026). Spikes S-a, S-b, S-c and S-e were run on
4 October 2026 (results in §4). S-d passed the same day. Deploy: DEPLOY.md, "Another model provider".
**GLM (Z.ai): built** (5 October 2026), §8. G-a to G-e passed (§8.3).
**Phase 2 (per owner): built** (5 October 2026), but not as §7 first planned. The admin page
(`admin.md`) stores the default and each owner's override. Owners add their own keys on the
Keys page (`byok.md`), and the `anthropic` provider is Claude by API key. Read §1–§3 as the
phase 1 design. Where they differ from the code, §7 says what changed.
**OpenAI, Gemini and other large providers: researched** (6 October 2026), §9. Next: the
OpenRouter spikes (§9.6).

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
  (§7, now built). Phase 1 is one provider for the whole instance, paid by the operator.

## 2. Why DeepSeek first

- Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`) built for Claude
  Code, so no proxy and no translator.
- 5M free tokens at signup, then about 1/20 of Opus per token.
- About 80% on SWE-bench Verified (aggregator numbers, not ours).

Any other backend with an Anthropic-compatible endpoint (MiniMax, Kimi, GLM, OpenRouter, a
local 9router) is one more table entry. No code changes. GLM is the next one (§8).

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
guide uses. `agent.py` still pops that var from `os.environ` at import; it reaches the CLI only
through `env=`.

As built, the keys of all providers are popped into one `KEYS` dict, used or not, and only the
`PROVIDER` that was selected is checked at startup. The table also has `anthropic` (byok.md §3):
no `models` and no `price`, so it uses `total_cost_usd`.

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

As built (phase 2): `provider_env(name, key=None)` takes the provider name and, optionally, an
owner's own key. `claude()` calls `provider_for(repo)` **once** → `(name, key id, key)`, and
gives the same `name` to `provider_env()`, `cost_of(out, name)` and its step. So a switch on the
admin page during a call cannot price the call with the wrong table.

The `step()` for each call also records the provider (and `key=<id>` on an owner's own key),
and the `base` step logs it once per job. That makes it visible in the run log which backend
wrote which PR.

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
quiet guess). A provider with no `price` (`claude`, `anthropic`) keeps using
`total_cost_usd` without any change. About 8 lines.

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

## 7. Phase 2: per owner — built

First planned as `.env` lines per owner (`PROVIDER_<owner>=…`, `DEEPSEEK_API_KEY_<owner>=…`).
That was not built. What was built, 5 October 2026:

- **Admin page** (`admin.md`): the operator stores the default provider and one override per
  owner in `settings` (`provider`, `provider:<owner>`). `PROVIDER` in `.env` is only the
  default until the page stores one. The page stores only a `ready()` provider.
- **Owners' own keys** (`byok.md` §4): an owner adds keys on the portal's Keys page. Each key is
  sealed in the `keys` table, and the selected one is `key:<owner>`. Same `env=` path, same
  sudoers line.
- **`provider_for(repo)`**: the owner's selected key, else `fallback()`: the owner's override,
  else (only for an owner in `OWN`) the stored default, else `PROVIDER`. Nothing found raises
  `NoKey`, and the job fails with the `NO_KEY` comment and a link to the Keys page.
- **The subscription (`claude`) runs only the owners in `OWN`** (byok.md §3). An override to
  `claude` for any other owner is refused (422).
- The cross-tenant `/proc/<pid>/environ` exposure under one `rookery-run` user is trade-offs
  #22.

## 8. GLM (Z.ai)

### 8.1 Why

Researched 5 October 2026. For the same price as DeepSeek, GLM can be better at coding work.

- **Price.** Z.ai prices per million tokens (docs.z.ai, 5 Oct 2026):

  | model | input (cache miss) | input (cache hit) | output |
  |---|---|---|---|
  | `glm-5.3` | 1.40 | 0.26 | 4.40 |
  | `glm-5.3-flash` | 0.15 | 0.03 | 0.50 |

  We priced the S-d tokens (3 issues, 33 calls) again with these numbers. The opus alias
  used 72k fresh, 355k cached and 38k output tokens. The sonnet and haiku aliases used 571k
  fresh, 2.43M cached and 62k output tokens. Result: GLM $0.55,
  DeepSeek $0.52, Kimi $2.14, Claude $3.72. Each model counts tokens in a different way,
  so expect ±30%.
- **Quality.** These are independent numbers from Artificial Analysis. No model was run in
  Claude Code.

  | | Coding Agent Index | Intelligence Index |
  |---|---|---|
  | GLM-5.3 (OpenCode) | 53.6 | 44.8 |
  | DeepSeek V4 Pro (Codex) | 43.1 | 53.2 |
  | GLM-5.3-Flash | not run | 41.8 |
  | DeepSeek V4 Flash (Codex) | 38.7 | 34.3 (V4.1: 39.5) |
  | Claude Code, Sonnet 5.5 at max effort | 68.4 | 56.0 |

  The sonnet alias does about 85% of our tokens, and on the Intelligence Index GLM-5.3-Flash
  beats DeepSeek's flash model. Benchmarks do not decide this. G-d decides it.
- **Rejected: the GLM Coding Plan** (a subscription at $18–160 a month). Its usage policy
  forbids account sharing and multi-user access, and it permits only "officially supported
  tools". If you break these rules, Z.ai can rate-limit, freeze or ban the account. A
  multi-tenant server breaks the sharing rule. Use only the pay-as-you-go API.
- **Rejected: Kimi.** It is about 4× the cost of GLM, and its agent score is not better
  (Kimi K3: 51.9).

### 8.2 The entry

```python
"glm": {"url": "https://api.z.ai/api/anthropic", "key": "ZAI_API_KEY",
        "models": {"opus": "glm-5.3", "sonnet": "glm-5.3-flash", "haiku": "glm-5.3-flash"},
        "price": {"glm-5.3": (1.40, 0.26, 4.40), "glm-5.3-flash": (0.15, 0.03, 0.50)}},
```

`ZAI_API_KEY` is popped at import, the same as `DEEPSEEK_API_KEY`. No other code changes,
unless G-a or G-b fails.

`GLM-4.7-Flash` is free and could be used for the haiku alias (memory calls). We do not use
it: we have not read the data terms for its free tier, and the memory calls cost less than
1 cent per issue.

### 8.3 Spikes before code

Do these spikes the same way as §4: run locally, keep `PROVIDER` unset, export the key by hand,
and use `env -u ANTHROPIC_API_KEY` for the Claude-side calls.

| # | Check | Pass means | If it fails |
|---|---|---|---|
| G-a | `claude -p "say hi" --output-format json` with `ANTHROPIC_API_KEY` set to the Z.ai key. Z.ai's guide uses `ANTHROPIC_AUTH_TOKEN` (Bearer). | The reply comes back and `modelUsage` names a GLM model: Z.ai accepts `x-api-key`. | Add an `auth` key to the entry. `"token"` sends the key as `ANTHROPIC_AUTH_TOKEN`. Add that name to the sudoers `env_keep` line (§3.3), and add a test. About 3 lines. |
| G-b | The same call: read the `modelUsage` keys | They are exactly `glm-5.3` / `glm-5.3-flash`. `cost_of()` looks up `price` by these names. | Use the names that the reply shows as `price` keys (for example, a different case or a `[1m]` suffix). |
| G-c | A resumed call | `cacheReadInputTokens` is above 0. If it is 0, every call costs the miss price: about 3× for GLM. | Cost and caps stay safe, because the cap trips early, not late. Record the cost in trade-offs. |
| G-d | 3 issues on the test repo, plan → approve, the same as S-d | Count `STATUS:` marker misses and `rookery:error` runs that come from the model, and the total cost. Compare with S-d: 0 misses, $0.56. **This is the quality gate.** | Do not add the provider. |
| G-e | An agent with `model: opus` | The call uses `glm-5.3`. | Same as S-e: stop. |

Results, 5 October 2026 (local, CLI as installed):

- **G-a: pass.** `ANTHROPIC_API_KEY` (`x-api-key`) works. No `auth` key. With no balance,
  Z.ai answers 429 `[1113] Insufficient balance`, not an auth error.
- **G-b: pass.** `modelUsage` keys are exactly `glm-5.3` and `glm-5.3-flash`.
- **G-c: pass.** The first resumed call read only 832 of 52k input tokens from the cache.
  The next call read 19k of 25k. The cache warms after one call, so the §8.1 estimate holds.
- **G-d: pass.** Issues #16–#18 (the S-d specs again), PRs #19–#21. 32 calls, all
  `provider=glm`, **0 marker misses**: 3 plan-ready, 12 task-done, 3 step-done, 4 check-ok,
  3 review-ok, 6 memory-updated. $0.73 in all (S-d: $0.56). The 1 `failed` call was a
  memory call that we stopped: DNS for `api.z.ai` failed on the dev box, and the CLI retried
  for 15 minutes. The 2 `rookery:error` runs were `gh` GraphQL `EOF`s on the dev box, the
  same as in S-d. Re-running resumed each one. All 3 ended `rookery:changes-wanted` because
  the test repo's `sentry` check edits a file on purpose, the same as in S-d. One memory
  reply was 8,334 characters, over `MEMORY_CAP`, so the old notes stayed. That is a model
  miss, but it does no harm.
  **Slower:** each call takes 2–4× the DeepSeek time (plan 149–223s against 57–82s, task
  58–71s against 16–31s, sentry up to 232s). One issue takes about 10–13 minutes from
  approve to PR, against about 3 minutes. Jobs are asynchronous, so the cost is queue time.
- **G-e: pass.** An agent with `model: opus` used `glm-5.3`.

Not tested again: S-c (resume across a provider switch). It tests the CLI, not the backend.
Z.ai's guide recommends `API_TIMEOUT_MS=3000000`. Add it only if G-d shows timeouts.

### 8.4 Data

Zhipu AI (Beijing) makes GLM. The international API is run by a company that is
registered in Singapore. Its API terms say that input is processed in real time, is not
stored, and is not used for training. As with DeepSeek (§5), tenants must be told.
Add this to `trade-offs.md` when it is built.

### 8.5 Changes

| File | Change |
|---|---|
| `agent.py` | the `glm` entry (§8.2), and `auth` only if G-a fails |
| `test_agent.py` | `provider_env()` for `glm`, and `cost_of()` with a `glm-5.3-flash` usage |
| `DEPLOY.md` | `.env` lines `PROVIDER=glm` and `ZAI_API_KEY=` |
| `CLAUDE.md` | add `ZAI_API_KEY` to the provider-key invariant |
| `ai-plans/trade-offs.md` | tenant code goes to Z.ai |

## 9. OpenAI, Gemini and other large providers

Researched 6 October 2026. The question: can an owner use an OpenAI (Codex) key, a Gemini key
or a key for another large provider? The harness is the `claude` CLI, and it speaks only
Anthropic's Messages API. So the question is how a model that is not Claude gets behind it.
There are three ways. They are different seams with very different costs.

### 9.1 An Anthropic-compatible endpoint: one `PROVIDERS` entry

This is how `deepseek` and `glm` work. There is no code change and no invariant changes.

- **OpenRouter** is the only route here to GPT and Gemini. Its Anthropic-format endpoint is
  `https://openrouter.ai/api`, made for `ANTHROPIC_BASE_URL`. The entry maps the `opus` /
  `sonnet` / `haiku` aliases to `openai/gpt-…` or `google/gemini-…` models and prices them.
  **Risk:** OpenRouter guarantees Claude Code only on Anthropic's own models. A GPT or Gemini
  model gets the request through a translation layer, and it was trained on a different
  harness's tools (Codex's `apply_patch`, the Gemini CLI's tools), not on Claude Code's
  `Edit` and `Bash`. Expect worse tool use than the benchmarks show. The O-d spike decides it.
- **Other direct endpoints**, each one entry: MiniMax, Qwen, Kimi (rejected on cost, §8.1).
- **OpenAI and Google** have no Anthropic-format endpoint that we found, only OpenAI-format
  ones. So an owner's own OpenAI or Gemini key cannot go into this table directly. Through
  OpenRouter the owner brings an OpenRouter key, not an OpenAI key.

### 9.2 A translating proxy on the box: rejected for now

LiteLLM or claude-code-router on `127.0.0.1` would turn Anthropic calls into OpenAI calls, so
a raw OpenAI key would work.

- It is a second service to run, and it is one process that holds every owner's key.
- It has the same tool-quality risk as 9.1, plus a translator that we must keep working.
- byok.md §4.4 already names a local proxy that swaps a per-call token for the key, as the
  upgrade for trade-offs #22. If that proxy is built for key isolation, translation can go
  into it. Translation alone is not a reason to build it.

### 9.3 The Codex CLI as a second harness: R6, later

This is R6 in `PRD.md`, behind a separate `HARNESS` flag (§1). It is the only way that an
OpenAI key gives good results, because GPT then runs in the harness that it was trained on.
`codex exec` runs headless and accepts an API key. But each invariant must be built again:

| Invariant | Under Codex |
|---|---|
| Deny `git push` / `gh` | There is no deny list of commands. The `workspace-write` sandbox turns off network, which can block a push. Spike. |
| No MCP (`--strict-mcp-config`) | MCP comes from `CODEX_HOME/config.toml`. Each job needs its own empty `CODEX_HOME`. |
| Steering read from base, once | Codex loads `AGENTS.md` and, it is reported, a project `.codex/config.toml` from the worktree, which the agent edits. This is the `.mcp.json` hole again. Spike. |
| `--agent rk:*`, plugin skills | No equivalent. The agent's prompt goes inline. |
| Session ids from `uuid5` | Codex makes its own ids. We must store them, which breaks "nothing local persists". |
| `digest()`, `cost_of()` | A different transcript and usage format: a second parser for each. |

**Rejected: a ChatGPT subscription login per owner**, for the same reason as the GLM Coding
Plan (§8.1): a multi-tenant server breaks the terms on shared use. Only a pay-as-you-go key.

Do the three spikes marked "Spike" before any code. If one of them fails, R6 needs a design
for that invariant first.

### 9.4 Not a larger model, but cheap: Claude on Bedrock

A company with AWS credits can run Claude through its own AWS account. The CLI supports it,
but through other variables (`CLAUDE_CODE_USE_BEDROCK`, AWS credentials and region), not
`ANTHROPIC_BASE_URL`. So `provider_env()` needs a small branch, and the sudoers `env_keep`
line needs the new names. Build it only when an owner asks.

### 9.5 Plan

1. **Now:** spike OpenRouter (9.6). If it passes, add an entry. The Keys page and the admin
   page list `PROVIDERS` by themselves. The other changes are a label in `PROV` in
   `ui/portal.html` and a row in `docs/keys.md`. One entry maps the aliases to one set of
   models, so GPT and Gemini are two entries (for example, `openrouter-gpt` and
   `openrouter-gemini`). A key in the `keys` table is for one provider, so an owner who wants
   both adds the same OpenRouter key twice.
2. **Not now:** the proxy (9.2). Join it to the key-isolation proxy if that is built.
3. **Later:** the Codex harness (9.3), as its own R6 plan. Its three spikes come first.

### 9.6 Spikes before code (OpenRouter)

Do these the same way as §4: run locally, keep `PROVIDER` unset, export the key by hand, and
use `env -u ANTHROPIC_API_KEY` for the Claude-side calls. Pick the GPT and Gemini models, and
read their prices from openrouter.ai/models, on the day of the spike.

| # | Check | Pass means | If it fails |
|---|---|---|---|
| O-a | `claude -p "say hi" --output-format json` with `ANTHROPIC_BASE_URL=https://openrouter.ai/api` and the key as `ANTHROPIC_API_KEY`. OpenRouter's guide uses `ANTHROPIC_AUTH_TOKEN`. | The reply comes back and `modelUsage` names the mapped model. | Add an `auth` key to the entry, as G-a describes. |
| O-b | The same call: read the `modelUsage` keys | They are the OpenRouter ids (`openai/…`, `google/…`), so `cost_of()` can find them in `price`. | Use the names that the reply shows. |
| O-c | A resumed call | `cacheReadInputTokens` is above 0. | Record the cost in trade-offs. The cap trips early, not late. |
| O-d | 3 issues on the test repo, plan → approve, the same as S-d, once with GPT and once with Gemini | Count `STATUS:` marker misses, `rookery:error` runs from the model, failed tool calls (`Edit` misses) and the total cost. Compare with S-d and G-d: 0 misses. **This is the quality gate.** | Do not add that model. |
| O-e | An agent with `model: opus` | The call uses the model mapped to `opus`. | Same as S-e: stop. |

Also find out: OpenRouter's fee on credits, and its data settings (which upstream providers
can receive the request, and if they keep or train on it). As with DeepSeek (§5), tenants
must be told where their code goes. Add this to `trade-offs.md` when it is built.

Sources: openrouter.ai/docs/guides/coding-agents/claude-code-integration,
docs.anthropic.com/en/api/openai-sdk, docs.litellm.ai/docs/anthropic_unified/native_passthrough.
The Codex notes come from third-party write-ups. The spikes check them.
