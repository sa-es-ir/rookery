# Skills, Custom Agents and Workflows

Lets a tenant change *how* Rookery works on their repo without weakening
*what* Rookery guarantees. It covers three things:

1. **Skills.** A catalog Rookery ships and the operator installs on the server,
   plus skills the tenant writes.
2. **Custom agents.** The tenant's own personas and instructions. They can
   replace a built-in role or add a new one.
3. **Workflows.** The order those agents run in. The default is today's
   pipeline: planmaker → approve → tasks → reviewer → PR.

**Status:** spikes done (§8, CLI 2.1.110 on the server, 1 October 2026).
`--strict-mcp-config` (PR #7, §9.0) and A0 (PR #8, §9.1) are merged, and both
were deployed together on 2 October 2026 and smoke-tested (§8). Phase A is split:
A1 (the backend) and A2 (the portal) were deployed together on 2 October 2026. B was
deployed on 2 October 2026 (not yet smoke-tested end to end); C is not started. **Decided:** tenant configuration
lives in the repo under `.rookery/` (§1.2), and org defaults wait for Phase C.
The first catalog is 4 skills (§9.3), and task skills are off by default (§9.5).
Both are recorded in [trade-offs.md](trade-offs.md). §9.2 is settled: the `RUN_CAP`
default went from 10 to 30.

---

## 1. Decisions everything else follows from

### 1.1 A workflow belongs to a repo, not to a user

Rookery's tenant is the repo. Caps, `can_write()` and the job queue are all
keyed by repo, and the people who approve a run are that repo's writers. If
workflows were per user, two maintainers on the same repo would get different
pipelines depending on who added the label.

So config resolves in this order: the repo's own config, then Rookery's
built-in default. An owner-wide default between the two is Phase C (§1.2). The
portal shows the signed-in user the *effective* workflow for each repo they can
see.

### 1.2 Config lives in the repo, under `.rookery/` (decided)

This follows the existing rule: "State lives on GitHub and in the target repo,
not here."

```
<target repo>
  .rookery/
    workflow.json                    # slots, extra steps, skills per slot
    agents/<name>.md                 # custom personas, same format as agents/*.md
    skills/<name>/SKILL.md           # bot-only skills
  .claude/skills/<name>/SKILL.md     # the repo's own skills, shared with its humans
  CLAUDE.md                          # the repo's own conventions
```

Org-wide defaults go in an `<owner>/.rookery` repo, the same idea as
`<owner>/.github`. The App has to be installed on that repo. **Phase C, not
before.** Phases A and B resolve repo → built-in only, so there is one source
to read, validate and badge.

Why the repo and not `rookery.db`:

- **No cross-tenant leak.** The agent can read `rookery.db`: it runs as the
  same user in the same workspace. Tenant A's task session could read tenant
  B's private personas from it.
- **Free features.** GitHub already provides versioning, review, blame and
  rollback.
- **Nothing for the operator to back up.**

The portal edits this config **by opening a PR** with the App's token, behind
`can_write()` (§6). It never writes the config directly.

> The disk-level leak still exists: tenant A's agent can `cat` tenant B's clone
> under `$WORKSPACE/repos/`. That is the existing "sandbox before strangers"
> item (PRD-Phases §1.2). This plan does not make it worse, and it does not fix
> it.

### 1.3 Everything that steers the agent is read from the base branch, once, at job start

This is the security core of the plan. If config came from the feature
branch, a task agent could edit `.rookery/workflow.json` partway through a run
and drop the reviewer or swap a persona for the steps after it. An issue
author without write access could also try to steer the bot through the issue
body.

The rule covers **every** input that steers the agent, not just
`.rookery/`:

| Input | Read from | Delivered as |
|---|---|---|
| `.rookery/workflow.json` | `origin/<base>` | parsed by the orchestrator |
| `.rookery/agents/*.md` | `origin/<base>` | per-job plugin `agents/` |
| `.rookery/skills/*`, `.claude/skills/*` | `origin/<base>` | per-job plugin `skills/` |
| `CLAUDE.md` (root) | `origin/<base>` | `--append-system-prompt` |
| target repo `.claude/settings.json` (hooks, permissions) | **not loaded** | `--setting-sources user` |
| target repo `.mcp.json` | **not loaded** | `--setting-sources user` (S4e) |
| claude.ai connectors of the CLI's login account | **not loaded** | `--strict-mcp-config`, no `--mcp-config` (S4e) |

All of it comes from `git show` / `git archive origin/<base> -- <path>`, done
once when the job starts and pinned to that sha for every step of the job. A
change to any of it inside a PR takes effect only after a human merges it.

**Fetch first.** `worktree()` fetches base only when it creates the worktree,
so the clone's `origin/<base>` can be days old by the time a reply or an
`approve` arrives. Every plan, reply and implement job runs `git fetch origin
<base>` at its start, then resolves `origin/<base>` to a sha and reads
everything at that sha.

**Pinned per job, not per issue.** Plan, reply and implement are separate
jobs, so one issue can see different config across them: a merge to base
between planning and `approve` takes effect at `approve`. That is intended.
Each job's run log records its own `base_sha`, so every run is reproducible. One
edge to accept: a resumed planning session's history may name an `rk:` skill
that the newer sha no longer ships. The call still runs, and the skill is
simply absent.

Why native discovery has to be switched off: §8 shows that today the CLI loads
`.claude/skills`, `CLAUDE.md` **and the target repo's hooks** straight from the
worktree. Those are the files a task agent is editing during the run.

What this does **not** buy: it is not a code-execution boundary. Under
`bypassPermissions` a task agent already runs repo code whenever it runs the
repo's tests, so a repo hook running on the server is the same exposure as
`npm test`. The gain is that the agent's *instructions* are pinned to base,
which a human reviewed. Isolating repo code from the server is still the
sandbox item (PRD-Phases §1.2).

If a branch touches `.rookery/`, `.claude/` or `CLAUDE.md`, the PR body says
so: "⚠ this PR changes agent configuration". It costs one `git diff
--name-only`.

---

## 2. Skills

### 2.1 What the CLI does today, and why we take it over

By default, `claude` running in the worktree already loads the repo's
`.claude/skills/`, `CLAUDE.md` and `.claude/settings.json` (S4a). The last one
includes **hooks, which run as the `rookery` user on the server for every
call**. That is the case in production today, independent of this plan.

`--setting-sources user` switches off all three together (S4d). You cannot
keep the skills and drop the hooks. So Rookery takes over delivery itself
(§1.3):

- the repo's skills are copied from base into the per-job plugin (S6
  confirmed they work from there)
