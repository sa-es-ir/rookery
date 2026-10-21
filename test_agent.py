"""Run: python test_agent.py  (the gates only; no network, no gh)."""
import hashlib
import hmac
import os

os.environ.update(WEBHOOK_SECRET="s3cr3t", REPOS_ONLY="", PROVIDER="claude", OWN="o")  # before import: .env only fills unset keys
import agent  # noqa: E402

real_sh, real_handle, real_worktree = agent.sh, agent.handle, agent.worktree

# approval is a regex, not a judgement call
assert agent.APPROVE.match("approve")
assert agent.APPROVE.match("  LGTM, ship it")
assert agent.APPROVE.match("go ahead")
assert not agent.APPROVE.match("please approve later")
assert not agent.APPROVE.match("not approved yet")
assert not agent.APPROVE.match("approver should look first")
# a quote-reply to the plan: the quote is not the commenter's, the reply under it is
assert agent.APPROVE.match(agent.unquoted("> ## Plan\n>     Reply `approve` to implement.\n\napprove"))
assert not agent.APPROVE.match(agent.unquoted("> approve\n\nnot yet, use sqlite"))

# STATUS marker is parsed, never inferred; the rest is the comment body
m = agent.MARKER.search("Some questions\n\nSTATUS: questions\n1. which db?")
assert m and m.group(1) == "questions"
assert not agent.MARKER.search("the STATUS: plan-ready marker must be on its own line")
for line in ("**STATUS: review-ok**", "**STATUS:** review-ok", "STATUS: review-ok  ", "`STATUS: review-ok`"):   # prod: haiku bolds it, opus copies the backticks
    m = agent.MARKER.search(f"fine\n{line}\nmore")
    assert m and m.group(1) == "review-ok", line
    assert (m.string[:m.start()] + m.string[m.end():]) == "fine\n\nmore", "the whole line is stripped"
assert not agent.MARKER.search("**STATUS: review-ok** but see below")
assert not agent.MARKER.search("STATUS: changed — merged it")   # prod: text after the marker is not one
fenced = "```\n**Plan**\n\n- one\n\nSTATUS: plan-ready\n```"   # prod: deepseek fenced its whole plan
assert agent.unmark(fenced, agent.MARKER.search(fenced)) == "**Plan**\n\n- one"
code = "```py\na = 1\n```\nSee above.\n```py\nb = 2\n```\nSTATUS: answered"
assert agent.unmark(code, agent.MARKER.search(code)) == code[:code.index("\nSTATUS")], "two fences are not one wrapper"

# a PR comment addresses Rookery by @AGENT_USER, never in a quote or code; the rest is the note
assert agent.mention("@Rookery-Agent look hard at the migration") == "look hard at the migration"
assert agent.mention("hey @rookery-agent") == "hey" and agent.mention("@rookery-agent") == ""
for ask in ("@rookery-agent review again\r\n", "@rookery-agent please re-review this PR, thanks!", "PTAL @rookery-agent"):
    assert agent.mention(ask) == "", ask   # only asking: no note, so an unchanged head is not reviewed twice
assert agent.mention("@rookery-agent review again, check the tests") == "review again, check the tests"
for text in ("@rookery-agent-x look", "me@rookery-agent", "> @rookery-agent look\n\nthanks", "see `@rookery-agent`",
             "```\n@rookery-agent\n```", "~~~\n@rookery-agent", "approve", ""):
    assert agent.mention(text) is None, text
assert agent.mention("> @rookery-agent old\n\n@rookery-agent new") == "new", "the quote is not in the note"

# webhook signature
raw = b'{"action":"opened"}'
good = "sha256=" + hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
assert hmac.compare_digest(good, "sha256=" + hmac.new(agent.SECRET, raw, hashlib.sha256).hexdigest())
assert not hmac.compare_digest(good, "sha256=" + hmac.new(b"wrong", raw, hashlib.sha256).hexdigest())

# per-repo context: the clone lives in the workspace, the base comes from GitHub
agent.WORKSPACE = __import__("pathlib").Path("/ws")
agent.REPOS = {"o/r": "main", "o/other": "develop"}
assert agent.ctx("o/r", "7") == ("o/r", 7, agent.WORKSPACE / "repos" / "o_r", "main")
assert agent.ctx("o/other", 7).base == "develop"

# task list: parsed by the orchestrator, ticked by the orchestrator
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

wt = Path(tempfile.mkdtemp())
d = wt / agent.pdir(7)
d.mkdir(parents=True)
(d / "tasks.md").write_text("""# Tasks

- [x] scaffold the module
- [ ] wire the handler
* [ ] add the test

Not a task: - [ ] inline mention, and `- [ ] ` in prose.
""", encoding="utf-8")

tasks = agent.read_tasks(d)
assert [t[1] for t in tasks] == [True, False, False], tasks
assert [t[2] for t in tasks] == ["scaffold the module", "wire the handler", "add the test"]

first_open = next(t for t in tasks if not t[1])
agent.tick(d, first_open[0])
after = agent.read_tasks(d)
assert [t[1] for t in after] == [True, True, False]
assert [t[2] for t in after] == [t[2] for t in tasks], "ticking must not disturb the text"
assert "- [x] wire the handler" in (d / "tasks.md").read_text(encoding="utf-8")

# every tick strictly reduces the open count, so the loop terminates
for at, _, _ in [t for t in after if not t[1]]:
    agent.tick(d, at)
assert not [t for t in agent.read_tasks(d) if not t[1]]
# a PR body's task line: the first sentence, plain, cut at a word; the task under it, folded
assert agent.summary("Add `X` to `a.csproj`. Then run the build.") == "Add X to a.csproj."
assert agent.summary("word " * 40).endswith("word…") and len(agent.summary("word " * 40)) <= agent.SUMMARY_CAP
assert agent.fold("✅ 1. a <b>", "> `t`") == "<details><summary>✅ 1. a &lt;b&gt;</summary>\n\n> `t`\n\n</details>"

# only tick() changes the list: whatever an agent does to it inside frozen() is undone
with agent.frozen(d / "tasks.md"):
    (d / "tasks.md").write_text("- [ ] a task an agent added\n", encoding="utf-8")
assert not [t for t in agent.read_tasks(d) if not t[1]]

# the two named roles, and the review verdicts the orchestrator branches on
roles = {p.stem: agent.parse_agent(p, p.stem) for p in (agent.HERE / "agents").glob("*.md")}
assert set(roles) == {"planmaker", "reviewer", "pr-reviewer", "scope-checker"}
assert all(r["description"] for r in roles.values())
assert not [r for r in ("reviewer", "pr-reviewer", "scope-checker") if "Edit" in roles[r]["tools"]], "reviewers report, never fix"
assert all(r["model"] == "opus" for r in roles.values()), "roles think; tasks grind on sonnet"
assert {agent.WORKFLOW[s]["agent"] for s in agent.WORKFLOW} == {"planmaker", None, "reviewer", "pr-reviewer"}
assert not [k for s in agent.WORKFLOW.values() for k in s["skills"]], "no skills unless a repo asks"
# the catalog: every skill passes the same allowlist a repo's does
cat = sorted(p.name for p in (agent.HERE / "skills").iterdir() if p.is_dir())
assert cat == ["changelog", "repo-recon", "security-review", "systematic-debugging",
               "verification-before-completion"]
for name in cat:
    assert agent.frontmatter(agent.HERE / "skills" / name / "SKILL.md", agent.SKILL_KEYS, name)["name"] == name
assert agent.MARKER.search("looks fine\nSTATUS: review-ok").group(1) == "review-ok"
assert agent.MARKER.search("STATUS: changes-needed\nbug in foo.py").group(1) == "changes-needed"

# PR review: the model returns JSON, the orchestrator writes the markdown and holds the caps
import json  # noqa: E402


def rv(verdict, review, raw=False):
    return agent.render_review(verdict, review if raw else f"prose\n<review>{json.dumps(review)}</review>", "rk:x")
out = rv("minor-changes", {"summary": "Mostly fine.", "answer": "yes\nit is",
                           "findings": [{"severity": "medium", "where": "a.py:3", "title": "a | b\nc",
                                         "detail": "why"}, {"severity": "low", "title": "nit"}]})
assert out.startswith("## 🟡 Minor changes\n\n🔎 **2 findings** · 🟡 1 medium · ⚪ 1 low\n\nMostly fine.\n\n"
                      "> **Your note:** yes\n> it is\n\n"), out
assert "| 1 | 🟡 medium | `a.py:3` | a \\| b c |\n| 2 | ⚪ low |  | nit |" in out, "| escaped, one line per row"
assert "<details><summary>Details</summary>\n\n**1. a \\| b c**\n\nwhy\n\n</details>" in out and "**2." not in out
assert "prose" not in out and out.endswith("<sub>rk:x</sub>\n")
out = rv("safe-to-merge", {"summary": "Sound.", "answer": "", "findings": []})
assert out == "## 🟢 Safe to merge\n\n✅ **No findings**\n\nSound.\n\n<sub>rk:x</sub>\n", "no findings: said plainly"
out = rv("changes-needed", {"summary": "s" * 900, "findings": [{"severity": "low", "title": "t" * 200,
                                                               "detail": "d" * 900}] * 12})
assert "s" * 599 + "…" in out and "t" * 149 + "…" in out and "d" * 799 + "…" in out and "| 10 |" in out
assert "| 11 |" not in out and "2 more findings not shown." in out, "cut, never failed"
assert "🔎 **12 findings** · ⚪ 12 low\n\n" in out, "the count is of all findings, not the ones shown"
assert agent.UNCONFIRMED not in out, "no wt, no location check"
fenced = '<review>\n```json\n{"summary": "ok", "findings": []}\n```\n</review>'
assert rv("safe-to-merge", fenced, raw=True).startswith("## 🟢"), "a code fence inside the tags is tolerated"
# set_phase: a GitHub 500 on the label edit is retried (DotNet.RateLimit#52), a lasting one still raises
sp_saved, edits, waits = (agent.gh, agent.labels_of, agent.time.sleep), [], []
def flaky(repo, *args, **kw):
    edits.append(args)
    if len(edits) < 3:
        raise RuntimeError("gh failed (1): GraphQL: Something went wrong while executing your query")
agent.gh, agent.labels_of, agent.time.sleep = flaky, lambda r, n: {"rookery:planning"}, waits.append
agent.set_phase(agent.Ctx("o/r", 52, None, "main"), "rookery:awaiting-input")
assert len(edits) == 3 and waits == [5, 20] and edits[0][-1] == "rookery:planning", (edits, waits)
edits.clear()
def down(repo, *args, **kw):
    edits.append(args)
    raise RuntimeError("gh failed (1): GraphQL: Something went wrong while executing your query")
agent.gh = down
try:
    agent.set_phase(agent.Ctx("o/r", 52, None, "main"), "rookery:awaiting-input")
    raise AssertionError("a lasting failure must raise")
except RuntimeError as e:
    assert "Something went wrong" in str(e) and len(edits) == 3, edits
agent.gh, agent.labels_of, agent.time.sleep = sp_saved
unclosed = '<review>{"summary": "ok", "findings": [{"severity": "low", "title": "t"}]\n</review>'
assert "| 1 | ⚪ low |" in rv("minor-changes", unclosed, raw=True), "a dropped last } is tolerated (Mantis#80)"
for verdict, review, why in [("review-ok", {"summary": "s", "findings": []}, "it replies safe-to-merge"),
                             ("safe-to-merge", "no tags here", "no <review> JSON"),
                             ("safe-to-merge", "<review>{nope</review>", "does not parse"),
                             ("safe-to-merge", {"findings": []}, "without a summary"),
                             ("minor-changes", {"summary": "s", "findings": [{"severity": "huge", "title": "t"}]},
                              "no title or severity"),
                             ("safe-to-merge", {"summary": "s", "findings": [{"severity": "high", "title": "t"}]},
                              "safe-to-merge but has a high finding")]:
    try:
        rv(verdict, review, raw=isinstance(review, str))
        raise AssertionError(why)
    except RuntimeError as e:
        assert why in str(e), e
# with a worktree, a `where` that is not in it is marked, never dropped
lw = Path(tempfile.mkdtemp())
(lw / "sub").mkdir()
(lw / "a.py").write_text("1\n2\n3\n")
(lw / "sub" / "b.py").write_text("x")
for where, ok in [("a.py:3", True), ("a.py", True), ("sub/b.py:1", True), ("a.py:2-3, sub/b.py:L1", True),
                  ("", True), ("the retry loop in foo()", True), ("`a.py:1`", True), ("sub/", True),
                  ("a.py:4", False), ("a.py:0", False), ("nope.py:1", False), ("a.py:1, nope.py", False),
                  ("../a.py", False), ("sub/:1", False)]:
    assert agent.located(lw, where) is ok, (where, ok)
out = agent.render_review("minor-changes", "<review>" + json.dumps(
    {"summary": "s", "findings": [{"severity": "medium", "where": "a.py:2", "title": "real"},
                                  {"severity": "low", "where": "ghost.py:9", "title": "guessed"}]}) + "</review>",
    "rk:x", wt=lw)
assert "| `a.py:2` | real |" in out and "| `ghost.py:9` ❔ | guessed |" in out, out
assert agent.UNCONFIRMED in out, "flagged, never dropped"

# the implement-flow reviewer: same JSON, its own two verdicts, the same consistency rule
try:
    agent.render_review("review-ok", '<review>{"summary": "s", "findings": [{"severity": "high", "title": "t"}]}'
                        '</review>', "rk:x", verdicts=agent.REVIEW_VERDICTS)
    raise AssertionError("review-ok with a high finding")
except RuntimeError as e:
    assert "says review-ok but has a high finding" in str(e), e

# planning writes agent-plans/<n>/ and nothing else — the rest is reverted
import subprocess  # noqa: E402

repo = Path(tempfile.mkdtemp())
git = lambda *a: subprocess.run(("git",) + a, cwd=repo, capture_output=True, check=True)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@t"), git("config", "user.name", "t")
(repo / "src.py").write_text("original\n", encoding="utf-8")
git("add", "-A"), git("commit", "-qm", "base")

(repo / agent.pdir(7)).mkdir(parents=True)          # untracked tree — must survive
(repo / agent.pdir(7) / "plan.md").write_text("the plan\n", encoding="utf-8")
(repo / "src.py").write_text("planner edited this\n", encoding="utf-8")  # tracked, revert
(repo / "stray.txt").write_text("junk\n", encoding="utf-8")              # untracked, delete

agent.WORKSPACE = Path(tempfile.mkdtemp())   # repo_settings() reads rookery.db there
agent.keep_plans_only(agent.Ctx("o/r", 7, repo, "main"), repo)

assert (repo / agent.pdir(7) / "plan.md").read_text(encoding="utf-8") == "the plan\n"
assert (repo / "src.py").read_text(encoding="utf-8") == "original\n", "tracked edit not reverted"
assert not (repo / "stray.txt").exists(), "untracked stray not cleaned"

# local_plans: a new issue's plan goes beside the worktree, never into it; a plan already
# somewhere stays there whichever way the setting flips
c8, c7, c9 = (agent.Ctx("o/r", n, repo, "main") for n in (8, 7, 9))
out8 = agent.wt_root("o/r") / "plans-8"
assert agent.plans(c8, repo) == (out8, str(out8.absolute())) and out8.is_dir(), "on by default: outside"
assert agent.plans(c7, repo)[1] == agent.pdir(7), "issue 7's plan is in the worktree already"
(repo / agent.pdir(8)).mkdir(parents=True)   # an agent writing the in-repo path by mistake
assert agent.plans(c8, repo)[0] == out8, "outside first: a stray agent-plans/8 moves nothing back"
with agent.closing(agent.db()) as con, con:
    con.execute("INSERT INTO settings VALUES ('repo:o/r', ?)", ('{"local_plans": false}',))
assert agent.plans(c9, repo) == (repo / agent.pdir(9), agent.pdir(9)), "off: in the repo"
assert agent.plans(c8, repo)[0] == out8, "turned off mid-issue: the plan stays where it is"
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM settings WHERE key = 'repo:o/r'")

# GitHub App: the JWT is RS256 and verifies against the App's public key
import base64  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

keys = Path(tempfile.mkdtemp())
pem, pub = keys / "app.pem", keys / "pub.pem"
subprocess.run(["openssl", "genrsa", "-traditional", "-out", str(pem), "2048"], capture_output=True, check=True)
subprocess.run(["openssl", "rsa", "-in", str(pem), "-pubout", "-out", str(pub)], capture_output=True, check=True)
agent.APP_ID, agent.APP_KEY = "4999655", str(pem)

unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
h, p, s = agent.jwt().split(".")
assert json.loads(unb64(h)) == {"alg": "RS256", "typ": "JWT"}
claims = json.loads(unb64(p))
assert claims["iss"] == "4999655" and claims["exp"] - claims["iat"] <= 600, claims
(keys / "sig").write_bytes(unb64(s))
v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(keys / "sig")],
                   input=f"{h}.{p}".encode(), capture_output=True)
assert v.returncode == 0, v.stdout + v.stderr

# installation token: minted once, reused, re-minted near expiry; never in os.environ
calls = []
def fake_api(method, path, bearer):
    calls.append(path)
    assert bearer.count(".") == 2
    if path.endswith("/installation"):
        return {"id": 162968573}
    exp = time.time() + (3600 if len(calls) < 3 else 300)
    return {"token": f"ghs_{len(calls)}",
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))}
agent.api = fake_api
assert agent.token("o/r") == "ghs_2" and agent.token("o/r") == "ghs_2"
assert calls == ["/repos/o/r/installation", "/app/installations/162968573/access_tokens"]
agent._tokens["o/r"] = ("ghs_old", time.time() + 300)  # 5 min left: re-mint
assert agent.token("o/r") != "ghs_old"

env = agent.auth("o/r")
assert env["GH_TOKEN"].startswith("ghs_")
assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:" + env["GH_TOKEN"]
assert "GH_TOKEN" not in os.environ and "GIT_CONFIG_VALUE_0" not in os.environ, \
    "the claude subprocess inherits os.environ — the token must never be there"
assert agent.ME == "rookery-gha[bot]"

# an issue created with the trigger label fires `opened` and `labeled`: start once
real_submit, submitted = agent.submit, []
agent.submit = submitted.append   # capture jobs instead of starting workers
def drain():
    out = submitted[:]
    submitted.clear()
    return out
agent.labels_of = lambda repo, n: set()
issue = {"number": 1, "title": "t", "body": "", "labels": [{"name": agent.TRIGGER}]}
for action, label in [("opened", None), ("labeled", agent.TRIGGER), ("labeled", "bug")]:
    agent.enqueue("issues", {"action": action, "label": {"name": label},
                             "repository": {"full_name": "o/r"}, "issue": issue})
assert [j["kind"] for j in drain()] == ["start"]

# installation events keep REPOS live: added repos are accepted at once, then onboarded
agent.enqueue("installation_repositories", {"action": "added", "repositories_added": [{"full_name": "o/new"}],
                                            "repositories_removed": []})
agent.enqueue("installation", {"action": "created", "repositories": [{"full_name": "p/one"}]})
assert agent.REPOS["o/new"] == "" and agent.REPOS["o/r"] == "main"
assert drain() == [{"kind": "onboard", "repo": "o/new"}, {"kind": "onboard", "repo": "p/one"}]
agent.enqueue("installation_repositories", {"action": "removed", "repositories_added": [],
                                            "repositories_removed": [{"full_name": "o/new"}]})
