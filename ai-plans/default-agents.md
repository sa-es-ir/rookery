# Default Agents — PlanMaker and Reviewer

Two named roles shipped with Rookery: **planmaker** runs the planning phase,
**reviewer** reads the finished branch before a human does. Implementation
tasks deliberately run as no agent at all.

**Status:** designed. A first implementation exists uncommitted on
`1-add-default-agents` and has not been reviewed. The `--agents` transport is
verified end to end; `--plugin-dir` is not.

---

## Why these two

Rookery already had a planner — `plan()` plus the `PLAN` / `REPLY` templates.
Promoting it to a named role is not a rename for its own sake; it buys three
things that were previously impossible:

1. **A persona separate from the job.** The templates carry *this issue*; the
   persona carries the standing rules of the role. Today the standing rules are
   re-stated in every template, which is how they drift.
2. **A per-role model.** Planning is the one phase where a stronger model pays
   for itself — a bad plan becomes fifteen tasks of wrong code. Grinding
   through those tasks does not need the same model.
3. **Somewhere for the reviewer to hang.** A reviewer with no role mechanism
   would have been a fourth prompt template and a fourth special case.

The reviewer is genuinely new. It is the VSDD verifier gate, carried over in
the weakest form that still helps — see [Prior art](#prior-art) and
[Escalation](#reviewer-escalation-future).

## The constraint that shapes the design

`claude` is invoked as a subprocess with **cwd set to the target repo's
worktree**, never to Rookery. Claude Code discovers project agents relative to
cwd. Therefore:

- `.claude/agents/*.md` inside Rookery is **never seen** by the subprocess.
- Writing agent files into the target worktree would pollute somebody else's
  repo with our machinery, and land it in the PR.

So the definitions must travel *with the invocation*. The CLI's `--agents`
flag takes a JSON object for exactly this, and `--agent <name>` selects one for
the session.

This does **not** mean the personas have to *live* as JSON. Files stay the
source of truth; JSON is only the transport.

## Layout

```
agents/
  planmaker.md
  reviewer.md
```

At the repo root, **not** `.claude/agents/`. These are data Rookery ships to
other repositories, not subagents for working on Rookery. Putting them in
`.claude/agents/` would make Claude Code load them as its own subagents for
anyone who opens this repo — wrong, and quietly confusing.

### File format

**The real Claude Code agent file format**, which is what the VSDD plugin's 11
agents already use — verified by reading them, not assumed:

```markdown
---
name: planmaker
description: >
  Turns a GitHub issue into an implementation plan and an ordered task
  list. Writes agent-plans/<n>/ and nothing else.
tools: [Read, Grep, Glob, Write, Edit, Bash]
---

You turn a GitHub issue into a plan somebody else executes.
...
```

Matching this exactly is a deliberate cost. A simpler flat `key: value` subset
would parse in about eight lines, but it would make the `--plugin-dir` swap
described below cost a rewrite of every agent file rather than just the loader.
Same format, both directions.

Three things a naive `split(":")` gets wrong, all present in the VSDD files:

- `description: >` is a **YAML folded block scalar** — the value is the
  indented lines that follow, joined.
- `tools: [Read, Grep, Glob]` is an **inline list**, not a string.
- `name:` is explicit, not derived from the filename. Keep it, and assert it
  matches the stem so the two cannot silently diverge.

### Loading

`agent.py` reads the directory once at import and serialises it:

```python
AGENTS = json.dumps({p.stem: parse_agent(p)
                     for p in sorted((HERE / "agents").glob("*.md"))})
```

The parser is the honest cost of this design: roughly twenty lines to handle
folded scalars and inline lists. It is still stdlib only — the subset is small
and fixed, so it does not justify a YAML dependency, which would change what
this project is.

`claude()` gains an `agent=` argument and appends `--agents AGENTS --agent
<name>` **only when set**, so task invocations stay byte-identical to what they
are today.

### Alternative considered — `--plugin-dir`

`claude --plugin-dir <path>` loads a plugin whose `agents/` directory Claude
Code parses itself. That would delete the loader and the parser outright, and
it is the native format — precisely the VSDD structure this design borrows
from.

Not chosen yet for one reason: it needs plugin scaffolding, and **it is
unverified that `--agent <name>` can select an agent loaded that way**. The
`--agents` JSON path has been tested end to end (a probe agent correctly
ignored the user prompt and obeyed its persona, with `tools` and `model`
accepted).

Ship the loader; swap to `--plugin-dir` if the plugin ecosystem becomes worth
joining. Because the files are in the native format, the swap deletes the
parser and changes nothing else — that is the whole reason for paying for the
parser now.

## The roles

| Role | Runs | Writes | Model |
| --- | --- | --- | --- |
| `planmaker` | the planning phase, including every resumed reply | `agent-plans/<n>/` only | seam exists, unset |
| `reviewer` | once, after the last task, before the push | nothing — read only | seam exists, unset |
| *(none)* | every implementation task | the target repo | default |

Implementation tasks running as **no** agent is deliberate. The default harness
plus the deny rules is already the right shape for them, and a persona there
would be three more paragraphs of context per task, on every task, forever.

Neither role sets a model. That is a deployment choice, not an orchestrator
one; `model: opus` in a role's frontmatter is the whole change.

## What is enforced, and what is only asked

Rookery's existing rule is that a prompt is never a guard. The roles do not
change that, and must not be mistaken for it.

| Concern | Mechanism | Kind |
| --- | --- | --- |
| Agent never pushes or uses `gh` | `--disallowed-tools Bash(git push:*) Bash(gh:*)` | enforced — deny beats `bypassPermissions` |
| Planning touches only `agent-plans/<n>/` | `keep_plans_only()` after every `plan()` | enforced — in the orchestrator |
| Reviewer does not edit | `tools:` list in the role | **hint only** |
| Task quality, plan shape, review focus | the persona body | asked |

A role's `tools:` list is a role hint, not a security boundary. The deny rules
and the orchestrator checks are what actually hold.

### Plan scope

`keep_plans_only()` runs after every `plan()` call. It walks
`git status --porcelain -uall` and reverts anything outside `agent-plans/<n>/`
— `git clean` for untracked paths, `git checkout` for tracked ones.

It **reverts rather than raises**, so the awaiting-input reply loop can call
`plan()` as many times as the conversation needs without accumulating stray
edits ahead of the task loop. Re-planning is therefore always idempotent.

Two traps worth recording, both hit while building it:

- `-uall` is required. Plain `--porcelain` collapses an untracked tree to its
  shallowest directory, so a first-ever plan reports as one `?? agent-plans/`
  entry, fails a `agent-plans/<n>/` prefix test, and gets **deleted**.
- `sh()` strips its output, so the leading space of a `" M path"` status code
  survives on every line except the first. Parse per line; never index by a
  fixed offset.

## Review is advisory

The reviewer runs once, between the last task and the push. It reads
`git diff origin/<base>...HEAD` against `plan.md`, `tasks.md` and
`decisions.md`, and emits `STATUS: review-ok` or `STATUS: changes-needed` plus
findings as markdown.

```
last task committed → claude:reviewing → push → draft PR
                                                    │
                              body: tasks + ## Review findings
                                                    │
                          review-ok      → claude:done
                          changes-needed → claude:error
```

**The branch is pushed and the PR opened either way.** The findings go into the
PR body under `## Review`, verbatim, because that is where the human already
is when they need them. A bad verdict only sets `claude:error` so somebody
looks.

The reason for advisory-first is durability: a blocking gate leaves the branch
existing only as a worktree on the orchestrator host, invisible on GitHub, and
gone if the laptop is wiped.

`STATUS:` is parsed here exactly as everywhere else — a missing marker is an
error, never a guess.

## Reviewer escalation (future)

Both stronger forms were considered and rejected for now. The reasons are the
trigger for revisiting them.

**Blocking gate.** `changes-needed` stops before `git push`; findings post as
an issue comment, phase goes to `claude:error`, no PR. Commenting `approve`
again re-runs from the first unticked task.

*Revisit when:* draft PRs from failed reviews turn out to be noise a team has
to triage. Until then, durability wins.

**Advisory plus a fix loop.** On `changes-needed`, append findings to
`tasks.md` as new tasks, re-enter the implement loop, review again.

*Revisit when:* there is data on how often reviews come back `changes-needed`
at all. Note that this is a real design, not an increment — a reviewer that
appends tasks breaks the invariant that *every tick strictly reduces the open
count*, so it needs its own separate bound (a maximum number of review rounds)
and a rule for what happens when a fix round makes the review worse.

## Prior art

From `plugins/vsdd/agents/` (11 definitions) in the `core_ai` repo — see the
Prior art table in
[github-issue-agent-plan.md](github-issue-agent-plan.md) for full paths.

Carried over: the agent file format, verbatim; agents as markdown with
frontmatter rather than config; a role per phase; structured markers rather
than prose parsing. `spec-verifier-*`'s "default posture: distrust" is the
reviewer persona's ancestor.

Not carried over: a verifier between *every* phase, and VSDD's structured
JSON verification report with a PASS/FAIL verdict. There is one review, at the
end, and its output is markdown for a human rather than a machine-readable
artifact — nothing downstream consumes it, so a schema would be ceremony.
Human PR review remains the real gate.

## Open decisions

- Whether `--plugin-dir` can serve an agent that `--agent <name>` selects. If
  it can, the loader and its parser are deleted. **Spike before the format
  ossifies.**
- Whether the planmaker should get an explicit model, and which. Needs a real
  plan-quality comparison, not a guess.
- Whether the reviewer should also read the target repo's own conventions
  (`CLAUDE.md`, contributing guide) rather than only the plan. Cheap to add,
  unclear whether it improves findings or just lengthens them.
- Whether a third role for implementation tasks ever earns its context cost.
  Currently no.

## Definition of done

Label an issue → the plan comment is written by `planmaker` and the worktree
contains changes to `agent-plans/<n>/` and nothing else → `approve` → tasks run
→ `claude:reviewing` appears → a draft PR opens with a `## Review` section
whose findings match the diff.
