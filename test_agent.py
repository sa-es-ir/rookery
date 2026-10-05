"""Run: python test_agent.py  (the gates only; no network, no gh)."""
import hashlib
import hmac
import os

os.environ.update(WEBHOOK_SECRET="s3cr3t", REPOS_ONLY="", PROVIDER="claude", OWN="o")  # before import: .env only fills unset keys
import agent  # noqa: E402

real_sh, real_handle = agent.sh, agent.handle

# approval is a regex, not a judgement call
assert agent.APPROVE.match("approve")
assert agent.APPROVE.match("  LGTM, ship it")
assert agent.APPROVE.match("go ahead")
assert not agent.APPROVE.match("please approve later")
assert not agent.APPROVE.match("not approved yet")
assert not agent.APPROVE.match("approver should look first")

# STATUS marker is parsed, never inferred; the rest is the comment body
m = agent.MARKER.search("Some questions\n\nSTATUS: questions\n1. which db?")
assert m and m.group(1) == "questions"
assert not agent.MARKER.search("the STATUS: plan-ready marker must be on its own line")
for line in ("**STATUS: review-ok**", "**STATUS:** review-ok", "STATUS: review-ok  "):   # prod: haiku bolds it
    m = agent.MARKER.search(f"fine\n{line}\nmore")
    assert m and m.group(1) == "review-ok", line
    assert (m.string[:m.start()] + m.string[m.end():]) == "fine\n\nmore", "the whole line is stripped"
assert not agent.MARKER.search("**STATUS: review-ok** but see below")

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

tasks = agent.read_tasks(wt, 7)
assert [t[1] for t in tasks] == [True, False, False], tasks
assert [t[2] for t in tasks] == ["scaffold the module", "wire the handler", "add the test"]

first_open = next(t for t in tasks if not t[1])
agent.tick(wt, 7, first_open[0])
after = agent.read_tasks(wt, 7)
assert [t[1] for t in after] == [True, True, False]
assert [t[2] for t in after] == [t[2] for t in tasks], "ticking must not disturb the text"
assert "- [x] wire the handler" in (d / "tasks.md").read_text(encoding="utf-8")

# every tick strictly reduces the open count, so the loop terminates
for at, _, _ in [t for t in after if not t[1]]:
    agent.tick(wt, 7, at)
assert not [t for t in agent.read_tasks(wt, 7) if not t[1]]

# the two named roles, and the review verdicts the orchestrator branches on
roles = {p.stem: agent.parse_agent(p, p.stem) for p in (agent.HERE / "agents").glob("*.md")}
assert set(roles) == {"planmaker", "reviewer", "pr-reviewer"}
assert all(r["description"] for r in roles.values())
assert "Edit" not in roles["reviewer"]["tools"] + roles["pr-reviewer"]["tools"], "reviewers report, never fix"
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
assert out.startswith("## 🟡 Minor changes\n\nMostly fine.\n\n> **Your note:** yes\n> it is\n\n"), out
assert "| 1 | 🟡 medium | `a.py:3` | a \\| b c |\n| 2 | ⚪ low |  | nit |" in out, "| escaped, one line per row"
assert "<details><summary>Details</summary>\n\n**1. a \\| b c**\n\nwhy\n\n</details>" in out and "**2." not in out
assert "prose" not in out and out.endswith("<sub>rk:x</sub>\n")
out = rv("safe-to-merge", {"summary": "Sound.", "answer": "", "findings": []})
assert out == "## 🟢 Safe to merge\n\nSound.\n\n<sub>rk:x</sub>\n", "no note, no findings: header and summary only"
out = rv("changes-needed", {"summary": "s" * 900, "findings": [{"severity": "low", "title": "t" * 200,
                                                               "detail": "d" * 900}] * 12})
assert "s" * 599 + "…" in out and "t" * 149 + "…" in out and "d" * 799 + "…" in out and "| 10 |" in out
assert "| 11 |" not in out and "2 more findings not shown." in out, "cut, never failed"
fenced = '<review>\n```json\n{"summary": "ok", "findings": []}\n```\n</review>'
assert rv("safe-to-merge", fenced, raw=True).startswith("## 🟢"), "a code fence inside the tags is tolerated"
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