- the root `CLAUDE.md` is passed with `--append-system-prompt`. That puts the
  whole file in one argv entry, and Linux caps a single argument at 128 KB
  (`MAX_ARG_STRLEN`). Above a 64 KB limit the job fails at start with a comment
  saying so. It does not truncate silently, and it does not surface as an exec
  error from `sh()`.

The cost is that nested `CLAUDE.md` files in subdirectories are no longer
picked up when the agent reads files there. That is acceptable for v1. Revisit
if a tenant depends on them.

For tenants nothing changes: skills in `.claude/skills/` and conventions in
`CLAUDE.md` work exactly as they do for their human developers. The README
should say so.

### 2.2 Transport: one `--plugin-dir`, assembled per job

```
$WORKSPACE/plugins/<owner>_<repo>/job-<id>/
  .claude-plugin/plugin.json          # {"name": "rk"}
  skills/<catalog skills this repo enabled>/   # from rookery/skills/
  skills/<repo skills>/                         # from origin/<base>
  agents/<built-in + repo agents>.md            # rookery/agents/ + .rookery/agents/
```

The directory is built at job start, reused by every call in that job, and
removed in a `finally` when the job ends, whatever the outcome. No cache across
jobs: `git archive` of a few small files is cheap. A cache keyed by sha would
be shared across issues, which leaves no obvious owner to delete it. A job
killed by a restart leaves its dir behind, and `main()` clears
`$WORKSPACE/plugins/` at startup, next to marking leftover jobs `interrupted`.
Facts from §8 that shape it:

- **Exactly one plugin per job.** 2.1.110 treats `--plugin-dir` as *one*
  plugin; a folder of plugins does not load its children (S1b).
- **Everything gets namespaced `rk:`.** Skills appear as `rk:<skill>` and
  agents as `rk:<agent>`. Names from different sources can still collide:
  catalog, repo and `.rookery` all land in one `skills/`. Precedence is
  `.rookery/skills` over `.claude/skills` over catalog, and the run log records
  which source won.
