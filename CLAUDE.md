# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
python test_agent.py                       # the whole test suite (asserts, no framework, no network)
python agent.py                            # webhook + portal on 127.0.0.1:$PORT (default 8787)
npx smee-client --url https://smee.io/<ch> --target http://127.0.0.1:8787/
python agent.py owner/repo 12              # dry run: plan issue 12 as if just labelled
python agent.py owner/repo 12 approve      # dry run: run the task loop, push, open the draft PR
python agent.py owner/repo 12 use sqlite   # dry run: anything else is fed back as a developer reply
python agent.py owner/repo 12 @rookery-agent check the migration   # dry run: review PR 12
```

There is no build, no lint, no dependency install — stdlib only, plus the `gh`, `claude`
and `openssl` CLIs on PATH. The exception is the local CLI in `cli/` (Rust, `docs/cli.md`):
`cargo test --manifest-path cli/Cargo.toml` runs the same `fixtures/` as `test_agent.py`, so
run both after changing `prompts/` or `fixtures/`. A release is a tag `rookery-cli-v<version>`
(bump `cli/Cargo.toml` first): `.github/workflows/release.yml` builds the binaries and installers
with `cargo-dist` (`dist-workspace.toml`; after a config change run `dist generate`, never edit
`release.yml` by hand), then `publish-crates.yml` publishes `rookery-cli` to crates.io
(`CARGO_REGISTRY_TOKEN` secret). The dry-run forms go through the same `handle()` as the webhook; only the
transport differs, so use them instead of standing up smee to test a change.

`.env` is gitignored and required at import time (`agent.py` reads
`WEBHOOK_SECRET` from the environment at module level). `test_agent.py` sets it
_before_ `import agent` for that reason — keep that ordering if you add tests.

## Architecture

Two files. `agent.py` is the pipeline; `web.py` is everything HTTP on one
`ThreadingHTTPServer`: the webhook (`Handler.do_POST` on `/`, verifies the HMAC, drops a
repeated `X-GitHub-Delivery` via `fresh()`, never blocks on a job), GitHub sign-in, the JSON
API, the static `ui/` files and the product docs (`docs/*.md` at `/docs/<name>.md`, rendered
by `ui/docs.html`, written in STE-style English). `route()` is the whole non-webhook app as a function of
(method, url, headers), which is how `test_agent.py` drives it. `agent.py`'s `__main__`
re-imports itself as `agent` before `main()`, so `web.py`'s `import agent` is the same
module, not a second copy. The webhook hands jobs to `submit()`, which puts each on its repo's
own queue in `queues`; the repo's daemon `worker()` is started lazily on its first job. Repos
run in parallel, but within a repo it is still one job at a time — deliberate, since two jobs
on one issue would race on its worktree and labels. A worker whose last job was `offboard`
exits under `qlock`, so a job submitted at that moment gets a fresh worker, never a dead one.

**The local CLI** (`cli/`, the `rookery` binary) is a second pipeline: the same flow on a local
repo, with no GitHub, no App and no account. It compiles in `prompts/` and `agents/` + `skills/`
(`build.rs`, `ROOKERY_SRC`): from the repo root here, from `cli/embed/` in the packaged crate,
which the publish job copies in. So a flow change is made on both sides, and `fixtures/` holds them
together. Items are numbered per repo in `<git-common-dir>/rookery/` (state, `runs.jsonl`, notes);
worktrees under `~/.rookery/worktrees/`; it never pushes. What it ports and drops is in
`docs/cli.md`.

**This repo is the orchestrator, never the target.** `REPOS` maps `owner/name` → base branch
and is never written to disk: `sync()` rebuilds it at startup from the App's installations
(`default_branch` is the base) — only the listing: a repo already cloned is live with no call of
its own, a new one gets an `onboard` job (clone + labels) like an install event — and `installation` / `installation_repositories` events
keep it current — `enqueue()` adds or drops the repo at once and queues an `onboard` /
`offboard` job on that repo's queue, so a trigger that arrives while the clone is still
being made is queued behind it. Clones live at `$WORKSPACE/repos/<owner>_<repo>`; `Ctx` (repo, issue number,
clone path, base branch) is threaded through every function. Each issue gets its own
`git worktree` at `$WORKSPACE/<owner>_<repo>/issue-<n>` on `feature/issue-<n>`, so two
repos can share an issue number.

**Retention.** A closed PR from the bot on `feature/issue-<n>` removes that worktree
(`drop_worktree()`); a repo leaving the installation removes its clone and all its
worktrees (`offboard()`). Both refuse — log and skip — when `unpushed()` finds a branch
with commits on no remote-tracking ref. The push goes to a URL, which updates no such
ref, so `implement()` records `refs/remotes/origin/<branch>` itself right after it.
`offboard()` also deletes the repo's notes (below).

**Repo memory.** One notes file per repo at `memory_file()` =
`$WORKSPACE/memory/<owner>_<repo>.md`, never in the target repo. After every
plan/reply/implement/pr-review job that ends `ok` or `failed` (not cancelled), `handle()`
submits a `memory` job to the repo's queue → `remember()`: `digest()` reads that job's
`claude` steps back from their transcripts (`CLAUDE_HOME/projects/*/<sid>.jsonl`, found by the
`sid` each `claude` step logs) and keeps tool calls, failed calls' first lines and each last
reply — never file contents or successful output — up to `DIGEST_CAP`; one `MEMORY_SLOT` call
(haiku, `--max-turns 1`, `--tools ""`, empty temp cwd, no plugin, `REMEMBER` prompt) returns the
notes between `<notes>` tags; the orchestrator checks the `STATUS` and writes the file. Notes over
`MEMORY_CAP` get `MEMORY_REWRITES` `REWRITE` calls (same slot, `key="memory-rewrite"`), then are cut
at the last line break under the cap: length never fails the job. A failed check keeps the old
notes; a memory job never comments or sets a label.
`pin()` reads the file into `active[repo]["memory"]`, logs its size and sha in the `base` step,
and `claude()` appends it (`MEMORY_NOTE`) between `CLAUDE.md` and the slot instructions; if the
three would pass 64 KB, the notes are left out for that job, never failing it. `MEMORY=0`
turns off reading and writing. The portal's `GET`/`DELETE /api/repos/<o>/<r>/memory` shows
and clears them (clear needs `can_write()`).

**State lives on GitHub and in the target repo, not here.** The `rookery:*` labels _are_ the
state machine (`PHASES`), advanced only via `set_phase()`, which removes stale phase labels.
Per-issue files can live in `agent-plans/<n>/` inside the target repo — `plan.md`, `tasks.md`,
`decisions.md` — and are committed and pushed with the change so they land in the PR. A repo
with `local_plans` on (the default) keeps them at `wt_root/plans-<n>/` instead (`plans()`): never committed,
passed to `claude` by `--add-dir`, the plan call keeps nothing in the worktree, removed with the
worktree. Wherever an issue's plan already is wins, outside first; `frozen()` restores `tasks.md`
around the review, `after` steps and follow-ups, since git cannot revert it out there.

**Repo settings.** `REPO_SETTINGS` (`local_plans` on, `review_prs` off), one JSON row
`repo:<owner/repo>` in `settings`, read by `repo_settings()`, set by
`POST /api/repos/<o>/<r>/settings` (`can_write()`), dropped by `offboard()`. Not in
`workflow.json`: `enqueue()` reads `review_prs` on the webhook thread, with no fetch. Nothing
local persists between runs: Claude session ids are `uuid5(NS, "repo#n/key")`, so re-running
the same issue resumes the same session. The review and `after` steps pass `per_job=True`,
which adds the job id: they re-run whole on every approve, and a resumed one repeats its last
verdict instead of looking again.

Flow: label `rookery:trigger` → `plan()` (`rookery:planning`) → either `rookery:awaiting-input`
(a reply re-enters `plan(resume=True)`, same session) or `rookery:awaiting-approval` → an
`approve` comment from someone with write access → `implement()` → `rookery:reviewing` → `rookery:done` /
`rookery:changes-wanted` (a `changes-needed` verdict) / `rookery:error`.

**Follow-up.** Once the PR is open (`rookery:done` / `rookery:changes-wanted`, or `rookery:error`
so a failed follow-up can be asked again), a `continue` job whose comment is a `mention()` runs
`follow_up()` (`act` `follow-up`); anything else there does nothing. `handle()` routes in order — `APPROVE` → `implement()`, `awaiting-*` → `plan()`,
then the mention — reading the phase when the job runs, not when the comment came. It 👀s the
comment, needs an open PR on `feature/issue-<n>` (else `NO_PR`), fetches its head and moves the
worktree up to it with `--ff-only` (never `worktree()`, whose `-B` would reset the branch; a
missing worktree is re-added at the PR head; diverged raises, never forces). Base is merged
(`#<n> merge <base>`, by the orchestrator) only when the PR is `CONFLICTING` / `BEHIND` or the
note names base, so a question stays a question; conflicted files go into `FOLLOW_UP` with the
merge left in progress. One `claude` call, `slot="task"`, `key="follow-up"`, `per_job=True`,
replies `FOLLOW_VERDICTS`; the worktree holds it to that: `answered` is reverted
(`keep_plans_only(keep=())`) and invalid with conflicts open, `changed` is refused while a
conflicted file holds a `CONFLICT` marker, then committed (the merge commit, or `#<n>
follow-up: <note>`), with `tasks.md` restored so a follow-up adds no tasks. Any raise aborts
the merge and reverts the worktree. HEAD past `origin/feature/issue-<n>` → `publish()`
(implement()'s tail: `after` steps, review, push, the sections as a comment on the open PR);
else the answer goes on the issue, phase unchanged.

**Named roles are plugin agents, `--agent rk:<name>`.** `planmaker` runs `plan()`,
`reviewer` runs the review pass, `scope-checker` is a built-in for an `after` check (no slot
uses it by default); implementation tasks deliberately run as _no_ agent — the
default harness plus the deny rules is the point. They live in `agents/*.md` (root, in the
native Claude Code agent format) and `pin()` copies them into the job's plugin, where
`parse_agent()` holds their frontmatter to `AGENT_KEYS`. Not `.claude/agents/*.md`:
`claude` runs with cwd set to the _target_ repo's worktree, so definitions in this repo
would never be discovered and writing them into the target would pollute it. Root, not
`.claude/`, so Claude Code does not load them as subagents for anyone working _on_ Rookery.
Both roles declare `model: opus` in their frontmatter; agentless task invocations pass
`--model sonnet` in `claude()` — a bad plan costs fifteen tasks of wrong code, grinding
through those tasks does not need the same model.

**Workflow slots.** `claude(slot=)` is `plan`, `task` or `review`; each slot has an `agent`,
`skills` and `instructions`, from the target repo's `.rookery/workflow.json` on base with
`WORKFLOW` filling what it leaves out (the default enables no skills). Enabled skills are
named in the prompt (`USE_SKILL`) — a skill that is merely installed almost never fires.
The repo's and owner's other skills (not the catalog's) are listed as an offer
(`OFFER_SKILLS`), so a repo with skills but no `workflow.json` still gets them used.
`instructions` go into `--append-system-prompt` after `CLAUDE.md`, under the same 64 KB cap.
The catalog in `skills/` (see `skills/README.md`) is copied into the plugin only when a slot
names it; `.rookery/skills` beats `.claude/skills` beats the catalog, and the `base` step
logs the winner in `sources`. `.rookery/agents/*.md` (top level only) join the built-in
`agents/` in the plugin, and one with a built-in's name replaces that role; the `base` step
logs the repo's agents in `agents`.

**Owner defaults.** `<owner>/.rookery` (`ORG`), when it is in `REPOS` (the App is installed
on it, and `REPOS_ONLY` lets it in), is a layer between the repo and the built-in:
`org_of()` fetches its base, and `steer()` reads `workflow.json`, `agents/` and `skills/` from
its root at that sha. Its `workflow.json` is `workflow()`'s `under`, so a repo's file falls
back to it key by key, and a repo's own `after` replaces the owner's list whole. Agents:
built-in < owner < `.rookery/agents`; skills: `.rookery/skills` > `.claude/skills` > owner >
catalog. Listed but not yet cloned fails the job ("still cloning"), never runs without it.
The `base` step logs `org` as `<owner>/.rookery@<sha>`. No owner-level `CLAUDE.md`.

**Config edits from the portal.** `POST /api/repos/<owner>/<repo>/config` (`can_write()`)
→ `propose()`: `CONFIG_FILE` paths (`workflow.json`, `agents/<name>.md`) under
`config_dir()` — `.rookery/`, or the root of `<owner>/.rookery` — are committed off a freshly
fetched base in a throwaway `GIT_INDEX_FILE` (the clone's checkout and worktrees untouched),
`steer(sha=commit)` validates that commit exactly as a job would, and only then is it pushed
to `rookery/config-<hex>` by URL and a PR opened. `config_files()` is the editor's starting
point, returned by the workflow GET even when the config does not validate. The GET also returns `under` (the owner's or built-in layer, minus `with`) so the portal's drag-and-drop builder can write only the keys that differ from it; the builder edits the same `workflow.json` draft as the editor, never anything else. It also returns `with` and `repos` (the builder's Repos shelf: same-owner repos in `REPOS` ∩ `visible(s)`, minus home, `with` and `.rookery`), so a repo the user cannot see never shows.

**Cross-repo (`with`).** `.rookery/workflow.json`'s `with` (repo's own file only, never the
owner layer; ≤ `MAX_WITH = 3`, same owner, in `REPOS`, cloned — `steer()` checks) lists
companions. The issue's repo is the home: its labels, comments, caps, provider, session and
memory run everything. `pin()` reads each companion's base (`a["with"]`: repo, base, sha,
`CLAUDE.md`); `mates(c)` turns it into companion `Ctx`s with the home's `n`. Each gets a
worktree `wt_root(companion)/<home-name>-issue-<n>` on `feature/<home-name>-issue-<n>`
(`tag()`, never `issue-<n>`: that is the companion's own issue), made by `plan()` and
`implement()` (`worktree(m, c)`), brought to its PR head by `at_head(m, c)` in `follow_up()`.
`claude()` adds each as `--add-dir`, its `CLAUDE.md` after the home's, and `WITH_REPOS`
(+ `WITH_PLAN`) to the prompt — never for `pr-review`. Companion skills/agents only add:
a name the home's assembly has stays the home's (`skipped`, logged), first in `with` wins.
`commit_all()` commits each changed companion as `<home>#<n> …` (never `#<n>`), the home as
before; `revert_all()` reverts every worktree after a check, the review and an answer
(paths `<repo>:<path>`); `plan()` reverts companions whole. `publish()` pushes companions
with commits first, opens their draft PRs (`Part of <home>#<n>`, never `Closes`), lists them
under `## Companion PRs` in the home's, then edits new ones to name the home PR. Base merge
in a follow-up is home only. `drop_worktree()` finds companions on disk in every same-owner
repo, each behind `unpushed()`. An issue or reply naming `<owner>/<name>` that `with` lacks
(`named()`, a regex on `unquoted()`): `notice()` posts `NOT_LISTED` once per repo (hidden
`<!-- rookery:with … -->` marker) before the plan call, and the prompt gets `NOT_MOUNTED`.
`step()`'s params are positional-only: companion steps carry `repo=`.

**`after` steps.** `workflow.json`'s `after` is up to `MAX_STEPS = 5` steps, each an `agent`,
a `mode` (`check` or `write`, `MODES`), `skills` and `instructions`; `claude(slot=)` takes the
step dict itself. `after_steps()` runs them in order between the task loop and the review
(`CHECK` / `WRITE` prompts, `key=f"step-{agent}"`). A check's edits are reverted
(`keep_plans_only(keep=())`) and flag the run; a write step is committed as `#<n> step
<agent>` (`--allow-empty`) and skipped on re-approve once that subject is on the branch. A
reply outside the mode's `MODES` pair, or a write step's `failed`, raises. Each step's reply
goes into the PR body as `## 🤖 <Agent> agent · <mode>` (the review as `## 🤖 Reviewer agent`); any check's `changes-needed` or reverted edit sets
`rookery:changes-wanted` like the reviewer's. The reviewer's own leftovers are reverted too, so no
later write step or task commits them. A branch touching `STEERING` gets `CONFIG_WARNING` at
the top of the PR body. Approving again after a run that opened the PR (a check flagged it)
posts that run's sections as a comment on the open PR, since `gh pr create` refuses a second.

`implement()` is the heart: loop the first unticked task in `tasks.md` → **a fresh Claude
session per task** (`key=f"task-{i}"`) → require `STATUS: task-done` → tick the checkbox →
`git commit` (`--allow-empty`, a verify task may change nothing) → next. `task-unverified`
(done, but a check it asks for cannot run on the box — e.g. a Windows-only test host) ticks
and commits the same, and its reply leads the PR body as `## ⚠️ Not verified here`; it is not
an error. PLAN tells the planner to ask only for checks the box can run. Fresh sessions keep context and cost flat regardless of issue size;
`decisions.md` is the only memory that crosses between tasks. A failure raises with everything
before it already committed, so commenting `approve` again resumes at that same task.

**PR review on request.** A second, smaller flow: a `pr-review` job, one advisory review
of any open same-repo PR. Triggers (`enqueue()`, each gated by `may()` = not `ME` and
`can_write()`): a PR comment matching `MENTION` (`@AGENT_USER`, default `rookery-agent`, a
plain machine account with no token on the box; `mention()` ignores quoted lines and code,
the rest of the comment is the note), the `REVIEW_LABEL` label, or a review request for
`AGENT_USER`; with `review_prs` on, also `opened` (`auto`, no `may()`: a writer turned it on),
never on `ME`'s PRs or a fork's. Any other PR comment is ignored, never a `continue` job. `TRIGGER` on a PR or `REVIEW_LABEL`
on an issue is a `wrong-label` job: a `WRONG_LABEL` comment and the label removed (`unlabel()`, REST: Ubuntu's gh 2.45 `gh pr edit` fails on classic Projects),
no run log, no `claude` call, no `may()`. `review_pr()` 👀s the
comment, refuses forks and closed PRs, fetches `refs/pull/<n>/head` into a
detached `pr-<n>` worktree (`pr_worktree()`, always removed after), runs the `pr-review` slot
(`rk:pr-reviewer` by default, `claude(wt=)`), and posts `gh pr review --comment`. Its verdict is
`PR_VERDICTS` (`safe-to-merge` / `minor-changes` / `changes-needed`, the `STATUS` marker) and its
findings are JSON in `<review>` tags; `render_review()` writes the posted markdown (header, the
finding count before the summary, table, collapsed details) and cuts to `REVIEW_CAPS`, and a reply
off that shape, or `safe-to-merge` with a `high` finding, fails the job. Given the worktree, it
marks ❔ a finding whose `where` names a file or line not in it (`located()`) — flagged, never
dropped, so it renders before the PR worktree is removed. One bug in several places is one
finding. The implement-flow `reviewer` uses the same JSON and renderer
(`REVIEW_VERDICTS`: `review-ok` / `changes-needed`, a `###` header under `## 🤖 Reviewer agent`);
the `after` checks too (`CHECK_VERDICTS`: `check-ok` / `changes-needed`, under the step's
header). A bad reply fails the job before the push, like a missing marker.
A note made only of `ASKING` words ("review again", "PTAL") is no note.
`PR_REVIEW` carries the worktree's absolute path and `pr_diff()`: the stat, then the diff
without lockfiles, cut at a file boundary before `DIFF_CAP` with the rest named for `Read`.
The orchestrator runs it, so the operator's RTK hook (which drops hunk lines) never touches
it. The `posted` step records the PR's head; a trigger without a note at a head that an `ok`
job already posted at (`reviewed_at()`) makes no `claude` call, only an "Already reviewed"
comment. `pr_cleanup()` removes the label / review request either way. Assigning `AGENT_USER` to an
issue is the same as `rookery:trigger`.

**Run log.** `handle()` opens a `jobs` row per plan/reply/implement/pr-review/memory (`begin()`), and the
pipeline appends `steps` (phase, task i of N, each claude call with key/status/cost/sid, tick,
commit, push, PR, error) via `step()`, which is a no-op outside a job. `finish()` sets
ok/failed/cancelled; a restart marks leftovers `interrupted`, and an interrupted implement or
follow-up gets `INTERRUPTED` and `rookery:error`, since its stale phase label would take no `approve`. `active[repo]` is the job a
worker is running — one per repo.

**Cancel.** `sh(track=repo)` registers the process in `active[repo]` under `alock`;
`cancel()` sets the flag and kills the tree (`killpg` / `taskkill /T`) under the same lock,
so a cancel can't land between the check and the start. `check_caps()` refuses further
calls, and the job fails through the normal path with a "Cancelled … by @login" comment.

**Portal auth.** GitHub OAuth through the App (`CLIENT_ID`/`CLIENT_SECRET`, callback
`PUBLIC_URL/auth/callback`, a `state` cookie). The user's token lists their installations'
repos once at login and is dropped; the session row (keyed by sha256 of the cookie, 8h)
keeps that list. Visible = that list ∩ `REPOS`. Signing in again replaces the session with a
fresh list; the App's Setup URL is `/auth/login`, so an install does that by itself. POSTs
need the session's `X-CSRF-Token`.
Cancel needs `can_write()`, the same check as `approve`. `REPOS_ONLY` limits an instance.
The Workflow view (`GET /api/repos/<owner>/<repo>/workflow`) runs `steer()` — `pin()`'s
fetch, checks and plugin assembly minus the job — into a temp dir, so the page and the next
run cannot disagree; an unseen repo is a 404. `fetch()` holds a per-repo lock, since that
GET fetches from an HTTP thread while the repo's worker may be fetching. `GET /api/skills`
reads `skills/` plus the slot/source table in `skills/README.md`.
Stats (`GET /api/repos/<o>/<r>/stats?days=30|90|365`, `visible()` only, read-only) is the
repo's first tab: `agent.stats()` fetches, then `history()` (`git log --no-merges --numstat`
on the clone, author names never emails), `github()` (repo facts, languages, and the bot share
from two `gh pr list --state merged` calls — PRs, since a squash changes Rookery's shas; a
failing part is `null`, never an error) cached 5 min per `(repo, days)` in `_stats` (a hit skips the fetch too),
and `runlog()` (the `jobs`/`steps` rows in the window) read fresh. Nothing is stored. The
portal lands on the last-picked repo (`localStorage`), else the first; no repos at all shows
the install card instead of the runs list.
`GET /api/pulse` is the one `/api/` route with no session: `agent.pulse()`, instance-wide totals
for the landing page's band (repos, agents, skills, providers, issues, PRs, reviews, tasks, and
merged PRs from one GitHub search per owner, cached `PULSE_TTL`): counts only, never a repo,
owner, title, login or cost. No numbers, no band: the page never shows zeros it made up.
`callback()` upserts the `users` row (first, last, sign-ins); `serve()` seeds it once from
`sessions`. `/admin.html` (unlinked) is for `ADMINS` (`.env`, GitHub logins): `GET /api/admin`
lists providers, overrides, owners and users, `POST /api/admin/provider` sets the default or
one owner's override. For anyone else, signed in or not, every `/api/admin*` is a 404.

**Providers.** `provider_for(repo)` → `(name, key id, key)` picks the backend behind every
`claude` call from `PROVIDERS` (`providers` in `prompts/defaults.json`, shared with the CLI's `rookery config`): the owner's own selected key (`key:<owner>` in `settings` → a
`keys` row), else `fallback()`: the owner's `provider:<owner>` row, else — only for an owner in
`OWN` — the stored `provider` default, else `PROVIDER` (`.env`, default `claude`, the CLI's own
`/login`); else it raises `NoKey`. Per owner, never per actor. `claude()` reads it **once** and
passes it to `provider_env(name, key)`, `cost_of(out, name)` and its step, so a switch mid-call
cannot price the call with the wrong table. The
admin page stores only a `ready()` provider (key in `KEYS`); a stored one whose key left
`.env` is logged at startup and fails that owner's calls, never falls back; the harness stays the `claude` CLI, so every
invariant holds. Another backend is an Anthropic-compatible endpoint: `provider_env()` gives
`ANTHROPIC_BASE_URL`, its key as `ANTHROPIC_API_KEY` and the `opus`/`sonnet`/`haiku` aliases'
models, passed as `env=` (none at all for `claude`, so its env is unchanged), through `sudo`
by the `env_keep` line (DEPLOY.md §5b). An unknown `PROVIDER` or a missing key fails startup.
The CLI prices every model as Claude, so `cost_of()` prices `modelUsage` from the provider's
`price` table, and a model not in it fails the call. The `base` and `claude` steps log
`provider`, and `key=<id>` on an owner's own key (never the key). `anthropic` is Claude by API key (`CLAUDE_API_KEY`, never `ANTHROPIC_API_KEY`,
which is popped so it cannot beat the `/login`): no `models`, no `price`, so `total_cost_usd`.
The subscription runs only the owners in `OWN` (`.env`, unset = `ADMINS`): `fallback()` never
gives `claude` to any other owner, `/api/admin/provider` refuses that override (422), and startup
logs an old one.

**Owners' own keys.** A personal account adds keys for `anthropic`, `deepseek` or `glm` on the
portal's Keys page (`#keys`; `/api/keys*` in `web.py`, `can_manage()` = the signed-in login *is*
the owner, of a repo in `REPOS`; orgs run on the operator's choice). The `keys` table holds each
key `seal()`ed — `openssl enc -aes-256-cbc -pbkdf2 -a -A` with `KEY_SECRET` in `env=`, the key on
stdin, then HMAC-SHA256 over the ciphertext (encrypt-then-MAC) — plus `last4`; `unseal()` checks
the MAC before it decrypts. The first key added is selected. No `KEY_SECRET`: adding is a 503 and
a selected key fails its owner's jobs. `pin()`'s first line is `provider_for()`, so a `NoKey`
fails start, reply, implement and pr-review jobs before any fetch, label or `claude` call;
`handle()` posts `NO_KEY` (issue, with `rookery:error`) or `NO_KEY_PR` (`gh pr comment`), the
portal link not in a code block. Caps apply on an owner's key too. Known limits: an owner's key is in the agent's process env
while it runs, and own keys are for personal accounts only (orgs run on the operator's choice).

**GitHub auth is a GitHub App, not a login.** `jwt()` signs an RS256 JWT with `openssl`
(`APP_ID`, `APP_KEY` from `.env`); `token(repo)` exchanges it for an installation token,
cached and re-minted with under 10 minutes left, since an implementation outlives the 1h
token. `auth(repo)` turns it into an `env=` for one `gh`/`git` call — `GH_TOKEN` plus a
`GIT_CONFIG_*` https auth header. There is no `gh auth login` on the box.

## Invariants — do not soften these

- **The orchestrator ticks checkboxes and commits; the agent never does.** `tick()` rewrites one
  byte by offset so task text is never disturbed. The TASK prompt tells the agent not to commit;
  `--disallowed-tools Bash(git push:*) Bash(gh:*)` enforces it (deny rules beat
  `bypassPermissions`). The agent never publishes. Deny rules match the command *after*
  `PreToolUse` hooks rewrite it, so the server's RTK hook needs the `rtk git push` / `rtk gh`
  forms denied too, and `rtk proxy` (it runs a command raw, unrewritten) — keep all five,
  and add the rewritten form of any new rule.
- **No MCP in any `claude` call: `--strict-mcp-config`, no `--mcp-config`.** Without it
  the target repo's `.mcp.json` spawns its servers on the box, and the claude.ai
  connectors of the account the CLI is logged in with reach every tenant's agent (verified by a spike).
- **What steers the agent is read from base, once per job, never the worktree.** Every
  plan/reply/implement job runs `pin()` first: `git fetch origin <base>`, then root
  `CLAUDE.md` (→ `--append-system-prompt`, over 64 KB fails the job), `.rookery/workflow.json`,
  `.rookery/skills`, `.claude/skills` and `.rookery/agents` (→ a per-job `--plugin-dir` with only `skills/` and
  `agents/`, all `rk:<name>`, removed by `finish()`, cleared at startup) at that sha, logged as
  the `base` step. `--setting-sources user` keeps the CLI from loading them, or the repo's
  hooks, from the worktree the agent edits. A skill's frontmatter may only carry `name`,
  `description`, `allowed-tools`, `argument-hint`, `disable-model-invocation`; an agent's `name`, `description`, `tools`, `model`,
  `skills`: `hooks:` would run shell on the box. An invalid workflow, a name off `NAME`, or a
  slot naming a skill or agent that is not in the plugin fails the job at start — never a
  silent fallback, since `--agent` ignores an unknown name without an error.
- **Never a `PHASES` label on a PR, and never `gh issue …` for one.** `handle()`'s
  `pr-review` failure path is `gh pr comment`, not `comment()`/`set_phase()`: `gh issue`
  cannot resolve a PR number, and a phase label on a PR would let a later `approve` comment
  run `implement()` on it. A PR review reads its steering from base like every job, never
  from the PR head.
- **Approval is `APPROVE`, a regex — never an LLM judgement**, matched on `unquoted()` (the
  comment minus quoted lines and fenced code, as `mention()` reads it), and only from a commenter with
  write, maintain or admin on _that_ repo (`can_write()`, the collaborator-permission API with
  the repo's installation token; a failed lookup denies). Replies need the same. `ME` (the App's
  bot login) is filtered out so the bot can't approve itself. With `with`, approve and a
  follow-up also need `can_write()` on every companion (`refused()`, after `pin()`: a comment,
  phase unchanged); replies need the home only. Never a `PHASES` label or a comment on a
  companion's issue or PR beyond its own PR body/comments.
- **Caps are checked before every `claude` call** (`check_caps()`): `RUN_CAP` calls and
  `USD_CAP` dollars per repo per UTC day (defaults 30 and $20), plus the operator's `PAUSED`
  file next to `agent.py`. Every call — failed ones too — is recorded with its
  `total_cost_usd` in `$WORKSPACE/rookery.db`, one sqlite3 connection per call, so a restart
  keeps the day's count. A cap raises like any other error (comment + `rookery:error`); mid
  implementation that means the current task is committed and the next one never starts.
  Caps are per tenant; `MAX_TASKS` and `MAX_STEPS` are per issue — keep them independent.
- **The GitHub token never enters `os.environ`**, only a per-call `env=`: the `claude`
  subprocess inherits `os.environ`, and the deny rules stop `gh`, not `curl` with a token.
  `GH_TOKEN`/`GITHUB_TOKEN` are popped at import for the same reason, and `CLIENT_SECRET`
  in `web.py`. Every provider's key (`CLAUDE_API_KEY`, `DEEPSEEK_API_KEY`, `ZAI_API_KEY`) is popped too, used or
  not, and kept only in `KEYS`; the selected one reaches `claude` only through `provider_env()`'s `env=`. An owner's own key
  reaches it the same way and no other; `KEY_SECRET` is popped too. A user's OAuth token lives only inside `callback()`: never stored, never in
  the env.
- **`rookery.db` is readable by the agent** (same user, same workspace), so it holds nothing
  replayable: session rows are keyed by the cookie's sha256, and no token is stored. An
  owner's API key is stored only sealed, and is write-only: no response, log line or step ever
  carries it, only its provider, label and last 4 characters.
- **With `RUN_AS` set (the server), every `claude` call runs as that user via `sudo`**
  (DEPLOY.md §5b): it can't read `app.pem`, `.env`, `rookery.db` or the notes, and `sudo`
  drops our env. Worktrees are shared through its group (`shared()`, umask 002); **the
  clone's `.git` never is** — its hooks and config run when the orchestrator commits, so a
  writable `.git` hands the agent our user back. `sealed()` keeps each worktree's `.git`
  pointer read-only for the same reason. A new path that runs `claude` goes through
  `claude()`, and one that reads what it wrote reads as `RUN_AS` (`digest()`).
- **The orchestrator writes repo notes; the model never does.** `remember()` writes
  `memory_file()` from a checked reply (`STATUS` marker, `<notes>` tags, `MEMORY_CAP` by a
  rewrite, then a cut), and the memory call has no tools and an empty cwd. A memory job never
  comments or labels: its failure must not look like a failed run. The notes are advisory to the agent
  (`MEMORY_NOTE`: code and `CLAUDE.md` win), and any agent on the box can edit any
  repo's file (an accepted limit).
- **The portal never approves.** No approve endpoint, no approve button; `APPROVE` stays a
  regex on issue comments. Visibility comes from the user's own installations; anything
  that steers a repo (cancel, a config PR) needs `can_write()`. Someone else's run or repo
  is a 404, not a 403. An `/api/admin*` route is a 404 for anyone not in `ADMINS`.
- **The portal writes config only by opening a PR**, never to base or to any branch
  `pin()` reads. `propose()` refuses any path off `CONFIG_FILE`, and a change that fails
  `steer()` never leaves the box. Merging stays a human's act on GitHub.
- **User-written text is never `innerHTML`** in `ui/portal.html` — issue titles and errors
  go in through `textContent` (`h()`).
- **The push goes to `https://github.com/<repo>.git`**, not `origin`, so it carries the
  App's token even when the clone's origin is ssh.
- **Planning writes `plans()` and nothing else** (`agent-plans/<n>/`, or nothing in the worktree when the plan is outside), enforced by `keep_plans_only()`
  after every `plan()` call, not by the PLAN prompt's wording. It reverts rather than raises,
  so the awaiting-input reply loop can call `plan()` repeatedly without accumulating stray
  edits. It parses `git status --porcelain -uall` — `-uall` matters, since plain `--porcelain`
  collapses a first-ever `agent-plans/` to one untracked-dir entry that the prefix test would
  then delete.
- **The review is advisory and runs once**, after the task loop and before the push. The
  branch is pushed and the draft PR opened either way, so work is never stranded in a local
  worktree; `changes-needed` only sets `rookery:changes-wanted` — never `rookery:error`, which
  means the run failed. `approve` on it re-runs the checks and the review. Escalating it to a blocking gate or a
  fix loop is specced in the plan doc — a fix loop needs its own bound, because a reviewer
  that appends tasks breaks "every tick strictly reduces the open count". `after` steps keep
  the same shape: checks are advisory, and neither mode appends tasks.
- **`STATUS:` is parsed, never inferred.** `MARKER` requires the marker alone on its line
  (markdown bold around it is tolerated, nothing else); a missing marker is an error, not a guess. A
  reply without one is asked once more for the line alone (`NO_MARKER`, same session, under the caps,
  logged as `<key>/status`), and that reply is parsed the same way; the first reply stays the reply.
  The marker is stripped from the text before it is
  posted as a comment.
- **`MAX_TASKS = 15`**, checked every iteration; the loop is bounded by `range(MAX_TASKS + 1)`
  with a `for/else` that raises on non-convergence. Every tick strictly reduces the open count.
- **Never push to base**: `assert branch != c.base` before `git push`; branch protection on the
  target repo is the backstop, not the primary guard.
- Webhook signature is checked with `hmac.compare_digest` before anything else; `enqueue()`
  ignores repos absent from `REPOS`. Deliveries are deduped on `X-GitHub-Delivery` (last 1000,
  in memory — a restart forgetting them is accepted); the phase-label checks in `enqueue()`
  remain the guard against a redelivery after that.
- **Retention never deletes unpushed commits.** `drop_worktree()`/`offboard()` check
  `unpushed()` first and keep everything if it finds any. `worker()` catches everything
  `handle()` lets out, so one bad job cannot kill the loop.
- Every exception in `handle()` becomes an issue comment plus `rookery:error` — the loop never
  dies silently.

## Style

Stdlib only; adding a dependency changes what this project is. Prompts are `.format()`
templates (`PLAN`, `REPLY`, `TASK`) — they are the interface to Claude, so treat their wording
as code. Every text a `claude` call is built from lives in `prompts/<name>.md` (LF, byte for
byte: no trailing newline is added), and the defaults and verdict tables replies are held to in
`prompts/defaults.json`; `agent.py` reads them at import into the same module-level names, and
the local CLI (`cli/`) compiles them in. GitHub-side texts (comments, PR bodies) stay in
`agent.py`. `fixtures/*.json` are golden cases (`MARKER`, tasks, `unquoted()`, `workflow()`,
frontmatter, `render_review()`) that `test_agent.py` and the CLI's tests both run: change a
behaviour there, and both sides must follow. `sh()` raises on non-zero; pass `check=False` for the cases (like
`gh label create` on an existing label) where failure is expected.

Phases 1–5 (GitHub App, onboarding
without a restart, approval and caps per repo, parallel repos, portal/API/run log) are
built and deployed (https://rookery.run, see [DEPLOY.md](DEPLOY.md)); phase 6 (a second
harness, e.g. the Codex CLI) is not built.

For designing UI pages you must use @DESIGN.md file and follow the rules/patterns there.
