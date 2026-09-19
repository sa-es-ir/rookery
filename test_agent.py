"""Run: python test_agent.py  (the gates only; no network, no gh)."""
import hashlib
import hmac
import os

os.environ.update(WEBHOOK_SECRET="s3cr3t")
import agent  # noqa: E402

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
roles = __import__("json").loads(agent.AGENTS)
assert set(roles) == {"planmaker", "reviewer"}
assert all(r["description"] and r["prompt"] for r in roles.values())
assert "Edit" not in roles["reviewer"]["tools"], "the reviewer reports, never fixes"
assert all(r["model"] == "opus" for r in roles.values()), "roles think; tasks grind on sonnet"
assert agent.MARKER.search("looks fine\nSTATUS: review-ok").group(1) == "review-ok"
assert agent.MARKER.search("STATUS: changes-needed\nbug in foo.py").group(1) == "changes-needed"

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

agent.offboard("o/r")
assert clone.exists() and wt1.exists(), "offboard must refuse while a branch is unpushed"
agent.drop_worktree("o/r", 1)
assert wt1.exists(), "drop_worktree must keep an unpushed worktree"
agent.drop_worktree("o/r", 2)
assert not (agent.wt_root("o/r") / "issue-2").exists()

git(wt1, "push", "-q", str(origin), "feature/issue-1:feature/issue-1")  # as implement() does: a URL...
git(wt1, "update-ref", "refs/remotes/origin/feature/issue-1", "feature/issue-1")  # ...then the ref
agent.offboard("o/r")
assert not clone.exists() and not agent.wt_root("o/r").exists(), "pushed work: the disk is freed"

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
agent.labels_of = lambda repo, n: {"claude:awaiting-approval"}
for repo, user in [("o/r", "alice"), ("o/other", "alice"), ("o/r", "carol"), ("o/r", agent.ME)]:
    agent.enqueue("issue_comment", {"action": "created", "repository": {"full_name": repo},
                                    "issue": {"number": 9}, "comment": {"user": {"login": user}, "body": "approve"}})
assert [(j["repo"], j["body"]) for j in drain()] == [("o/r", "approve")]

# caps: runs and dollars per repo per UTC day, in SQLite, so a restart keeps them
agent.WORKSPACE = Path(tempfile.mkdtemp())
agent.PAUSED = agent.WORKSPACE / "PAUSED"
agent.RUN_CAP, agent.USD_CAP = 3, 1.0
assert agent.cost_of('{"result": "x", "total_cost_usd": 0.25}') == 0.25
assert agent.cost_of("claude crashed") == 0.0
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
agent.sh = lambda *a, **k: json.dumps({"result": "done\nSTATUS: task-done", "total_cost_usd": 0.4})
c = agent.Ctx("o/r", 9, agent.WORKSPACE, "main")
assert agent.claude(c, "p", key="task-0") == ("task-done", "done")
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

print("ok")
