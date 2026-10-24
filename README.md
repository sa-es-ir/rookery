<p align="center">
  <img src="rookery.png" alt="Rookery" width="200">
</p>

<h3 align="center">Label an issue. Get a draft PR.</h3>

<p align="center">
  A self-hostable GitHub App that turns issues into reviewed, draft pull requests:<br>
  it plans, asks questions, waits for your <code>approve</code>, then builds it task by task.
</p>

<p align="center">
  <a href="https://rookery.run"><img alt="Hosted: rookery.run" src="https://img.shields.io/badge/hosted-rookery.run-fd8925"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-stdlib%20only-2ea44f">
  <img alt="Harness: Claude Code" src="https://img.shields.io/badge/harness-Claude%20Code-6b21ef">
  <a href="LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-blue"></a>
</p>

https://github.com/user-attachments/assets/0000497f-d035-46c1-91c9-bb340bd65935

---

## Why Rookery

Most coding agents either chat with you in an editor or open a PR you didn't ask for.
Rookery sits where your team already works — **GitHub issues** — and keeps a human on
every gate that matters.

- 🏷️ **One label to start.** Add `rookery:trigger` to an issue. That's the whole UI.
- 🗺️ **Plan first, code second.** You get a written plan and task list as an issue
  comment. Questions come as comments too. Nothing is built until someone with write
  access comments `approve`.
- 🧱 **One task, one fresh session.** Each task runs in its own agent session and gets
  its own commit, so cost and context stay flat no matter how big the issue is.
- 🔍 **Reviewed before you see it.** A reviewer agent (plus any checks you configure)
  reads the finished diff. The findings go in the PR body.
- 🛡️ **The agent never publishes.** It cannot `git push` or call `gh`. The orchestrator
  commits, pushes to a feature branch and opens a **draft** PR. Merging is always yours.
- 🔁 **Resumable.** A failed task stops with everything before it committed. Fix it,
  comment `approve`, and it picks up at the same task.
