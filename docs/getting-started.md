# Getting started

## Before you start

- The repository must have at least one commit on its default branch. Rookery makes its branches from that commit.
- Protect your default branch. Rookery does not push to it, but branch protection is a second guard.

## 1. Install the App

1. Open [rookery.run](/) and select **Open portal**.
2. Select **Sign in with GitHub**.
3. Install the Rookery GitHub App on your account or organization.
4. Select the repositories that Rookery can work on.

Rookery adds a repository when you select it. It does not need a restart. Rookery clones the repository and creates the `rookery:*` labels. The base branch is the default branch of the repository.

To stop Rookery on a repository, clear it in the App settings. Rookery then stops work on that repository and deletes its local copy.

## 2. Write a good issue

Rookery reads the issue title, the issue body and the comments. Write the issue for a developer who does not know the code:

- Say what must change.
- Say how to know that the change is correct.
- Give file names, names of functions, or error messages if you know them.

## 3. Run your first issue

1. Add the `rookery:trigger` label to the issue.
2. Wait for the label `rookery:awaiting-approval` or `rookery:awaiting-input`.
3. Read the plan in the comment.
4. If Rookery asks a question, answer it in a comment.
5. When the plan is correct, comment `approve`.
6. Wait for the draft pull request.

The [Workflow](workflow.md) page tells you what each label means.

## 4. Watch the run

Open the [portal](/portal.html). For each repository, you see:

- The runs of today and their cost.
- Each step of a run: the tasks, each Claude call, the commits and the pull request.
- The workflow that the next issue gets.

You can cancel a running job from the portal. You cannot approve a plan from the portal. Approval is always an issue comment.