- **Names are checked before they are used.** Every skill and agent name, from
  a directory or from `workflow.json`, must match `^[a-z0-9][a-z0-9-]*$`. A name
  from `workflow.json` must also exist in the assembled plugin. These names are
  rendered into prompts ("Use the `rk:x` skill") and into `--agent`, so
  anything else fails the job at start.
- **Skill frontmatter is allowlisted too, not only agents' (§3.2).** A
  `SKILL.md` keeps `name`, `description` and `allowed-tools`. `hooks`, and any
  other key, fail the job at start with a comment naming the file and key.
  Without this, a repo skill copied into the plugin could carry hooks back in
  through its frontmatter.
- **Only `skills/` and `agents/` are assembled.** No `hooks/`, `.mcp.json` or
  `commands/` from any source. A hook runs shell outside the agent loop, and
  MCP needs secrets (§7).
- **Agents move into the plugin too.** `--agent rk:<name>` selects a plugin
  agent, and its `model:` and `skills:` frontmatter are honoured (S2b, S3a).
  This settles the open question in default-agents.md: the `--agents` JSON
  transport goes away. `parse_agent()` shrinks to the frontmatter-key
  allowlist check in §3.2. The JSON path also coexists with `--plugin-dir`
  (S7), so the migration can happen in either order.

### 2.3 Skills fire when named, not when merely available

| | `verify-done` fired | Source |
|---|---|---|
| Skill installed, prompt does not name it | **0 / 3** | S5a |
| Prompt says "Use the verify-done skill" | **3 / 3** | S5b |
| Agent preloads it with `skills:` frontmatter, Skill tool denied | content present | S3a (control without preload: absent, S3b) |

So "enable a skill" means the orchestrator **names it** in the slot's prompt
(the `skills` list in `workflow.json` renders as "Use the `rk:x` skill …"), or
the slot's agent preloads it. Having a skill in the plugin dir alone does
almost nothing. Preloading puts the whole skill body into context on every
call; naming loads it only when invoked. Default to naming.

### 2.4 The built-in catalog

Vendored into `rookery/skills/`, pinned to a commit, and read by the operator
before it lands. Never fetched at run time: no `--plugin-url`, no marketplace
pulls. A skill can ship scripts, and they run on the operator's box.

**Bold** = first cut.

| Slot | Skill | Source | Why |
|---|---|---|---|
| plan | **repo-recon** | ours | Finds the build/test/lint commands and records them in `plan.md` under "How to verify", so every task has a check to run. Probably the highest-leverage skill here. |
| plan | writing-plans | obra/superpowers | Overlaps the planmaker persona. Compare before adopting. |
| task | **verification-before-completion** | obra/superpowers | Run the checks and show evidence before claiming done, which makes `STATUS: task-done` honest. |
| task | **test-driven-development** | obra/superpowers | Opt-in. |
| task | **systematic-debugging** | obra/superpowers | Root cause before patching when tests fail. |
| task | language packs (`python`, `dotnet`, `node`, `go`) | ours, small | Idiomatic build/test/format commands and common traps per ecosystem. |
| task | frontend-design | anthropics/skills | Opt-in for UI work. |
| task | webapp-testing | anthropics/skills | Operator opt-in only: needs browsers on the box, which has no AVX2. |
| check | **security-review** | ours | The most-requested extra review. |
| check | performance-review, accessibility-review | ours | Narrow reviewers produce sharper findings than one broad one. |
| write | **changelog** | ours | Updates CHANGELOG from the diff. |
| write | docs-writer | ours | |
| — | skill-creator | anthropics/skills | Linked from the portal so tenants can write good skills. Not loaded into runs. |

Left out: document skills (`pdf`, `docx`, `xlsx`, `pptx`) and the art skills.

---

## 3. Custom agents

### 3.1 Format and naming

The same native agent format as `agents/*.md`, placed in `.rookery/agents/`.
At run time they are selected as `rk:<name>`.

**`--agent` with a name that does not exist is silently ignored.** S2a passed
the bare `plug-agent` and got a run with the default model and no persona, and
no error. A typo in `workflow.json` would therefore quietly run the wrong
role. The orchestrator must only ever pass names it has just put into the
plugin, and must fail the job at start for any slot that names an agent that
does not exist.

### 3.2 Allowed frontmatter keys