agent.enqueue("installation", {"action": "deleted", "repositories": [{"full_name": "p/one"}]})
assert "o/new" not in agent.REPOS and "p/one" not in agent.REPOS
assert drain() == [{"kind": "offboard", "repo": "o/new"}, {"kind": "offboard", "repo": "p/one"}]
agent.enqueue("issues", {"action": "labeled", "label": {"name": agent.TRIGGER},
                         "repository": {"full_name": "o/new"}, "issue": issue})
assert not submitted, "a removed repo's events are ignored"

# a closed PR frees its worktree — only the bot's PRs, only feature/issue-<n>
pr = lambda ref, user: {"action": "closed", "repository": {"full_name": "o/r"},
                        "pull_request": {"head": {"ref": ref}, "user": {"login": user}}}
agent.enqueue("pull_request", pr("feature/issue-3", agent.ME))
agent.enqueue("pull_request", pr("feature/issue-4", "mallory"))
agent.enqueue("pull_request", pr("feature/issue-5-extra", agent.ME))
assert drain() == [{"kind": "drop-worktree", "repo": "o/r", "n": 3}]

# retention never deletes unpushed commits
agent.WORKSPACE = Path(tempfile.mkdtemp())
origin = agent.WORKSPACE / "origin.git"
git = lambda cwd, *a: subprocess.run(("git",) + a, cwd=cwd, capture_output=True, check=True, text=True).stdout
git(agent.WORKSPACE, "init", "-q", "--bare", "-b", "main", str(origin))
clone = agent.clone_dir("o/r")
git(agent.WORKSPACE, "clone", "-q", str(origin), str(clone))
git(clone, "config", "user.email", "t@t"), git(clone, "config", "user.name", "t")
git(clone, "commit", "-q", "--allow-empty", "-m", "base"), git(clone, "push", "-q", "origin", "main")
for n in (1, 2):
    git(clone, "worktree", "add", "-q", "-B", f"feature/issue-{n}", str(agent.wt_root("o/r") / f"issue-{n}"), "origin/main")
wt1 = agent.wt_root("o/r") / "issue-1"
git(wt1, "commit", "-q", "--allow-empty", "-m", "work")    # committed, not pushed
agent.memory_file("o/r").parent.mkdir(parents=True)
agent.memory_file("o/r").write_text("notes", encoding="utf-8")

agent.offboard("o/r")
assert clone.exists() and wt1.exists(), "offboard must refuse while a branch is unpushed"
assert agent.memory_file("o/r").exists(), "and keeps the notes with it"
agent.drop_worktree("o/r", 1)
assert wt1.exists(), "drop_worktree must keep an unpushed worktree"
agent.drop_worktree("o/r", 2)
assert not (agent.wt_root("o/r") / "issue-2").exists()

git(wt1, "push", "-q", str(origin), "feature/issue-1:feature/issue-1")  # as implement() does: a URL...
git(wt1, "update-ref", "refs/remotes/origin/feature/issue-1", "feature/issue-1")  # ...then the ref
agent.offboard("o/r")
assert not clone.exists() and not agent.wt_root("o/r").exists(), "pushed work: the disk is freed"
assert not agent.memory_file("o/r").exists(), "a repo that left takes its notes with it"

# startup: every repo of every installation, across pages, with its default branch
seen = []
agent.onboard = lambda repo, base=None: seen.append((repo, base))
agent.api = lambda method, path, bearer: (
    {"token": "ghs_i"} if method == "POST" else
    [{"id": 1}] if path.startswith("/app/installations") else
    {"repositories": [{"full_name": f"o/r{i}", "default_branch": "dev"}
                      for i in range(100 if path.endswith("page=1") else 3)]})
agent.sync()
assert len(seen) == 103 and seen[0] == ("o/r0", "dev"), seen[:2]
agent.ONLY, seen[:] = {"o/r5"}, []   # REPOS_ONLY: a local instance serves only these
agent.sync()
assert seen == [("o/r5", "dev")]
agent.ONLY = set()

# approval and replies: write/maintain/admin on *that* repo, looked up per comment
import urllib.error  # noqa: E402

perms = {("o/r", "alice"): "admin", ("o/r", "bob"): "write", ("o/r", "carol"): "read",
         ("o/other", "alice"): "none"}
def fake_api(method, path, bearer):
    _, _, repo_a, repo_b, _, user, _ = path.split("/")
    if (repo_a + "/" + repo_b, user) not in perms:
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
    return {"permission": perms[(repo_a + "/" + repo_b, user)]}
agent.api, agent.token = fake_api, lambda repo: "ghs_x"
assert agent.can_write("o/r", "alice") and agent.can_write("o/r", "bob")
assert not agent.can_write("o/r", "carol"), "read is not enough"
assert not agent.can_write("o/other", "alice"), "admin on one repo is nothing on another"
assert not agent.can_write("o/r", "nobody"), "a failed lookup denies"

agent.REPOS = {"o/r": "main", "o/other": "main"}
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
for repo, user in [("o/r", "alice"), ("o/other", "alice"), ("o/r", "carol"), ("o/r", agent.ME)]:
    agent.enqueue("issue_comment", {"action": "created", "repository": {"full_name": repo},
                                    "issue": {"number": 9}, "comment": {"user": {"login": user}, "body": "approve"}})
assert [(j["repo"], j["body"]) for j in drain()] == [("o/r", "approve")]

# PR review: a mention, the label or a review request, from a writer. Any other PR comment is
# nothing — even `approve` on a PR carrying a phase label (labels_of says awaiting-approval here)
for user, text in [("bob", "@rookery-agent look hard at the migration"), ("bob", "approve"),
                   ("bob", "@rookery-agent-x look"), ("bob", "> @rookery-agent look"), ("carol", "@rookery-agent"),
                   (agent.ME, "@rookery-agent")]:
    agent.enqueue("issue_comment", {"action": "created", "repository": {"full_name": "o/r"},
                                    "issue": {"number": 12, "title": "Fix", "pull_request": {}},
                                    "comment": {"id": 5, "user": {"login": user}, "body": text}})
assert drain() == [{"kind": "pr-review", "repo": "o/r", "n": 12, "title": "Fix", "by": "bob",
                    "note": "look hard at the migration", "comment": 5}]
def pr_event(action, sender, **k):
    agent.enqueue("pull_request", {"action": action, "repository": {"full_name": "o/r"}, "sender": {"login": sender},
                                   "pull_request": {"number": 12, "title": "Fix", "head": {"ref": "x"},
                                                    "user": {"login": "bob"}}, **k})
pr_event("labeled", "bob", label={"name": agent.REVIEW_LABEL})
pr_event("labeled", "bob", label={"name": "bug"})
pr_event("labeled", "carol", label={"name": agent.REVIEW_LABEL})
pr_event("review_requested", "alice", requested_reviewer={"login": "Rookery-Agent"})
pr_event("review_requested", "alice", requested_reviewer={"login": "dave"})
pr_event("review_requested", "alice", requested_team={"name": "core"})
pr_event("review_requested", "carol", requested_reviewer={"login": "rookery-agent"})
assert [(j["kind"], j["by"], j.get("label"), j.get("requested")) for j in drain()] == [
    ("pr-review", "bob", True, None), ("pr-review", "alice", None, True)]
# review_prs: every same-repo PR once, when it is opened. Off by default; never Rookery's own, never a fork's
opened = lambda user, head: pr_event("opened", user, pull_request={
    "number": 12, "title": "Fix", "head": {"ref": "x", "repo": head and {"full_name": head}}, "user": {"login": user}})
opened("dave", "o/r")
assert not drain() and agent.repo_settings("o/r") == {"local_plans": True, "review_prs": False}
with agent.closing(agent.db()) as con, con:
    con.execute("INSERT INTO settings VALUES ('repo:o/r', ?)", ('{"review_prs": true}',))
for user, head in [("dave", "o/r"), (agent.ME, "o/r"), ("dave", "mallory/r"), ("dave", None)]:
    opened(user, head)
pr_event("synchronize", "dave")
assert drain() == [{"kind": "pr-review", "repo": "o/r", "n": 12, "title": "Fix", "by": "dave", "auto": True}], \
    "no write check: a writer turned it on, and a same-repo head can push"
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM settings WHERE key = 'repo:o/r'")
# assigning AGENT_USER to an issue starts it, like the trigger label, and only before any phase
for who, phases in [("rookery-agent", set()), ("dave", set()), ("rookery-agent", {"rookery:planning"})]:
    agent.labels_of = lambda repo, n: phases
    agent.enqueue("issues", {"action": "assigned", "assignee": {"login": who},
                             "repository": {"full_name": "o/r"}, "issue": issue})
assert [j["kind"] for j in drain()] == ["start"]
# a label on the wrong kind of item: a comment and the label removed, `gh pr …` on a PR; never for our own
pr_event("labeled", "bob", label={"name": agent.TRIGGER})
pr_event("labeled", agent.ME, label={"name": agent.TRIGGER})
for who in ("bob", agent.ME):
    agent.enqueue("issues", {"action": "labeled", "label": {"name": agent.REVIEW_LABEL}, "sender": {"login": who},
                             "repository": {"full_name": "o/r"}, "issue": issue})
wrong = drain()
assert wrong == [{"kind": "wrong-label", "repo": "o/r", "n": 12, "label": agent.TRIGGER},
                 {"kind": "wrong-label", "repo": "o/r", "n": 1, "label": agent.REVIEW_LABEL}], wrong
ghs, saved_gh = [], agent.gh
agent.gh = lambda repo, *a, input=None, check=True: ghs.append((a, input, check)) or ""
for j in wrong:
    agent.handle(j)
agent.gh = saved_gh
assert ghs == [(("pr", "comment", "12", "--body-file", "-"), agent.WRONG_LABEL[agent.TRIGGER], True),
               (("pr", "edit", "12", "--remove-label", agent.TRIGGER), None, False),
               (("issue", "comment", "1", "--body-file", "-"), agent.WRONG_LABEL[agent.REVIEW_LABEL], True),
               (("issue", "edit", "1", "--remove-label", agent.REVIEW_LABEL), None, False)], ghs
assert "add `rookery:pr-review` or mention `@rookery-agent`" in agent.WRONG_LABEL[agent.TRIGGER]

# caps:runs and dollars per repo per UTC day, in SQLite, so a restart keeps them
agent.WORKSPACE = Path(tempfile.mkdtemp())
agent.PAUSED = agent.WORKSPACE / "PAUSED"
agent.RUN_CAP, agent.USD_CAP = 3, 1.0
assert agent.cost_of('{"result": "x", "total_cost_usd": 0.25}', "claude") == 0.25
assert agent.cost_of("claude crashed", "claude") == 0.0

# providers: claude is the CLI's own login, untouched; another backend is env= only, priced by us
assert agent.PROVIDER == "claude" and agent.provider_env("claude") == {}
assert "DEEPSEEK_API_KEY" not in os.environ and "ZAI_API_KEY" not in os.environ, "every provider key is popped, used or not"
agent.KEYS.update(deepseek="sk-test", glm="sk-test")
assert agent.provider_env("deepseek") == {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_API_KEY": "sk-test",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro", "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash"}
use = {"inputTokens": 1_000_000, "cacheCreationInputTokens": 1_000_000, "cacheReadInputTokens": 1_000_000,
       "outputTokens": 1_000_000}
out = json.dumps({"total_cost_usd": 99, "modelUsage": {"deepseek-flash": use, "deepseek-v4-pro": use}})
assert abs(agent.cost_of(out, "deepseek") - (0.30 * 2 + 0.006 + 1.20 + 1.32 * 2 + 0.044 + 3.96)) < 1e-9, "ours, not the CLI's"
try:
    agent.cost_of(json.dumps({"modelUsage": {"deepseek-chat": use}}), "deepseek")
    raise AssertionError("an unpriced model is never guessed")
except RuntimeError as e:
    assert "deepseek-chat" in str(e)
# spikes G-a/G-b: x-api-key works, modelUsage names are these exactly
assert agent.provider_env("glm") == {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic", "ANTHROPIC_API_KEY": "sk-test",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3-flash",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash"}
out = json.dumps({"modelUsage": {"glm-5.3-flash": use}})
assert abs(agent.cost_of(out, "glm") - (0.15 * 2 + 0.03 + 0.50)) < 1e-9
agent.KEYS["deepseek"] = ""
assert [agent.ready(p) for p in ("claude", "deepseek", "glm", "nope")] == [True, False, True, False]
for p in ("deepseek", "nope"):   # never a quiet fallback to the operator's login
    try:
        agent.provider_env(p)
        raise AssertionError(p)
    except RuntimeError:
        pass
agent.KEYS.update(deepseek="", glm="", anthropic="")
# anthropic: Claude by key, the CLI's own aliases and price
agent.KEYS["anthropic"] = "sk-ant"
assert agent.provider_env("anthropic") == {"ANTHROPIC_BASE_URL": "https://api.anthropic.com", "ANTHROPIC_API_KEY": "sk-ant"}
assert agent.cost_of(json.dumps({"total_cost_usd": 0.3, "modelUsage": {"claude-x": {}}}), "anthropic") == 0.3
agent.KEYS["anthropic"] = ""

# the provider per owner: its override, else the stored default, else PROVIDER from .env
def put(key, value):
    with agent.closing(agent.db()) as con, con:
        con.execute("DELETE FROM settings WHERE key = ?", (key,))
        if value:
            con.execute("INSERT INTO settings VALUES (?, ?)", (key, value))
prov = lambda repo: agent.provider_for(repo)[0]
def nokey(repo):
    try:
        agent.provider_for(repo)
    except agent.NoKey as e:
        return e.why
assert agent.provider_for("o/r") == ("claude", None, None), ".env's"
put("provider", "glm")
assert prov("o/r") == "glm", "the stored default beats .env"
put("provider:o", "deepseek")
assert prov("o/r") == "deepseek" and prov("o/x") == "deepseek", "per owner, not per repo"
put("provider:o", None)
assert prov("o/r") == "glm", "a dropped override falls back"
put("provider", None)
assert prov("o/r") == "claude"
# an owner outside OWN: never the operator's default, never the subscription — NoKey, not a fallback
for setup in ((), (("provider:acme", "claude"),), (("provider", "glm"),), (("provider", "glm"), ("provider:acme", "claude"))):
    for k, v in setup:
        put(k, v)
    assert nokey("acme/r") == "no API key", setup
    for k, _ in setup:
        put(k, None)
assert prov("O/r") == "claude", "OWN ignores case"
put("provider:acme", "deepseek")
assert agent.provider_for("acme/r") == ("deepseek", None, None), "an override is the operator's explicit choice"
put("provider:acme", None)

# owners' own keys: sealed with KEY_SECRET (openssl + HMAC), and the selected one beats any override
agent.KEY_SECRET = "master-test"
blob = agent.seal("sk-own-key-0123456789")
assert "sk-own-key" not in blob and agent.unseal(blob) == "sk-own-key-0123456789"
assert agent.seal("sk-own-key-0123456789") != blob, "salted: the same key seals differently"
ct, tag = blob.rsplit(".", 1)
for bad in (ct[:-2] + ("AA" if ct[-2:] != "AA" else "BB") + "." + tag, ct + "." + "0" * 64, ct):
    try:
        agent.unseal(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
agent.KEY_SECRET = "another"
try:
    agent.unseal(blob)
    raise AssertionError("another KEY_SECRET reads nothing")
except ValueError:
    pass
agent.KEY_SECRET = "master-test"
def addkey(owner, provider, key):
    with agent.closing(agent.db()) as con, con:
        return con.execute("INSERT INTO keys (owner, provider, label, secret, last4, added, by) VALUES (?, ?, '', ?, ?, 0, ?)",
                           (owner, provider, agent.seal(key), key[-4:], owner)).lastrowid
k1 = addkey("acme", "glm", "sk-acme-glm-0123456789")
assert nokey("acme/r") == "no API key", "added, not selected"
put("key:acme", str(k1))
put("provider:acme", "deepseek")
assert agent.provider_for("acme/r") == ("glm", k1, "sk-acme-glm-0123456789"), "who pays, chooses"
put("key:o", str(k1))
assert prov("o/r") == "claude", "another owner's key id selects nothing"
put("key:o", None)
agent.KEY_SECRET = ""
assert nokey("acme/r") == "the selected API key cannot be read", "never a quiet fallback to the override"
agent.KEY_SECRET = "master-test"
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM keys WHERE id = ?", (k1,))
assert agent.provider_for("acme/r") == ("deepseek", None, None), "a deleted key falls through to the override"
put("provider:acme", None), put("key:acme", None)
agent.check_caps("o/r")                      # nothing used yet
agent.record("o/r", 0.25), agent.record("o/r", 0.25)
agent.record("o/other", 5.0)                 # another repo's spend is its own
assert agent.usage("o/r") == (2, 0.5)
agent.check_caps("o/r")
agent.record("o/r", 0.0)
def capped(repo):
    try:
        agent.check_caps(repo)
    except RuntimeError as e:
        return str(e)
assert "3/3 runs" in capped("o/r"), "the run cap"
assert "$5.00/$1.00" in capped("o/other"), "the dollar cap"
with agent.closing(agent.db()) as con, con:   # yesterday's usage does not count
    con.execute("UPDATE runs SET day = '2000-01-01'")
assert agent.usage("o/r") == (0, 0) and capped("o/r") is None
agent.PAUSED.touch()
assert "paused" in capped("o/r")
agent.PAUSED.unlink()

# claude(): the cap is checked before the call, and every call is recorded, failed ones too
agent.worktree = lambda c: agent.WORKSPACE
argv, prompts = [], []
agent.sh = lambda *a, **k: argv.append(a) or prompts.append(k["input"]) or json.dumps({"result": "done\nSTATUS: task-done", "total_cost_usd": 0.4})
c = agent.Ctx("o/r", 9, agent.WORKSPACE, "main")
assert agent.claude(c, "p", key="task-0") == ("task-done", "done")
# no MCP: neither the target repo's .mcp.json nor the login account's connectors
assert "--strict-mcp-config" in argv[0] and "--mcp-config" not in argv[0], argv
# none of the target repo's settings: its hooks would run here, its skills/CLAUDE.md come from base
assert argv[0][argv[0].index("--setting-sources") + 1] == "user", argv
assert not [x for x in argv[0] if x.startswith(("--plugin-dir", "--append-system-prompt"))], "outside a job: none"
assert argv[0][1:3] == ("-p", "--output-format") and prompts == ["p"], argv   # on stdin: one argv entry is capped at 128 KB
# deny rules see the hook-rewritten command: the RTK forms must be denied too
assert {"Bash(git push:*)", "Bash(gh:*)", "Bash(rtk git push:*)", "Bash(rtk gh:*)"} <= set(argv[0]), argv
assert "Bash(rtk proxy:*)" in argv[0], "rtk proxy runs a command raw: none of the four would match it"
# RUN_AS: the same argv, run as that user through sudo (DEPLOY.md §5b); unset, no sudo
assert "sudo" not in argv[0], argv
agent.RUN_AS = "rookery-run"
argv.clear(), agent.claude(c._replace(repo="o/runas"), "p", key="task-0")   # its own cap
agent.RUN_AS = ""
assert argv[0][:5] == ("sudo", "-n", "-H", "-u", "rookery-run"), argv
assert argv[0][argv[0].index("claude"):][1:3] == ("-p", "--output-format") and "p" not in argv[0] and "Bash(rtk proxy:*)" in argv[0], argv
# PROVIDER=claude inherits the env exactly as before; another backend's overrides go in env=, never argv
envs, done = [], json.dumps({"result": "STATUS: task-done", "modelUsage": {}})
agent.sh = lambda *a, **k: envs.append(k["env"]) or argv.append(a) or done
agent.claude(c._replace(repo="o/prov"), "p", key="task-0")
agent.KEYS["deepseek"] = "sk-test"
put("provider:o", "deepseek")
argv.clear(), agent.claude(c._replace(repo="o/prov"), "p", key="task-0")
assert envs[0] is None and envs[1]["ANTHROPIC_API_KEY"] == "sk-test" and envs[1]["PATH"] == os.environ["PATH"]
assert "sk-test" not in str(argv), argv
# the provider is read once per call: switched after the env is built, the price is still deepseek's
real_cost, priced = agent.cost_of, []
def switch(*a, **k):
    put("provider:o", "claude")   # the admin page, mid-call
    return json.dumps({"result": "STATUS: task-done", "modelUsage": {"deepseek-flash": use}})
agent.sh, agent.cost_of = switch, lambda out, name: priced.append(name) or real_cost(out, name)
agent.claude(c._replace(repo="o/prov"), "p", key="task-0")
assert priced == ["deepseek"], priced
agent.cost_of, agent.KEYS["deepseek"] = real_cost, ""
put("provider:o", None)
agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "done\nSTATUS: task-done", "total_cost_usd": 0.4})
# a task resumes its session on approve-again; the review and after steps start one per job
sc = agent.Ctx("o/sid", 9, agent.WORKSPACE, "main")
real_caps, agent.check_caps = agent.check_caps, lambda repo: None   # caps are tested above
def sid(**k):
    argv.clear(), agent.claude(sc, "p", key="review", **k)
    return argv[0][argv[0].index("--session-id") + 1]
