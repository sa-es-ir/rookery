# Safety and limits

Rookery runs an AI agent on your code. These rules control what the agent can do. Rookery always applies them.

## The agent does not publish

- The agent cannot run `git push` or `gh`. Rules in the Claude command-line tool block these commands.
- Rookery commits each task. The agent does not commit.
- Rookery pushes the branch. It does not push to the base branch.
- Rookery opens a draft pull request. It does not merge.

## People control each step

- Only a person with write, maintain or admin access can approve a plan or reply to it.
- Approval is a fixed text pattern. An LLM does not decide if a comment is an approval.
- The portal has no approve button.
- The portal changes configuration only with a pull request. A person must merge it.

## What controls the agent

Rookery reads `CLAUDE.md`, `.rookery/` and `.claude/skills/` from the base branch. It does not read them from the branch that the agent changes. Thus the agent cannot change its own instructions during a run.

Rookery also blocks:

- MCP servers. A `.mcp.json` file in the repository does not start a server.
- Hooks in skills and agents. A skill or agent with keys that are not permitted fails the job.
- Hooks and settings in the repository. The Claude command-line tool does not load them.

## The planning step writes only the plan

The plan agent can change only `agent-plans/<n>/`. Rookery reverts all other changes after each planning call.

## Daily limits

Each repository has two limits for each UTC day:

| Limit | Default |
| --- | --- |
| Claude calls | 30 |
| Cost | 20 USD |

Rookery checks the limits before each Claude call. Failed calls also count. When a repository reaches a limit, Rookery writes a comment and sets `rookery:error`. The tasks before that point stay committed. On the next UTC day, comment `approve` to continue.

The limits of one repository do not change the limits of other repositories.

## Size limits for each issue

| Limit | Value |
| --- | --- |
| Tasks in a plan | 15 |
| Extra steps (`after`) | 5 |
| Root `CLAUDE.md` | 64 KB |

## Tokens

- The GitHub token of the App goes only to the `gh` or `git` command that needs it. The agent does not get it.
- Rookery uses your GitHub sign-in token one time to list your repositories. It does not keep the token.
- `rookery.db` holds no tokens.

## Your commits are safe

Rookery deletes a local worktree or clone only if all its commits are on GitHub. If it finds commits that are not on GitHub, it keeps all the files and writes a log message.
