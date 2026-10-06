---
name: changelog
description: Use when a finished change needs a changelog entry - finds the repo's changelog, follows its existing format, and adds user-facing entries for this change under the unreleased section
---

# Changelog

## Why

A changelog is read by people deciding whether to upgrade. They need to know
what changed for them, not which files moved. One clear line per change beats
a summary of the diff.

## Find the changelog

Look for `CHANGELOG.md`, `CHANGES.md`, `HISTORY.md`, `NEWS.md` or
`docs/changelog*` at the repo root and in `docs/`. If the repo's own
instructions (`CLAUDE.md`, `CONTRIBUTING.md`) say how changelog entries are
made (for example a `changes/` or `.changeset/` directory with one file per
change), do it that way instead.

If there is none, create `CHANGELOG.md` in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, with only an
`## [Unreleased]` section.

## Write the entry

1. Read the change: `git diff origin/<base>...HEAD` and the plan.
2. Copy the file's existing format exactly: headings, the categories it uses
   (`Added`, `Changed`, `Fixed`, `Removed`, `Deprecated`, `Security`),
   bullet style, and how it links issues.
3. Add entries under the unreleased section. Create that section at the top
   if it is missing, in the same style as the released ones.
4. Write for a user of the project: what they can now do, or what behaves
   differently. Name breaking changes as breaking, first.
5. Reference the issue the way the file already does (for example `(#12)`).

If the change has no effect a user of the project can see, add no entry and
say so in your reply. An empty line in a changelog costs its readers more than
a missing one.

## Never

- Edit a released version's section, or invent a version number or date.
  Releasing is someone else's job.
- List internal refactors, test changes or `agent-plans/` files, unless the
  file already records that kind of change.
- Put names, email addresses, secrets or internal URLs in an entry.
- Touch any file other than the changelog and the decisions file your prompt
  names.