agent.keep_plans_only(agent.Ctx("o/r", 7, repo, "main"), repo)

assert (repo / agent.pdir(7) / "plan.md").read_text(encoding="utf-8") == "the plan\n"
assert (repo / "src.py").read_text(encoding="utf-8") == "original\n", "tracked edit not reverted"
assert not (repo / "stray.txt").exists(), "untracked stray not cleaned"

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
assert agent.provider_for("o/r") == "claude", ".env's"
put("provider", "glm")
assert agent.provider_for("o/r") == "glm", "the stored default beats .env"
put("provider:o", "deepseek")
assert agent.provider_for("o/r") == "deepseek" and agent.provider_for("acme/r") == "glm", "per owner, not per repo"
put("provider:o", None)
assert agent.provider_for("o/r") == "glm", "a dropped override falls back"
put("provider", None)
assert agent.provider_for("o/r") == "claude"
# the subscription only for an owner in OWN: anyone else raises, from an override or the default
for setup in ((), (("provider:acme", "claude"),), (("provider", "glm"), ("provider:acme", "claude"))):
    for k, v in setup:
        put(k, v)
    try:
        agent.provider_for("acme/r")
        raise AssertionError(setup)
    except RuntimeError as e:
        assert "no provider for acme" in str(e), e
    for k, _ in setup:
        put(k, None)
assert agent.provider_for("O/r") == "claude", "OWN ignores case"
put("provider:acme", "deepseek")
assert agent.provider_for("acme/r") == "deepseek", "an override is the operator's explicit choice"
put("provider:acme", None)
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
argv = []
agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "done\nSTATUS: task-done", "total_cost_usd": 0.4})
c = agent.Ctx("o/r", 9, agent.WORKSPACE, "main")
assert agent.claude(c, "p", key="task-0") == ("task-done", "done")
# no MCP: neither the target repo's .mcp.json nor the login account's connectors
assert "--strict-mcp-config" in argv[0] and "--mcp-config" not in argv[0], argv
# none of the target repo's settings: its hooks would run here, its skills/CLAUDE.md come from base
assert argv[0][argv[0].index("--setting-sources") + 1] == "user", argv
assert not [x for x in argv[0] if x.startswith(("--plugin-dir", "--append-system-prompt"))], "outside a job: none"
assert argv[0][1:3] == ("-p", "p"), argv      # prompt right after -p: --disallowed-tools is variadic
# deny rules see the hook-rewritten command: the RTK forms must be denied too
assert {"Bash(git push:*)", "Bash(gh:*)", "Bash(rtk git push:*)", "Bash(rtk gh:*)"} <= set(argv[0]), argv
assert "Bash(rtk proxy:*)" in argv[0], "rtk proxy runs a command raw: none of the four would match it"
# RUN_AS: the same argv, run as that user through sudo (DEPLOY.md §5b); unset, no sudo
assert "sudo" not in argv[0], argv
agent.RUN_AS = "rookery-run"
argv.clear(), agent.claude(c._replace(repo="o/runas"), "p", key="task-0")   # its own cap
agent.RUN_AS = ""
assert argv[0][:5] == ("sudo", "-n", "-H", "-u", "rookery-run"), argv
assert argv[0][argv[0].index("claude"):][1:3] == ("-p", "p") and "Bash(rtk proxy:*)" in argv[0], argv
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
assert sorted(p.name for p in (a["plugin"] / "agents").iterdir()) == ["planmaker.md", "pr-reviewer.md", "reviewer.md"]
assert a["workflow"]["pr-review"] == agent.WORKFLOW["pr-review"], "a slot the repo leaves out is the built-in's"
assert sorted(p.name for p in a["plugin"].iterdir()) == [".claude-plugin", "agents", "skills"], "no hooks, no MCP"
argv[:] = []
agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "STATUS: plan-ready"})
agent.claude(pc, "p", key="plan", slot="plan")
agent.claude(pc, "p", key="task-0", slot="task")
agent.sh = real_sh
plan_argv, task_argv = argv
assert plan_argv[plan_argv.index("--agent") + 1] == "rk:planmaker" and "--model" not in plan_argv, plan_argv
assert plan_argv[2] == "p\n\nUse the `rk:repo-recon` skill.\n\n" + agent.OFFER_SKILLS.format(names="`rk:verify`"), \
    "an enabled skill is named, or it never fires; the repo's others are offered"