agent.active["o/sid"] = {"job": 1, "cancel": None}
assert sid() == sid(), "tasks keep resuming"
first = sid(per_job=True)
agent.active["o/sid"] = {"job": 2, "cancel": None}
assert sid(per_job=True) not in (first, sid()), "a new job is a new session"
del agent.active["o/sid"]
agent.check_caps = real_caps
def boom(*a, **k):
    raise RuntimeError("claude failed (1): overloaded")
agent.sh = boom
try:
    agent.claude(c, "p", key="task-1")
except RuntimeError:
    pass
assert agent.usage("o/r") == (2, 0.4)
agent.record("o/r", 0.0)                      # 3/3: the next call never starts
agent.sh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude ran over the cap"))
try:
    agent.claude(c, "p", key="task-2")
    raise AssertionError("expected the cap to stop it")
except RuntimeError as e:
    assert "daily cap" in str(e)

# never the parent's stdin: `claude -p` reads it even with the prompt in argv
import sys  # noqa: E402

agent.sh = real_sh
assert agent.sh(sys.executable, "-c", "import sys; print(len(sys.stdin.read()))") == "0"

# pin(): CLAUDE.md and skills come from a freshly fetched origin/<base>, never the worktree
agent.WORKSPACE = Path(tempfile.mkdtemp())
git = lambda cwd, *a: subprocess.run(("git",) + a, cwd=cwd, capture_output=True, check=True, text=True).stdout
origin, up, clone = agent.WORKSPACE / "origin.git", agent.WORKSPACE / "up", agent.clone_dir("o/r")
git(agent.WORKSPACE, "init", "-q", "--bare", "-b", "main", str(origin))
git(agent.WORKSPACE, "clone", "-q", str(origin), str(up))          # a maintainer, merging to base
git(up, "config", "user.email", "t@t"), git(up, "config", "user.name", "t")
git(up, "commit", "-q", "--allow-empty", "-m", "base"), git(up, "push", "-q", "origin", "main")
git(agent.WORKSPACE, "clone", "-q", str(origin), str(clone))       # Rookery's clone: about to go stale
def merge(files):
    for name, text in files.items():
        (up / name).parent.mkdir(parents=True, exist_ok=True)
        (up / name).unlink() if text is None else (up / name).write_text(text, encoding="utf-8")
    git(up, "add", "-A"), git(up, "commit", "-qm", "x"), git(up, "push", "-q", "origin", "main")
merge({"CLAUDE.md": "Use tabs.\n", ".claude/skills/verify/run.sh": "make test\n",
       ".claude/skills/verify/SKILL.md": "---\nname: verify\ndescription: >\n  run the checks\n"
                                         "allowed-tools:\n- Bash\n---\nRun the tests.\n"})
(clone / "CLAUDE.md").write_text("from the worktree\n", encoding="utf-8")
pc = agent.Ctx("o/r", 3, clone, "main")
agent.begin(pc, "plan", "t")
agent.pin(pc)
a = agent.active["o/r"]
assert a["claude_md"] == "Use tabs.", a["claude_md"]
assert (a["plugin"] / "skills" / "verify" / "run.sh").read_text(encoding="utf-8") == "make test\n"
assert json.loads((a["plugin"] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")) == {"name": "rk"}
argv[:] = []
agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "STATUS: task-done"})
agent.claude(pc, "p", key="task-0")
agent.sh = real_sh
assert f"--plugin-dir={a['plugin']}" in argv[0], argv
assert [x for x in argv[0] if x.startswith("--append-system-prompt=") and x.endswith("\n\nUse tabs.")], argv
plugin = a["plugin"]
agent.finish("o/r", "ok")
assert not plugin.exists(), "the plugin dir is gone after the job"
with agent.closing(agent.db()) as con:
    based = json.loads(con.execute("SELECT detail FROM steps WHERE what = 'base'").fetchone()[0])
assert based["sha"] == git(up, "rev-parse", "HEAD").strip() and based["skills"] == ["verify"], "base_sha logged"
assert based["sources"] == {"verify": ".claude/skills"}, "which source won"
assert "--agents" not in argv[0] and "--model" in argv[0], "no workflow.json: tasks run as no agent"
assert "Notes Rookery kept" not in "".join(argv[0]) and "memory" not in based, "no notes yet: nothing said"

# the repo's notes: read once by pin(), logged, appended after CLAUDE.md for every call in the job
agent.memory_file("o/r").parent.mkdir(parents=True, exist_ok=True)
agent.memory_file("o/r").write_text("- build: make", encoding="utf-8")
agent.RUN_CAP = 99
def pinned(**over):
    argv.clear()
    agent.begin(pc, "plan", "t")
    try:
        agent.pin(pc)
        agent.active["o/r"].update(over)
        agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "STATUS: task-done"})
        agent.claude(pc, "p", key="task-0", slot={**agent.WORKFLOW["task"], "instructions": "Step rule."})
    finally:
        agent.sh = real_sh
        job = agent.active["o/r"]["job"]
        agent.finish("o/r", "ok")
    with agent.closing(agent.db()) as con:
        base_ = json.loads(con.execute("SELECT detail FROM steps WHERE job = ? AND what = 'base'", (job,)).fetchone()[0])
    return next((x for x in argv[0] if x.startswith("--append-system-prompt=")), ""), base_
sysp, base_ = pinned()
assert sysp.index("Use tabs.") < sysp.index(agent.MEMORY_NOTE + "- build: make") < sysp.index("Step rule."), sysp
assert base_["memory"] == "13, " + __import__("hashlib").sha256(b"- build: make").hexdigest()[:12], base_
agent.MEMORY = False   # MEMORY=0: neither read nor written
sysp, base_ = pinned()
assert "Notes Rookery kept" not in sysp and "memory" not in base_
agent.MEMORY = True
agent.memory_file("o/r").write_text("n" * (64 * 1024 - 5), encoding="utf-8")
sysp, base_ = pinned()
assert "Notes Rookery kept" not in sysp and base_["memory"].startswith("left out"), "over 64 KB: left out, never fatal"
agent.memory_file("o/r").unlink()

# .rookery/workflow.json on base: slots name skills and add instructions; agents are plugin agents
merge({".rookery/workflow.json": json.dumps({
           "plan": {"skills": ["repo-recon"], "instructions": "One migration per task."},
           "task": {"skills": ["verify", "systematic-debugging"]}}),
       ".rookery/skills/verify/SKILL.md": "---\nname: verify\n---\nfrom .rookery\n",
       ".claude/skills/repo-recon/SKILL.md": "---\nname: repo-recon\n---\nthe repo's own\n"})
agent.begin(pc, "plan", "t")
agent.pin(pc)
a = agent.active["o/r"]
sk = a["plugin"] / "skills"
assert (sk / "verify" / "SKILL.md").read_text(encoding="utf-8").endswith("from .rookery\n")
assert not (sk / "verify" / "run.sh").exists(), "the winner's files only, never a merge of two skills"
assert (sk / "repo-recon" / "SKILL.md").read_text(encoding="utf-8").endswith("the repo's own\n"), "repo beats catalog"
assert (sk / "systematic-debugging" / "find-polluter.sh").exists(), "an enabled catalog skill, files and all"
assert not (sk / "changelog").exists(), "a catalog skill nobody enabled stays out"
assert sorted(p.name for p in (a["plugin"] / "agents").iterdir()) == ["planmaker.md", "pr-reviewer.md", "reviewer.md",
                                                                        "scope-checker.md"]
assert a["workflow"]["pr-review"] == agent.WORKFLOW["pr-review"], "a slot the repo leaves out is the built-in's"
assert sorted(p.name for p in a["plugin"].iterdir()) == [".claude-plugin", "agents", "skills"], "no hooks, no MCP"
argv[:], prompts[:] = [], []
agent.sh = lambda *a, **k: argv.append(a) or prompts.append(k["input"]) or json.dumps({"result": "STATUS: plan-ready"})
agent.claude(pc, "p", key="plan", slot="plan")
agent.claude(pc, "p", key="task-0", slot="task")
agent.sh = real_sh
plan_argv, task_argv = argv
assert plan_argv[plan_argv.index("--agent") + 1] == "rk:planmaker" and "--model" not in plan_argv, plan_argv
assert prompts[0] == "p\n\nUse the `rk:repo-recon` skill.\n\n" + agent.OFFER_SKILLS.format(names="`rk:verify`"), \
    "an enabled skill is named, or it never fires; the repo's others are offered"
sysp = next(x for x in plan_argv if x.startswith("--append-system-prompt="))
assert "Use tabs." in sysp and sysp.endswith("One migration per task."), "instructions follow the persona"
assert prompts[1].endswith("Use the `rk:systematic-debugging` skill.\n\n"
                             + agent.OFFER_SKILLS.format(names="`rk:repo-recon`")), "never the catalog's, never twice"
assert "One migration" not in "".join(task_argv), "a slot's instructions are its own"
job_id = a["job"]
agent.finish("o/r", "ok")
with agent.closing(agent.db()) as con:
    logged = [json.loads(d) for w, d in con.execute("SELECT what, detail FROM steps WHERE job = ?", (job_id,))]
assert logged[0]["sources"] == {"verify": ".rookery/skills", "repo-recon": ".claude/skills",
                                "systematic-debugging": "catalog"}, logged[0]
assert [(s["agent"], s["skills"], s["model"]) for s in logged[1:]] == [
    ("planmaker", ["repo-recon"], "opus"), (None, ["verify", "systematic-debugging"], "sonnet")], logged

def pin_error(files):
    if files:
        merge(files)
    agent.begin(pc, "plan", "t")
    try:
        agent.pin(pc)
    except RuntimeError as e:
        return str(e)
    finally:
        agent.finish("o/r", "failed")
wf_error = lambda wf: pin_error({".rookery/workflow.json": wf if isinstance(wf, str) else json.dumps(wf)})
assert "not valid JSON" in wf_error("{"), "an invalid file fails, never falls back to the default"
assert "top level" in wf_error({"before": []})
assert "at most 5 steps" in wf_error({"after": [{"agent": f"a{i}", "mode": "check"} for i in range(6)]})
assert "needs an `agent` and a `mode`" in wf_error({"after": [{"agent": "reviewer"}]})
assert "needs an `agent` and a `mode`" in wf_error({"after": [{"agent": "reviewer", "mode": ["check"]}]})
assert "needs an `agent` and a `mode`" in wf_error({"after": [{"mode": "write"}]})
assert "`after[0]` is an object with only" in wf_error({"after": [{"agent": "reviewer", "mode": "check", "x": 1}]})
assert "names an agent twice" in wf_error({"after": [{"agent": "reviewer", "mode": "check"},
                                                    {"agent": "reviewer", "mode": "write"}]})
assert "`after[0]` names the agent `ghost`" in wf_error({"after": [{"agent": "ghost", "mode": "check"}]})
assert "`after[0]` names the skill" in wf_error({"after": [{"agent": "reviewer", "mode": "check", "skills": ["nope"]}]})
assert "a list of names" in wf_error({"task": {"skills": "verify"}})
assert "must match" in wf_error({"task": {"skills": ["../../etc"]}})
assert "must match" in wf_error({"plan": {"agent": ""}})
assert "no skill by that name" in wf_error({"task": {"skills": ["test-drivn-development"]}})
assert "no agent by that name" in wf_error({"review": {"agent": "ghost"}}), "--agent would ignore it silently"
assert "`pr-review` names the agent `ghost`" in wf_error({"pr-review": {"agent": "ghost"}})
assert "`pr-review` names the skill" in wf_error({"pr-review": {"skills": ["nope"]}})
assert "plus the `plan` instructions" in wf_error({"plan": {"instructions": "x" * (64 * 1024)}})
merge({".rookery/workflow.json": "{}"})
assert "64 KB" in pin_error({"CLAUDE.md": "x" * (64 * 1024 + 1)}), "an oversized CLAUDE.md fails at start"
assert "`hooks`" in pin_error({"CLAUDE.md": "ok\n", ".rookery/skills/verify/SKILL.md":
                               "---\nname: verify\nhooks:\n  PreToolUse: []\n---\n"}), "skill hooks rejected"
assert "must match" in pin_error({".claude/skills/verify/SKILL.md": "---\nname: verify\n---\n",
                                  ".claude/skills/Bad_Name/SKILL.md": "---\nname: x\n---\n"})
assert not list((agent.WORKSPACE / "plugins").rglob("job-*")), "a failed job's plugin dir is gone too"
# frontmatter: an allowlist of top-level keys, however the YAML is laid out
fm = agent.WORKSPACE / "x.md"
def fm_error(text, keys=agent.SKILL_KEYS):
    fm.write_text(text, encoding="utf-8")
    try:
        agent.parse_agent(fm, "x") if keys == agent.AGENT_KEYS else agent.frontmatter(fm, keys, "x")
    except RuntimeError as e:
        return str(e)
assert "`hooks`" in fm_error("---\n  name: x\n  hooks:\n    Stop: []\n---\n"), "an indented top level is still top"
assert "`mcpServers`" in fm_error("---\nname: x\nmcpServers: {}\n---\n", agent.AGENT_KEYS)
assert "`permissionMode`" in fm_error("---\nname: x\npermissionMode: bypassPermissions\n---\n", agent.AGENT_KEYS)
assert "`model`" in fm_error("---\nname: x\nmodel: gpt-5\n---\n", agent.AGENT_KEYS)
assert fm_error("---\nname: x\nargument-hint: [name]\ndisable-model-invocation: true\n---\n") is None
assert "`context`" in fm_error("---\nname: x\ncontext: fork\n---\n")
assert "filename" in fm_error("---\nname: y\n---\n", agent.AGENT_KEYS)
assert fm_error("---\nname: x\ndescription: >\n  hooks: not a key\nskills:\n- a\n---\n", agent.AGENT_KEYS) is None

# .rookery/agents on base: added to the plugin, or replacing a built-in role by name
merge({".claude/skills/Bad_Name/SKILL.md": None, ".rookery/skills/verify/SKILL.md": "---\nname: verify\n---\n"})
assert "`hooks`" in pin_error({".rookery/agents/sec.md": "---\nname: sec\nhooks:\n  Stop: []\n---\n"}), \
    "a repo agent is held to the same allowlist"
assert "filename" in pin_error({".rookery/agents/sec.md": "---\nname: other\n---\n"})
merge({".rookery/agents/sec.md": "---\nname: sec\nmodel: haiku\n---\nfind holes\n",
       ".rookery/agents/docs.md": "---\nname: docs\n---\nwrite docs\n",
       ".rookery/agents/reviewer.md": "---\nname: reviewer\nmodel: sonnet\n---\nthe repo's reviewer\n",
       ".rookery/agents/notes.txt": "not an agent\n", ".rookery/agents/sub/x.md": "---\nname: x\n---\n",
       ".rookery/workflow.json": json.dumps({"after": [
           {"agent": "sec", "mode": "check", "skills": ["verify"], "instructions": "OWASP only."},
           {"agent": "docs", "mode": "write"}]})})
agent.RUN_CAP = 99
agent.begin(pc, "implement")
agent.pin(pc)
a = agent.active["o/r"]
assert sorted(p.name for p in (a["plugin"] / "agents").iterdir()) == ["docs.md", "planmaker.md", "pr-reviewer.md",
                                                                        "reviewer.md", "scope-checker.md", "sec.md"], "top-level *.md only"
assert (a["plugin"] / "agents" / "reviewer.md").read_text(encoding="utf-8").endswith("the repo's reviewer\n")
assert a["agents"]["reviewer"]["model"] == "sonnet" and a["agent_sources"]["planmaker"] == "built-in"
argv[:], prompts[:] = [], []
agent.sh = lambda *a, **k: argv.append(a) or prompts.append(k["input"]) or json.dumps({"result": "STATUS: check-ok"})
assert agent.claude(pc, "p", key="step-sec", slot=a["workflow"]["after"][0]) == ("check-ok", "")
agent.sh = real_sh
assert argv[0][argv[0].index("--agent") + 1] == "rk:sec" and prompts[0] == "p\n\nUse the `rk:verify` skill.\n\n" + agent.OFFER_SKILLS.format(names="`rk:repo-recon`")
assert next(x for x in argv[0] if x.startswith("--append-system-prompt=")).endswith("OWASP only.")
with agent.closing(agent.db()) as con:
    based = json.loads(con.execute("SELECT detail FROM steps WHERE job = ? AND what = 'base'",
                                   (a["job"],)).fetchone()[0])
assert based["agents"] == ["docs", "reviewer", "sec"], "the repo's agents are logged"