| Key | Allowed | Note |
|---|---|---|
| `name`, `description` | yes | `name` must equal the file stem, as today |
| `tools` | yes | a hint; the deny rules still apply |
| `model` | `{haiku, sonnet, opus}` | honoured from a plugin agent (S2b); caps bound spend |
| `skills` | yes | preload works for the main session (S3a) |
| `hooks`, `mcpServers`, `permissionMode`, anything else | **rejected** | the job fails at start with a comment naming the key |

### 3.3 Override vs extend

A repo can **replace** a built-in role by using the same name. That is safe,
because no invariant lives in a persona: plan scope is `keep_plans_only()`,
publishing is the deny rules plus a token that is never in the env, and
approval is a regex.

The common case is *adding a few rules*, so each slot also takes
`instructions`, which is appended to the role's persona. No agent file is
needed for that.

---

## 4. Workflows

### 4.1 Slots, not a DAG engine

```
plan ──► [approve gate] ──► tasks (loop) ──► after-steps… ──► review ──► push + draft PR
 │            fixed              │              new                │
 agent        not                agent          ordered list       agent
 +skills      configurable       +skills        of agents          advisory
```

The approve gate, plan-scope enforcement, the task-loop bound, the caps and
the orchestrator-only commit and push are not part of the config. That means
there is nothing to validate for them, and nothing a tenant can weaken.

### 4.2 `workflow.json`

JSON, because the stdlib parses it.

```json
{
  "plan":   { "agent": "planmaker", "skills": ["repo-recon"],
              "instructions": "Every schema change gets its own migration task." },
  "task":   { "agent": null, "skills": ["verification-before-completion"] },
  "after":  [
    { "agent": "test-writer", "mode": "write" },
    { "agent": "security",    "mode": "check", "skills": ["security-review"] }
  ],
  "review": { "agent": "reviewer" }
}
```

Any key that is missing falls back to the org default and then to the
built-in. An invalid file fails the job at start, with a comment naming the
problem. Rookery never silently runs the default instead.

### 4.3 Two step modes, each with a fixed STATUS set

| Mode | May edit | STATUS | After it | Resume |
|---|---|---|---|---|
| `check` | no. Enforced: a dirty `git status` is reverted, as in `keep_plans_only()`, and flagged | `check-ok` / `changes-needed` | findings go into the PR body as `## <agent>` | re-runs every time |
| `write` | yes | `step-done` / `failed` | orchestrator commits `#<n> step <agent>` (`--allow-empty`) | skipped if that commit is already on the branch |

Resume state lives in git, which is how `implement()` already resumes from
`tasks.md`. A `failed` write step raises like a failed task. A
`changes-needed` from a check only sets `rookery:error`, as the reviewer does
today.

Neither mode appends tasks, so this invariant is untouched: every tick
strictly reduces the open count.

### 4.4 Bounds and cost

- `MAX_STEPS = 5` after-steps, kept separate from `MAX_TASKS`.
- One issue with plan + 6 tasks + 3 steps + review is 11 calls, which is more
  than today's `RUN_CAP = 10` per repo per day. Without any steps, plan + 6
  tasks + review is already 8. **Decide this before Phase B ships** (§9.2).
- For scale: every spike call cost $0.006–0.07, about $0.44 for the whole
  spike. Here the call count bites before the dollar cap does.

---

## 5. Run log

- Each `claude` step records `agent`, `skills` and `model` next to the
  existing key/status/cost.
- Each job records `base_sha`, the commit every steering input was read at
  (§1.3).
- The PR body gets a footer: `workflow: <repo>@<sha> · steps: test-writer, security`.

---

## 6. Portal

| Phase | Page | What |
|---|---|---|
| A | **Workflow** view per repo | The effective pipeline as the slot diagram: agent per slot, a source badge (built-in / repo), skills, `instructions`, validation errors. |
| A | **Skill catalog** | Every built-in skill: description, source, pinned version, suitable slots. |
| C | "Enable for repo" / agent editor | Builds the change and **opens a PR** via the App, behind `can_write()`. A human merges it, so §1.3 holds. The editor applies the §3.2 allowlist while the user types, and job start validates again regardless. |

The portal never approves and never runs a workflow. Both are unchanged.

How it sits in `ui/portal.html` (repo sidebar plus a main area with list and
detail views), following DESIGN.md:

