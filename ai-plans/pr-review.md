# PR review on request (issue #10)

Today Rookery runs one flow: issue → plan → approve → tasks → review → draft PR.
This adds a second, much smaller flow. Someone with write access asks Rookery to
review a pull request, and Rookery posts one advisory review on it. Any PR qualifies,
not only Rookery's own. The review runs as a workflow slot, so a repo, or its owner's
`.rookery`, can put its own agent in place of the built-in one. The machinery for that
(`steer()`, `.rookery/agents`, owner defaults, the portal editor) already exists.

**Status:** built 3 October 2026, with the recommended answers to §10: one reviewer, forks
refused, a comment-type PR review, §6 in v1, no mention on issues.

---

## 1. How people ask

The GitHub user [`rookery-agent`](https://github.com/rookery-agent) is the name people use
for Rookery. It is a plain machine account. Rookery never logs in as it and holds no token
for it. All work still goes through the App and its installation token. The login is
`AGENT_USER` in `.env`, default `rookery-agent`.

| Trigger | Event | Needs `rookery-agent` as collaborator? |
|---|---|---|
| **`@rookery-agent` in a PR comment** (main trigger) | `issue_comment.created` on a PR | No. GitHub's autocomplete does not offer it, but the full handle works, and the webhook carries the comment text whatever its notification does. |
| Label `rookery:pr-review` on a PR (fallback, needs no typing) | `pull_request.labeled` | No |
| Request a review from `rookery-agent` (optional, §6) | `pull_request.review_requested` | Yes |

Each one queues the same `pr-review` job. Each one requires `sender != ME` and
`can_write(sender)`, the same gate as `approve`. A non-writer cannot spend the repo's daily
cap. The text after the mention goes to the reviewer as the requester's note, for example
`@rookery-agent look hard at the migration`.

`MENTION = re.compile(r"(?<![\w-])@rookery-agent(?![\w-])", re.I)`, built from `AGENT_USER`,
so `@rookery-agent-x` is not a match. Quoted lines (`> …`) and fenced code are stripped before
matching. Without that, a reply that quotes an earlier request would trigger a second review.

## 2. The flow

```
@rookery-agent in a PR comment | label rookery:pr-review | review request (§6)
  → enqueue(): sender is not ME, can_write(sender)
  → submit({"kind": "pr-review", repo, n, note})        # same per-repo queue as everything else
  → handle(): begin(c, "pr-review") → pin(c) → review_pr(c, note)
       👀 reaction on the triggering comment            # "queued and running" for a mention
       gh pr view n: refuse if cross-repo (§7) or not open
       fetch pull/<n>/head + the PR's base ref → detached worktree pr-<n>
       claude(PR_REVIEW, key="pr-review", slot="pr-review", per_job=True, wt=pr worktree)
       gh pr review n --comment --body-file -           # the findings, with the verdict on top
  → finally: remove the worktree; remove the label / the review request, if one triggered it
```

To review again (after new pushes, say), mention it again, add the label again, or request a
review again. A label or a review request that is still there means "queued or running",
and nothing else.

## 3. What changes in `agent.py`

- **Constants.** `AGENT_USER` (env, default `rookery-agent`), `MENTION` built from it, and
  `REVIEW_LABEL = "rookery:pr-review"`. `onboard()` creates the label with the others, so
  existing repos get it at the next restart (`sync()` → `onboard()`).
  `WORKFLOW["pr-review"] = {"agent": "pr-reviewer", "skills": [], "instructions": ""}`.
  Because the slot is in `WORKFLOW`, `workflow()` validates it, `slots()` and `steer()`
  check that its agent and skills exist, and `web.workflow()` returns it. None of those
  need a code change.
- **`agents/pr-reviewer.md`** is a new built-in persona (opus). `reviewer.md` reviews against
  an approved plan, and most PRs have no plan, so a separate persona is the honest choice.
  It is read-only and reports findings only, and a finding is worth writing only if it
  changes what the human does.
- **`PR_REVIEW`** is a new prompt template. It contains the PR number, title, body and the
  requester's note, and the diff command `git diff origin/<baseRef>...HEAD`. It says: read
  only, do not use gh. It asks for `STATUS: review-ok` / `changes-needed` plus markdown,
  worst first, that is posted verbatim. If the PR touches `agent-plans/`, the plan is in the
  diff and the agent can read it.
- **`claude(..., wt=None)`** gets one new kwarg: `wt = wt or worktree(c)`. Without it,
  `claude()` would create an `issue-<n>` worktree from base for the PR number.
- **`pr_worktree(c)`** fetches `+refs/pull/<n>/head:refs/remotes/origin/pr/<n>` and the PR's
  base ref through `fetch()` (it already takes any refspec, under the per-repo lock). Then it
  runs `git worktree add --detach wt_root/pr-<n> origin/pr/<n>`. If a leftover directory
  exists, it is removed first. The worktree is always detached and never committed to, so
  removing it can never lose work, and `unpushed()` is not involved.
- **`review_pr(c, note)`** runs the flow in §2. The review body starts with one line naming
  the verdict and `rk:<agent>`, plus the skills it ran with, the same way an `after` step's section does.
- **`handle()`** gets a `pr-review` branch. Its failure path is different from the issue flow,
  and this matters. It must **not** call `set_phase()` or `labels_of()`, because both
  go through `gh issue …`, which cannot resolve a PR number. It must also never put a `PHASES`
  label on a PR, because a later `approve` comment on that PR would then run `implement()` on
  it. The error goes out through `gh pr comment`; the label and the review request are
  removed in `finally`.
- **`enqueue()`**:
  - `issue_comment.created` on a PR (`"pull_request" in p["issue"]`) whose text matches
    `MENTION` → submit, with the comment id (for the 👀) and the note. Any other comment on a
    PR is ignored: it never becomes a `continue` job. Today that is true only because
    Rookery's PRs carry no phase label. This makes it true by construction.
  - `pull_request.labeled`, label `rookery:pr-review` → submit.
  - The writer check (`can_write()`, one API call) runs on the webhook thread, as it does
    today for comments. Nothing else blocks there.

`c.base` stays `REPOS[repo]`, the default branch. `pin()` reads the steering files from it,
as it does for every job, and never from the PR head. A PR that edits
`.rookery/agents/pr-reviewer.md` is therefore reviewed by the **merged** persona, not the
proposed one. The diff uses the PR's own base ref, which can be a branch other than the
default.

## 4. Bring your own reviewer

No new mechanism. The three existing ways all apply to the new slot:

| Want | Do |
|---|---|
| Change the built-in's persona | `.rookery/agents/pr-reviewer.md` (the same name replaces the built-in) |
| Use a differently named agent, with skills and instructions | `.rookery/workflow.json`: `{"pr-review": {"agent": "my-reviewer", "skills": ["security-review"], "instructions": "…"}}` and `.rookery/agents/my-reviewer.md` |
| The same for every repo of an owner | The same files at the root of `<owner>/.rookery` |

The frontmatter allowlist (`AGENT_KEYS`), the deny rules, `--strict-mcp-config` and
`--setting-sources user` all apply unchanged. A custom reviewer gets no more power than
the built-in one.

## 5. Portal and docs

- `ui/portal.html`: add `'pr-review'` to the slot-key list in `problems()` and the catalog's
  "Add to" slots. Add a second small flow under the issue flow: *Mention `@rookery-agent` on
  the PR* → `slotNode(SLOT['pr-review'], …)` → *Review on the PR*. The confirm text in
  `stop()` says "approve resumes", which is wrong for a PR run, so word it by `r.act`.
- `docs/workflow.md`: a "Review a pull request" section with the trigger table from §1.
  `docs/configuration.md`: the `pr-review` slot and the table in §4. Both in STE style.
- `CLAUDE.md`: one architecture paragraph, and the PHASES-never-on-a-PR rule under Invariants.
- `DEPLOY.md`: `AGENT_USER` in the `.env` list. The App must have the **Pull request** event
  ticked. Check this on the live App before deploying; `drop_worktree` already depends on it
  (`closed`), but DEPLOY.md step 0 lists only Issues + Issue comment. No new permission:
  Pull requests RW covers the review, the label and removing a review request, and Issues RW
  covers the reaction.
- `skills/README.md`: add `pr-review` to the slot column wherever a skill fits it
  (`security-review`).

## 6. Optional: assign and request review, for repos that add `rookery-agent`

GitHub only offers collaborators in the Assignees and Reviewers pickers. Copilot is built
into GitHub, and the third-party agents there come through its Agent HQ program, which an
ordinary App cannot join. So these two triggers work only on repos that invite
`rookery-agent`: write access on a personal repo, or read access as an org member. Someone
accepts the invitation by signing in as `rookery-agent`. This stays manual: automating it
would need that account's token on the box, and trade-off #7 says the agent can read files there.

- `pull_request.review_requested` with `requested_reviewer.login == AGENT_USER` → `pr-review`.
  The review is posted by `rookery-gha[bot]`, not `rookery-agent`, so GitHub would show the
  request as pending forever. `finally` removes it (`DELETE …/requested_reviewers` with the
  App's token), and that also lets someone request it again.
- `issues.assigned` with `assignee.login == AGENT_USER` → `start`, exactly like
  `rookery:trigger`, with the same "no phase label yet" check.

Both are a few lines in `enqueue()` on top of §3 and cost nothing on repos that do not
invite the account. They can ship with v1 or after it (§9).

## 7. Fork PRs are refused in v1

Untrusted code is what the reviewer reads, and the reviewer runs with Bash under
`bypassPermissions`. Trade-off #7 says a prompt-injected agent can read `app.pem` and act as
the App on **every** installation. A same-repo PR was pushed by someone with write access,
which is the same trust the issue flow already relies on. A fork PR can come from anyone,
and the writer who mentions `@rookery-agent` cannot vet every line for injected instructions.

So `review_pr()` comments "Rookery does not review pull requests from forks yet" and stops
when `isCrossRepository` is true. This goes into `trade-offs.md`, to revisit together with #7
(a second user, or the sandbox).

## 8. Tests (`test_agent.py`, the existing fakes)

- `enqueue`, mention: a writer's `@rookery-agent` on a PR submits, with the note;
  `@rookery-agent-x`, a quoted or fenced mention, a non-writer, or ME submit nothing; a PR
  comment without a mention submits nothing, even with a phase label on the PR.
- `enqueue`, label: `rookery:pr-review` from a writer submits; another label does not.
- `enqueue`, §6: a review request or an assignment for `AGENT_USER` submits; one for anybody
  else does not.
- `workflow()`: `pr-review` is accepted, has the default `pr-reviewer`, and can be overridden
  per key; an unknown agent in it fails `steer()`.
- `handle` pr-review: the review is posted with `gh pr review --comment`, the label and the
  review request are removed, and the worktree is gone. On failure: a `gh pr comment`, no
  `issue edit` or phase label, the label is removed. A fork is refused before any `claude` call.
- `claude(wt=)` runs in the given directory and does not create an `issue-<n>` worktree.

## 9. Not in v1, and when to add it

- **Inline comments on lines.** They need the review-comments API with diff positions, and
  the agent would have to output them in a structured way. Add this if one review comment
  proves too coarse to act on.
- **Auto-review on `opened` / `synchronize`.** This spends the daily cap on every push. Add
  it as a workflow flag (`"pr-review": {"auto": true}`) once people ask.
- **Fork PRs.** See §7.
- **A `request changes` verdict.** GitHub refuses that on the bot's own PRs, and a bot
  blocking merges goes against "the review is advisory".
- **Answering follow-up questions in the thread.** Every mention is a new review, in a new
  session (`per_job=True`). A conversation needs a session per thread.

## 10. Decisions to confirm

1. **One reviewer or several.** v1 runs one agent per review: the repo's own agent replaces
   the built-in. Do you also want several in one review (built-in + security + yours), shaped
   like `after` (a list, each one a section)? That is a bigger change in the config surface
   and the portal.
2. **Refuse forks in v1** (§7): yes or no?
3. **Where the review goes.** A PR review of type *comment* (it shows in the Reviews
   panel; recommended), or a plain PR comment?
4. **§6 in v1 or after?** Recommended: in v1. It is small, and `rookery-agent` already exists.
5. **`@rookery-agent` on an issue** (not a PR), to start planning without the label: add
   it now, or keep issues on `rookery:trigger` (and §6's assign) only? Recommended: later.
   It would need the same "no phase label yet" check, and a mention on an issue that is
   already in flight has no clear meaning.
