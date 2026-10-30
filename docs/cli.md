# Local CLI

The `rookery` command runs the Rookery flow on your computer, on a local git repository. It does not use GitHub, the App, a webhook or a rookery.run account.

The flow is the same as on the server:

1. Rookery writes a plan and a task list.
2. You read the plan. You answer its questions or you approve it.
3. Rookery runs each task in a new Claude session. Rookery ticks the task and commits it.
4. Rookery runs the `after` steps and the advisory review.
5. You merge the branch when it is correct. Rookery does not push.

## Before you start

You must have:

- `git`.
- The [`claude`](https://docs.claude.com/en/docs/claude-code) CLI, version 2.1.110 or later. Log in to it with `/login`, or select an API key with `rookery config`.
- A git repository with at least one commit on its base branch.

## Install

On Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://github.com/sa-es-ir/rookery/releases/latest/download/rookery-cli-installer.ps1 | iex"
```

On macOS and Linux:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/sa-es-ir/rookery/releases/latest/download/rookery-cli-installer.sh | sh
```

With a Rust toolchain:

```bash
cargo install rookery-cli --locked
```

Each method installs one binary, `rookery`, in the Cargo `bin` folder. Then go to your repository and run `rookery doctor`. It checks `git`, `claude`, the repository and the base branch. It makes one small Claude call (about $0.01).

## Run an item

```bash
rookery new "add a --json flag"    # write a plan
rookery reply 1 "use serde"        # answer the questions of the planner
rookery approve 1                  # run the tasks, the after steps and the review
rookery show 1                     # see the phase, the commits and the review
```

Each item has a number. `new` gives the next number in the repository.

Give the details of the item with `-m "text"`, or put them in a markdown file and use `-f issue.md`. Use `--base <branch>` to start from a branch that is not the default branch.

Run `rookery` with no command to open a prompt. Type `/` to see the commands. Type `exit` or press Ctrl-D to close the prompt.

## Commands

| Command | What it does |
|---|---|
| `rookery new "title" [-m body] [-f file] [--base b]` | Writes a plan. The item stops at `awaiting-input` or `awaiting-approval`. |
| `rookery reply <n> "text"` | Sends your answer to the planner. The planner writes the plan again. |
| `rookery approve <n> [--max-usd X]` | Runs the tasks, the `after` steps and the review. |
| `rookery ask <n> "note"` | Asks a question about the change, or asks for a change. Rookery commits a change and reviews it again. |
| `rookery review [<range>] [-m note]` | Reviews the commits on a branch. The default is the current branch. |
| `rookery ls` | Lists the items of the repository. |
| `rookery show <n>` | Shows one item: phase, branch, commits, tasks, review and the next command. |
| `rookery log [<n>]` | Shows the last runs, or all the runs of one item. |
| `rookery drop <n> [--force]` | Removes the worktree and the branch of an item. |
| `rookery memory [--clear]` | Shows or removes the notes about this repository. |
| `rookery config [<provider>] [--forget]` | Selects the model provider and its API key. |
| `rookery doctor` | Checks the setup. |

## Where Rookery works

Rookery does not change your checkout. Each item gets its own git worktree on a new branch, `rookery/<n>-<title>`. The worktree is in `~/.rookery/worktrees/`.

The worktree starts from the last commit on the base branch. Rookery does not see changes that you did not commit. If your checkout has such changes, `new` shows a warning.

The plan, the run log and the repository notes are in `.git/rookery/` of your repository. They are not committed.

To get the change, merge the branch of the item into your branch. Then run `rookery drop <n>`.

## Stop and resume

Press Ctrl-C to stop a run. Rookery stops the Claude call and all the processes that it started. The tasks that Rookery committed before you pressed Ctrl-C stay on the branch.

If a run fails or you stop it, run `rookery approve <n>` again. It starts at the first task that is not ticked.

`drop` does not remove a branch that has commits on no other branch. It also does not remove a worktree with changes that are not committed. Use `--force` to remove them.

## Cost

Each Claude call costs money on an API key, or uses your plan limits on a Claude login.

- `--max-usd X` stops a run before a call when the run used X dollars.
- Set `"max_usd": X` in `~/.rookery/config.json` to use the same limit on each `approve` and `ask`.
- A plan has a maximum of 15 tasks. A workflow has a maximum of 5 `after` steps.

`rookery log` shows the cost of each run.

## Model providers

`rookery config` selects the provider for all Claude calls:

- `claude`: your own `claude` login. This is the default.
- `anthropic`: Claude models with an Anthropic API key.
- `deepseek`: DeepSeek models with a DeepSeek API key.
- `glm`: GLM models with a Z.ai API key.

Rookery keeps the key in `~/.rookery/config.json`. The key is not encrypted. `rookery config <provider> --forget` removes it.

## Workflow and agents

The CLI reads the same files as the server, from the last commit on the base branch:

- `CLAUDE.md`
- `.rookery/workflow.json`
- `.rookery/agents/`
- `.rookery/skills/` and `.claude/skills/`

The [Configuration](configuration.md) page tells you what these files do. `~/.rookery/defaults/` is your own default layer. It has the same form as the `<owner>/.rookery` repository on the server.

## Safety

- The agent does not commit and does not push. Rookery commits. Rookery does not push.
- The agent does not use MCP servers. Use `--mcp` to let it use the MCP servers of your own Claude setup. Rookery never loads the `.mcp.json` file of the repository.
- The planner can change only the plan files. Rookery reverts all other changes after each plan call.
- Rookery reads its instructions from the base branch, not from the worktree that the agent edits.

## Not in the CLI

These server features are not in the CLI:

- GitHub issues as input, and pull requests.
- Companion repositories (`with`).
- A merge of the base branch in a follow-up.
- The portal and the run log on rookery.run.