- **Runs | Workflow** switch in the main header for the selected repo, as two
  pill tag buttons. The Workflow view is read-only in Phase A: no buttons, so
  no Ember CTA.
- **The slot diagram is the Workflow Node pattern.** Each slot is a node
  (`#1a1624`, 12px radius) showing its agent, model and skills, joined by
  Electric Current gradient lines. The approve gate is drawn as a fixed node
  (Glowing Inset style, no agent) so it reads as not configurable. Validation
  errors use the existing `lbl err` style, on the offending node and above the
  diagram.
- **Skill catalog** is a link in the sidebar's bottom bar. It is static data
  from `skills/`, so it needs no repo.
- **API:** `GET /api/repos/<owner>/<repo>/workflow` returns the effective
  config plus errors. It is computed by **the same function the job calls at
  start**, so the portal and the next run cannot disagree. It runs the same
  `visible(s)` check as runs, and an unseen repo is a 404. A repo's custom
  personas are private to that tenant. The endpoint fetches base like a job does
  (§1.3). `GET /api/skills` serves the catalog.
- **Every string from the repo** (`instructions`, agent and skill
  descriptions, error text that quotes a key) goes in through `h()` /
  `textContent`, never `innerHTML`, per the existing invariant.
- **No "≈ calls per issue".** It depends on the task count, which only the plan
  knows, so the number would mislead.

---

## 7. Deliberately out of scope

- **MCP servers per tenant.** They need tenant secrets and encrypted storage,
  the same blocker as bring-your-own-key, so they ship with Phase 6.
- **Choosing a workflow per issue by label** (`rookery:wf/hotfix`). The same
  file could become `{ "default": {...}, "hotfix": {...} }`. Not built until
  someone asks.
- **A community marketplace.** The catalog stays operator-curated.
- **Branching or parallel steps.**

---

## 8. Spike results: 2.1.110 on the production server, 1 October 2026

These ran as `rookery` in a throwaway `/tmp/spike` (now removed), with a fake
repo and a one-plugin dir. Total cost was about $0.44. The service was not
touched.

| # | Question | Result |
|---|---|---|
| S1a | `--plugin-dir <plugin>` loads skills under `-p`? | **Yes.** Listed as `rk:plug-skill` and invoked. |
| S1b | `--plugin-dir <folder of plugins>` loads each child? | **No** on 2.1.110 (later CLIs do). Treated as one empty plugin. Assemble exactly one plugin. |
| S2a | `--agent plug-agent` (bare name) for a plugin agent? | **Silently ignored.** Default model, no persona, no error. |
| S2b | `--agent rk:plug-agent`? | **Yes.** Persona applied and frontmatter `model: haiku` honoured. |
| S3a/b | Agent `skills:` preload works for the main session? | **Yes.** It answered from the preloaded skill with the Skill tool denied. The control without the preload could not. |
| S4a | Target repo's `.claude/skills`, `CLAUDE.md`, hooks under `-p` + `bypassPermissions`? | **All three load. Repo hooks run on the server.** That is production behaviour today. |
| S4d | `--setting-sources user`? | Hooks off, **but repo skills and `CLAUDE.md` are off too.** It is all or nothing. |
| S6 | Repo skills copied into the plugin, with `--setting-sources user`? | **Yes.** `rk:repo-skill` loaded and invoked, and no hook ran. |
| S7 | `--agents` JSON alongside `--plugin-dir`? | **Yes.** The JSON agent used a plugin skill. The migration can be gradual. |
| S5 | Does a skill fire on its description alone? | **0/3** unnamed vs **3/3** named, on sonnet. Name it in the prompt (§2.3). |
| S4e-A | Target repo's `.mcp.json`, today's flags? | **Spawned.** Its stdio command ran with no approval and no repo setting. That is production behaviour today. |
| S4e-B | Same, plus repo `.claude/settings.json` `enableAllProjectMcpServers: true`? | **Spawned.** |
| S4e-C | B + `--setting-sources user`? | **Not spawned.** But `init.mcp_servers` still lists the **claude.ai connectors of the account `rookery` is logged in with** (`Claude Docs`: connected, `Google Drive`: needs-auth). |
| S4e-D | B + `--setting-sources user --strict-mcp-config`? | **Not spawned, and `mcp_servers` is empty.** This is the A0 flag set. |
| S4e-E | `.mcp.json` + `--strict-mcp-config` alone? | **Not spawned, and empty.** |

