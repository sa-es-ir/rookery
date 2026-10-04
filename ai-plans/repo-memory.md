# Repo memory, cheaper PR reviews, and an operator MCP server

Every `claude` call starts cold. As Rookery takes on more repos, and more issues per repo,
each run pays again to learn the same layout, commands and gotchas. This plan has three
parts, in build order:

- **A. PR review fixes.** Small, and they target the calls that cost the most today.
- **B. Repo memory on the server.** One notes file per repo. A cheap model writes it from
  each job's transcripts. Nothing goes into the target repo.
- **C. codebase-memory MCP.** One operator-owned MCP server, with an index per repo. Needs
  a spike (S10) first.

**Status:** plan approved, 3 October 2026. A and B built on branch `12-agent-memory`, with
tests, and deployed 3 October 2026 (uncommitted tree, scp). Checked on the box first: A5
(`rtk proxy echo …` with the exact `claude()` flags lands in `permission_denials`) and S10g
(`--tools "" --max-turns 1 --model haiku` on 2.1.110 replies, as `claude-haiku-4-5`). S10 and
C not started.

---

## 1. What the agent spends today

Read-only query on `rookery.db` in production, 3 October 2026. All spend since
19 September: **$3.73 over 32 calls.**

| What | Calls | USD | Avg |
|---|---|---|---|
| `pr-review` (opus): Mantis #76, Mantis-Frontend #110 ×2 | 3 | 1.81 | 0.60 |
| `after` steps (haiku) | 9 | 0.31 | 0.03 |
| plan, review, task: throwaway repos only | 13 | 1.21 | about 0.09 |

So far, three PR reviews on real repos cost half of all spend. No issue has been implemented
on a real repo yet, so there is no data yet on how much the implement flow spends exploring.

The session transcripts (`~rookery/.claude/projects/*/<sid>.jsonl`) of the three reviews:

| | Mantis-Frontend #110 | Mantis #76 |
|---|---|---|
| Turns | 22 | 33 |
| Cache writes | 132k | 92k |
| **Cache reads** | **616k** | **1,173k** |
| Output | 4.8k | 8.3k |

- **The reviews did not explore the repo.** About 8 of the 18–26 tool calls were
  `git diff origin/<base>...HEAD -- <dir>`, one directory at a time. The other calls read
  the changed files. Repo memory would have saved little here.
- **The cost comes from the number of turns.** Each turn reads the whole context again from
  the prompt cache, so 30 turns cost about 30× the context.
- **RTK cuts the diffs.** The operator's RTK hook (DEPLOY.md §7) rewrote each
  `git diff` to `rtk git diff`, 37–79% smaller. It does this by dropping hunk lines: the
  output has `... (46 additions truncated)` and `... (more changes truncated)`. So the
  reviewer diffed a directory, diffed its subdirectories again, then `Read` the files to
  see what was cut. Measured on #110 (52 files):

  | | chars |
  |---|---|
  | raw `git diff` of the whole PR | 164,384 |
  | raw, without `package-lock.json` | 154,066 |
  | `rtk git diff` of the whole PR | 23,804 (−85%) |
  | what the reviewer pulled in: 4 RTK diffs (86.5k) + 9 file reads (38k) + other (10k) | 135,103 |

  One RTK diff is 85% smaller, but the whole review read only about 12% less than the raw
  diff, and took more turns to do it. Mantis #76 is the same shape: 68k of diffs, 30k of
  reads.
- **#110 was reviewed twice**, 4 minutes apart, at the same base sha, for $1.11. Nothing
  skips a head that already has a review.
- **Every review wasted its first turn** on `cd /home/user/repos/<repo>`. The model invents
  that path. It is not in our code and does not exist on the box.

---

## 2. Part A: PR review fixes

All of these are in `review_pr()`, `PR_REVIEW` and `claude()`'s deny list. One PR. The
only invariant it touches is the deny-rule list (A5).

**A1. Put the diff in the prompt.** After `pr_worktree()`, the orchestrator runs, in the
worktree:

- `git diff --stat origin/<base>...HEAD`, always included;
- `git diff origin/<base>...HEAD -- . ':!package-lock.json' ':!*.lock'`, included up to
  `DIFF_CAP = 100_000` characters (about 25k tokens).

The orchestrator's `sh()` does not go through the RTK hook (the hook applies only to the
agent's Bash tool), so this diff is complete. Above the cap, the diff is cut at the last
file boundary before `DIFF_CAP`, and the prompt names the files left out: "read these files
with Read". `Read` is not touched by RTK, so the reviewer sees them whole.

