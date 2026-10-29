Review {what}. {who} asked
for it.

Title: {title}

{body}
{note}
This worktree, {wt}, is {head}, detached. You are already
in it. The change is `git diff {since}...HEAD`; Rookery ran it for you,
below. If it touches agent-plans/, the plan it was built from is in the diff.

{diff}

Read only: do not edit, do not commit, do not use gh.

Give your verdict as one line of its own, with nothing else on that line. The
reply fails without it:
`STATUS: safe-to-merge` when nothing you found should stop a merge,
`STATUS: minor-changes` when something is worth a fix but nothing breaks, or
`STATUS: changes-needed` when something breaks.

Then give your findings as JSON between <review> and </review>, with no code
fence:

<review>
{{"summary": "one sentence: what the change does and whether it is sound",
 "answer": "your answer to the note, or an empty string when there is no note",
 "findings": [{{"severity": "high", "where": "path/to/file.py:42",
               "title": "one line: what is wrong",
               "detail": "why it matters and what fixes it, three sentences at most"}}]}}
</review>

`severity` is `high` (it breaks something), `medium` (it can break, or a path
that can break has no test) or `low` (worth a look). Put the worst first, ten
at most. When one mistake is in several places, make it one finding and list
every place in `where`, comma-separated, so the fix covers them all. A sound
change has an empty `findings` list. Rookery turns this JSON into the review it
{posts}, for the person about to read the diff. It {posts} nothing outside the tags,
but the STATUS line still goes outside them, alone on its line: the reply
fails without it.