sysp = next(x for x in plan_argv if x.startswith("--append-system-prompt="))
assert "Use tabs." in sysp and sysp.endswith("One migration per task."), "instructions follow the persona"
assert task_argv[2].endswith("Use the `rk:systematic-debugging` skill.\n\n"
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
                                                                        "reviewer.md", "sec.md"], "top-level *.md only"
assert (a["plugin"] / "agents" / "reviewer.md").read_text(encoding="utf-8").endswith("the repo's reviewer\n")
assert a["agents"]["reviewer"]["model"] == "sonnet" and a["agent_sources"]["planmaker"] == "built-in"
argv[:] = []
agent.sh = lambda *a, **k: argv.append(a) or json.dumps({"result": "STATUS: check-ok"})
assert agent.claude(pc, "p", key="step-sec", slot=a["workflow"]["after"][0]) == ("check-ok", "")
agent.sh = real_sh
assert argv[0][argv[0].index("--agent") + 1] == "rk:sec" and argv[0][2] == "p\n\nUse the `rk:verify` skill.\n\n" + agent.OFFER_SKILLS.format(names="`rk:repo-recon`")
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
assert "## 🤖 Sec agent · check · rk:verify\n\n### 🟢 Looks good\n\nNo holes.\n\n<sub>rk:sec · rk:verify</sub>\n\n" \
       "_Rookery reverted" in body_, body_
assert "## 🤖 Docs agent · write\n\nwrote docs" in body_, body_
assert body_.index("Sec agent") < body_.index("Docs agent") < body_.index("## 🤖 Reviewer agent")
assert "## 🤖 Reviewer agent\n\n### 🟢 Looks good\n\nFine.\n\n| # |" in body_ and "<sub>rk:reviewer</sub>" in body_, \
    "the reviewer's JSON is rendered like a PR review, one heading level down"
assert body_.endswith(" · steps: sec, docs"), body_[-80:]
assert phases[-1] == "rookery:changes-wanted" and "Changes wanted by sec" in said[-1], "an edit in a check is flagged"
# approve again: the committed write step is skipped, the check runs again
calls.clear()
agent.implement(pc)
assert calls == ["step-sec", "review"], calls
assert len(prs) == 1 and pr_said[0][0] == "https://github.com/o/r/pull/1" and "Sec agent" in pr_said[0][1], \
    "the re-run's sections go on the open PR"
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
        "## 🔴 Changes needed\n\nBreaks x.\n\n| # | Severity | Where | Finding |\n|---|---|---|---|\n"
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

# remember(): one haiku call, one turn, no tools, in an empty dir; the orchestrator writes the notes
notes_f, replies, argv = agent.memory_file("o/r"), [], []
agent.worktree = lambda c: (_ for _ in ()).throw(AssertionError("the memory call needs no worktree"))
agent.check_caps = lambda repo: None
def fake_sh(*a, **k):
    argv.append((a, k["cwd"]))
    assert not os.listdir(k["cwd"]), "an empty cwd: nothing to read or edit"
    return json.dumps({"result": replies.pop(0), "total_cost_usd": 0.01})
