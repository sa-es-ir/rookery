You are planning work for {source} in {repo}.

Title: {title}

{body}

Write two files, and change nothing else — no code, no commits:

- {d}/plan.md — a short implementation plan.
- {d}/tasks.md — that plan split into at most {max} tasks, each one a
  `- [ ] ` markdown checkbox on its own line, ordered, each independently
  implementable by someone who has read plan.md and nothing else.

The tasks run on {box} with no GUI and no services the repo does not
start itself. A task that verifies the work asks only for what can run there (a
build, the tests that can run); name anything that cannot, such as a
Windows-only test host, in plan.md as a check for the human reviewer.

If something is genuinely ambiguous, ask instead of guessing.

Reply with exactly one line `STATUS: questions` or `STATUS: plan-ready`, and
around it the markdown to post as an issue comment: the questions, or a summary
of the plan ending with "Reply `approve` to implement."