# after steps: between the task loop and the review; a check may not edit, a write is committed
git(clone, "config", "user.email", "t@t"), git(clone, "config", "user.name", "t")
wt = agent.wt_root("o/r") / "issue-3"
git(clone, "fetch", "-q", "origin", "main"), git(clone, "worktree", "add", "-q", "-B", "feature/issue-3", str(wt), "origin/main")
agent.worktree = lambda c: wt
(wt / agent.pdir(3)).mkdir(parents=True)
(wt / agent.pdir(3) / "tasks.md").write_text("- [x] one\n", encoding="utf-8")
(wt / agent.pdir(3) / "plan.md").write_text("# Plan\n\nDo it.\n", encoding="utf-8")
(wt / "CLAUDE.md").write_text("Use spaces.\n", encoding="utf-8")   # the branch changes what steers the agent
git(wt, "add", "-A"), git(wt, "commit", "-qm", "#3 task 1: one")
said, prs, phases, calls = [], [], [], []
def fake_claude(c, prompt, key="plan", resume=False, slot="task", per_job=False):
    calls.append(key)
    assert per_job, "the review and after steps never resume an earlier job's session"
    if key == "step-sec":
        assert prompt.startswith("Check the finished work") and slot["agent"] == "sec"
        (wt / "CLAUDE.md").write_text("tampered\n", encoding="utf-8")
        (wt / "scratch.txt").write_text("x", encoding="utf-8")
        return "check-ok", '<review>{"summary": "No holes.", "findings": []}</review>'
    if key == "step-docs":
        (wt / "docs.md").write_text("docs\n", encoding="utf-8")
        return "step-done", "wrote docs"
    (wt / "review.txt").write_text("x", encoding="utf-8")   # the reviewer is read-only too
    return "review-ok", '<review>{"summary": "Fine.", "findings": [{"severity": "low", "title": "nit"}]}</review>'
real_gh, real_claude = agent.gh, agent.claude
agent.claude, agent.comment = fake_claude, lambda c, body: said.append(body)
agent.set_phase = lambda c, phase: phases.append(phase)
pr_open, pr_said = [], []
def fake_gh(repo, *a, input=None, **k):
    if a[:2] == ("pr", "view"):   # open once created: a second approve must not create again
        return "https://github.com/o/r/pull/1" if pr_open else ""
    if a[:2] == ("pr", "create"):
        assert not pr_open, "gh refuses a second PR for the branch"
        pr_open.append(1)
        return prs.append(input) or "https://github.com/o/r/pull/1"
    if a[:2] == ("pr", "comment"):
        return pr_said.append((a[2], input)) or ""
    return json.dumps({"title": "t"})
agent.gh = fake_gh
agent.sh = lambda *a, **k: "" if a[:2] == ("git", "push") else real_sh(*a, **k)
agent.implement(pc)
subjects = git(wt, "log", "--format=%s", "origin/main..HEAD").splitlines()
assert subjects == ["#3 step docs", "#3 task 1: one"], subjects
assert (wt / "CLAUDE.md").read_text(encoding="utf-8") == "Use spaces.\n" and not (wt / "scratch.txt").exists(), \
    "a check's edits are reverted"
assert not (wt / "review.txt").exists() and not git(wt, "status", "--porcelain"), "nothing left for a later commit"
body_ = prs[0]
assert body_.startswith(agent.CONFIG_WARNING), "the branch touches CLAUDE.md"
assert "## 🤖 Sec agent · check · rk:verify\n\n### 🟢 Looks good\n\n✅ **No findings**\n\nNo holes.\n\n<sub>rk:sec · rk:verify</sub>\n\n" \
       "_Rookery reverted" in body_, body_
assert "## 🤖 Docs agent · write\n\nwrote docs" in body_, body_
assert body_.index("Sec agent") < body_.index("Docs agent") < body_.index("## 🤖 Reviewer agent")
assert "## 🤖 Reviewer agent\n\n### 🟢 Looks good\n\n🔎 **1 finding** · ⚪ 1 low\n\nFine.\n\n| # |" in body_ and "<sub>rk:reviewer</sub>" in body_, \
    "the reviewer's JSON is rendered like a PR review, one heading level down"
assert body_.endswith(" · steps: sec, docs"), body_[-80:]
assert "<details><summary>📋 Plan</summary>\n\n# Plan\n\nDo it.\n\n</details>" in body_, "the plan leads the PR, folded"
assert phases[-1] == "rookery:changes-wanted" and "Changes wanted by sec" in said[-1], "an edit in a check is flagged"
# approve again: the committed write step is skipped, the check runs again
calls.clear()
agent.implement(pc)
assert calls == ["step-sec", "review"], calls
assert len(prs) == 1 and pr_said[0][0] == "https://github.com/o/r/pull/1" and "Sec agent" in pr_said[0][1], \
    "the re-run's sections go on the open PR"
assert "📋 Plan" not in pr_said[0][1], "the plan is in the PR body once, not in every comment"
# STATUS is held to the step's mode
for mode, status in [("check", "step-done"), ("write", "check-ok"), ("write", "failed")]:
    agent.claude = lambda c, p, key="plan", resume=False, slot="task", **k: (status, "x")
    try:
        agent.after_steps(pc, wt, [{**agent.STEP, "agent": "z", "mode": mode}])
        raise AssertionError(f"{mode} accepted {status}")
    except RuntimeError as e:
        assert "step z" in str(e), e
agent.claude, agent.gh, agent.sh = real_claude, real_gh, real_sh
agent.finish("o/r", "ok")

