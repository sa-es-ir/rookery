# Built-in skill catalog

Copied into a job's plugin as `rk:<name>` only when the repo's
`.rookery/workflow.json` names it in a slot. A repo skill with the same name, in
`.rookery/skills/` or `.claude/skills/`, wins. Vendored, read before landing, and
never fetched at run time: a skill's scripts run on the operator's box.

| Skill | Slot | Source |
|---|---|---|
| `repo-recon` | plan | ours |
| `verification-before-completion` | task | [obra/superpowers@8ca22db](https://github.com/obra/superpowers/tree/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills), MIT ([LICENSE-superpowers](LICENSE-superpowers)) |
| `systematic-debugging` | task | same |
| `test-driven-development` | task | same |
| `security-review` | check | ours |
| `changelog` | write | ours |

`check` and `write` are `after` steps: a repo adds one with an agent of its own, or
reuses one, e.g. `{"agent": "reviewer", "mode": "check", "skills": ["security-review"]}`.

Local changes to upstream, to re-apply on an update:

- `systematic-debugging/`: dropped `CREATION-LOG.md` and `test-*.md` (notes for
  skill authors, not for the agent); `superpowers:` skill references renamed to
  `rk:`.
