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
| `security-review` | check | ours |
| `changelog` | write | ours |

`security-review` also fits the `pr-review` slot: `{"pr-review": {"skills": ["security-review"]}}`.
(Not in the table's Slot column: the portal reads one slot per row from it.)

The built-in `scope-checker` agent (`agents/`) is a `check` step too: it flags changes the
plan did not ask for. Adapted from The Agency's Minimal Change Engineer
([msitarzewski/agency-agents@652b269](https://github.com/msitarzewski/agency-agents/blob/652b269/engineering/engineering-minimal-change-engineer.md)),
MIT ([LICENSE-agency-agents](LICENSE-agency-agents)); `security-review`'s client-side secrets,
row-level security and model-input checks are adapted from its AI-Generated Code Security
Auditor, same licence.

`check` and `write` are `after` steps: a repo adds one with an agent of its own, or
reuses one, e.g. `{"agent": "reviewer", "mode": "check", "skills": ["security-review"]}`.

Local changes to upstream, to re-apply on an update:

- `systematic-debugging/`: dropped `CREATION-LOG.md` and `test-*.md` (notes for
  skill authors, not for the agent); `superpowers:` skill references renamed to
  `rk:`; the pointer to `test-driven-development` removed (not in the catalog).
