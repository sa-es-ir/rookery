# Configuration

You can control how Rookery works on a repository. Put the configuration files in the repository. Rookery reads them from the base branch at the start of each job.

Rookery does not read configuration from the branch that the agent changes. Thus a change to the configuration has an effect only after a person merges it to the base branch.

## Files

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Instructions for the agent. Rookery reads only the file at the root. The maximum size is 64 KB. |
| `.rookery/workflow.json` | The agent, the skills and the instructions for each part of a run. |
| `.rookery/agents/<name>.md` | Agents for Rookery only. |
| `.rookery/skills/<name>/SKILL.md` | Skills for Rookery only. |
| `.claude/skills/<name>/SKILL.md` | Skills that your developers also use. |

If you do not add these files, Rookery uses its default workflow with no skills.

## workflow.json

The file has one entry for each slot. A slot is one part of a run:

- `plan`: the planning.
- `task`: each task.
- `review`: the review after the last task.
- `pr-review`: the review of a pull request that a person asks for (see [Workflow](workflow.md)).
- `after`: a list of extra steps before the review.

Each slot can set:

- `agent`: the name of the agent to use.
- `skills`: a list of skill names.
- `instructions`: text that Rookery adds to the instructions of the agent.

```json
{
  "plan":   { "skills": ["repo-recon"], "instructions": "Every schema change gets its own task." },
  "task":   { "skills": ["verification-before-completion"] },
  "after":  [
    { "agent": "reviewer", "mode": "check", "skills": ["security-review"] },
    { "agent": "docs",     "mode": "write", "skills": ["changelog"] }
  ],
  "review": { "agent": "reviewer" }
}
```

If you do not set a key, Rookery uses the default value for that key.

If the file is not valid, or if it names a skill or an agent that does not exist, the job fails at the start. Rookery writes the error in a comment. It does not use the default workflow in place of a configuration that is not correct.

## Agents

Rookery has three agents:

- `planmaker` writes the plan.
- `reviewer` reviews the full change.
- `pr-reviewer` reviews a pull request when a person asks for it.

The tasks do not use an agent. They use the default Claude harness.

To add an agent, write `.rookery/agents/<name>.md`. Use the Claude Code agent format. The frontmatter can have only these keys: `name`, `description`, `tools`, `model` and `skills`.

An agent with the name `planmaker`, `reviewer` or `pr-reviewer` replaces the agent of Rookery with that name.

Names use only lower-case letters, digits and hyphens.

### Use your own pull request reviewer

| To do this | Do this |
| --- | --- |
| Change the instructions of the Rookery reviewer | Write `.rookery/agents/pr-reviewer.md`. |
| Use an agent with a different name, skills or instructions | In `.rookery/workflow.json`, write `{"pr-review": {"agent": "my-reviewer", "skills": ["security-review"]}}`. Then write `.rookery/agents/my-reviewer.md`. |
| Do the same for all repositories of an owner | Put the same files at the root of `<owner>/.rookery`. |

Rookery reads the reviewer from the base branch, not from the pull request. Thus a pull request that changes `.rookery/agents/pr-reviewer.md` gets a review from the agent that is on the base branch now.

## Skills

When a slot names a skill, Rookery tells the agent to use that skill in each step of the slot.

Rookery also gives the agent a list of the other skills in `.rookery/skills/`, `.claude/skills/` and `<owner>/.rookery`. The agent uses a skill from this list only when the description of the skill agrees with the work. If a skill must run each time, name it in the slot.

If two skills have the same name, Rookery uses the first one that it finds in this order:

1. `.rookery/skills/`
2. `.claude/skills/`
3. The owner defaults (see below)
4. The catalog of Rookery

The frontmatter of a skill can have only these keys: `name`, `description` and `allowed-tools`.

### Skill catalog

| Skill | Slot | Purpose |
| --- | --- | --- |
| `repo-recon` | plan | Read the repository before the plan. |
| `verification-before-completion` | task | Check the work before the task is complete. |
| `systematic-debugging` | task | Find the cause of a bug before it changes the code. |
| `security-review` | check, pr-review | Look for security problems in the change. |
| `changelog` | write | Add an entry to the changelog. |

The portal shows the catalog in **Skills**.

## Extra steps (after)

`after` is a list of a maximum of 5 steps. The steps run in order, after the last task and before the review. Each step has a `mode`:

| Mode | Can change files | Replies | Result |
| --- | --- | --- | --- |
| `check` | No. Rookery reverts all changes and marks the run. | `check-ok` or `changes-needed` | The findings go into the pull request body. `changes-needed` sets `rookery:changes-wanted`. The push continues. |
| `write` | Yes | `step-done` or `failed` | Rookery commits the change as `#<n> step <agent>`. `failed` stops the run. |

When you approve again, Rookery does not repeat a `write` step that has a commit on the branch. It runs each `check` step again.

## Owner defaults

To use one configuration for all repositories of an owner, make a repository with the name `<owner>/.rookery`. Install the App on it.

Put these at the root of that repository:

- `workflow.json`
- `agents/`
- `skills/`

The configuration of a repository wins over the owner defaults, one key at a time. An `after` list in a repository replaces all of the `after` list of the owner.

## Change the configuration from the portal

Open a repository in the portal and select **Workflow**. You see the workflow that the next issue gets. If the configuration is not valid, you see the error that the job will show.

Select **Propose a change** to edit `workflow.json` or an agent file. Rookery checks the change. Then it opens a pull request. Nothing changes until a person merges the pull request. You must have write access to the repository.

## A pull request that changes the configuration

If a branch of Rookery changes `.rookery/`, `.claude/` or the root `CLAUDE.md`, the pull request starts with a warning. Review that change carefully. After you merge it, it controls the agent on the next runs.
