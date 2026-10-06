# Workflow

Rookery keeps the state of an issue in its labels. Each issue has one `rookery:*` label at a time.

## Labels

| Label | Meaning | What you do |
| --- | --- | --- |
| `rookery:trigger` | You ask Rookery to start. | Add this label. |
| `rookery:planning` | Rookery writes the plan. | Wait. |
| `rookery:awaiting-input` | Rookery asked a question in a comment. | Answer the question in a comment. |
| `rookery:awaiting-approval` | The plan is ready. | Read the plan. Comment `approve`, or comment with changes. |
| `rookery:implementing` | Rookery does the tasks. | Wait. |
| `rookery:reviewing` | The reviewer reads the full change. | Wait. |
| `rookery:done` | The draft pull request is open and the review found no problems. | Review the pull request. |
| `rookery:changes-wanted` | The draft pull request is open. The reviewer or a check step wants changes. | Read the findings in the pull request body. Fix the code, or comment `approve` to do the checks and the review again. |
| `rookery:error` | The run stopped because of an error. | Read the last comment. |

Add `rookery:trigger` to an issue only. If you add it to a pull request, Rookery removes it and writes a comment. In the same way, add `rookery:pr-review` to a pull request only. If you add it to an issue, Rookery removes it and writes a comment.

## Planning

The plan agent reads the issue and the code. It writes three files in `agent-plans/<n>/`:

- `plan.md`: the plan.
- `tasks.md`: a checklist of tasks.
- `decisions.md`: a log. Each task can add to it. The next task reads it.

The plan agent can change only `agent-plans/<n>/`. Rookery reverts any other change that the plan agent makes.

If you comment while the label is `rookery:awaiting-input` or `rookery:awaiting-approval`, Rookery reads the comment as a reply. It updates the plan in the same Claude session.

## Approval

A comment is an approval when it starts with one of these words:

- `approve` or `approved`
- `lgtm`
- `go ahead`

Upper case and lower case are the same. "I approve it" is not an approval, because it does not start with one of the words.

Only a person with write, maintain or admin access to the repository can approve or reply. Rookery ignores comments from other people and from its own bot account.

## Implementation

Rookery does the tasks in `tasks.md` in order. For each task:

1. Rookery starts a new Claude session for that task only.
2. The agent does the task. The agent does not commit.
3. The agent must reply with `STATUS: task-done`.
4. Rookery ticks the task in `tasks.md` and commits the change.

A new session for each task keeps the cost of each task at the same level. `decisions.md` is the only memory that goes from one task to the next.

A plan can have a maximum of 15 tasks. For a larger change, divide the issue into smaller issues.

## Review and extra steps

After the last task, Rookery runs the extra steps of the repository, if there are any. Then the reviewer agent reads the full change. The [Configuration](configuration.md) page tells you how to add steps.

The review is advisory. Rookery pushes the branch and opens the draft pull request for all results. If the reviewer or a check step wants changes, the label is `rookery:changes-wanted` and the findings are in the pull request body. This label does not mean that the run failed.

## When a run fails

If a step fails, Rookery writes the error in a comment and sets `rookery:error`. All tasks before the failure stay committed.

To continue:

1. Read the error in the last comment.
2. Correct the cause. For example, change the plan, change the code, or wait for the daily limit to reset.
3. Comment `approve` again.

Rookery continues at the task that failed. It does not repeat the tasks that are complete.

If Rookery has no API key for the owner of the repository, the run does not start. The comment has a link to the **Keys** page in the portal and tells you how to start the run again. Refer to [API keys](keys.md).

## Cancel

A person with write access can cancel a running job in the portal. Rookery stops the Claude call, writes a comment and sets `rookery:error`. Comment `approve` to continue.

## Review a pull request

Rookery can review a pull request that it did not write. Ask for the review in one of these ways:

| Do this on the pull request | Notes |
| --- | --- |
| Write a comment that contains `@rookery-agent`. | The rest of the comment goes to the reviewer as your note. For example: `@rookery-agent look at the migration`. |
| Add the label `rookery:pr-review`. | Rookery removes the label when the review is complete. |
| Request a review from `rookery-agent`. | This works only if `rookery-agent` is a collaborator on the repository. Rookery removes the request when the review is complete. |

Only a person with write, maintain or admin access can ask for a review. A review uses the daily limit of the repository, the same as other runs.

Rookery ignores a mention in a quote (`> …`) or in code. Thus a reply that quotes an earlier request does not start a second review.

Rookery adds a 👀 reaction when the review starts: to your comment for a mention, or to the pull request for the label or a review request. Then it posts one review of type comment on the pull request. The review is advisory. It does not approve the pull request and it does not block a merge.

The review always has the same shape:

1. The verdict at the top:

   | Verdict | Meaning |
   | --- | --- |
   | 🟢 Safe to merge | The reviewer found nothing that must stop a merge. |
   | 🟡 Minor changes | Some changes are worth making, but nothing breaks. |
   | 🔴 Changes needed | Something breaks: a bug, a security hole, or a change that does not do what the pull request says. |

2. One sentence about the change.
3. The answer to your note, if you wrote one.
4. A table of findings, worst first, with a maximum of 10. Each finding has a severity (🔴 high, 🟡 medium or ⚪ low), a file and line, and one line about the problem.
5. More information about the findings, in a collapsed **Details** section.
6. The agent that made the review, at the bottom.

The format is the same for all model providers. Rookery makes the review from data that the reviewer returns, and Rookery limits the length of each part.

To review again, for example after new commits, ask again. If the pull request has no new commits since the last review, Rookery does not review it again. It writes a comment that tells you this. To get a new review of the same commits, mention `@rookery-agent` with a note that tells Rookery what to examine. A note such as "review again" or "please review" is not sufficient, because it does not give new information.

Rookery gives the reviewer the full diff of the pull request. Lock files are not in it. If the diff is more than 100,000 characters, Rookery gives the reviewer the first files and a list of the other files. The reviewer reads those files itself.

Rookery does not review a pull request from a fork. It writes a comment that tells you this.

If the review fails, Rookery writes the error in a comment on the pull request. It does not add a `rookery:*` state label to a pull request.

You can also assign an issue to `rookery-agent` to start a plan. This is the same as the label `rookery:trigger`. It works only if `rookery-agent` is a collaborator.

## After the pull request closes

When the pull request closes, merged or not merged, Rookery deletes the local working copy of that issue. It does not delete a branch that has commits that are not on GitHub.