S4e ran on 1 October 2026, same box and CLI, as `rookery` in `/tmp/spike-s4e`
(removed), on haiku, about $0.08. The repo's server was `sh -c 'echo spawned >>
marker'`, and the marker file was the evidence.

**What S4e found besides `.mcp.json`:** every production call today carries
the operator account's claude.ai connectors. A tenant's task agent can read and
write the operator's Claude Docs. That is a cross-tenant leak of the operator's
data, the same class as the `rookery.db` argument in §1.2.
`--strict-mcp-config` closes it on its own (S4e-E), so it does not need to wait
for A0 (§9.0).

Gotcha for the spike itself, and for anything that pipes into `sh()`: `claude
-p` reads stdin even when the prompt is an argument. A script fed through
`bash -s` lost its tail to the first `claude` call. `sh()` passes
`input=None`, which inherits the parent's stdin. Under systemd that is
`/dev/null`, so it is safe, but pass `stdin=DEVNULL` explicitly in A0.

Gotcha for the implementation: `--disallowed-tools` is variadic and swallows a
positional prompt that comes after it. `claude()` is safe today only because
the prompt comes right after `-p`. Keep it there.

**S8, run during A0** (same box and CLI, as `rookery` in `/tmp/spike-a0`, removed;
haiku, about $0.18). Root `CLAUDE.md` delivered by `--append-system-prompt`, with
the exact flags `claude()` builds. The worktree had a `CLAUDE.md` with one codeword,
base's appended copy had another, both said "start every reply with `[rules-ok]`",
and the repo's `.claude/settings.json` had `SessionStart` and `UserPromptSubmit`
hooks writing a marker.

| Variant | Format rule followed | Codeword | Hooks |
|---|---|---|---|
| today's flags (native discovery) | 6/6 | worktree's | ran every call |
| A0 flags, no agent | 5/7 | **base's, 7/7** | none |
| A0 flags + `--agents`/`--agent` | 2/2, persona also followed | base's, 2/2 | none |
| A0 flags, appended text prefixed with the CLI's own CLAUDE.md header ("IMPORTANT: These instructions OVERRIDE…") | 5/5 | base's, 5/5 | none |

So the appended copy replaces the worktree's, works alongside `--agent`, and the
`rk` plugin loads from `--plugin-dir=<path>` (`init.plugins`, `rk:probe-skill`).
Without the header it is slightly weaker than native; `claude()` sends the header.

