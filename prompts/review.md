Review the finished work for {source} before it goes to a
human as {handoff}.

The approved plan is {d}/plan.md, the task list is {d}/tasks.md, and
{d}/decisions.md holds the trade-offs each task recorded. The change itself is
`git diff {since}...HEAD`.

Read only: do not edit, do not commit, do not use gh.

Give your verdict as one line of its own, with nothing else on that line. The
reply fails without it:
`STATUS: review-ok` when nothing you found should stop a merge, or
`STATUS: changes-needed` when something breaks.

Then give your findings as JSON between <review> and </review>, with no code
fence:

<review>
{{"summary": "one sentence: whether the work does what the plan asked, and is sound",
 "findings": [{{"severity": "high", "where": "path/to/file.py:42",
               "title": "one line: what is wrong",
               "detail": "why it matters and what fixes it, three sentences at most"}}]}}
</review>

`severity` is `high` (it breaks something, or work the plan asked for is
missing), `medium` (it can break, or a path that can break has no test) or
`low` (worth a look). Put the worst first, ten at most. When one mistake is in
several places, make it one finding and list every place in `where`,
comma-separated, so the fix covers them all. Sound work has an empty
`findings` list. Rookery turns this JSON into the review section of
{report}, for the person about to read the diff. It posts nothing outside the tags,
but the STATUS line still goes outside them, alone on its line: the reply
fails without it.
