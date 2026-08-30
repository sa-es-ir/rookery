"""Run: python test_agent.py  (the gates only; no network, no gh)."""
import hashlib
import hmac
import os

os.environ.update(WEBHOOK_SECRET="s3cr3t", ALLOWLIST="alice, Bob")
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

# allowlist is normalised to lowercase
assert agent.ALLOW == {"alice", "bob"}

# webhook signature
raw = b'{"action":"opened"}'
good = "sha256=" + hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
assert hmac.compare_digest(good, "sha256=" + hmac.new(agent.SECRET, raw, hashlib.sha256).hexdigest())
assert not hmac.compare_digest(good, "sha256=" + hmac.new(b"wrong", raw, hashlib.sha256).hexdigest())

# per-repo config, base branch defaulting to main
agent.REPOS = {"o/r": {"path": "/src/r"}, "o/other": {"path": "/src/other", "base": "develop"}}
assert agent.ctx("o/r", "7") == ("o/r", 7, __import__("pathlib").Path("/src/r"), "main")
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

print("ok")
