---
name: repo-recon
description: Use when planning work on a repository - finds how this repo is built, tested and linted, and records the exact commands in plan.md under "How to verify" so every task has a check to run
---

# Repo Recon

## Why

Each task runs in a fresh session that has read plan.md and nothing else. If
plan.md does not say how to check the work, every task guesses, and a task that
guesses wrong reports done on code that was never run.

## Find the commands

Look, in this order, and stop reading a source once it has answered:

1. The repo's own instructions: `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`,
   `README.md` (a "Development", "Testing" or "Build" section).
2. CI: `.github/workflows/*.yml`. The steps CI runs are the ones that gate a
   merge, so they beat anything a README says.
3. Build manifests: `package.json` `scripts`, `Makefile`, `justfile`,
   `pyproject.toml` / `tox.ini` / `noxfile.py`, `*.csproj` / `*.sln`,
   `go.mod`, `Cargo.toml`, `pom.xml` / `build.gradle`.
4. Linter and formatter config: `.eslintrc*`, `ruff.toml`, `.editorconfig`,
   `.pre-commit-config.yaml`.

Prefer the narrowest command that still proves something: one test file or
project, not the whole suite, when the repo supports it. Note any setup a fresh
checkout needs first (install, restore, generate).

Run a command only if it is cheap and safe (a `--help`, a dry run, a single
fast test). Never run anything that deploys, publishes or needs credentials.

## Record them

Add a section to plan.md:

```markdown
## How to verify

- Build: `<command>`
- Test: `<command>` (one file: `<command> path/to/test`)
- Lint/format: `<command>`
- Setup on a fresh checkout: `<command>`, or "none"
```

Write only what you found. If the repo has no tests, say "no test suite" rather
than inventing a command, and make writing the first test part of a task when
the change needs one.

Then make each task in tasks.md end with the check it should pass, using these
commands.