- 🧠 **Learns your repo.** Short per-repo notes carry lessons from one run to the next.
- 💸 **Hard caps.** Per-repo daily limits on calls and dollars, checked before every call.
- 🔑 **Bring your own model key.** Claude, DeepSeek or GLM — see [providers](#model-providers).
- 🪶 **Tiny.** Two Python files, stdlib only. No database server, no queue, no framework.

## How it works

```
 label ─▶ plan ─▶ questions? ─▶ approve ─▶ task 1 … task N ─▶ checks ─▶ review ─▶ draft PR
          └──── issue comments ────┘        (one commit each)
```

Follow the issue's `rookery:*` label to see where it is:

| Label                       | Meaning                                                        |
| --------------------------- | -------------------------------------------------------------- |
| `rookery:planning`          | writing the plan                                               |
| `rookery:awaiting-input`    | a question is in the comments — answer it                      |
| `rookery:awaiting-approval` | read the plan, then comment `approve` / `lgtm` / `go ahead`    |
| `rookery:implementing`      | working through the task list                                  |
| `rookery:reviewing`         | the reviewer is reading the finished diff                      |
| `rookery:done`              | the draft PR is open and the review is clean                   |
| `rookery:changes-wanted`    | the draft PR is open; the reviewer or a check wants changes    |
| `rookery:error`             | the reason is the last comment; fix it, then `approve` resumes |

The plan, the task list and a `decisions.md` log live in `agent-plans/<n>/` in your repo
and ship with the PR, so reviewers see _why_, not only _what_.

### Also on request: PR reviews

Mention `@rookery-agent` on any open PR (or add the `rookery:pr-review` label, or request
a review from it) and Rookery posts one advisory review: a verdict, a findings table and
details. Add a note after the mention to steer it — `@rookery-agent check the migration`.

## Model providers

The agent harness is always the [Claude Code](https://docs.claude.com/en/docs/claude-code)
CLI, so every safety rule holds whichever model is behind it. The model is chosen per
GitHub owner: an owner's own key, or the operator's default.

| Provider               | Status       | Models behind the agent                    |
| ---------------------- | ------------ | ------------------------------------------ |
| **Claude** (CLI login) | ✅ Supported | Opus plans and reviews, Sonnet builds      |
| **Anthropic API key**  | ✅ Supported | Same Claude models, billed to your key     |
| **DeepSeek**           | ✅ Supported | DeepSeek V4 Pro / Flash                    |
| **Z.ai (GLM)**         | ✅ Supported | GLM 5.3 / GLM 5.3 Flash                    |
| **OpenRouter**         | 🧪 Next      | GPT and Gemini models, after quality tests |
| **Claude on Bedrock**  | 📋 Planned   | Claude through your own AWS account        |
| **Codex CLI**          | 🔭 Later     | OpenAI models in their native harness      |

Owners add keys on the portal's **Keys** page. Keys are encrypted at rest, never shown
again (only the last 4 characters), and the daily caps still apply.
More: [docs/keys.md](docs/keys.md).

## Get started

### Hosted

Open **[rookery.run](https://rookery.run)**, sign in with GitHub, install the App on a
repo, add your model key, and label an issue. Guide:
[docs/getting-started.md](docs/getting-started.md).

### Self-host

You need Python 3.10+, `git`, `openssl`, the [`gh`](https://cli.github.com) CLI and the
[`claude`](https://docs.claude.com/en/docs/claude-code) CLI on your PATH, plus your own
GitHub App. No packages to install.

```bash
git clone https://github.com/sa-es-ir/rookery && cd rookery
cp .env.example .env               # WEBHOOK_SECRET, APP_ID, APP_KEY, ...
python agent.py owner/repo 12      # dry run: plan issue 12, no webhook needed
python agent.py                    # webhook + portal on http://127.0.0.1:8787
```

For local development, forward the App's webhook with
`npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8787/`.
For a real server, follow [docs/self-hosting.md](docs/self-hosting.md) and the full
Linux guide in [DEPLOY.md](DEPLOY.md).

## Make it yours

A repo steers its own runs from files on its **base branch** — never from the branch the
agent edits, so a config change takes effect only after a human merges it.

- **`CLAUDE.md`** at the root is read like it is for your own developers.
- **`.rookery/workflow.json`** picks the agent, skills and instructions for each step, and
  adds up to 5 `after` steps (checks or writers) between the last task and the review.
- **`.rookery/agents/*.md`** and **`.rookery/skills/`** add your own agents and skills, or
  replace the built-in `planmaker` / `reviewer`.
- **`<owner>/.rookery`** holds defaults for every repo of an owner.

```json
{
  "plan": {
    "skills": ["repo-recon"],
    "instructions": "Every schema change gets its own task."
  },
  "task": { "skills": ["verification-before-completion"] },
  "after": [
    { "agent": "reviewer", "mode": "check", "skills": ["security-review"] },
    { "agent": "docs", "mode": "write", "skills": ["changelog"] }
  ]
}
```

A bad config fails the run at start with a comment — Rookery never quietly falls back.
The built-in skill catalog is in [skills/](skills/README.md); the full reference is
[docs/configuration.md](docs/configuration.md) and [docs/workflow.md](docs/workflow.md).

## The portal

`python agent.py` also serves a small control plane. Sign in with GitHub and, for each
repo you installed the App on, see today's spend against the caps, every run and where it
stopped (_"Failed at task 9 of 12"_), each step with its cost, cancel a running job, and
view or propose workflow changes — as a pull request, never a direct write.

There is deliberately **no approve button**: approval stays an `approve` comment on the
issue, from someone with write access.

## Safety, in short

- Approval is a regex on a comment from a user with write access — never an LLM judgement.
- The agent is denied `git push` and `gh`; it runs with no MCP servers and loads no
  hooks or settings from the repo it edits.
- Planning can write only `agent-plans/<n>/`; anything else is reverted.
- Never pushes to the base branch; the GitHub token never enters the agent's environment.
- Caps (default 30 calls and $20 per repo per UTC day) are checked before every call; a
  `PAUSED` file next to `agent.py` stops all new calls.

Details: [docs/safety.md](docs/safety.md).

## Roadmap

- [x] GitHub App, onboarding without a restart, per-repo approval and caps
- [x] Parallel repos, portal, run log, cancel
- [x] Per-repo workflows, custom agents and skills, owner defaults
- [x] PR reviews on request, repo memory
- [x] Multiple model providers and bring-your-own-key
- [ ] OpenRouter (GPT, Gemini) — after tool-use quality tests
- [ ] Claude on Bedrock — when an owner asks
- [ ] A second harness (Codex CLI) — on demand

## Development

```bash
python test_agent.py   # the whole suite: plain asserts, no framework, no network
```

The dry-run forms go through the same code path as the webhook — use them instead of
standing up smee:

```bash
python agent.py owner/repo 12              # plan the issue, as if just labelled
python agent.py owner/repo 12 approve      # run the task loop, push, open the draft PR
python agent.py owner/repo 12 use sqlite   # anything else is fed back as a reply
python agent.py owner/repo 12 @rookery-agent check the migration   # review PR 12
```

`agent.py` is the pipeline; `web.py` is the webhook, sign-in, API and the portal in `ui/`.
[CLAUDE.md](CLAUDE.md) documents the architecture and the invariants that must not be
softened. Issues and PRs are welcome.

## License

Rookery is licensed under the [GNU Affero General Public License v3.0](LICENSE). You can
use, modify and self-host it freely. If you run a modified version as a network service,
you must make your source available to its users. The skills adapted from
[superpowers](skills/LICENSE-superpowers) keep their MIT license.
