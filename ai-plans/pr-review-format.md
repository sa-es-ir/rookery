# A fixed format for the PR review

**Status:** built 5 October 2026. Decisions in §5 took the recommended answers.

## 1. The problem

`PR_REVIEW` asks for "findings as markdown, worst first", and `review_pr()` posts the
reply verbatim under a `**review-ok** · rk:pr-reviewer` line. Nothing bounds its shape or
length. On `PROVIDER=deepseek` the review is an essay; on Claude it is shorter but still
free-form. A developer has to read all of it to learn the one thing they want first: can
this merge?

## 2. The change: the model returns data, the orchestrator writes the markdown

Same pattern as `remember()` and its `<notes>` tags. The look is the same on every
provider, and the caps are enforced in code, not asked for in the prompt.

**Verdict — three levels, still the `STATUS:` marker** (PR review only):

| STATUS | Header |
|---|---|
| `safe-to-merge` | 🟢 Safe to merge |
| `minor-changes` | 🟡 Minor changes |
| `changes-needed` | 🔴 Changes needed |

**Findings — JSON between `<review>` tags:**

```json
{"summary": "one sentence",
 "answer": "the answer to the requester's note, or empty",
 "findings": [{"severity": "high|medium|low", "where": "web.py:212",
               "title": "one line", "detail": "a few sentences, optional"}]}
```

**`render_review()`** builds the posted review:

```markdown
## 🟡 Minor changes
Session handling is sound; one cleanup gap on logout.

> **Your note:** yes, the migration is reversible.

| # | Severity | Where | Finding |
|---|---|---|---|
| 1 | 🔴 high | `web.py:212` | Session cookie not cleared on logout |

<details><summary>Details</summary>
...one paragraph per finding that has a detail...
</details>

<sub>rk:pr-reviewer</sub>
```

Verdict, summary and table read in five seconds; the long part is collapsed.

**Caps** (`REVIEW_CAPS`): 10 findings, title 150 chars, `where` 120, summary and answer
600, detail 800. Over a cap is cut with `…` (and "N more findings not shown"), never a
failure — the review is already paid for. `|` is escaped and newlines collapsed in table
cells so a cell cannot break the table.

**Where the contract lives:** in the `PR_REVIEW` template, not `agents/pr-reviewer.md`.
A repo or owner can replace the agent (`.rookery/agents/pr-reviewer.md`); the output
contract must survive that. The agent file keeps the judgement (what is worth a finding)
and gains what each severity means.

## 3. Failures

- No `<review>` tags, JSON that does not parse, or a shape that is wrong (no `summary`,
  a finding without `title` or with an unknown `severity`) → the job fails, like a missing
  `STATUS` marker: "parsed, never inferred". The error comment carries the tail of the
  reply so the paid text is not lost.
- `safe-to-merge` with a `high` finding → the job fails. A header that contradicts the
  table is worse than no review.
- A ```` ```json ```` fence inside the tags is tolerated (stripped); models add one.

## 4. Out of scope, next

- **The implement-flow `reviewer`: built 5 October 2026** — `REVIEW` asks for the same JSON,
  `render_review(verdicts=REVIEW_VERDICTS, level="###")` writes it under `## 🤖 Reviewer agent`.
- **The `after` checks: built 5 October 2026** — `CHECK` asks for the same JSON,
  `render_review(verdicts=CHECK_VERDICTS, level="###")` writes it under the step's
  `## 🤖 <Agent> agent · check` header. Verdicts stay `review-ok` / `check-ok` /
  `changes-needed`, since `implement()`, `MODES` and the `rookery:changes-wanted` label key
  off them. Trade-offs #20.
- The portal does not show the PR-review verdict; nothing to change there.

## 5. Decisions

1. Bad or missing JSON fails the job (alternative was: post the raw text collapsed under
   the verdict header).
2. One consistency rule: `safe-to-merge` with a `high` finding fails the job.
3. Names: `safe-to-merge` / `minor-changes` / `changes-needed`.

## 6. Verify

- `python test_agent.py`: rendering, pipe escaping, caps, bad JSON, the consistency rule,
  the end-to-end `review_pr()` post.
- Production: one review on a real PR on DeepSeek, read on GitHub.