The expected win is **fewer turns**, not fewer characters read. #110's reviewer read 135k
characters in 22 turns; with A1 it starts with about 100k of complete diff and needs only
the reads it chooses.

**RTK stays on `git diff`.** Considered and dropped: adding `git diff` to RTK's
`exclude_commands`. A1 already gives the reviewer the complete diff, and task agents keep
RTK's saving when they diff their own work.

**A2. Skip a head that already has a review.** The `posted` step also records `head` (the
PR's head sha, from `gh pr view --json headRefOid`). Before the worktree is made: if this
repo and PR have an `ok` pr-review job that posted at the same head, and the new trigger
has no note, do not call `claude`. Post one PR comment instead: "Already reviewed at
`<sha7>`. Push a change, or mention me with a note, to review again." A label or a
review-request trigger is cleaned up as usual (`pr_cleanup()`).

**A3. Give the absolute path.** `PR_REVIEW` names the worktree's absolute path and says
"you are already in it". One line.

**A4. Reviewer model.** `agents/pr-reviewer.md` stays `model: opus`. Revisit after A1
has been measured on real reviews. A repo can already replace the reviewer with `.rookery/agents/pr-reviewer.md`
on a cheaper model.

**A5. Deny `rtk proxy`.** `rtk proxy <cmd>` runs a command raw, and the hook does not
rewrite it. So `rtk proxy git push` and `rtk proxy gh …` match none of today's four deny
rules (`git push`, `gh`, `rtk git push`, `rtk gh`). Nothing breaks today, because the token
never reaches the agent's env, but the deny rules are meant to be a second guard. Add
`Bash(rtk proxy:*)` to `--disallowed-tools`, and change the invariant in `CLAUDE.md` from
"keep all four" to "keep all five". Not tested yet: check it the same way as S9, on the
box, with the exact `claude()` flags, before the deploy.

**Tests** (the existing fakes in `test_agent.py`): the prompt has the stat and the diff;
lockfiles are left out; a diff over the cap is cut at a file boundary and the prompt names
the files left out; a second trigger at the same head with no note makes no `claude` call
and posts the "already reviewed" comment; a note, or a new head, runs the review;
`Bash(rtk proxy:*)` is in every `claude` argv.

---

## 3. Part B: repo memory on the server

### 3.1 Shape

```
any plan / reply / implement / pr-review job ends (ok or failed, not cancelled)
  → handle() submits {"kind": "memory", repo, job: <id>} to the same repo queue
  → remember(repo, job):
       digest = digest(transcripts of that job's claude calls)     # orchestrator, no model
       new = claude(MEMORY prompt: current memory + digest, haiku, no tools)
       check the reply, then write $WORKSPACE/memory/<owner>_<repo>.md   # orchestrator writes
  → the next job's pin() reads the file once, and claude() appends it after CLAUDE.md
```

- **Store:** `$WORKSPACE/memory/<owner>_<repo>.md`. Not the target repo, not
  `rookery.db`. `offboard()` deletes it with the clone.
- **Writer:** the orchestrator, from the model's reply. The model never writes the file
  itself. This is the same rule as "the orchestrator ticks checkboxes".
- **Who runs it:** a `memory` job on the repo's own queue, right after the job it learns
  from. One job at a time per repo, so two writes never race and there is no lock. No cron
  and no scheduler thread are needed. The cost is that the repo's next job waits for one
  haiku call (tens of seconds).

### 3.2 The digest (no model)

A raw transcript is too big to give to the model: #110's was about 130k tokens of unique
content. `digest()` reads each `claude` step of the job, finds its transcript, and keeps:

- each tool call: tool name plus path, command or pattern, cut to 200 characters;
- each tool error, or Bash call with a non-zero exit: the first 5 lines of its output;
- the final reply, cut to 3,000 characters;
- the step key (`plan`, `task-3`, `pr-review` …) and its `STATUS`.

It never keeps file contents or successful command output. Those are big, and they are the
most likely place for injected text. Target size: under 12 KB.

To find the transcript, `claude()` adds `sid` to its `claude` step. `digest()` globs
`~/.claude/projects/*/<sid>.jsonl`. A missing transcript is skipped and logged, never fatal.

### 3.3 The model call

- `--model haiku`, cwd an empty temp directory, `--max-turns 1`, no plugin, no agent, the
  same `--setting-sources user --strict-mcp-config` and deny rules as every call. S10 also
  checks whether 2.1.110 can turn off all tools (`--tools ""` or the equivalent).
- It goes through `check_caps()` and `record()`, so it counts against the repo's daily cap
  like any call. It is its own row in `jobs` (`act = "memory"`), so the portal shows its
  cost and its result.
- Expected cost: about 5k tokens of input on haiku, about the size of the `after` steps
  measured in §1 (avg $0.03).

`MEMORY` prompt, in outline:

> You keep the notes that Rookery's agents read at the start of every run on `{repo}`.
> Below are the current notes, then a digest of one run. Return the complete updated
> notes. Keep facts that help a future run on this repo: where things live, the commands
> that build and test it and whether they work on this box, conventions, and traps
> (commands that failed, and why). Remove notes the digest shows to be wrong. Do not
> record anything about this one issue or PR, and no secrets, tokens or URLs with
> credentials. At most {cap} characters. Put the notes between `<notes>` and `</notes>`,
> then one line: `STATUS: memory-updated` or `STATUS: memory-unchanged`.

**Checks before the write:** the `STATUS` marker is required (`MARKER`, as everywhere);
the notes must be between the tags; their size must be at most `MEMORY_CAP = 8_000`
characters. A failed check keeps the old file and logs a `memory` error step. A memory job
never comments on the issue or PR and never sets a label: memory is a side channel, so its
failure must not show up as a failed run.

### 3.4 Reading it

- `pin()` reads the file once per job, like `CLAUDE.md`, into `active[repo]["memory"]`, and
  logs `memory: <chars>, <sha256[:12]>` in the `base` step. That keeps every run
  reproducible from its log.
- `claude()` appends it to `rules`, after `CLAUDE.md` and before the slot instructions:

  > Notes Rookery kept from earlier runs on this repo. They can be out of date. When they
  > disagree with the code or with CLAUDE.md, the code and CLAUDE.md win.

- Every slot gets it: plan, task, review, `after` steps and pr-review.
- The 64 KB limit on `--append-system-prompt` now counts `CLAUDE.md` + memory +
  instructions together. A file that would push it over is left out for that job and
  logged. It never fails the job.

### 3.5 Portal

- `GET /api/repos/<owner>/<repo>/memory`: the notes, their size, and the job that last
  wrote them. Same visibility as the run log (someone else's repo is a 404).
- `DELETE` on the same path clears the notes. Needs `can_write()`, like cancel.
- Editing by hand in the portal is not in v1.
- `docs/`: one short page saying what the notes are, where they come from, and how to
  clear them.

### 3.6 Switch

`MEMORY=0` in `.env` turns off both the writing and the reading, for the whole instance.
There is no per-repo switch, because the user asked for nothing in the repo. "Clear" in the
portal is the per-repo reset.

### 3.7 Tests

`digest()` on a sample transcript (tool calls kept, file contents dropped, cut sizes);
`remember()` with a faked `claude`: good reply writes the file; missing marker, missing
tags or over-cap reply keeps the old file; a cancelled job queues no memory job;
`pin()` + `claude()` put the notes in the appended prompt; `MEMORY=0` does neither;
`offboard()` removes the file; the portal GET/DELETE with and without `can_write()`.

### 3.8 What it does not protect against

- **Any tenant's agent can write any repo's notes.** The agents run as `rookery`, the same
  user that owns `$WORKSPACE` (trade-offs #7). An agent on repo A can edit the notes of repo
  B, and B's next run reads them as instructions. This adds to #7; it does not create a new
  class of problem, since the same agent can already read `app.pem`. The sandbox, or a second
  user for `claude`, fixes both. New trade-offs entry.
- **The notes steer runs without a human review.** They are written from runs a person
  started, they are tenant-scoped, and the portal shows them. New trade-offs entry.

---

## 4. Part C: codebase-memory MCP for all repos

### 4.1 Mechanism

`claude()` changes `--strict-mcp-config` to
`--strict-mcp-config --mcp-config <plugin dir>/mcp.json`. `pin()` writes that file into the
job's plugin directory, which already exists per job and is removed by `finish()`. The file
is generated from `MCP_SERVERS` in `agent.py`, so there is no operator file on disk that a
tenant could edit to change it for the next job. Each job gets its own repo's store:

```json
{"mcpServers": {"codebase-memory": {
  "command": "codebase-memory-mcp",
  "args": ["--tool-profile=scout"],
  "env": {"<store dir variable from S10>": "$WORKSPACE/mcp/<owner>_<repo>"}}}}
```

- **Deny the tools that change state**, with the same `--disallowed-tools` list:
  `mcp__codebase-memory__delete_project`, `…__index_repository`, `…__ingest_traces`,
  `…__manage_adr`. The orchestrator does the indexing (4.2). The agent only reads.
- **The UI and the watcher are off on the box** (`config set ui_enabled false`,
  `watcher_enabled false`). The UI is an HTTP server on port 9749.
- **The agent rarely uses a tool it is not told about.** Skills fired 0/3 until the prompt
  named them (S5). The TASK, PLAN and PR_REVIEW prompts get one line that names the tools.
- **Invariant change:** "No MCP in any `claude` call" becomes "only the MCP config that
  `pin()` writes from `MCP_SERVERS`". `CLAUDE.md` changes with this PR. The target repo's
  `.mcp.json` and the claude.ai connectors stay out.

### 4.2 Indexing

`pin()` runs `codebase-memory-mcp cli index_repository` on the job's worktree before the
first `claude` call, with the repo's store. The index matches the code the agent is about
to edit. S10 measures how long that takes on this CPU, and whether a second worktree of the
same repo indexes incrementally or from scratch.

### 4.3 Spike S10 (on the production box, as `rookery`, in `/tmp/spike-s10`)

| # | Question | Blocks |
|---|---|---|
| S10a | Does the binary run on this CPU (qemu64, no AVX2)? The `claude` CLI does not, above 2.1.110. | all of C |
| S10b | With `--strict-mcp-config --mcp-config x.json`: only `x.json`'s server starts, a repo `.mcp.json` does not spawn, and `init.mcp_servers` has no claude.ai connectors? | C |
| S10c | Which variable or flag moves the store? With two stores, does `list_projects` show only its own repo? | C (tenant isolation) |
| S10d | Do the tools return paths in the worktree, so the agent never edits a file somewhere else? | C |
| S10e | Index time and size for Mantis and Mantis-Frontend; incremental across worktrees or not | 4.2 |
| S10f | Do `--disallowed-tools mcp__…` deny rules hold under `bypassPermissions`? | C |
| S10g | Can 2.1.110 run with no tools at all (`--tools ""` or similar)? | B (3.3) |

S10g is small and only needs the CLI, so it runs during B. The rest runs before C starts.

### 4.4 What it does not protect against

The same as 3.8: the stores are files owned by `rookery`, so any agent can read or change
any repo's index on disk. The MCP tools themselves see only one store per job (if S10c
holds). New trade-offs entry.

---

## 5. Build order

| Step | PR | Done when |
|---|---|---|
| A | PR review fixes (§2) | the next real review takes fewer turns than §1's 22–33; a repeated trigger at the same head costs nothing |
| B | Repo memory (§3), with S10g | a second issue on a repo starts with notes in its appended prompt; the `memory` job shows in the run log with its cost |
| S10 | spike (§4.3), written up in this file | every row answered |
| C | codebase-memory (§4), only if S10 passes | the tools are in `init.mcp_servers`, only for this repo's store; the run log shows the agent called them |

Each PR updates `CLAUDE.md` (architecture and any invariant it changes), `docs/`, and
`ai-plans/trade-offs.md`.

---

## 6. Not in v1, and when to add it

- **Editing the notes in the portal.** Add when someone wants to correct a note rather
  than clear it.
- **Owner-wide notes** (one file for all of `<owner>`'s repos, like `<owner>/.rookery`).
  Add when two repos of one owner keep relearning the same thing.
- **A batch or cron writer** (one call per repo per day instead of per job). Add when the
  memory calls cost a visible share of the cap.
- **Vector memory** (PRODUCT-BRIEF §7). Add when 8 KB of notes stops being enough.
- **Other MCP servers.** `MCP_SERVERS` holds more than one. Each new one is checked
  against the same rules: no secret in its config (#7), one store per repo if it keeps
  state, and its write tools denied.

---

## 7. Decisions

Decided 3 October 2026:

- **When the memory job runs:** after every job, on the repo's queue.
- **Which jobs teach it:** `ok` and `failed`. Failures carry the traps.
- **RTK on `git diff`:** stays. A1's inline diff gives the reviewer the complete change.
- **`MEMORY_CAP`:** 8,000 characters.
- **Reviewer model (A4):** stays opus. Revisit after A1 has been measured on real reviews.
- **A2 for a label or review-request trigger:** skipped at the same head, like a mention.
- **A5:** `Bash(rtk proxy:*)` is added to the deny rules.

Nothing is open. Build in the order of §5: A, then B (with S10g), then S10, then C.