real_sh_, agent.sh = agent.sh, fake_sh
agent.comment = agent.set_phase = nope   # a side channel: never a comment or a label
memjob = {"kind": "memory", "repo": "o/r", "n": 30, "job": learned}
replies.append("<notes>\n- Tests: `npm test`, needs `-x`.\n</notes>\nSTATUS: memory-updated")
agent.handle(memjob)
assert notes_f.read_text(encoding="utf-8") == "- Tests: `npm test`, needs `-x`."
cmd, cwd = argv[0]
assert cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--max-turns") + 1] == "1", cmd
assert cmd[cmd.index("--tools") + 1] == "" and "--agent" not in cmd and "Bash(rtk proxy:*)" in cmd
assert not [x for x in cmd if x.startswith(("--plugin-dir", "--append-system-prompt"))], "nothing steers it"
prompt_ = cmd[2]
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
assert "- Tests: `npm test`" in argv[-1][0][2], "the next call sees the current notes"
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
          "after": [{**agent.STEP, "agent": "sec", "mode": "check"}]}
    owf = {**agent.WORKFLOW, "review": {**agent.WORKFLOW["review"], "instructions": "Owner's."}, "after": []}
    return {"sha": "abc", "claude_md": "é", "workflow": wf, "sources": {"tdd": "catalog"},
            "skills": {"tdd": "red, green"}, "agents": {"planmaker": {"model": "opus"}, "reviewer": {}, "pr-reviewer": {},
                                                        "sec": {"model": "haiku", "description": "holes"}},
            "agent_sources": {"planmaker": "built-in", "reviewer": "built-in", "pr-reviewer": "built-in",
                              "sec": ".rookery/agents"},
            "org": {"repo": "o/.rookery", "sha": "def"}, "org_workflow": owf}
agent.steer = fake_steer
real_files, agent.config_files = agent.config_files, lambda repo: {"workflow.json": "{}\n"}
w = body(req("GET", "/api/repos/o/r/workflow", sid=sid))
assert w["sha"] == "abc" and w["claude_md"] == 2 and w["error"] is None, w
assert [(k, v["agent"], v["model"], v["from"]) for k, v in w["slots"].items()] == [
    ("plan", "planmaker", "opus", "built-in"), ("task", None, "sonnet", "repo"),
    ("review", "reviewer", None, "repo"), ("pr-review", "pr-reviewer", None, "built-in")], \
    "the repo's review is the built-in's, but over the owner's"
assert w["org"] == {"repo": "o/.rookery", "sha": "def"}
assert w["skills"] == {"tdd": {"source": "catalog", "description": "red, green"}}
assert [(s["agent"], s["mode"], s["model"], s["from"]) for s in w["after"]] == [("sec", "check", "haiku", "repo")]
assert w["agents"]["sec"] == {"description": "holes", "source": ".rookery/agents"}
assert w["config"] == {"dir": ".rookery/", "files": {"workflow.json": "{}\n"}}, "what the editor starts from"
assert not dirs[0].exists(), "the throwaway plugin dir is gone"
agent.steer = lambda *a: (_ for _ in ()).throw(RuntimeError("`task` names the skill `x`"))
w = body(req("GET", "/api/repos/o/r/workflow", sid=sid))
assert w["error"] == "`task` names the skill `x`", "the error the next job would fail with"
assert w["config"]["files"], "a broken config can still be edited"
agent.steer, agent.config_files = real_steer, real_files

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
assert a["default"] == "claude" and a["overrides"] == {} and a["owners"] == [{"owner": "o", "repos": 2, "own": True}], a
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
assert a["overrides"] == {"acme": "glm"} and agent.provider_for("acme/x") == "glm" and agent.provider_for("o/r") == "claude"
assert {"owner": "acme", "repos": 0, "own": False} in a["owners"], "an override shows with no repo served"
assert body(setp({"owner": None, "provider": "glm"}))["default"] == "glm" and agent.provider_for("o/r") == "glm"
assert body(setp({"owner": "acme", "provider": None}))["overrides"] == {}
r = setp({"owner": "acme", "provider": "claude"})
assert r[0] == 422 and "OWN" in body(r)["error"], "the subscription is not for acme"
assert body(setp({"owner": "O", "provider": "claude"}))["overrides"] == {"O": "claude"}, "o is in OWN"
setp({"owner": "O", "provider": None})
setp({"owner": None, "provider": "claude"})
assert body(req("GET", "/api/admin", sid=sid))["default"] == "claude", "the default may stay claude"
agent.KEYS["glm"], web.ADMINS = "", set()
assert req("GET", "/api/admin", sid=sid)[0] == 404

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

print("ok")