**Smoke test after the A0 deploy** (2 October 2026, production, `rookery-throwaway-2`
issue #4 → draft PR #5; plan + 1 task + review, about $0.29). Base `main` carried a
`CLAUDE.md` with a codeword rule, a `.claude/skills/smoke-check` skill, and a
`.claude/settings.json` `SessionStart` hook touching `/tmp/rookery-smoke-hook-ran`.
Both the plan and the implement job logged their own `base` step (sha, `["smoke-check"]`);
`plan.md` ended with the codeword, so the appended `CLAUDE.md` reached the agent; the
hook marker never appeared across all three calls; each job's `plugins/…/job-<id>` was
gone after it finished. The server had still been running pre-PR-#7 code, so
`--strict-mcp-config` went live in this same deploy.

**S9, RTK hook** (2 October 2026, production box, as `rookery` in `/tmp/spike-rtk`,
removed; haiku, about $0.12). RTK 0.50.0 (musl) installed as an operator-level
`PreToolUse` hook in `~rookery/.claude/settings.json` (DEPLOY.md §7), with the exact
`claude()` flags:

| Check | Result |
|---|---|
| Hook fires under `--setting-sources user --strict-mcp-config -p` | **Yes.** `ls`, `git status`, `git log` were rewritten, 78% fewer tokens |
| A failing test's assertion message still reaches the agent (`unittest`, `npm test`) | **Yes**, verbatim |
| `git push`, `gh`, and `git status; gh …` / `… && git push` with the exclusions set | **All denied** |
| `git push` with **no** exclusions and today's deny rules | **Ran.** It became `rtk git push` and missed `Bash(git push:*)`; only the missing remote stopped it |
| Same, plus `Bash(rtk git push:*) Bash(rtk gh:*)` | **Denied** |

So deny rules match the command after the hook rewrites it. `claude()` now denies the
`rtk` forms too, and the RTK config excludes `git push` and `gh`: two independent guards.
Caveman and codebase-memory MCP were considered and **not** installed. Caveman would
shorten the plan, comments and PR text that tenants read. codebase-memory keeps one index
shared by every tenant, which needs a spike with a per-tenant store first.

---

## 9. Open decisions

0. ~~Ship `--strict-mcp-config` now, ahead of A0?~~ **Done: merged in PR #7.**
   It stops the operator's claude.ai connectors from reaching every tenant's
   agent, and stops a repo's `.mcp.json` from spawning (S4e-E). Restricting the
   CLI login account's connectors is a separate backstop, not a replacement.
1. ~~Turn off native project settings (`--setting-sources user`)?~~ **Done:
   A0, PR #8, deployed 2 October 2026.** It closes "the agent edits its own
   instructions mid-run" and pins every steering input to reviewed base (§1.3).
   It is not a sandbox: repo code still runs whenever the tests do. The cost is
   losing nested `CLAUDE.md` files.
2. ~~Caps~~ **Decided: raise the `RUN_CAP` default from 10 to 30** (2 October
   2026, with Phase B). One line; `USD_CAP` stays the spend bound, and per-issue
   counting would need an issue column in `runs` for no new guarantee.
3. ~~First catalog cut~~ **Decided: 4 skills.** `repo-recon`,
   `verification-before-completion`, `systematic-debugging` and
   `test-driven-development`. `security-review` and `changelog` wait for
   Phase B's `after` steps (trade-offs.md §2).
4. ~~Org-level `<owner>/.rookery` in Phase A?~~ **Decided: Phase C** (§1.2).
5. ~~Default task skills~~ **Decided: off by default.** A repo with no
   `workflow.json` gets no task skills (trade-offs.md §1).

## 10. Phasing

| Phase | Ships | Test asserts |
|---|---|---|
| 0 | ~~Spikes~~ done (§8) | n/a |
| 0.5 | ~~`--strict-mcp-config` in `claude()` (§9.0)~~ **merged, PR #7** | the flag is in every `claude` argv |
| A0 | **Done: PR #8, deployed and smoke-tested 2 October 2026** (`pin()`). Fetch base at job start, `--setting-sources user --strict-mcp-config` (S4e-D), `stdin=DEVNULL` on `claude`, root `CLAUDE.md` from base via `--append-system-prompt`, repo `.claude/skills` from base into a per-job plugin removed in `finally`, `base_sha` in the run log. **Its own PR and deploy.** | no project hooks load; skills and `CLAUDE.md` are read from the freshly fetched `origin/<base>`, not the worktree; an oversized `CLAUDE.md` fails at start; skill `hooks:` frontmatter rejected; the plugin dir is gone after the job |
| A1 | **Deployed 2 October 2026, with A2.** Backend half of A: catalog in `skills/` (pinned to obra/superpowers@8ca22db, see `skills/README.md`), `.rookery/workflow.json` (plan/task/review: agent, skills, `instructions`), skills named in slot prompts, `instructions` after `CLAUDE.md` in `--append-system-prompt`, agents in the plugin as `rk:<name>`, `.rookery/skills` > `.claude/skills` > catalog, run-log fields, PR footer. `after` in the file is rejected until B. | a bad config, a bad name or an unknown skill/agent fails at start; unknown frontmatter keys rejected (skills and agents); only `skills/` + `agents/` assembled; precedence and the winner logged |
| A2 | **Deployed 2 October 2026, with A1.** Portal half of A: Workflow view, skill catalog, `GET /api/repos/<owner>/<repo>/workflow`, `GET /api/skills` | the workflow API 404s an unseen repo and uses the job's validator |
| B | **Deployed 2 October 2026; no end-to-end smoke test yet.** `.rookery/agents/` (top-level `*.md`, same name replaces a built-in, logged in the `base` step), `after` steps (check/write) via `after_steps()`, `MAX_STEPS = 5`, `## <agent>` PR sections, `steps:` in the footer, config-change warning, the reviewer's leftovers reverted, Workflow view draws the steps. With it: `RUN_CAP` 30 (§9.2) and the `security-review` (check) and `changelog` (write) catalog skills | a check step's edits reverted; write-step resume via commit subject; STATUS set enforced per mode |
| C | Portal editing via PR, agent editor, org defaults | edits need `can_write()`; nothing written except through a PR |
