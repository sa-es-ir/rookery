# Overview

Rookery is a GitHub App. It turns a GitHub issue into a draft pull request.

You add one label to an issue. Rookery reads the issue and writes a plan. You approve the plan with a comment. Rookery then does the tasks in the plan, one at a time, and opens a draft pull request. Rookery never merges. A human reviews and merges the pull request.

## The loop

1. You add the `rookery:trigger` label to an issue.
2. Rookery writes a plan and a task list. If it needs information, it asks a question in a comment.
3. You answer the questions. Then you comment `approve`.
4. Rookery does each task in a new Claude session and commits each task.
5. A reviewer agent reads the full change.
6. Rookery pushes the branch and opens a draft pull request.

```
rookery:trigger → planning → awaiting-input ⇄ awaiting-approval → implementing → reviewing → done
```

## What you get

- A branch `feature/issue-<n>` with one commit for each task.
- A folder `agent-plans/<n>/` in the pull request. It holds the plan, the task list and the decisions log.
- A draft pull request. Its body holds the review and the result of each extra step.

## What Rookery does not do

- It does not merge a pull request.
- It does not push to your base branch.
- It does not start work without an `approve` comment from a person with write access.
- It does not use an LLM to decide if a comment is an approval. A fixed pattern decides.

## Pages

- [Getting started](getting-started.md): install the App and run your first issue.
- [Workflow](workflow.md): the labels, the comments and what to do when a run fails.
- [Configuration](configuration.md): control the agents, skills and steps for a repository.
- [Safety and limits](safety.md): the rules that Rookery always follows.