# a PR's worktree: its head, detached, beside the issue worktrees, with its base fetched for the diff
git(up, "checkout", "-q", "-b", "feat")
(up / "feat.txt").write_text("x\n", encoding="utf-8")
git(up, "add", "-A"), git(up, "commit", "-qm", "feat"), git(up, "push", "-q", "origin", "HEAD:refs/pull/7/head")
git(up, "checkout", "-q", "main")
pc7 = agent.Ctx("o/r", 7, clone, "main")
wt7 = agent.pr_worktree(pc7, "main")
assert wt7 == agent.wt_root("o/r") / "pr-7" and git(wt7, "diff", "--name-only", "origin/main...HEAD").split() == ["feat.txt"]
assert git(wt7, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD", "detached: nothing to commit to"
(wt7 / "junk.txt").write_text("x", encoding="utf-8")
assert agent.pr_worktree(pc7, "main") == wt7 and not (wt7 / "junk.txt").exists(), "a leftover is replaced"
# claude(wt=) runs there, and never makes an issue-<n> worktree for the PR's number
agent.worktree, cwds = lambda c: (_ for _ in ()).throw(AssertionError("an issue worktree for a PR")), []
agent.sh = lambda *a, **k: cwds.append(k.get("cwd")) or json.dumps({"result": "STATUS: review-ok"})
agent.claude(pc7, "p", key="pr-review", slot="pr-review", per_job=True, wt=wt7)
agent.sh = real_sh
assert cwds == [wt7], cwds
agent.drop_pr_worktree(pc7)
assert not wt7.exists() and "pr-7" not in git(clone, "worktree", "list")
# the review prompt carries the diff whole, minus lockfiles, cut at a file boundary past DIFF_CAP
git(up, "checkout", "-q", "-b", "feat17")
for name, text in {"a.txt": "a\n", "b.txt": "b" * 300 + "\n", "c.txt": "c\n", "package-lock.json": "{}\n",
                   "web/yarn.lock": "x\n"}.items():
    (up / name).parent.mkdir(exist_ok=True)
    (up / name).write_text(text, encoding="utf-8")
git(up, "add", "-A"), git(up, "commit", "-qm", "feat17"), git(up, "push", "-q", "origin", "HEAD:refs/pull/17/head")
git(up, "checkout", "-q", "main")
pc17 = agent.Ctx("o/r", 17, clone, "main")
wt17 = agent.pr_worktree(pc17, "main")
d17 = agent.pr_diff(wt17, "main")
assert "package-lock.json |" in d17 and "+{}" not in d17 and "+x" not in d17, "the stat names lockfiles, the diff skips them"
assert "+a\n" in d17 and "+c\n" in d17 and "read them with Read" not in d17, d17
real_cap, agent.DIFF_CAP = agent.DIFF_CAP, 400
d17 = agent.pr_diff(wt17, "main")
agent.DIFF_CAP = real_cap
assert "+a\n" in d17 and "+bbb" not in d17 and "+c\n" not in d17, "cut whole files, at the first that does not fit"
assert d17.endswith("read them with Read:\n\n- b.txt\n- c.txt"), d17[-120:]
agent.drop_pr_worktree(pc17)

# <owner>/.rookery: the owner's defaults, under the repo's own config and over the built-in
oup = agent.WORKSPACE / "org-up"
git(agent.WORKSPACE, "init", "-q", "--bare", "-b", "main", str(agent.WORKSPACE / "org.git"))
git(agent.WORKSPACE, "clone", "-q", str(agent.WORKSPACE / "org.git"), str(oup))
git(oup, "config", "user.email", "t@t"), git(oup, "config", "user.name", "t")
def org_merge(files):
    for name, text in files.items():
        (oup / name).parent.mkdir(parents=True, exist_ok=True)
        (oup / name).write_text(text, encoding="utf-8")
    git(oup, "add", "-A"), git(oup, "commit", "-qm", "x"), git(oup, "push", "-q", "origin", "main")
org_merge({"workflow.json": json.dumps({"plan": {"instructions": "Org rule."}, "review": {"agent": "auditor"},
                                        "after": [{"agent": "auditor", "mode": "check"}]}),
           "agents/auditor.md": "---\nname: auditor\nmodel: haiku\n---\naudit\n",
           "agents/sec.md": "---\nname: sec\n---\nthe owner's sec\n",
           "skills/house-style/SKILL.md": "---\nname: house-style\n---\nours\n",
           "skills/verify/SKILL.md": "---\nname: verify\n---\nthe owner's verify\n"})
agent.REPOS["o/.rookery"] = ""
assert "still cloning o/.rookery" in pin_error({}), "never a run without the owner's defaults"
git(agent.WORKSPACE, "clone", "-q", str(agent.WORKSPACE / "org.git"), str(agent.clone_dir("o/.rookery")))
agent.REPOS["o/.rookery"] = "main"
agent.begin(pc, "implement")
agent.pin(pc)
a = agent.active["o/r"]
assert a["workflow"]["plan"]["instructions"] == "Org rule." and a["workflow"]["review"]["agent"] == "auditor", \
    "a slot the repo leaves out comes from the owner"
assert [s["agent"] for s in a["workflow"]["after"]] == ["sec", "docs"], "the repo's own `after` replaces the owner's"
assert a["agent_sources"]["auditor"] == "o/.rookery" and a["agent_sources"]["sec"] == ".rookery/agents"
assert (a["plugin"] / "agents" / "sec.md").read_text(encoding="utf-8").endswith("find holes\n"), "repo beats owner"
assert a["sources"]["house-style"] == "o/.rookery" and a["sources"]["verify"] == ".rookery/skills"
with agent.closing(agent.db()) as con:
    based = json.loads(con.execute("SELECT detail FROM steps WHERE job = ? AND what = 'base'",
                                   (a["job"],)).fetchone()[0])
assert based["org"] == "o/.rookery@" + git(oup, "rev-parse", "HEAD").strip(), "the owner's sha is logged too"
agent.finish("o/r", "ok")
org_merge({"workflow.json": json.dumps({"plan": {"agent": "ghost"}})})
assert pin_error({}).startswith("workflow.json in o/.rookery: `plan` names the agent `ghost`"), \
    "read fresh, and the error names the owner's file"
org_merge({"agents/auditor.md": "---\nname: auditor\nhooks: {}\n---\n", "workflow.json": "{}"})
assert pin_error({}).startswith("agents/auditor.md in o/.rookery: frontmatter `hooks`")
del agent.REPOS["o/.rookery"]

# the portal changes config only through a PR: a commit built off to the side, checked by steer()
assert agent.config_dir("o/r") == ".rookery/" and agent.config_dir("o/.rookery") == ""
got = agent.config_files("o/r")
assert sorted(got) == ["agents/docs.md", "agents/reviewer.md", "agents/sec.md", "workflow.json"], got
assert got["agents/sec.md"] == "---\nname: sec\nmodel: haiku\n---\nfind holes\n", "byte for byte, not stripped"
pushed, prs = [], []
agent.sh = lambda *a, **k: pushed.append(a[-1]) or "" if a[:2] == ("git", "push") else real_sh(*a, **k)
agent.gh = lambda repo, *a, input=None, **k: prs.append((a, input)) or "https://github.com/o/r/pull/9"
before = git(clone, "status", "--porcelain"), git(clone, "rev-parse", "HEAD")
def propose_error(files):
    try:
        agent.propose("o/r", files, "alice")
    except (RuntimeError, ValueError) as e:
        return str(e)
assert "only workflow.json and agents/<name>.md" in propose_error({"CLAUDE.md": "x"})
assert "only workflow.json" in propose_error({"agents/../../x.md": "x"})
assert "no agent by that name" in propose_error({"agents/docs.md": None}), "the workflow still names docs"
assert "`hooks`" in propose_error({"agents/qa.md": "---\nname: qa\nhooks: {}\n---\n"})
assert "already what main has" in propose_error({"agents/sec.md": got["agents/sec.md"]})
assert not pushed and not prs, "nothing that fails the check leaves the box"
url = agent.propose("o/r", {"agents/qa.md": "---\nname: qa\n---\nTest it.\n", "agents/docs.md": None,
                            "workflow.json": json.dumps({"after": [{"agent": "qa", "mode": "check"}]})}, "alice")
assert url == "https://github.com/o/r/pull/9" and len(pushed) == 1, pushed
commit, branch = pushed[0].split(":refs/heads/")
assert branch.startswith("rookery/config-"), branch
assert git(clone, "show", f"{commit}:.rookery/agents/qa.md") == "---\nname: qa\n---\nTest it.\n"
assert "docs.md" not in git(clone, "ls-tree", "--name-only", f"{commit}:.rookery/agents")
assert git(clone, "rev-parse", f"{commit}^").strip() == git(up, "rev-parse", "HEAD").strip(), "on a fresh base"
assert (git(clone, "status", "--porcelain"), git(clone, "rev-parse", "HEAD")) == before, "the clone is untouched"
args, pr_body = prs[0]
assert args[:2] == ("pr", "create") and "--draft" not in args and args[args.index("--head") + 1] == branch
assert pr_body.startswith(agent.CONFIG_WARNING) and "@alice" in pr_body and "- deletes `.rookery/agents/docs.md`" in pr_body
agent.sh, agent.gh = real_sh, real_gh

# cross-repo: the home lists companions in `with`; one plan, one approve, a draft PR per repo that changed
x_saved = agent.worktree, agent.comment, agent.set_phase, agent.can_write, agent.labels_of, agent.REPOS
agent.WORKSPACE, agent.worktree = Path(tempfile.mkdtemp()), real_worktree
g = lambda cwd, *a: subprocess.run(("git",) + a, cwd=cwd, capture_output=True, check=True, text=True).stdout.strip()
def remote(name):
    """o/<name>: a bare origin, a maintainer's clone that merges to main, and Rookery's clone."""
    origin_, up_, clone_ = agent.WORKSPACE / f"{name}.git", agent.WORKSPACE / f"{name}-up", agent.clone_dir(f"o/{name}")
    g(agent.WORKSPACE, "init", "-q", "--bare", "-b", "main", str(origin_))
    for d_ in (up_, clone_):
        g(agent.WORKSPACE, "clone", "-q", str(origin_), str(d_))
        g(d_, "config", "user.email", "t@t"), g(d_, "config", "user.name", "t")
    land(up_, {"app.txt": name + "\n"})
    return up_
def land(up_, files):
    for name, text in files.items():
        (up_ / name).parent.mkdir(parents=True, exist_ok=True)
        (up_ / name).unlink() if text is None else (up_ / name).write_text(text, encoding="utf-8")
    g(up_, "add", "-A"), g(up_, "commit", "-q", "--allow-empty", "-m", "x"), g(up_, "push", "-q", "origin", "HEAD:main")
bup, fup = remote("back"), remote("front")
agent.REPOS = {"o/back": "main", "o/front": "main", "o/shop": "main", "x/web": "main"}
hc = agent.Ctx("o/back", 12, agent.clone_dir("o/back"), "main")
def with_error(wf):
    land(bup, {".rookery/workflow.json": wf if isinstance(wf, str) else json.dumps(wf)})
    agent.begin(hc, "plan", "t")
    try:
        agent.pin(hc)
    except RuntimeError as e:
        return str(e)
    finally:
        agent.finish("o/back", "failed")
assert "another owner" in with_error({"with": ["x/web"]})
assert "is the repo itself" in with_error({"with": ["o/back"]})
assert "not a repo Rookery is installed on" in with_error({"with": ["o/nope"]})
assert "not a repo Rookery is installed on" in with_error({"with": ["o/.rookery"]})
for bad in (["o/a", "o/b", "o/c", "o/d"], "o/front", ["o/front", "o/front"], ["front"], [1]):
    assert "`with` is a list of at most 3 different repos" in with_error({"with": bad}), bad
agent.REPOS["o/front"] = ""
assert "still cloning o/front" in with_error({"with": ["o/front"]}), "never a run without a companion"
agent.REPOS["o/front"] = "main"
assert agent.workflow("{}", "x", agent.workflow('{"with": ["o/front"]}', "y"))["with"] == [], "never the owner layer's"
# a companion's skills and agents join the plugin; a name the home already has stays the home's
land(fup, {"CLAUDE.md": "Frontend rule.\n", ".rookery/agents/stylist.md": "---\nname: stylist\nmodel: haiku\n---\ncss\n",
           ".rookery/agents/reviewer.md": "---\nname: reviewer\n---\nthe frontend's reviewer\n",
           ".rookery/skills/ui-kit/SKILL.md": "---\nname: ui-kit\n---\nfront ui\n",
           ".claude/skills/ui-kit/SKILL.md": "---\nname: ui-kit\n---\nits own second source\n",
           ".claude/skills/verify/SKILL.md": "---\nname: verify\n---\nfront verify\n",
           ".rookery/agents/bad.md": "---\nname: bad\nhooks: {}\n---\n"})
assert with_error({"with": ["o/front"]}).startswith("agents/bad.md in o/front: frontmatter `hooks`"), \
    "a companion is held to the same allowlist"
land(fup, {".rookery/agents/bad.md": None})
land(bup, {"CLAUDE.md": "Backend rule.\n", ".claude/skills/verify/SKILL.md": "---\nname: verify\n---\nback verify\n",
           ".rookery/workflow.json": json.dumps({"with": ["o/front"], "after": [{"agent": "stylist", "mode": "check"}]})})
agent.begin(hc, "plan", "t")
agent.pin(hc)
a = agent.active["o/back"]
assert a["agent_sources"]["stylist"] == "with:o/front", "a home step may name a companion's agent"
assert a["agent_sources"]["reviewer"] == "built-in" and \
    (a["plugin"] / "agents" / "reviewer.md").read_text(encoding="utf-8") == (agent.HERE / "agents" / "reviewer.md").read_text(encoding="utf-8")
assert a["sources"]["ui-kit"] == "with:o/front" and a["sources"]["verify"] == ".claude/skills"
assert (a["plugin"] / "skills" / "ui-kit" / "SKILL.md").read_text(encoding="utf-8").endswith("front ui\n")
assert (a["plugin"] / "skills" / "verify" / "SKILL.md").read_text(encoding="utf-8").endswith("back verify\n")
assert a["skipped"] == ["reviewer (o/front)", "verify (o/front)"], a["skipped"]
with agent.closing(agent.db()) as con:
    based = json.loads(con.execute("SELECT detail FROM steps WHERE job = ? AND what = 'base'", (a["job"],)).fetchone()[0])
assert based["with"] == ["o/front@" + g(fup, "rev-parse", "HEAD")] and based["skipped"] == a["skipped"], based
assert agent.mates(hc) == [agent.Ctx("o/front", 12, agent.clone_dir("o/front"), "main")]

# named(): the owner's repos an issue names that this job does not mount — a regex, never the model
assert agent.named(hc, "Needs o/shop and https://github.com/o/shop-ui/pull/3, also o/ops#4 and O/Ops.\n"
                       "Not facebook/react#1, o/back, o/front, o/.rookery, shop, src/o/x or me@o/y.\n"
                       "> o/quoted\n\n```\no/fenced\n```\n") == ["o/shop", "o/shop-ui", "o/ops"]
assert agent.named(hc, "see github.com/O/SHOP.") == ["o/shop"], "spelled as REPOS spells it"

# plan(): companions mounted, the notice before the plan call, nothing written in a companion
said, phases, ghs, pushed, prompts, argv, comments = [], [], [], [], [], [], ""
agent.comment = lambda c, b: said.append(b) or ghs.append(("comment",))
agent.set_phase = lambda c, p: phases.append(p)
def fake_gh(repo, *a, input=None, check=True):
    ghs.append((repo, a, input))
    if a[:2] == ("issue", "view"):
        return comments if "comments" in a else json.dumps({"title": "Add discount"})
    if a[:2] == ("pr", "create"):
        return f"https://github.com/{repo}/pull/{sum(1 for x in ghs if x[1:2] and x[1][:2] == ('pr', 'create'))}"
    return ""
agent.gh = fake_gh
fwt = lambda n: agent.wt_root("o/front") / f"back-issue-{n}"
bwt = lambda n: agent.wt_root("o/back") / f"issue-{n}"
out12 = lambda: agent.wt_root("o/back") / "plans-12"
replies = {}
def fake_sh(*a, **k):
    if a[:2] == ("git", "push"):
        return pushed.append((a[2], a[3])) or ""
    if a[0] != "claude":
        return real_sh(*a, **k)
    argv.append(a), prompts.append(k["input"])
    kind = next(x for x in replies if k["input"].startswith(x))
    return json.dumps({"result": replies[kind](k["cwd"])})
def planned(cwd):
    noticed.append(len(said))
    (fwt(12) / "app.txt").write_text("planner was here\n", encoding="utf-8")
    (Path(cwd) / "stray.txt").write_text("x", encoding="utf-8")
    (out12() / "tasks.md").write_text("- [ ] change both\n- [ ] backend only\n", encoding="utf-8")
    (out12() / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return "Plan.\n\n## Not in this plan\n\n- o/shop: a page\n\nSTATUS: plan-ready"
replies["You are planning"], noticed = planned, []
agent.sh, comments = fake_sh, "earlier\n<!-- rookery:with o/admin -->"
agent.plan(hc, "Add discount", "Show it on o/front and o/shop. o/admin too.", resume=False)
assert fwt(12).exists() and g(fwt(12), "rev-parse", "--abbrev-ref", "HEAD") == "feature/back-issue-12", \
    "never feature/issue-12: the companion has its own issue 12"
assert (fwt(12) / "app.txt").read_text(encoding="utf-8") == "front\n" and not g(fwt(12), "status", "--porcelain"), \
    "planning changes no companion"
assert not (bwt(12) / "stray.txt").exists()
assert len(said) == 2 and said[0].startswith("This issue names `o/shop`, but Rookery can change only `o/back`"), said
assert '`"with": ["o/front", "o/shop"]`' in said[0] and "installations/new" not in said[0], said[0]
assert said[0].endswith("<!-- rookery:with o/shop -->") and "o/admin" not in said[0], "o/admin was noticed before"
assert noticed == [1], "the notice went out before the plan call"
assert "The issue also names `o/shop`, `o/admin`. Rookery cannot change them" in prompts[0], "both, noticed or not"
assert f"- `o/front` at `{fwt(12).absolute()}`, base `main`." in prompts[0] and agent.WITH_PLAN in prompts[0]
# prod (Mantis#88): "Change a repo only when the work needs it" alone, the planner left out the
# companion's button and modal the issue described, without asking
assert "plan that part in\nits repo too" in prompts[0] and "when\nyou cannot tell, ask" in prompts[0]
assert f"--add-dir={fwt(12).absolute()}" in argv[0]
sysp = next(x for x in argv[0] if x.startswith("--append-system-prompt="))
assert sysp.index("Backend rule.") < sysp.index("Contents of CLAUDE.md in o/front") < sysp.index("Frontend rule."), sysp
said.clear(), argv.clear(), prompts.clear()
agent.REPOS.pop("o/shop")   # the same owner, no App on it: installing it is the first step
comments = ""
agent.notice(hc, ["o/shop"])
assert "install the Rookery App on `o/shop` (https://github.com/apps/rookery-gha/installations/new), then add it" in said[0], said
said.clear()
agent.finish("o/back", "ok")

# implement(): a commit per repo that changed, the companion's under the home's full name
can = {"bob"}
agent.can_write = lambda repo, user: user in can or repo == "o/back"
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
def task(cwd):
    (Path(cwd) / "app.txt").write_text(f"back change {len(prompts)}\n", encoding="utf-8")
    if "change both" in prompts[-1]:
        (fwt(12) / "app.txt").write_text("front changed\n", encoding="utf-8")
    return "Did it.\nSTATUS: task-done"
def check_(cwd):
    (fwt(int(Path(cwd).name.split("-")[1])) / "app.txt").write_text("a check edited this\n", encoding="utf-8")
    return 'STATUS: check-ok\n<review>{"summary": "Looks styled.", "findings": []}</review>'
replies.update({"You are implementing": task, "Check the finished": check_,
                "Review the finished": lambda cwd: 'STATUS: review-ok\n<review>{"summary": "Fine.", "findings": []}</review>'})
said.clear(), ghs.clear()
agent.handle({"kind": "continue", "repo": "o/back", "n": 12, "body": "approve", "by": "carol"})
assert not argv and phases[-1] == "rookery:awaiting-approval", "a home-only writer cannot approve; the phase stays"
assert said == ["`approve` needs write access on `o/front` too: this issue can change that repo (`with` in "
                "`.rookery/workflow.json`). Ask someone with write access on every one of them."], said
agent.handle({"kind": "continue", "repo": "o/back", "n": 12, "body": "approve", "by": "bob"})
assert g(bwt(12), "log", "--format=%s", "origin/main..HEAD").splitlines() == ["#12 task 2: backend only", "#12 task 1: change both"]
assert g(fwt(12), "log", "--format=%s", "origin/main..HEAD").splitlines() == ["o/back#12 task 1: change both"], \
    "the companion commits only what changed in it, under the home's full name"
assert (fwt(12) / "app.txt").read_text(encoding="utf-8") == "front changed\n", "the check's edit is reverted"
assert all(f"--add-dir={fwt(12).absolute()}" in x for x in argv) and all("Its change is" in p for p in prompts)
assert not any(agent.WITH_PLAN in p for p in prompts), "the scope rule is planning's: a task changes only what its task says"
assert pushed == [("https://github.com/o/front.git", "feature/back-issue-12:feature/back-issue-12"),
                  ("https://github.com/o/back.git", "feature/issue-12:feature/issue-12")], "the companion first"
assert not agent.unpushed(agent.clone_dir("o/front"), "feature/back-issue-12"), "its tracking ref is recorded"
creates = [(r, a_, i) for r, a_, i in [x for x in ghs if len(x) == 3] if a_[:2] == ("pr", "create")]
(fr, fa, fbody), (br, ba, bbody) = creates
assert fr == "o/front" and "--draft" in fa and fa[fa.index("--head") + 1] == "feature/back-issue-12"
assert fbody.startswith("Part of o/back#12. Merge it together with the pull request for o/back#12.") and "Closes" not in fbody
assert "- o/back#12 task 1: change both" in fbody
assert br == "o/back" and "## Companion PRs\n\nMerge these together with this one.\n\n- `o/front`: https://github.com/o/front/pull/1" in bbody
assert "o/front:app.txt" in bbody and "Closes #12" in bbody, "a check's companion edit is named with its repo"
edits = [(r, a_, i) for r, a_, i in [x for x in ghs if len(x) == 3] if a_[:2] == ("pr", "edit")]
assert edits == [("o/front", ("pr", "edit", "https://github.com/o/front/pull/1", "--body-file", "-"),
                  fbody.replace("the pull request for o/back#12", "https://github.com/o/back/pull/2"))], edits
assert said[-1].startswith("Draft PR: https://github.com/o/back/pull/2\nAnd in `o/front`: https://github.com/o/front/pull/1")
assert phases[-1] == "rookery:changes-wanted"
with agent.closing(agent.db()) as con:
    logged = [(w, json.loads(d)) for w, d in con.execute(
        "SELECT what, detail FROM steps WHERE what IN ('commit', 'push', 'pr') AND job = (SELECT MAX(id) FROM jobs WHERE act = 'implement')")]
assert [(w, d.get("repo")) for w, d in logged] == [("commit", None), ("commit", "o/front"), ("commit", None),
                                                   ("push", "o/front"), ("push", None), ("pr", "o/front"), ("pr", None)], logged

# a companion with nothing for this issue: no push, no PR, no section
hc13 = hc._replace(n=13)
(agent.wt_root("o/back") / "plans-13").mkdir()
(agent.wt_root("o/back") / "plans-13" / "tasks.md").write_text("- [ ] backend only\n", encoding="utf-8")
pushed.clear(), ghs.clear(), said.clear()
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
agent.handle({"kind": "continue", "repo": "o/back", "n": 13, "body": "approve", "by": "bob"})
assert fwt(13).exists() and pushed == [("https://github.com/o/back.git", "feature/issue-13:feature/issue-13")], pushed
assert [x[0] for x in ghs if len(x) == 3 and x[1][:2] == ("pr", "create")] == ["o/back"]
assert "Companion PRs" not in next(x[2] for x in ghs if len(x) == 3 and x[1][:2] == ("pr", "create"))
drain()   # the memory jobs: tested elsewhere

# a follow-up brings each companion to its branch's head; one never pushed starts from base
agent.sh = real_sh
agent.begin(hc, "follow-up")
agent.pin(hc)
head12 = g(fwt(12), "rev-parse", "HEAD")
assert agent.at_head(agent.mates(hc)[0], hc) == fwt(12) and g(fwt(12), "rev-parse", "HEAD") == head12, "never reset"
hc14 = hc._replace(n=14)
assert agent.at_head(agent.mates(hc14)[0], hc14) == fwt(14) and \
    g(fwt(14), "rev-parse", "HEAD") == g(agent.clone_dir("o/front"), "rev-parse", "origin/main")
agent.finish("o/back", "ok")

# retention: the home's PR closed takes the companions' worktrees too, each behind its own unpushed() check
(fwt(13) / "app.txt").write_text("local only\n", encoding="utf-8")
g(fwt(13), "commit", "-qam", "never pushed")
agent.sh = real_sh
agent.drop_worktree("o/back", 13)
assert not bwt(13).exists() and fwt(13).exists(), "unpushed work in a companion is kept"
agent.drop_worktree("o/back", 12)
assert not bwt(12).exists() and not fwt(12).exists() and not out12().exists()
(agent.worktree, agent.comment, agent.set_phase, agent.can_write, agent.labels_of, agent.REPOS), agent.gh = x_saved, real_gh

# run log: each job's steps land in rookery.db, so "failed at task 2 of 3" is readable
real_pin, agent.pin = agent.pin, lambda c: None   # pin() is tested above
agent.sh, agent.WORKSPACE = real_sh, Path(tempfile.mkdtemp())
wt = agent.WORKSPACE / "wt"
wt.mkdir()
git = lambda *a: subprocess.run(("git",) + a, cwd=wt, capture_output=True, check=True)
git("init", "-q", "-b", "main"), git("config", "user.email", "t@t"), git("config", "user.name", "t")
(wt / agent.pdir(5)).mkdir(parents=True)
(wt / agent.pdir(5) / "tasks.md").write_text("- [ ] one\n- [ ] two\n- [ ] three\n", encoding="utf-8")
git("add", "-A"), git("commit", "-qm", "plan")
agent.worktree = lambda c: wt
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
agent.set_phase = lambda c, phase: agent.step(c.repo, "phase", label=phase)
posted = []
agent.comment = lambda c, body: posted.append(body)
agent.REPOS = {"o/r": "main"}
real_claude = agent.claude
def fake_claude(c, prompt, key="plan", resume=False, agent_=None, **k):
    agent.step(c.repo, "claude", key=key, status="task-done" if key == "task-0" else "failed", usd=0.1)
    return ("task-done", "did it") if key == "task-0" else ("failed", "could not")
agent.claude = fake_claude
agent.handle({"kind": "continue", "repo": "o/r", "n": 5, "body": "approve"})
with agent.closing(agent.db()) as con:
    jobs = con.execute("SELECT id, repo, n, act, status FROM jobs").fetchall()
    steps = [(w, json.loads(d)) for w, d in con.execute("SELECT what, detail FROM steps ORDER BY rowid")]
assert [j[1:] for j in jobs] == [("o/r", 5, "implement", "failed")], jobs
assert [w for w, _ in steps] == ["phase", "task", "claude", "tick", "commit", "task", "claude", "error", "phase"], steps
assert steps[5][1] == {"i": 2, "of": 3, "text": "two"}, "the task it failed at, and of how many"
assert "task 2 (two) reported failed" in steps[7][1]["message"] and posted, "error logged and commented"
assert steps[8][1] == {"label": "rookery:error"}
assert not agent.active, "a finished job is no longer active"
agent.step("o/r", "claude", key="x")            # outside a job: nothing is written
with agent.closing(agent.db()) as con:
    assert con.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == len(steps)
# approve on a flagged run re-runs implement(), like approve after an error
reran, saved_impl, saved_sub = [], agent.implement, agent.submit
agent.implement, agent.submit = lambda c: reran.append(c.n), lambda job: None
agent.labels_of = lambda repo, n: {"rookery:changes-wanted"}
agent.handle({"kind": "continue", "repo": "o/r", "n": 5, "body": "approve"})
assert reran == [5], "approve on rookery:changes-wanted re-checks"
agent.implement, agent.submit = saved_impl, saved_sub
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
# local_plans: the task loop reads and ticks tasks.md beside the worktree, and commits only the code
with agent.closing(agent.db()) as con, con:
    con.execute("INSERT INTO settings VALUES ('repo:o/r', ?)", ('{"local_plans": true}',))
out6 = agent.wt_root("o/r") / "plans-6"
out6.mkdir(parents=True)
(out6 / "tasks.md").write_text("- [ ] one\n- [ ] two\n", encoding="utf-8")
seen_d, saved_pub = [], agent.publish
def fake_claude(c, prompt, key="plan", **k):
    seen_d.append(f"{out6.absolute()}/decisions.md" in prompt)
    (wt / f"{key}.txt").write_text("code", encoding="utf-8")
    return "task-done", "did it"
agent.claude, agent.publish = fake_claude, lambda c, wt_, done: seen_d.append(len(done))
agent.implement(agent.ctx("o/r", 6))
assert seen_d == [True, True, 3], seen_d   # the Tasks heading and two tasks
assert (out6 / "tasks.md").read_text(encoding="utf-8") == "- [x] one\n- [x] two\n"
assert git("show", "--name-only", "--format=", "HEAD~1", "HEAD").stdout.decode().split() == ["task-0.txt", "task-1.txt"]
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM settings WHERE key = 'repo:o/r'")
agent.publish = saved_pub
agent.claude = real_claude

# PR review: one comment-type review; a failure is a PR comment, never a phase label on the PR
ghs, apis, made, ran = [], [], [], []
prv = {"title": "Fix", "body": "", "baseRefName": "dev", "isCrossRepository": False, "state": "OPEN",
       "headRefOid": "abc1234def"}
def fake_gh(repo, *a, input=None, check=True):
    ghs.append((a, input))
    return json.dumps(prv) if a[:2] == ("pr", "view") else ""
def fake_claude(c, prompt, key="plan", resume=False, slot="task", per_job=False, wt=None):
    assert "`git diff origin/dev...HEAD`" in prompt and "THE DIFF" in prompt, prompt
    assert f"This worktree, {Path('pr-8').absolute()}, is" in prompt, "no first turn spent finding it"
    ran.append((key, slot, per_job, wt, "look at the migration" in prompt))
    return "changes-needed", '<review>{"summary": "Breaks x.", "findings": [{"severity": "high", "title": "bug in x.py"}]}</review>'
nope = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh issue … / a phase label on a PR"))
saved = (agent.gh, agent.api, agent.pr_worktree, agent.drop_pr_worktree, agent.labels_of, agent.set_phase,
         agent.comment, agent.pr_diff)
agent.gh, agent.claude, agent.labels_of, agent.set_phase, agent.comment = fake_gh, fake_claude, nope, nope, nope
agent.api = lambda m, path, bearer, data=None: apis.append((m, path, data)) or {}
agent.pr_worktree = lambda c, base: made.append(("add", base)) or Path("pr-8")
agent.drop_pr_worktree = lambda c: made.append(("drop",))
agent.pr_diff = lambda wt, base: "THE DIFF"   # tested on a real repo above
job_ = {"kind": "pr-review", "repo": "o/r", "n": 8, "title": "Fix", "by": "bob", "note": "look at the migration",
        "comment": 55, "label": True, "requested": True}
agent.handle(job_)
assert ran == [("pr-review", "pr-review", True, Path("pr-8"), True)] and made == [("add", "dev"), ("drop",)]
assert (("pr", "review", "8", "--comment", "--body-file", "-"),
        "## 🔴 Changes needed\n\n🔎 **1 finding** · 🔴 1 high\n\nBreaks x.\n\n| # | Severity | Where | Finding |\n|---|---|---|---|\n"
        "| 1 | 🔴 high |  | bug in x.py |\n\n<sub>rk:pr-reviewer</sub>\n") in ghs, ghs
assert (("pr", "edit", "8", "--remove-label", agent.REVIEW_LABEL), None) in ghs, "the label means queued or running"
assert apis == [("POST", "/repos/o/r/issues/comments/55/reactions", {"content": "eyes"}),
                ("DELETE", "/repos/o/r/pulls/8/requested_reviewers", {"reviewers": ["rookery-agent"]})], apis
assert drain()[-1]["kind"] == "memory", "a finished review teaches the repo's notes"
# the same head again, with no note: no claude call, one comment, the trigger still cleaned up
ghs.clear(), apis.clear(), made.clear(), ran.clear()
agent.handle({**job_, "note": "", "comment": None, "requested": None})
assert not ran and not made, "nothing new to look at"
assert [a[:3] for a, _ in ghs] == [("pr", "view", "8"), ("pr", "comment", "8"), ("pr", "edit", "8")], ghs
assert ghs[1][1].startswith("Already reviewed at `abc1234`."), ghs[1][1]
skipped_job = drain()[-1]   # its memory job finds no claude call to learn from: see the memory tests
# a note, or a new head, reviews again
ghs.clear()
agent.handle({**job_, "note": "and the tests?", "comment": None, "label": None, "requested": None})
prv["headRefOid"] = "fff0000"
agent.handle({**job_, "note": "", "comment": None, "label": None, "requested": None})
assert [r[4] for r in ran] == [False, False] and len(made) == 4, ran
drain()
ghs.clear(), apis.clear(), made.clear(), ran.clear()
prv["isCrossRepository"] = True
agent.handle({**job_, "comment": None, "requested": None})
assert apis == [("POST", "/repos/o/r/issues/8/reactions", {"content": "eyes"})], "no comment: 👀 on the PR"
assert not ran and not made, "a fork is refused before anything runs"
assert [a[:3] for a, _ in ghs] == [("pr", "view", "8"), ("pr", "comment", "8"), ("pr", "edit", "8")], ghs
assert "from forks" in ghs[1][1]
with agent.closing(agent.db()) as con:
    assert [r[0] for r in con.execute("SELECT status FROM jobs WHERE act = 'pr-review' ORDER BY id")] == \
        ["ok", "ok", "ok", "ok", "failed"]
(agent.gh, agent.api, agent.pr_worktree, agent.drop_pr_worktree, agent.labels_of, agent.set_phase,
 agent.comment, agent.pr_diff), agent.claude = saved, real_claude

# an owner with nothing to run on fails at pin()'s first line: no fetch, no claude call, the keys link
said, calls = [], []
nk_saved = agent.gh, agent.comment, agent.set_phase, agent.fetch, agent.claude, agent.pin
agent.pin = real_pin
agent.comment = lambda c, b: said.append(("comment", c.n, b))
agent.set_phase = lambda c, p: said.append(("phase", c.n, p))
agent.gh = lambda repo, *a, input=None, check=True: said.append(a[:3] + (input,)) or ""
agent.fetch = agent.claude = lambda *a, **k: calls.append(a)
agent.REPOS["acme/r"] = "main"
agent.handle({"kind": "start", "repo": "acme/r", "n": 4, "title": "t", "body": ""})
assert not calls and [s[:2] for s in said] == [("comment", 4), ("phase", 4)] and said[1][2] == "rookery:error", said
assert said[0][2].startswith("Agent error: no API key for **acme**. Add a key at "
                             f"{agent.PUBLIC_URL}/portal.html#keys and select it.") and "```" not in said[0][2], said
assert f"add `{agent.TRIGGER}` again" in said[0][2]
said.clear()
agent.handle({"kind": "pr-review", "repo": "acme/r", "n": 8, "note": "", "comment": None, "label": True, "requested": None})
assert not calls and [s[:3] for s in said] == [("pr", "comment", "8"), ("pr", "edit", "8")], "a PR: no label, no gh issue"
assert "portal.html#keys" in said[0][3] and f"add `{agent.REVIEW_LABEL}` again" in said[0][3]
for j in drain():   # the memory jobs find no claude call to learn from
    agent.handle(j)
assert not calls
with agent.closing(agent.db()) as con:
    assert [r[0] for r in con.execute("SELECT status FROM jobs WHERE repo = 'acme/r' ORDER BY id")] == ["failed", "failed"]
del agent.REPOS["acme/r"]
agent.gh, agent.comment, agent.set_phase, agent.fetch, agent.claude, agent.pin = nk_saved

# repo memory: digest() reads a job's transcripts back, keeping calls and errors, never contents
agent.CLAUDE_HOME = agent.WORKSPACE / "claude-home"
(agent.CLAUDE_HOME / "projects" / "-ws-o-r").mkdir(parents=True)
line = lambda kind, *content: json.dumps({"type": kind, "message": {"content": list(content)}})
(agent.CLAUDE_HOME / "projects" / "-ws-o-r" / "sid-1.jsonl").write_text("\n".join([
    line("user", {"type": "text", "text": "the task prompt"}),
    line("assistant", {"type": "text", "text": "Let me look."},
         {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}),
    line("user", {"type": "tool_result", "content": "SECRET FILE CONTENTS", "is_error": False}),
    line("assistant", {"type": "tool_use", "name": "Bash", "input": {"command": "npm test \\\n  " + "-x " * 100}}),
    line("user", {"type": "tool_result", "is_error": True,
                  "content": [{"type": "text", "text": "\n".join(f"err {i}" for i in range(8))}]}),
    "not json",
    line("assistant", {"type": "text", "text": "Done.\nSTATUS: task-done" + "z" * 4000})]), encoding="utf-8")
mc = agent.Ctx("o/r", 30, Path("."), "main")
agent.begin(mc, "implement")
agent.step("o/r", "claude", key="task-0", status="task-done", usd=0.1, sid="sid-1")
agent.step("o/r", "claude", key="task-1", status="failed", usd=0.1, sid="gone")   # no transcript: skipped
agent.step("o/r", "claude", key="old", status="task-done", usd=0.1)               # logged before sid was
learned = agent.active["o/r"]["job"]
agent.finish("o/r", "failed")
dg = agent.digest(learned)
assert dg.startswith("## task-0 (STATUS: task-done)\n- Read: src/app.py\n- Bash: npm test \\ -x -x"), dg[:120]
assert "SECRET" not in dg and "the task prompt" not in dg, "no file contents, no prompt"
assert "    ! err 4" in dg and "err 5" not in dg, "a failed call's first 5 lines"
assert max(len(l) for l in dg.splitlines()) <= 3000 + len("Reply:") and "\nReply:\nDone." in dg, "the last reply, cut"
assert "Let me look" not in dg and "task-1" not in dg and "## old" not in dg, dg[-200:]
agent.DIGEST_CAP, real_dcap = 500, agent.DIGEST_CAP
assert agent.digest(learned).startswith("(earlier calls cut)") and len(agent.digest(learned)) < 600
agent.DIGEST_CAP = real_dcap
# with RUN_AS, a 0700 project dir hides the transcript from us: find and read it as RUN_AS
sh_saved, run_saved, agent.RUN_AS, ran = agent.sh, agent.RUN_AS, "runner", []
agent.sh = lambda *a, **k: ran.append(a) or ((agent.CLAUDE_HOME / "projects" / "-ws-o-r" / "sid-1.jsonl")
                                            .read_text(encoding="utf-8") if "sid-1.jsonl" in a else "")
assert agent.digest(learned) == dg and len(ran) == 2, "same digest; task-1's empty find is skipped"
assert ran[0][:4] == ("sudo", "-n", "-u", "runner") and "find" in ran[0] and "-quit" in ran[0], ran[0]
agent.sh, agent.RUN_AS = sh_saved, run_saved

# remember(): one haiku call, one turn, no tools, in an empty dir; the orchestrator writes the notes
notes_f, replies, argv = agent.memory_file("o/r"), [], []
agent.worktree = lambda c: (_ for _ in ()).throw(AssertionError("the memory call needs no worktree"))
agent.check_caps = lambda repo: None
def fake_sh(*a, **k):
    argv.append((a, k["cwd"], k["input"]))
    assert not os.listdir(k["cwd"]), "an empty cwd: nothing to read or edit"
    return json.dumps({"result": replies.pop(0), "total_cost_usd": 0.01})
real_sh_, agent.sh = agent.sh, fake_sh
agent.comment = agent.set_phase = nope   # a side channel: never a comment or a label
memjob = {"kind": "memory", "repo": "o/r", "n": 30, "job": learned}
replies.append("<notes>\n- Tests: `npm test`, needs `-x`.\n</notes>\nSTATUS: memory-updated")
agent.handle(memjob)
assert notes_f.read_text(encoding="utf-8") == "- Tests: `npm test`, needs `-x`."
cmd, cwd, prompt_ = argv[0]
assert cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--max-turns") + 1] == "1", cmd
assert cmd[cmd.index("--tools") + 1] == "" and "--agent" not in cmd and "Bash(rtk proxy:*)" in cmd
assert not [x for x in cmd if x.startswith(("--plugin-dir", "--append-system-prompt"))], "nothing steers it"
assert "npm test" in prompt_ and "<current-notes>\n\n</current-notes>" in prompt_
assert "At most 8000\ncharacters" in prompt_, "the cap is in the prompt"
for bad in ["no marker at all", "STATUS: memory-updated", "<notes>" + "x" * 8001 + "</notes>\nSTATUS: memory-updated",
            "<notes>x</notes>\nSTATUS: plan-ready"]:
    replies.append(bad)
    agent.handle(memjob)
    assert notes_f.read_text(encoding="utf-8") == "- Tests: `npm test`, needs `-x`.", f"kept after: {bad[:30]}"
replies.append("<notes>stale</notes>\nSTATUS: memory-unchanged")
agent.handle(memjob)
assert notes_f.read_text(encoding="utf-8").startswith("- Tests"), "unchanged writes nothing"
assert "- Tests: `npm test`" in argv[-1][2], "the next call sees the current notes"
with agent.closing(agent.db()) as con:
    mem = con.execute("SELECT id, status FROM jobs WHERE act = 'memory' ORDER BY id").fetchall()
    rem = con.execute("SELECT detail FROM steps WHERE what = 'remembered'").fetchall()
assert [s for _, s in mem] == ["ok", "failed", "failed", "failed", "failed", "ok"], mem
assert len(rem) == 1 and json.loads(rem[0][0])["source"] == learned and json.loads(rem[0][0])["chars"] == len(notes_f.read_text(encoding="utf-8"))
calls_before = len(argv)
agent.handle(skipped_job)   # the skipped review: no claude call, so no memory run at all
with agent.closing(agent.db()) as con:
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE act = 'memory'").fetchone()[0] == len(mem)
assert len(argv) == calls_before
agent.sh, agent.check_caps = real_sh_, real_caps
(agent.labels_of, agent.set_phase, agent.comment) = saved[4:7]

# parallel repos: a worker per repo, one job at a time within it
import threading  # noqa: E402

agent.submit = real_submit
release, ran = threading.Event(), []
def fake_handle(job):
    if job.get("n") == 1:
        release.wait(5)            # a long implementation on o/slow
    ran.append((job["repo"], job.get("n"), job["kind"]))
agent.handle = fake_handle
agent.submit({"kind": "continue", "repo": "o/slow", "n": 1})
agent.submit({"kind": "start", "repo": "o/slow", "n": 2})   # same repo: waits its turn
agent.submit({"kind": "start", "repo": "o/fast", "n": 3})   # other repo: does not
deadline = time.time() + 5
while ("o/fast", 3, "start") not in ran and time.time() < deadline:
    time.sleep(0.01)
assert ran == [("o/fast", 3, "start")], "planning on o/fast waited for o/slow"
release.set()
agent.submit({"kind": "offboard", "repo": "o/fast"})
while len(ran) < 4 and time.time() < deadline:
    time.sleep(0.01)
assert [r for r in ran if r[0] == "o/slow"] == [("o/slow", 1, "continue"), ("o/slow", 2, "start")],     "order within a repo"
while "o/fast" in agent.queues and time.time() < deadline:
    time.sleep(0.01)
assert "o/fast" not in agent.queues and "o/slow" in agent.queues, "offboard stops only that worker"
agent.submit({"kind": "onboard", "repo": "o/fast"})         # re-added: a fresh worker
while len(ran) < 5 and time.time() < deadline:
    time.sleep(0.01)
assert ran[-1] == ("o/fast", None, "onboard")

# a redelivered webhook carries the same X-GitHub-Delivery: act on it once
assert agent.fresh("d-1") and not agent.fresh("d-1") and agent.fresh("d-2")
assert agent.fresh(None) and agent.fresh(None), "no id: nothing to dedupe on"
for i in range(3):
    agent.fresh(f"x-{i}", keep=2)
assert agent.fresh("x-0", keep=2), "bounded: the oldest is forgotten"

# web: sign-in through GitHub, server-side sessions, and who sees what
import urllib.parse  # noqa: E402

import web  # noqa: E402

agent.WORKSPACE = Path(tempfile.mkdtemp())
agent.REPOS = {"o/r": "main", "o/other": "main"}
def req(method, url, sid=None, csrf=None, cookie=None, data=b""):
    h = {}
    if sid or cookie:
        h["Cookie"] = cookie or f"rookery_session={sid}"
    if csrf:
        h["X-CSRF-Token"] = csrf
    return web.route(method, url, h, data)
body = lambda r: json.loads(r[2])

assert req("GET", "/api/repos")[0] == 401 and req("GET", "/api/runs/1")[0] == 401, "nothing without a session"
assert req("GET", "/api/me", sid="made-up")[0] == 401

code, hdrs, _ = req("GET", "/auth/login")
loc = dict(hdrs)["Location"]
state = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)["state"][0]
assert code == 302 and loc.startswith("https://github.com/login/oauth/authorize?")
assert f"rookery_state={state};" in dict(hdrs)["Set-Cookie"]
assert req("GET", "/auth/callback?code=c&state=forged", cookie=f"rookery_state={state}")[0] == 400, \
    "a callback whose state is not this browser's is refused"
assert req("GET", f"/auth/callback?code=c&state={state}")[0] == 400, "no state cookie, no login"

web.exchange = lambda code: "ghu_user"
def fake_api(method, path, bearer):
    assert bearer == "ghu_user", "installations are listed with the user's own token"
    if path == "/user":
        return {"login": "alice"}
    if path.startswith("/user/installations?"):
        return {"installations": [{"id": 7}]}
    assert path.startswith("/user/installations/7/repositories?"), path
    return {"repositories": [{"full_name": "o/r"}, {"full_name": "o/not-served"}]}
agent.api = fake_api
code, hdrs, _ = req("GET", f"/auth/callback?code=c&state={state}", cookie=f"rookery_state={state}")
cookies = [v for k, v in hdrs if k == "Set-Cookie"]
sid = next(c.split(";")[0].split("=", 1)[1] for c in cookies if c.startswith("rookery_session="))
assert code == 302 and dict(hdrs)["Location"] == "/portal.html"
assert all("HttpOnly" in c and "SameSite=Lax" in c for c in cookies)
assert f"Max-Age={8 * 3600}" in next(c for c in cookies if c.startswith("rookery_session="))
with agent.closing(agent.db()) as con:
    dump = repr(con.execute("SELECT * FROM sessions").fetchall())
assert "ghu_user" not in dump and sid not in dump, "no token and no replayable session id at rest"
assert "ghu_user" not in os.environ.values()
with agent.closing(agent.db()) as con, con:   # first sign-in recorded; backdated to see it kept
    assert con.execute("SELECT login, logins FROM users").fetchall() == [("alice", 1)]
    con.execute("UPDATE users SET first = 1")

me = body(req("GET", "/api/me", sid=sid))
assert me["login"] == "alice" and me["csrf"]
assert [r["repo"] for r in body(req("GET", "/api/repos", sid=sid))["repos"]] == ["o/r"], \
    "installations ∩ REPOS: o/other is not alice's, o/not-served is not served"
assert me["app"] == agent.ME.removesuffix("[bot]"), "the portal links to the App's install page"

# signing in again (the App's Setup URL after an install, or "refresh") re-reads the list
# and replaces the session rather than adding one
fake_repos = fake_api
agent.REPOS["o/new"] = "main"   # alice just installed the App on it
agent.api = lambda m, p, b: ({"repositories": [{"full_name": "o/r"}, {"full_name": "o/new"}]}
                             if p.startswith("/user/installations/7/") else fake_repos(m, p, b))
st = urllib.parse.parse_qs(urllib.parse.urlsplit(dict(req("GET", "/auth/login")[1])["Location"]).query)["state"][0]
hdrs = req("GET", f"/auth/callback?code=c&state={st}", cookie=f"rookery_state={st}; rookery_session={sid}")[1]
old, sid = sid, next(v.split(";")[0].split("=", 1)[1] for k, v in hdrs
                     if k == "Set-Cookie" and v.startswith("rookery_session="))
assert req("GET", "/api/me", sid=old)[0] == 401, "the old session is gone"
assert [r["repo"] for r in body(req("GET", "/api/repos", sid=sid))["repos"]] == ["o/new", "o/r"]
with agent.closing(agent.db()) as con:
    assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
agent.api = fake_api
del agent.REPOS["o/new"]
me = body(req("GET", "/api/me", sid=sid))

# runs: only visible repos', each summarised from its steps
for repo in ("o/r", "o/other"):
    rc = agent.Ctx(repo, 4, Path("."), "main")
    agent.begin(rc, "plan", "Add a thing")
    agent.step(repo, "task", i=9, of=12, text="wire it")
    agent.step(repo, "claude", key="task-8", status="failed", usd=0.25)
    agent.step(repo, "error", message="task 9 (wire it) reported failed")
    agent.finish(repo, "failed")
with agent.closing(agent.db()) as con:
    ids = dict(con.execute("SELECT repo, id FROM jobs"))
mine, theirs = ids["o/r"], ids["o/other"]
listed = body(req("GET", "/api/runs", sid=sid))["runs"]
assert [(r["repo"], r["status"], r["task"]["i"], r["task"]["of"], r["usd"]) for r in listed] == \
    [("o/r", "failed", 9, 12, 0.25)], listed
assert body(req("GET", "/api/runs?repo=o/other", sid=sid))["runs"] == []
assert req("GET", f"/api/runs/{theirs}", sid=sid)[0] == 404, "another tenant's run looks like no run"
one = body(req("GET", f"/api/runs/{mine}", sid=sid))
assert one["title"] == "Add a thing" and [s["what"] for s in one["steps"]] == ["task", "claude", "error"]

# workflow: agent.steer() — what pin() runs at job start — into a throwaway dir; an unseen repo is a 404
assert req("GET", "/api/repos/o/other/workflow", sid=sid)[0] == 404, "another tenant's personas stay theirs"
assert req("GET", "/api/repos/o/nope/workflow", sid=sid)[0] == 404
assert "still cloning" in body(req("GET", "/api/repos/o/r/workflow", sid=sid))["error"]
agent.clone_dir("o/r").mkdir(parents=True)
real_steer, dirs = agent.steer, []
def fake_steer(repo, path, base, plug):
    dirs.append(plug)
    wf = {**agent.WORKFLOW, "task": {**agent.WORKFLOW["task"], "skills": ["tdd"]},
          "after": [{**agent.STEP, "agent": "sec", "mode": "check"}], "with": ["o/front"]}
    owf = {**agent.WORKFLOW, "review": {**agent.WORKFLOW["review"], "instructions": "Owner's."}, "after": [],
           "with": ["o/owners"]}
    return {"sha": "abc", "claude_md": "é", "workflow": wf, "sources": {"tdd": "catalog"},
            "skills": {"tdd": "red, green"}, "agents": {"planmaker": {"model": "opus"}, "reviewer": {}, "pr-reviewer": {},
                                                        "sec": {"model": "haiku", "description": "holes"}},
            "agent_sources": {"planmaker": "built-in", "reviewer": "built-in", "pr-reviewer": "built-in",
                              "sec": ".rookery/agents"},
            "org": {"repo": "o/.rookery", "sha": "def"}, "org_workflow": owf, "skipped": ["sec (o/front)"]}
agent.steer = fake_steer
real_files, agent.config_files = agent.config_files, lambda repo: {"workflow.json": "{}\n"}
# the shelf of companions: same owner, in REPOS and in this session's list, minus home and `with`
saved_repos = dict(agent.REPOS)
agent.REPOS.update({"o/front": "main", "o/admin": "main", "o/hidden": "main", "o/.rookery": "main", "x/web": "main"})
with agent.closing(agent.db()) as con, con:
    alice_had = con.execute("SELECT id, repos FROM sessions WHERE login = 'alice'").fetchall()
    con.execute("UPDATE sessions SET repos = ? WHERE login = 'alice'",
                (json.dumps(["o/r", "o/front", "o/admin", "o/gone", "o/.rookery", "x/web"]),))
w = body(req("GET", "/api/repos/o/r/workflow", sid=sid))
assert w["with"] == ["o/front"] and w["max_with"] == 3 and w["skipped"] == ["sec (o/front)"], w
assert w["repos"] == ["o/admin"], "not home, not listed, not another owner's, not one it cannot see or REPOS lacks"
assert "with" not in w["under"], "the owner layer's `with` is never the repo's"
assert w["install"].endswith("/installations/new")
erin = web.new_session("erin", ["o/r", "o/hidden"])
assert body(req("GET", "/api/repos/o/r/workflow", sid=erin))["repos"] == ["o/hidden"], "another session, its own shelf"
agent.REPOS = saved_repos
with agent.closing(agent.db()) as con, con:
    con.executemany("UPDATE sessions SET repos = ? WHERE id = ?", [(r, i) for i, r in alice_had])
w = body(req("GET", "/api/repos/o/r/workflow", sid=sid))
assert w["sha"] == "abc" and w["claude_md"] == 2 and w["error"] is None, w
assert [(k, v["agent"], v["model"], v["from"]) for k, v in w["slots"].items()] == [
    ("plan", "planmaker", "opus", "built-in"), ("task", None, "sonnet", "repo"),
    ("review", "reviewer", None, "repo"), ("pr-review", "pr-reviewer", None, "built-in")], \
    "the repo's review is the built-in's, but over the owner's"
assert w["org"] == {"repo": "o/.rookery", "sha": "def"}
assert w["skills"] == {"tdd": {"source": "catalog", "description": "red, green"}}
assert [(s["agent"], s["mode"], s["model"], s["from"]) for s in w["after"]] == [("sec", "check", "haiku", "repo")]
assert w["agents"]["sec"] == {"description": "holes", "source": ".rookery/agents", "model": "haiku"}
assert w["under"]["review"]["instructions"] == "Owner's." and w["under"]["after"] == [], "the layer under the repo's file"
assert w["config"] == {"dir": ".rookery/", "files": {"workflow.json": "{}\n"}}, "what the editor starts from"
assert not dirs[0].exists(), "the throwaway plugin dir is gone"
agent.steer = lambda *a: (_ for _ in ()).throw(RuntimeError("`task` names the skill `x`"))
w = body(req("GET", "/api/repos/o/r/workflow", sid=sid))
assert w["error"] == "`task` names the skill `x`", "the error the next job would fail with"
assert w["config"]["files"], "a broken config can still be edited"
agent.steer, agent.config_files = real_steer, real_files

# stats: A from git log on the clone, B from the run log, C from GitHub
assert req("GET", "/api/repos/o/r/stats")[0] == 401
assert req("GET", "/api/repos/o/other/stats", sid=sid)[0] == 404, "another tenant's numbers are theirs"
assert req("GET", "/api/repos/o/r/stats?days=7", sid=sid)[0] == 400
agent.REPOS["o/r"] = ""
assert "still cloning" in body(req("GET", "/api/repos/o/r/stats", sid=sid))["error"]
agent.REPOS["o/r"] = "main"
up, now = Path(tempfile.mkdtemp()), time.time()
def commit(who, ago, files):
    for f, text in files.items():
        (up / f).write_bytes(text)
    subprocess.run(["git", "add", "-A"], cwd=up, check=True)
    d = f"@{int(now - ago * 86400)} +0000"
    subprocess.run(["git", "-c", f"user.name={who}", "-c", "user.email=who@example.com", "commit", "-qm", "c"],
                   cwd=up, check=True, env={**os.environ, "GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=up, check=True)
for who, ago, files in [("carl", 100, {"c.py": b"1\n2\n3\n4\n5\n"}), ("ann", 20, {"a.py": b"1\n2\n"}),
                        ("bob", 10, {"b.py": b"x\n", "logo.png": b"\x00\x01\x02bin"}),
                        ("ann", 1, {"a.py": b"1\n2\n3\n4\n"})]:
    commit(who, ago, files)
subprocess.run(["git", "clone", "-q", str(up), str(agent.clone_dir("o/r"))], check=True, capture_output=True)
for ac, steps_ in (("implement", [("claude", {"usd": 1.0}), ("pr", {"url": "u/9"}), ("pr", {"url": "u/9"})]),
                   ("pr-review", [("posted", {"verdict": "safe-to-merge", "head": "h"})])):
    agent.begin(agent.Ctx("o/r", 5, Path("."), "main"), ac)
    for what, d in steps_:
        agent.step("o/r", what, **d)
    agent.finish("o/r", "ok")
with agent.closing(agent.db()) as con, con:   # a run before the window
    old = con.execute("INSERT INTO jobs (repo, n, act, started, status) VALUES ('o/r', 1, 'plan', ?, 'ok')",
                      (now - 200 * 86400,)).lastrowid
    con.execute("INSERT INTO steps VALUES (?, 0, 'claude', ?)", (old, json.dumps({"usd": 9})))
ghq, fetched, saved_c = [], [], (agent.fetch, agent.gh, agent.api, agent.token)
agent.fetch, agent.token = lambda *a: fetched.append(a), lambda repo: "t"
agent.gh = lambda repo, *a, **k: ghq.append(a) or json.dumps(
    [{"additions": 4, "deletions": 1}] if "--author" in a else [{"additions": 4, "deletions": 1}] * 4)
agent.api = lambda m, p, b: {"stargazers_count": 3, "forks_count": 1, "open_issues_count": 2} if p == "/repos/o/r" \
    else (_ for _ in ()).throw(RuntimeError("rate limited"))
raw = req("GET", "/api/repos/o/r/stats", sid=sid)
st = json.loads(raw[2])
assert raw[0] == 200 and b"example.com" not in raw[2], "names only, never emails"
assert (st["days"], st["commits"], st["authors"], st["added"], st["removed"]) == (90, 3, 2, 5, 0), st
assert st["unit"] == "week" and len(st["trend"]) == 13 and [t["commits"] for t in st["trend"][-3:]] == [1, 1, 1]
assert [(a["name"], a["commits"]) for a in st["top_authors"]] == [("ann", 2), ("bob", 1)]
assert [(f["path"], f["churn"]) for f in st["top_files"]] == [("a.py", 4), ("b.py", 1), ("logo.png", 0)], \
    "a binary file counts 0 lines"
assert st["rookery"] == {"runs": {"failed": 1, "ok": 2}, "issues": 1, "prs": 1, "reviews": 1,
                         "verdicts": {"safe-to-merge": 1}, "usd": 1.25, "per_pr": 1.25}, st["rookery"]
assert st["facts"] == {"stars": 3, "forks": 1, "open_issues": 2} and st["languages"] is None, \
    "a failing GitHub call leaves its part null"
assert st["bot"] == {"merged": 4, "prs": 1, "lines": 5, "pr_share": 0.25, "line_share": 1}, st["bot"]
assert ghq[1][-2:] == ("--author", "app/" + agent.ME.removesuffix("[bot]")) and "merged:>=" in " ".join(ghq[0])
assert len(ghq) == 2 and body(req("GET", "/api/repos/o/r/stats", sid=sid))["commits"] == 3 and len(ghq) == 2, \
    "cached: no second round of GitHub calls"
assert len(fetched) == 1, "a hit skips the fetch too"
y = body(req("GET", "/api/repos/o/r/stats?days=365", sid=sid))
assert (y["commits"], y["authors"], y["unit"], len(y["trend"])) == (4, 3, "month", 12)
assert len(body(req("GET", "/api/repos/o/r/stats?days=30", sid=sid))["trend"]) == 5
agent.gh = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh down"))
agent._stats.clear()
assert body(req("GET", "/api/repos/o/r/stats", sid=sid))["bot"] is None, "no bot share, still a page"
agent.fetch, agent.gh, agent.api, agent.token = saved_c

# config edits: a PR through agent.propose(), only from a writer of that repo
proposed = []
agent.propose = lambda repo, files, by: proposed.append((repo, files, by)) or "https://github.com/o/r/pull/9"
agent.can_write = lambda repo, user: user == "alice"
edit = lambda repo, data, s=sid, c=None: req("POST", f"/api/repos/{repo}/config", sid=s, csrf=c or me["csrf"],
                                             data=json.dumps(data).encode())
assert req("POST", "/api/repos/o/r/config", sid=sid, data=b'{"files": {}}')[0] == 403, "no CSRF token"
assert edit("o/other", {"files": {"workflow.json": "{}"}})[0] == 404, "another tenant's repo is no repo"
assert edit("o/r", {"files": ["workflow.json"]})[0] == 400 and edit("o/r", {"files": {"x": 1}})[0] == 400
code, _, out = edit("o/r", {"files": {"workflow.json": "{}", "agents/x.md": None}})
assert code == 201 and json.loads(out)["url"].endswith("/pull/9")
assert proposed == [("o/r", {"workflow.json": "{}", "agents/x.md": None}, "alice")]
dave = web.new_session("dave", ["o/r"])
assert edit("o/r", {"files": {"workflow.json": "{}"}}, dave, body(req("GET", "/api/me", sid=dave))["csrf"])[0] == 403, "seeing is not steering"
agent.propose = lambda *a: (_ for _ in ()).throw(RuntimeError("`plan` names the agent `ghost`"))
code, _, out = edit("o/r", {"files": {"workflow.json": "{}"}})
assert code == 422 and "ghost" in json.loads(out)["error"], "the check's own words"
assert len(proposed) == 1
# repo settings: kept here, not in the repo, so no PR; seen by whoever sees the repo, changed by a writer
put = lambda repo, data, s=sid, c=None: req("POST", f"/api/repos/{repo}/settings", sid=s, csrf=c or me["csrf"],
                                            data=json.dumps(data).encode())
assert body(req("GET", "/api/repos/o/r/settings", sid=sid)) == {"local_plans": True, "review_prs": False}
assert req("GET", "/api/repos/o/other/settings", sid=sid)[0] == 404 and put("o/other", {"review_prs": True})[0] == 404
assert req("POST", "/api/repos/o/r/settings", sid=sid, data=b'{"review_prs": true}')[0] == 403, "no CSRF token"
assert put("o/r", {"review_prs": True}, dave, body(req("GET", "/api/me", sid=dave))["csrf"])[0] == 403, "seeing is not steering"
for bad in ({}, {"review_prs": 1}, {"plans": True}, ["review_prs"]):
    assert put("o/r", bad)[0] == 400, bad
assert body(put("o/r", {"review_prs": True})) == {"local_plans": True, "review_prs": True}
assert body(put("o/r", {"local_plans": False})) == {"local_plans": False, "review_prs": True}, "one key changes one setting"
assert agent.repo_settings("o/r") == {"local_plans": False, "review_prs": True}
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM settings WHERE key = 'repo:o/r'")
cat = {s["name"]: s for s in body(req("GET", "/api/skills", sid=sid))["skills"]}
assert sorted(cat) == ["changelog", "repo-recon", "security-review", "systematic-debugging",
                       "verification-before-completion"], cat
assert cat["security-review"]["slot"] == "check" and cat["changelog"]["source"] == "ours"
assert cat["repo-recon"]["slot"] == "plan" and cat["repo-recon"]["source"] == "ours"
assert cat["systematic-debugging"]["source"] == "obra/superpowers@8ca22db", "\"same\" repeats the row above"
assert cat["systematic-debugging"]["url"].startswith("https://github.com/obra/superpowers/tree/8ca22db")
assert all(s["description"] for s in cat.values())

# repo notes: read by anyone who sees the repo, cleared only by a writer, another tenant's is a 404
assert req("GET", "/api/repos/o/other/memory", sid=sid)[0] == 404
m_ = body(req("GET", "/api/repos/o/r/memory", sid=sid))
assert (m_["notes"], m_["run"], m_["cap"], m_["enabled"]) == ("", None, 8000, True), m_
agent.memory_file("o/r").parent.mkdir(parents=True, exist_ok=True)
agent.memory_file("o/r").write_text("- build: make", encoding="utf-8")
rc = agent.Ctx("o/r", 4, Path("."), "main")
agent.begin(rc, "memory")
agent.step("o/r", "remembered", chars=13, sha="x", source=1)
agent.finish("o/r", "ok")
m_ = body(req("GET", "/api/repos/o/r/memory", sid=sid))
assert m_["notes"] == "- build: make" and m_["chars"] == 13 and m_["run"]["id"], m_
assert req("DELETE", "/api/repos/o/r/memory", sid=sid)[0] == 403, "no CSRF token"
assert req("DELETE", "/api/repos/o/other/memory", sid=sid, csrf=me["csrf"])[0] == 404
assert req("DELETE", "/api/repos/o/r/memory", sid=dave, csrf=body(req("GET", "/api/me", sid=dave))["csrf"])[0] == 403,     "seeing is not steering"
assert agent.memory_file("o/r").exists()
assert req("DELETE", "/api/repos/o/r/memory", sid=sid, csrf=me["csrf"])[0] == 204
assert not agent.memory_file("o/r").exists() and body(req("GET", "/api/repos/o/r/memory", sid=sid))["run"] is None

# admin: ADMINS only; anyone else, signed in or not, finds no such endpoint
web.ADMINS = {"alice"}
dcsrf = body(req("GET", "/api/me", sid=dave))["csrf"]
assert req("GET", "/api/admin")[0] == 404, "signed out: a 404, not a 401"
assert req("GET", "/api/admin", sid=dave)[0] == 404
assert req("POST", "/api/admin/provider", sid=dave, csrf=dcsrf, data=b'{"owner": null, "provider": "claude"}')[0] == 404
assert req("POST", "/api/admin/provider", sid=sid, data=b'{"owner": null, "provider": "claude"}')[0] == 403, "no CSRF"
setp = lambda d: req("POST", "/api/admin/provider", sid=sid, csrf=me["csrf"], data=json.dumps(d).encode())
a = body(req("GET", "/api/admin", sid=sid))
assert a["default"] == "claude" and a["overrides"] == {} and a["owners"] == [{"owner": "o", "repos": 2, "own": True, "key": None}], a
assert {p["name"]: p["ready"] for p in a["providers"]} == {"claude": True, "anthropic": False, "deepseek": False, "glm": False}
assert next(p for p in a["providers"] if p["name"] == "glm")["key"] == "ZAI_API_KEY"
u = a["users"][0]
assert (u["login"], u["logins"], u["first"], u["live"], u["repos"]) == ("alice", 2, 1, True, 1), \
    "a second sign-in adds to logins and keeps first; repos counts the served ones"
assert setp({"owner": None, "provider": "glm"})[0] == 422, "no key in .env, no setting"
assert setp({"owner": "acme", "provider": "nope"})[0] == 422
for bad in ({"owner": "o/x", "provider": "claude"}, {"owner": None, "provider": None}, {"provider": "claude"},
            {"owner": "acme", "provider": ["glm"]}, ["acme"]):
    assert setp(bad)[0] == 400, bad
agent.KEYS["glm"] = "k"
a = body(setp({"owner": "acme", "provider": "glm"}))
assert a["overrides"] == {"acme": "glm"} and prov("acme/x") == "glm" and prov("o/r") == "claude"
assert {"owner": "acme", "repos": 0, "own": False, "key": None} in a["owners"], "an override shows with no repo served"
assert body(setp({"owner": None, "provider": "glm"}))["default"] == "glm" and prov("o/r") == "glm"
assert body(setp({"owner": "acme", "provider": None}))["overrides"] == {}
r = setp({"owner": "acme", "provider": "claude"})
assert r[0] == 422 and "OWN" in body(r)["error"], "the subscription is not for acme"
assert body(setp({"owner": "O", "provider": "claude"}))["overrides"] == {"O": "claude"}, "o is in OWN"
setp({"owner": "O", "provider": None})
setp({"owner": None, "provider": "claude"})
assert body(req("GET", "/api/admin", sid=sid))["default"] == "claude", "the default may stay claude"
agent.KEYS["glm"], web.ADMINS = "", set()
assert req("GET", "/api/admin", sid=sid)[0] == 404

# own keys: only the account itself manages them, and a key never comes back out
agent.REPOS["alice/app"] = "main"
seen = []
def kreq(method, path, d=None, s=sid, c=None):
    r = req(method, path, sid=s, csrf=c or me["csrf"], data=json.dumps(d).encode() if d is not None else b"")
    seen.append(r[2].decode())
    return r
good = {"owner": "alice", "provider": "deepseek", "label": " main ", "key": "sk-alice-0123456789abcdef"}
agent.KEY_SECRET = ""
assert body(kreq("GET", "/api/keys")) == {"enabled": False, "providers": ["anthropic", "deepseek", "glm"],
    "owners": [{"owner": "alice", "fallback": None, "selected": None, "keys": []}]}, "never `claude`, never o's"
assert kreq("POST", "/api/keys", good)[0] == 503, "no KEY_SECRET, no key"
agent.KEY_SECRET = "master-test"
assert req("POST", "/api/keys", sid=sid, data=json.dumps(good).encode())[0] == 403, "no CSRF"
for owner in ("o", "Alice", "dave", ["alice"]):
    assert kreq("POST", "/api/keys", {**good, "owner": owner})[0] in (400, 404), owner
assert kreq("POST", "/api/keys", {**good, "owner": "o"})[0] == 404, "o's keys are o's, not alice's"
for bad in ({"provider": "claude"}, {"provider": "nope"}, {"provider": None}, {"label": "x" * 41}, {"label": 3},
            {"key": "short"}, {"key": "sk has spaces 0123456789"}, {"key": "x" * 301}, {"key": 12345678901234567890}):
    assert kreq("POST", "/api/keys", {**good, **bad})[0] == 422, bad
r = kreq("POST", "/api/keys", good)
k = body(r)["owners"][0]
k1 = k["keys"][0]["id"]
assert r[0] == 201 and k["selected"] == k1, "the first key is selected"
assert {x: k["keys"][0][x] for x in ("provider", "label", "last4", "by")} == \
    {"provider": "deepseek", "label": "main", "last4": "cdef", "by": "alice"}
assert agent.provider_for("alice/app") == ("deepseek", k1, good["key"])
k2 = body(kreq("POST", "/api/keys", {**good, "provider": "glm", "key": "zai-alice-0123456789abcd"}))["owners"][0]
assert k2["selected"] == k1, "a second key is not selected by itself"
k2 = k2["keys"][1]["id"]
assert kreq("POST", "/api/keys/select", {"owner": "alice", "id": k2})[0] == 200 and prov("alice/app") == "glm"
assert {"owner": "alice", "repos": 1, "own": False, "key": "glm"} in web.admin_view()["owners"]
for d in ({"owner": "alice", "id": 999}, {"owner": "alice", "id": True}, {"owner": "alice", "id": str(k1)},
          {"owner": "o", "id": None}):
    assert kreq("POST", "/api/keys/select", d)[0] == 404, d
dave_csrf = body(req("GET", "/api/me", sid=dave))["csrf"]
assert kreq("DELETE", f"/api/keys/{k2}", s=dave, c=dave_csrf)[0] == 404, "someone else's key is a 404"
assert kreq("POST", "/api/keys/select", {"owner": "alice", "id": None}, s=dave, c=dave_csrf)[0] == 404
assert body(kreq("GET", "/api/keys", s=dave))["owners"] == []
assert kreq("DELETE", f"/api/keys/{k2}")[0] == 200 and body(kreq("GET", "/api/keys"))["owners"][0]["selected"] is None
assert nokey("alice/app") == "no API key", "the selected key removed: alice is not in OWN, so nothing runs"
assert kreq("POST", "/api/keys/select", {"owner": "alice", "id": k1})[0] == 200 and prov("alice/app") == "deepseek"
assert kreq("POST", "/api/keys/select", {"owner": "alice", "id": None})[0] == 200 and nokey("alice/app")
with agent.closing(agent.db()) as con:
    dump = repr(con.execute("SELECT * FROM keys").fetchall()) + repr(con.execute("SELECT * FROM settings").fetchall())
assert not [x for x in seen + [dump] if "0123456789" in x], "no key in any response or at rest"
with agent.closing(agent.db()) as con, con:
    con.execute("DELETE FROM keys")
del agent.REPOS["alice/app"]

# POSTs need the session's CSRF token; logout ends the session server-side
assert req("POST", "/auth/logout", sid=sid)[0] == 403
assert req("POST", "/auth/logout", sid=sid, csrf="wrong")[0] == 403
assert req("POST", "/auth/logout", sid=sid, csrf=me["csrf"])[0] == 204
assert req("GET", "/api/me", sid=sid)[0] == 401
sid = web.new_session("alice", ["o/r"])
with agent.closing(agent.db()) as con, con:
    con.execute("UPDATE sessions SET expires = 1")
assert req("GET", "/api/me", sid=sid)[0] == 401, "expired"

# static: a flat whitelist of ui/ files, nothing else in the tree
assert req("GET", "/")[0] == 200 and dict(req("GET", "/")[1])["Content-Type"] == "text/html"
assert b'class="hero"' in req("GET", "/")[2]   # / is the landing page; the portal is /portal.html
assert req("GET", "/styles.css")[0] == 200
for bad in ("/agent.py", "/../agent.py", "/%2e%2e/agent.py", "/.env", "/ui/portal.html", "/..\\agent.py"):
    assert req("GET", bad)[0] == 404, bad
# docs: flat docs/*.md as markdown, and every page ui/docs.html lists exists
assert req("GET", "/docs/overview.md")[0] == 200 and b"# Overview" in req("GET", "/docs/overview.md")[2]
for bad in ("/docs/../agent.py", "/docs/%2e%2e/CLAUDE.md", "/docs/nope.md", "/docs/overview.html", "/docs/"):
    assert req("GET", bad)[0] == 404, bad
import re  # noqa: E402
for name in re.findall(r'\["([\w-]+)", "', (agent.HERE / "ui" / "docs.html").read_text(encoding="utf-8")):
    assert req("GET", f"/docs/{name}.md")[0] == 200, name


# the webhook over a real socket: HMAC first, 204 before any work, then enqueue
import threading as _t  # noqa: E402
import urllib.request as _u  # noqa: E402

got = []
agent.enqueue = lambda event, p: got.append((event, p))
srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
_t.Thread(target=srv.serve_forever, daemon=True).start()
def hook(payload, secret=b"s3cr3t", delivery="d-web-1"):
    sig = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    rq = _u.Request(f"http://127.0.0.1:{srv.server_address[1]}/", data=payload, method="POST",
                    headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery})
    try:
        return _u.urlopen(rq, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
assert hook(b'{"a": 1}', secret=b"wrong") == 401
assert hook(b'{"a": 1}') == 204 and hook(b'{"a": 1}') == 204   # a redelivery: answered, not acted on
deadline = time.time() + 5
while not got and time.time() < deadline:
    time.sleep(0.01)
assert got == [("issues", {"a": 1})], got
assert _u.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5).status == 200, "the UI"
srv.shutdown()

# cancel: kill the running claude, refuse any next call, end like any failure
import sys  # noqa: E402

agent.sh, agent.WORKSPACE, agent.REPOS = real_sh, Path(tempfile.mkdtemp()), {"o/r": "main"}
agent.labels_of = lambda repo, n: {"rookery:awaiting-approval"}
posted.clear()
def slow_implement(c):
    agent.step(c.repo, "task", i=1, of=2, text="slow")
    agent.sh(sys.executable, "-c", "import time; time.sleep(60)", track=c.repo)  # stands in for claude
    raise AssertionError("the process was not killed")
agent.implement = slow_implement
agent.submit = submitted.append
submitted.clear()
th = _t.Thread(target=real_handle, args=({"kind": "continue", "repo": "o/r", "n": 6, "body": "approve"},))
t0 = time.time()
th.start()
while not (agent.active.get("o/r") or {}).get("proc") and time.time() - t0 < 5:
    time.sleep(0.01)
job = agent.active["o/r"]["job"]
agent.can_write = lambda repo, user: user == "bob"
bob, carol = web.new_session("bob", ["o/r"]), web.new_session("carol", ["o/r"])
csrf = lambda sid: body(req("GET", "/api/me", sid=sid))["csrf"]
assert req("POST", f"/api/runs/{job}/cancel", sid=bob)[0] == 403, "no CSRF token"
assert req("POST", f"/api/runs/{job}/cancel", sid=carol, csrf=csrf(carol))[0] == 403, "seeing is not steering"
assert req("POST", f"/api/runs/{job + 1}/cancel", sid=bob, csrf=csrf(bob))[0] == 404
assert req("POST", f"/api/runs/{job}/cancel", sid=bob, csrf=csrf(bob))[0] == 202
th.join(10)
assert not th.is_alive() and time.time() - t0 < 10, "the running process was killed, not waited out"
assert body(req("GET", f"/api/runs/{job}", sid=bob))["status"] == "cancelled"
assert posted == ["Agent error:\n\n```\nCancelled from the Rookery UI by @bob.\n```"], posted
assert [s["label"] for s in body(req("GET", f"/api/runs/{job}", sid=bob))["steps"] if s["what"] == "phase"]     == ["rookery:error"]
assert req("POST", f"/api/runs/{job}/cancel", sid=bob, csrf=csrf(bob))[0] == 409, "no longer running"
assert not submitted, "a cancelled job teaches the notes nothing"
agent.begin(agent.Ctx("o/r", 6, Path("."), "main"), "implement")   # cancelled between tasks:
assert agent.cancel("o/r", agent.active["o/r"]["job"], "bob")        # nothing to kill, but
assert "Cancelled" in capped("o/r"), "the next claude call never starts"
agent.finish("o/r", "cancelled")

# the landing page's pulse: no session, totals only, the merged count from one search per owner
searched, real_api, real_token = [], agent.api, agent.token
agent.api = lambda m, path, tok: searched.append(urllib.parse.unquote(path)) or {"total_count": 7}
agent.token = lambda repo: "t"
agent._pulse[:] = 0, None
with agent.closing(agent.db()) as con:
    before = dict(con.execute("SELECT what, COUNT(*) FROM steps GROUP BY what"))
agent.begin(agent.Ctx("o/r", 90, Path("."), "main"), "implement")
for what, d in [("tick", {}), ("tick", {}), ("pr", {"url": "u"}), ("pr", {"url": "u"}), ("posted", {"verdict": "x"})]:
    agent.step("o/r", what, **d)
agent.finish("o/r", "ok")
p = body(req("GET", "/api/pulse"))
assert p["merged"] == 7 and len(searched) == len({r.split("/")[0] for r in agent.REPOS}), searched
assert all(f"user:{r.split('/')[0]}" in " ".join(searched) for r in agent.REPOS)
assert p["tasks"] == before.get("tick", 0) + 2 and p["reviews"] == before.get("posted", 0) + 1, p
assert p["agents"] == 4 and p["skills"] >= 1 and p["repos"] == len(agent.REPOS), p
assert not {"repo", "title", "login", "usd"} & set(json.dumps(p).split('"')), "nothing that names a tenant"
agent.api = lambda *a: 1 / 0
agent._pulse[0] = 0
assert body(req("GET", "/api/pulse"))["merged"] == 7, "a failed search keeps the last count"
agent.api, agent.token = real_api, real_token

# follow-up: a mention on the issue once its draft PR is open — an answer, or a change pushed and reviewed
agent.sh, agent.WORKSPACE, agent.REPOS = real_sh, Path(tempfile.mkdtemp()), {"o/r": "main"}
g = lambda cwd, *a: subprocess.run(("git",) + a, cwd=cwd, capture_output=True, check=True, text=True).stdout.strip()
origin, up, clone = agent.WORKSPACE / "origin.git", agent.WORKSPACE / "up", agent.clone_dir("o/r")
g(agent.WORKSPACE, "init", "-q", "--bare", "-b", "main", str(origin))
g(agent.WORKSPACE, "clone", "-q", str(origin), str(up))
g(up, "config", "user.email", "t@t"), g(up, "config", "user.name", "t")
(up / "app.txt").write_text("a\n", encoding="utf-8")
g(up, "add", "-A"), g(up, "commit", "-qm", "base")
base0 = g(up, "rev-parse", "HEAD")
(up / "app.txt").write_text("main\n", encoding="utf-8")
g(up, "commit", "-qam", "main moves"), g(up, "push", "-q", "origin", "main")
g(agent.WORKSPACE, "clone", "-q", str(origin), str(clone))
g(clone, "config", "user.email", "t@t"), g(clone, "config", "user.name", "t")
def branch_for(n, f="app.txt"):
    """feature/issue-<n> as implement() left it, from before main moved: conflicts on app.txt."""
    g(up, "checkout", "-q", "-b", f"feature/issue-{n}", base0)
    (up / agent.pdir(n)).mkdir(parents=True)
    (up / agent.pdir(n) / "tasks.md").write_text("- [x] one\n", encoding="utf-8")
    (up / f).write_text(f"branch {n}\n", encoding="utf-8")
    g(up, "add", "-A"), g(up, "commit", "-qm", f"#{n} task 1: one"), g(up, "push", "-q", "origin", f"feature/issue-{n}")
    g(up, "checkout", "-q", "main")
    return agent.wt_root("o/r") / f"issue-{n}"
fu = {"said": [], "phases": [], "pushed": [], "pr_said": [], "calls": [], "reply": None, "pr": {}}
def fu_gh(repo, *a, input=None, check=True):
    if a[:2] == ("pr", "view"):
        pr = fu["pr"].get(a[2], {})
        if "--jq" in a:
            return pr.get("url", "") if pr.get("state") == "OPEN" else ""
        return json.dumps(pr) if pr else ""
    if a[:2] == ("pr", "comment"):
        return fu["pr_said"].append(input) or ""
    return json.dumps({"title": "t"})
def fu_claude(c, prompt, key="plan", resume=False, slot="task", per_job=False, wt=None):
    fu["calls"].append((key, prompt))
    if key == "review":
        return "review-ok", '<review>{"summary": "Fine.", "findings": []}</review>'
    assert key == "follow-up" and slot == "task" and per_job and wt == agent.wt_root("o/r") / f"issue-{c.n}"
    return fu["reply"](wt)
def fu_sh(*a, **k):
    if a[:2] == ("git", "push"):
        return fu["pushed"].append(a[3]) or ""
    return real_sh(*a, **k)
fu_saved = (agent.gh, agent.claude, agent.comment, agent.set_phase, agent.labels_of, agent.pin, agent.api, agent.token,
            agent.submit, agent.implement, agent.plan)
agent.gh, agent.claude, agent.sh = fu_gh, fu_claude, fu_sh
agent.comment = lambda c, body: fu["said"].append(body)
agent.set_phase = lambda c, phase: fu["phases"].append(phase)
agent.pin = lambda c: agent.active[c.repo].update(sha="abc123")
agent.api, agent.token, agent.submit = (lambda m, path, bearer, data=None: fu["said"].append(("api", path)) or {},
                                        lambda repo: "t", lambda job: None)
phase = {"rookery:done"}
agent.labels_of = lambda repo, n: phase
def follow(n, body, reply=None):
    for k in ("said", "phases", "pushed", "pr_said", "calls"):
        fu[k].clear()
    fu["reply"] = reply
    real_handle({"kind": "continue", "repo": "o/r", "n": n, "body": body, "comment": None})
url = lambda n: f"https://github.com/o/r/pull/{n}"
pr_for = lambda n, mergeable="CONFLICTING": fu["pr"].update({f"feature/issue-{n}": {
    "state": "OPEN", "url": url(n), "mergeable": mergeable, "mergeStateStatus": "DRAFT"}})

# routing: only a mention, and only once the PR is open; approve and plan replies are unchanged
routed = []
agent.implement = lambda c: routed.append("implement")
agent.plan = lambda c, title, body, resume: routed.append(("plan", resume))
follow(20, "thanks, looks good")
assert not fu["said"] and not fu["calls"] and not routed, "ordinary talk on a done issue: nothing"
real_handle({"kind": "continue", "repo": "o/r", "n": 20, "body": "@rookery-agent", "comment": 7})
assert fu["said"] == [("api", "/repos/o/r/issues/comments/7/reactions"), agent.FOLLOW_EMPTY] and not fu["calls"]
phase = {"rookery:changes-wanted"}
follow(20, "approve")
phase = {"rookery:awaiting-input"}
follow(20, "@rookery-agent use sqlite")
assert routed == ["implement", ("plan", True)], routed
agent.implement, agent.plan = fu_saved[9], fu_saved[10]
phase = {"rookery:changes-wanted"}
follow(20, "@rookery-agent why sqlite?")
assert fu["said"] == [agent.NO_PR] and not fu["calls"], "no open PR: say so, no claude call"

# a conflict with base, resolved: the orchestrator merges, the agent resolves, one merge commit, pushed, reviewed
wt = branch_for(21)
pr_for(21)
def resolve(wt):
    assert "app.txt" in fu["calls"][-1][1] and "in progress" in fu["calls"][-1][1], "the conflicted files are named"
    assert "<<<<<<< " in (wt / "app.txt").read_text(encoding="utf-8")
    (wt / "app.txt").write_text("main and branch 21\n", encoding="utf-8")
    return "changed", "Merged main; kept both lines."
follow(21, "@rookery-agent pull the latest from main branch and resolve the conflicts", resolve)
assert wt.exists() and g(wt, "log", "-1", "--format=%s %p") .startswith("#21 merge main ") and \
    len(g(wt, "log", "-1", "--format=%p").split()) == 2, "one merge commit, carrying the resolution"
assert (wt / "app.txt").read_text(encoding="utf-8") == "main and branch 21\n" and not g(wt, "status", "--porcelain")
assert g(wt, "merge-base", "--is-ancestor", "origin/main", "HEAD") == "", "base is in"
assert fu["pushed"] == ["feature/issue-21:feature/issue-21"] and [k for k, _ in fu["calls"]] == ["follow-up", "review"]
assert fu["said"][-1].startswith("Merged main; kept both lines.\n\nUpdated: " + url(21)), fu["said"]
assert fu["phases"] == ["rookery:reviewing", "rookery:done"], fu["phases"]
assert "## 🤖 Follow-up\n\n> pull the latest" in fu["pr_said"][0] and "## 🤖 Reviewer agent" in fu["pr_said"][0], \
    "the open PR gets this run's sections as a comment"

# a marker left in, or an answer with conflicts open: raise, and nothing half-merged stays
for n, reply in ((22, lambda wt: ("changed", "done")), (23, lambda wt: ("answered", "it is fine"))):
    wt = branch_for(n)
    pr_for(n)
    follow(n, "@rookery-agent resolve the conflicts", reply)
    assert fu["phases"] == ["rookery:error"] and "Agent error" in fu["said"][-1], fu["said"]
    assert ("markers are still in app.txt" if n == 22 else "answered` with merge conflicts") in fu["said"][-1]
    assert not g(wt, "status", "--porcelain") and g(wt, "rev-parse", "HEAD") == g(wt, "rev-parse", f"origin/feature/issue-{n}")
    assert not fu["pushed"], "nothing published"
# prod: a failed follow-up left rookery:error, and asking again did nothing. Now it runs again
phase = {"rookery:error"}
follow(22, "@rookery-agent try again: resolve the conflicts", resolve)
assert fu["pushed"] == ["feature/issue-22:feature/issue-22"] and fu["phases"][-1] == "rookery:done", fu["said"]
phase = {"rookery:changes-wanted"}

# a question: an answer on the issue, any edit reverted, no merge, no push, the phase unchanged
wt = branch_for(24)
pr_for(24, "MERGEABLE")
def answer(wt):
    assert "Rookery merged" not in fu["calls"][-1][1] and "in progress" not in fu["calls"][-1][1]
    (wt / "app.txt").write_text("oops\n", encoding="utf-8")
    return "answered", "Because the tests use it."
follow(24, "@rookery-agent why sqlite?", answer)
assert fu["said"] == ["Because the tests use it.\n\n_Rookery reverted the edits to app.txt: an answer changes nothing._"], fu["said"]
assert not fu["phases"] and not fu["pushed"] and [k for k, _ in fu["calls"]] == ["follow-up"]
assert (wt / "app.txt").read_text(encoding="utf-8") == "branch 24\n"
# someone pushed to the PR since: the worktree moves up to it, never resets it
g(up, "checkout", "-q", "feature/issue-24"), (up / "x.txt").write_text("x\n", encoding="utf-8")
g(up, "add", "-A"), g(up, "commit", "-qm", "a person's fix"), g(up, "push", "-q", "origin", "feature/issue-24")
g(up, "checkout", "-q", "main")
follow(24, "@rookery-agent and now?", lambda wt: ("answered", "Still fine."))
assert g(wt, "log", "-1", "--format=%s") == "a person's fix" and fu["said"] == ["Still fine."] and not fu["pushed"]
# a change asked for in words: committed as one follow-up commit, pushed
def change(wt):
    (wt / "x.txt").write_text("y\n", encoding="utf-8")
    (wt / agent.pdir(24) / "tasks.md").write_text("- [x] one\n- [ ] more\n", encoding="utf-8")
    return "changed", "Set x to y."
follow(24, "@rookery-agent set x to y\nand nothing else", change)
assert g(wt, "log", "-1", "--format=%s") == "#24 follow-up: set x to y" and fu["pushed"]
assert (wt / agent.pdir(24) / "tasks.md").read_text(encoding="utf-8") == "- [x] one\n", "a follow-up adds no tasks"

# a clean base merge, then an answer: HEAD moved, so it is pushed and reviewed
wt = branch_for(25, "other.txt")
pr_for(25, "MERGEABLE")
def clean(wt):
    assert "It merged cleanly" in fu["calls"][-1][1]
    return "answered", "Main is in now."
follow(25, "@rookery-agent is main in?", clean)
assert g(wt, "log", "-1", "--format=%s") == "#25 merge main" and fu["pushed"] == ["feature/issue-25:feature/issue-25"]
assert [k for k, _ in fu["calls"]] == ["follow-up", "review"] and fu["phases"][-1] == "rookery:done"
with agent.closing(agent.db()) as con:
    assert "follow-up" in {r[0] for r in con.execute("SELECT act FROM jobs")}, "a job of its own in the run log"
(agent.gh, agent.claude, agent.comment, agent.set_phase, agent.labels_of, agent.pin, agent.api, agent.token,
 agent.submit, agent.implement, agent.plan) = fu_saved
agent.sh = real_sh

print("ok")
