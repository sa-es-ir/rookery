#!/usr/bin/env python3
"""GitHub issue -> Claude Code agent, for every repo the GitHub App is installed on.

State lives on GitHub (installations, labels, comments) and in the target repo
(agent-plans/<n>/). The only local state is a clone per repo, a git worktree
per issue, and rookery.db (the per-repo daily usage behind the caps, and the run
log the UI reads). Claude
session ids are derived from repo#issue/key, so no session state is persisted.
"""
import base64
import calendar
import collections
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines() if (HERE / ".env").exists() else []:
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

# "owner/name" -> base branch. Rebuilt from the App's installations by sync() at
# startup, kept current by installation events; "" until onboard() has run.
REPOS = {}
WORKSPACE = Path(os.environ.get("WORKSPACE", HERE / "work"))
# the `claude` CLI must use the claude.ai login; a stale key in the env fails it.
os.environ.pop("ANTHROPIC_API_KEY", None)
# GitHub credentials reach gh/git through env= per call, never os.environ, so the
# claude subprocess (which inherits os.environ) cannot read them.
for k in ("GH_TOKEN", "GITHUB_TOKEN"):
    os.environ.pop(k, None)
APP_ID = os.environ.get("APP_ID", "")
APP_KEY = os.environ.get("APP_KEY", str(HERE / "app.pem"))
SECRET = os.environ["WEBHOOK_SECRET"].encode()
# per repo per UTC day; a run is one claude call. RUN_CAP/USD_CAP override them.
RUN_CAP = int(os.environ.get("RUN_CAP", "10"))
USD_CAP = float(os.environ.get("USD_CAP", "20"))
PAUSED = HERE / "PAUSED"  # operator switch: while this file exists, no claude call starts
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")  # "" = all interfaces, for a public webhook

# threadName is the repo on a worker thread, so parallel repos' `#n` lines stay apart
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s", datefmt="%H:%M:%S")
log = logging.info

TRIGGER = "claude:trigger"
PHASES = ["claude:planning", "claude:awaiting-input", "claude:awaiting-approval",
          "claude:implementing", "claude:reviewing", "claude:done", "claude:error"]
APPROVE = re.compile(r"^\s*(approve|approved|lgtm|go ahead)\b", re.I)
MARKER = re.compile(r"^STATUS:[ \t]*(\S+)[ \t]*$", re.M)
TASK_LINE = re.compile(r"^[ \t]*[-*] \[( |x)(\] .+)$", re.M | re.I)
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
MAX_TASKS = 15

queues = {}                  # repo -> its job queue; a worker thread per queue
qlock = threading.Lock()     # guards queues, so a worker never exits past a new job
seen = collections.OrderedDict()  # recent X-GitHub-Delivery ids; GitHub redelivers
ME = "rookery-gha[bot]"  # the App's bot login — filtered so we never answer ourselves


class Ctx(NamedTuple):
    """One issue in one configured repo — threaded through everything."""
    repo: str   # owner/name
    n: int      # issue number
    path: Path  # local clone of repo
    base: str   # base branch


def ctx(repo, n):
    return Ctx(repo, int(n), clone_dir(repo), REPOS[repo])


def clone_dir(repo):
    return WORKSPACE / "repos" / repo.replace("/", "_")


def wt_root(repo):
    """Where repo's per-issue worktrees live."""
    return WORKSPACE / repo.replace("/", "_")


def pdir(n):
    """Per-issue state, inside the target repo, committed with the change."""
    return f"agent-plans/{n}"


# --- shell / gh --------------------------------------------------------------

def sh(*args, cwd=None, check=True, env=None, input=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       env=env, input=input)
    if check and p.returncode:
        raise RuntimeError(f"{args[0]} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def gh(repo, *args, check=True, input=None):
    return sh("gh", *args, "--repo", repo, check=check, env=auth(repo), input=input)


def comment(c, body):
    log(f"#{c.n} commenting ({len(body)} chars)")
    gh(c.repo, "issue", "comment", str(c.n), "--body-file", "-", input=body)


# --- GitHub App auth ---------------------------------------------------------

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt():
    """RS256 App JWT. The stdlib cannot sign RSA, so openssl does — an external
    CLI like gh and claude."""
    now = int(time.time())
    head = b64url(b'{"alg":"RS256","typ":"JWT"}') + "." + b64url(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": APP_ID}).encode())
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", APP_KEY, "-binary"],
                       input=head.encode(), capture_output=True, check=True)
    return head + "." + b64url(p.stdout)


def api(method, path, bearer):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def api_all(path, bearer, key=None):
    """Every page of a list endpoint; `key` names the list in a wrapped response."""
    out, page = [], 1
    while True:
        r = api("GET", f"{path}?per_page=100&page={page}", bearer)
        items = r[key] if key else r
        out += items
        if len(items) < 100:
            return out
        page += 1


_tokens = {}  # repo -> (installation token, expiry as epoch seconds)


def token(repo):
    """Installation token for repo, re-minted when under 10 minutes remain — an
    implementation outlives the 1h token, so every gh/git call asks again."""
    tok, exp = _tokens.get(repo, ("", 0))
    if exp - time.time() < 600:
        j = jwt()
        inst = api("GET", f"/repos/{repo}/installation", j)["id"]
        r = api("POST", f"/app/installations/{inst}/access_tokens", j)
        tok, exp = r["token"], calendar.timegm(time.strptime(r["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        _tokens[repo] = (tok, exp)
        log(f"{repo} minted installation token")
    return tok


def auth(repo):
    """env= for one gh/git call: GH_TOKEN for gh, an https auth header for git.
    GIT_CONFIG_* keeps the header out of argv and out of the clone's .git/config."""
    tok = token(repo)
    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    return {**os.environ, "GH_TOKEN": tok, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def can_write(repo, user):
    """write, maintain or admin on *this* repo — the only people who may steer it.
    maintain reports as "write" in `permission`; a non-collaborator gets read/none."""
    try:
        r = api("GET", f"/repos/{repo}/collaborators/{user}/permission", token(repo))
    except urllib.error.HTTPError as e:
        log(f"{repo} permission lookup for {user} failed: {e}")
        return False
    return r["permission"] in ("admin", "write")


def labels_of(repo, n):
    return {l["name"] for l in json.loads(gh(repo, "issue", "view", str(n), "--json", "labels"))["labels"]}


def set_phase(c, phase):
    stale = [l for l in labels_of(c.repo, c.n) if l in PHASES and l != phase]
    args = ["issue", "edit", str(c.n), "--add-label", phase]
    for l in stale:
        args += ["--remove-label", l]
    gh(c.repo, *args)
    step(c.repo, "phase", label=phase)
    log(f"#{c.n} phase: {phase}")


# --- usage and caps ----------------------------------------------------------

def db():
    """A fresh connection per call: per-repo workers write from several threads."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(WORKSPACE / "rookery.db", timeout=30)
    # runs: one row per claude call, for the caps. jobs/steps: the run log — one
    # job per plan/implement, its steps in order, so a failure is visible task by task.
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (repo TEXT, day TEXT, usd REAL);
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, repo TEXT, n INTEGER, act TEXT,
            title TEXT, started REAL, ended REAL, status TEXT);
        CREATE TABLE IF NOT EXISTS steps (job INTEGER, at REAL, what TEXT, detail TEXT);
    """)
    return con


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def record(repo, usd):
    with closing(db()) as con, con:
        con.execute("INSERT INTO runs VALUES (?, ?, ?)", (repo, today(), usd))


def usage(repo):
    """-> (runs, usd) for repo so far today."""
    with closing(db()) as con:
        return con.execute("SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM runs WHERE repo = ? AND day = ?",
                           (repo, today())).fetchone()


# --- run log -----------------------------------------------------------------
# active[repo] is the job its worker is running. One worker per repo, so one
# entry per repo.

active = {}


def begin(c, act, title=None):
    with closing(db()) as con, con:
        job = con.execute("INSERT INTO jobs (repo, n, act, title, started, status) VALUES (?, ?, ?, ?, ?, 'running')",
                          (c.repo, c.n, act, title, time.time())).lastrowid
    active[c.repo] = {"job": job}


def step(repo, what, **detail):
    """Append to the running job's log; outside a job (dry-run helpers, tests) a no-op."""
    if a := active.get(repo):
        with closing(db()) as con, con:
            con.execute("INSERT INTO steps VALUES (?, ?, ?, ?)", (a["job"], time.time(), what, json.dumps(detail)))


def finish(repo, status):
    if a := active.pop(repo, None):
        with closing(db()) as con, con:
            con.execute("UPDATE jobs SET ended = ?, status = ? WHERE id = ?", (time.time(), status, a["job"]))


def check_caps(repo):
    """Before every claude call, so a running implementation stops after the task it
    is on — that task is committed, and `approve` later resumes at the next one.
    ponytail: checked before the call, so one call can overshoot USD_CAP by its own cost."""
    if PAUSED.exists():
        raise RuntimeError(f"Rookery is paused by the operator ({PAUSED.name} file). Nothing ran.")
    runs, usd = usage(repo)
    if runs >= RUN_CAP or usd >= USD_CAP:
        raise RuntimeError(f"{repo} hit its daily cap: {runs}/{RUN_CAP} runs, ${usd:.2f}/${USD_CAP:.2f} "
                           "today. It resets at 00:00 UTC; comment again after that to continue.")


def cost_of(out):
    try:
        return float(json.loads(out).get("total_cost_usd") or 0)
    except ValueError:
        return 0.0


# --- claude ------------------------------------------------------------------

def worktree(c):
    """Per-issue branch + working dir, namespaced by repo, so nothing collides."""
    wt = wt_root(c.repo) / f"issue-{c.n}"
    if not wt.exists():
        log(f"#{c.n} creating worktree {wt} from origin/{c.base}")
        wt.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "fetch", "origin", c.base, cwd=c.path, env=auth(c.repo))
        sh("git", "worktree", "add", "-B", f"feature/issue-{c.n}", str(wt),
           f"origin/{c.base}", cwd=c.path)
    return wt


def claude(c, prompt, key="plan", resume=False, agent=None):
    """One session per key: planning is one long thread, each task is its own."""
    sid = str(uuid.uuid5(NS, f"{c.repo}#{c.n}/{key}"))
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           # guardrail: the agent never publishes. Deny rules beat bypassPermissions.
           "--disallowed-tools", "Bash(git push:*)", "Bash(gh:*)"]
    if agent:
        # an agent's `tools` list is a role hint, not a boundary — the deny
        # rules above and the checks in plan() are what actually enforce.
        cmd += ["--agents", AGENTS, "--agent", agent]
    else:
        # implementation tasks run as no agent, so the model is set here; the two
        # roles carry their own `model:` in agents/*.md.
        cmd += ["--model", "sonnet"]
    check_caps(c.repo)
    log(f"#{c.n} claude {key}{' (resume)' if resume else ''} — this takes a while...")
    wt, out, m = worktree(c), "", None
    try:
        try:
            out = sh(*cmd, *(["--resume", sid] if resume else ["--session-id", sid]), cwd=wt)
        except RuntimeError as e:
            # a previous run already created this session (it crashed, or we are being
            # re-run): the id is derived from repo#issue/key, so pick that session up.
            if resume or "already in use" not in str(e):
                raise
            log(f"#{c.n} session {sid} exists — resuming it")
            out = sh(*cmd, "--resume", sid, cwd=wt)
        text = json.loads(out).get("result", "")
        m = MARKER.search(text)
    finally:
        # a failed call still counts as a run; its cost is lost with its stdout
        record(c.repo, cost_of(out))
        step(c.repo, "claude", key=key, status=m.group(1) if m else "no-marker" if out else "failed",
             usd=cost_of(out))
    log(f"#{c.n} claude {key} -> {m.group(1) if m else 'NO STATUS MARKER'}")
    if not m:
        raise RuntimeError(f"no STATUS marker in agent reply:\n{text[-1500:]}")
    return m.group(1), (text[:m.start()] + text[m.end():]).strip()


# Named roles, loaded from agents/*.md and passed to the CLI as --agents/--agent.
# They hold the standing rules of a role; the templates below hold the job.
# Implementation tasks run as no agent at all — the default harness with the
# deny rules is the point.

def parse_agent(p):
    """The Claude Code agent file format, minimal subset: a folded `description: >`,
    an inline `tools: [...]`, and the body as the prompt. Same format `--plugin-dir`
    would read, so that swap deletes this function and changes nothing else."""
    fm, body = re.match(r"^---\n(.*?)\n---\n(.*)$",
                        p.read_text(encoding="utf-8"), re.S).groups()
    meta, key = {}, None
    for line in fm.splitlines():
        m = re.match(r"(\w+):[ \t]*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            meta[key] = "" if val == ">" else val   # folded block scalar: value is below
        elif line.strip() and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
    assert meta.pop("name", p.stem) == p.stem, f"{p}: name must match the filename"
    if "tools" in meta:
        meta["tools"] = [t.strip() for t in meta["tools"].strip("[]").split(",") if t.strip()]
    return {**meta, "prompt": body.strip()}


AGENTS = json.dumps({p.stem: parse_agent(p)
                     for p in sorted((HERE / "agents").glob("*.md"))})

PLAN = """You are planning work for GitHub issue #{n} in {repo}.

Title: {title}

{body}

Write two files, and change nothing else — no code, no commits:

- {d}/plan.md — a short implementation plan.
- {d}/tasks.md — that plan split into at most {max} tasks, each one a
  `- [ ] ` markdown checkbox on its own line, ordered, each independently
  implementable by someone who has read plan.md and nothing else.

If something is genuinely ambiguous, ask instead of guessing.

Reply with exactly one line `STATUS: questions` or `STATUS: plan-ready`, and
around it the markdown to post as an issue comment: the questions, or a summary
of the plan ending with "Reply `approve` to implement."
"""

REPLY = """A developer replied on issue #{n}:

{body}

Fold their answers into {d}/plan.md and {d}/tasks.md — those must stand on
their own, because the implementation sessions will not see this conversation.
Then reply with one line `STATUS: questions` or `STATUS: plan-ready` plus the
markdown to post as an issue comment.
"""

TASK = """You are implementing GitHub issue #{n}, one task at a time, in this
worktree. The approved plan is {d}/plan.md. Read {d}/decisions.md first if it
exists — earlier tasks recorded their trade-offs there.

Your only task right now:

{task}

Leave the other tasks in {d}/tasks.md alone. Do not commit, do not push, do not
touch {base}, do not use gh — the orchestrator commits for you.

Append any trade-off or non-obvious choice you make to {d}/decisions.md, one
short bullet each, with the reason. That file is the only memory the next task
will have of you.

Reply with exactly one line `STATUS: task-done` or `STATUS: failed`, plus one
short paragraph on what changed.
"""

REVIEW = """Review the finished work for GitHub issue #{n} before it goes to a
human as a draft pull request.

The approved plan is {d}/plan.md, the task list is {d}/tasks.md, and
{d}/decisions.md holds the trade-offs each task recorded. The change itself is
`git diff origin/{base}...HEAD`.

Read only: do not edit, do not commit, do not use gh.

Reply with exactly one line `STATUS: review-ok` or `STATUS: changes-needed`,
plus the findings as markdown, worst first. This text is posted verbatim into
the pull request body, so write it for the person about to read the diff.
"""


# --- tasks -------------------------------------------------------------------

def read_tasks(wt, n):
    """-> [(offset of the checkbox char, done?, text)] in file order."""
    p = wt / pdir(n) / "tasks.md"
    if not p.exists():
        raise RuntimeError(f"{pdir(n)}/tasks.md is missing — the plan produced no task list")
    text = p.read_text(encoding="utf-8")
    return [(m.start(1), m.group(1).lower() == "x", m.group(2)[2:].strip())
            for m in TASK_LINE.finditer(text)]


def tick(wt, n, at):
    """The orchestrator marks tasks done, never the agent."""
    p = wt / pdir(n) / "tasks.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text[:at] + "x" + text[at + 1:], encoding="utf-8")


# --- jobs --------------------------------------------------------------------

def keep_plans_only(c, wt):
    """Planning writes agent-plans/<n>/ and nothing else — revert the rest.

    The PLAN prompt asks for this; here it is enforced, because a prompt is not
    a guard. Every call to plan() reverts what the previous one left behind, so
    calling it again — which a long question-and-answer thread does repeatedly —
    can never accumulate stray edits ahead of the task loop.
    """
    keep = pdir(c.n) + "/"
    # -uall, not the default: plain --porcelain collapses an untracked tree to
    # its shallowest dir, so a first-ever plan reports as `?? agent-plans/` and
    # would fail the prefix test below and be deleted.
    for line in sh("git", "status", "--porcelain", "-uall", cwd=wt).splitlines():
        # sh() strips its output, so the leading space of a " M path" code is
        # gone on the first line but not the rest — normalise, never index.
        code, _, path = line.strip().partition(" ")
        path = path.strip().split(" -> ")[-1].strip('"')  # a rename reports both sides
        if path.startswith(keep):
            continue
        log(f"#{c.n} planner touched {path} — reverting")
        # `clean` takes untracked files and whole untracked dirs; `checkout`
        # restores modified and deleted tracked ones.
        sh("git", *(["clean", "-fdq"] if code == "??" else ["checkout"]), "--", path, cwd=wt)


def plan(c, title, body, resume):
    set_phase(c, "claude:planning")
    prompt = (REPLY.format(n=c.n, body=body, d=pdir(c.n)) if resume
              else PLAN.format(n=c.n, repo=c.repo, title=title, body=body,
                               d=pdir(c.n), max=MAX_TASKS))
    status, msg = claude(c, prompt, resume=resume, agent="planmaker")
    keep_plans_only(c, worktree(c))
    comment(c, msg)
    set_phase(c, "claude:awaiting-input" if status == "questions" else "claude:awaiting-approval")


def implement(c):
    """One fresh session per unticked task. Re-running resumes where it stopped."""
    set_phase(c, "claude:implementing")
    wt, done = worktree(c), []
    for _ in range(MAX_TASKS + 1):
        tasks = read_tasks(wt, c.n)
        if len(tasks) > MAX_TASKS:
            raise RuntimeError(f"{len(tasks)} tasks exceeds the {MAX_TASKS}-task cap — "
                               "split the issue or shorten the plan")
        todo = next(((i, at, text) for i, (at, is_done, text) in enumerate(tasks) if not is_done), None)
        if todo is None:
            break
        i, at, text = todo
        log(f"#{c.n} task {i + 1}/{len(tasks)}: {text}")
        step(c.repo, "task", i=i + 1, of=len(tasks), text=text)
        status, msg = claude(c, TASK.format(n=c.n, d=pdir(c.n), task=text, base=c.base),
                             key=f"task-{i}")
        if status != "task-done":
            raise RuntimeError(f"task {i + 1} ({text}) reported {status}:\n\n{msg}")
        tick(wt, c.n, at)
        step(c.repo, "tick", i=i + 1, of=len(tasks))
        sh("git", "add", "-A", cwd=wt)
        sh("git", "commit", "-m", f"#{c.n} task {i + 1}: {text}", cwd=wt)
        step(c.repo, "commit", i=i + 1, sha=sh("git", "rev-parse", "--short", "HEAD", cwd=wt))
        log(f"#{c.n} committed task {i + 1}")
        done.append(f"**{i + 1}. {text}**\n\n{msg}")
    else:
        raise RuntimeError("task loop did not converge")

    branch = f"feature/issue-{c.n}"
    assert branch != c.base

    set_phase(c, "claude:reviewing")
    verdict, review = claude(c, REVIEW.format(n=c.n, d=pdir(c.n), base=c.base),
                             key="review", agent="reviewer")

    log(f"#{c.n} pushing {branch} and opening draft PR")
    # an explicit https URL, so the push is the App's even when origin is ssh
    sh("git", "push", f"https://github.com/{c.repo}.git", f"{branch}:{branch}",
       cwd=wt, env=auth(c.repo))
    # a push to a URL updates no remote-tracking ref; record it, so unpushed()
    # sees this work as safe on GitHub
    sh("git", "update-ref", f"refs/remotes/origin/{branch}", branch, cwd=wt)
    step(c.repo, "push", branch=branch)
    title = json.loads(gh(c.repo, "issue", "view", str(c.n), "--json", "title"))["title"]
    url = gh(c.repo, "pr", "create", "--draft", "--base", c.base, "--head", branch,
             "--title", title, "--body-file", "-",
             input="\n\n".join(done) + f"\n\n## Review\n\n{review}\n\nCloses #{c.n}")
    # The review is advisory: the PR opens either way, so the work is always on
    # GitHub rather than stranded in a local worktree. A bad verdict only flags.
    step(c.repo, "pr", url=url)
    ok = verdict == "review-ok"
    comment(c, f"Draft PR: {url}" if ok else
            f"Draft PR: {url}\n\nThe reviewer wants changes — see **Review** in the PR body.")
    set_phase(c, "claude:done" if ok else "claude:error")


# --- repos: onboarding and retention ----------------------------------------

def sync():
    """Rebuild REPOS from every installation of the App — GitHub is the source of truth."""
    for inst in api_all("/app/installations", jwt()):
        tok = api("POST", f"/app/installations/{inst['id']}/access_tokens", jwt())["token"]
        for r in api_all("/installation/repositories", tok, "repositories"):
            onboard(r["full_name"], r["default_branch"])


def onboard(repo, base=None):
    """Make repo live: clone it once, create the labels, record its base branch."""
    base = base or api("GET", f"/repos/{repo}", token(repo))["default_branch"]
    path = clone_dir(repo)
    if not path.exists():
        log(f"{repo} cloning into {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", f"https://github.com/{repo}.git", str(path), env=auth(repo))
    for l in PHASES + [TRIGGER]:
        gh(repo, "label", "create", l, "--color", "5319e7", check=False)
    REPOS[repo] = base
    log(f"{repo} live, base {base}")


def unpushed(clone, ref):
    """True when ref has commits on no remote-tracking ref — work that exists only here."""
    return bool(sh("git", "rev-list", "-1", ref, "--not", "--remotes", cwd=clone))


def rmtree(p):
    # git makes object files read-only, and Windows will not delete those as-is
    shutil.rmtree(p, onexc=lambda f, x, _: (os.chmod(x, stat.S_IWRITE), f(x)))


def drop_worktree(repo, n):
    """The issue's PR is closed: its worktree has nothing left to do."""
    wt, clone, branch = wt_root(repo) / f"issue-{n}", clone_dir(repo), f"feature/issue-{n}"
    if not wt.exists():
        return
    if unpushed(clone, branch):
        log(f"{repo}#{n} {branch} has unpushed commits — keeping {wt}")
        return
    sh("git", "worktree", "remove", "--force", str(wt), cwd=clone)
    log(f"{repo}#{n} removed worktree {wt}")


def offboard(repo):
    """repo left the installation: free its clone and worktrees, unless that loses work."""
    clone = clone_dir(repo)
    if not clone.exists():
        return
    heads = [l.split()[1] for l in sh("git", "worktree", "list", "--porcelain", cwd=clone).splitlines()
             if l.startswith("branch ")]
    if stuck := [h for h in heads if unpushed(clone, h)]:
        log(f"{repo} NOT removed — unpushed commits on {', '.join(stuck)}")
        return
    if wt_root(repo).exists():
        rmtree(wt_root(repo))
    rmtree(clone)
    log(f"{repo} removed {clone} and its worktrees")


# --- dispatch ----------------------------------------------------------------

def handle(job):
    if job["kind"] == "onboard":
        return onboard(job["repo"])
    if job["kind"] == "offboard":
        return offboard(job["repo"])
    if job["kind"] == "drop-worktree":
        return drop_worktree(job["repo"], job["n"])
    c = ctx(job["repo"], job["n"])
    log(f"{c.repo}#{c.n} job: {job['kind']}")
    try:
        if job["kind"] == "start":
            begin(c, "plan", job["title"])
            plan(c, job["title"], job["body"], resume=False)
        else:
            phase = labels_of(c.repo, c.n)
            # approving again after an error resumes at the first unticked task
            if APPROVE.match(job["body"]) and phase & {"claude:awaiting-approval", "claude:error"}:
                begin(c, "implement")
                implement(c)
            elif phase & {"claude:awaiting-input", "claude:awaiting-approval"}:
                begin(c, "reply")
                plan(c, None, job["body"], resume=True)
        finish(c.repo, "ok")
    except Exception as e:
        log(f"#{c.n} FAILED: {e}")
        step(c.repo, "error", message=str(e)[-2000:])
        try:
            comment(c, f"Agent error:\n\n```\n{e}\n```")
            set_phase(c, "claude:error")
        finally:
            finish(c.repo, "failed")


def submit(job):
    """Onto the repo's own queue: repos run in parallel, one job at a time within a
    repo. The queue and its worker are created on the repo's first job."""
    with qlock:
        if job["repo"] not in queues:
            queues[job["repo"]] = queue.Queue()
            threading.Thread(target=worker, args=(job["repo"], queues[job["repo"]]),
                             name=job["repo"], daemon=True).start()
        queues[job["repo"]].put(job)


def worker(repo, q):
    while True:
        job = q.get()
        try:
            handle(job)
        except Exception as e:  # e.g. the error comment itself failed: never let the worker die
            log(f"{job['repo']} {job['kind']} FAILED: {e}")
        with qlock:
            # the repo left and nothing followed: stop. A re-add starts a fresh worker.
            if job["kind"] == "offboard" and q.empty():
                del queues[repo]
                return


# --- webhook -----------------------------------------------------------------

class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        sig = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, self.headers.get("X-Hub-Signature-256", "")):
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()
        if not fresh(self.headers.get("X-GitHub-Delivery")):
            log(f"duplicate delivery {self.headers['X-GitHub-Delivery']} — ignored")
            return
        try:
            enqueue(self.headers.get("X-GitHub-Event", ""), json.loads(raw))
        except Exception as e:
            print("dispatch error:", e, file=sys.stderr)

    def log_message(self, *a):
        pass


def fresh(delivery, keep=1000):
    """False for a delivery id seen recently. In memory: a restart forgetting it is fine.
    Only the HTTP thread calls this — HTTPServer serves one request at a time."""
    if not delivery:
        return True
    if delivery in seen:
        return False
    seen[delivery] = None
    if len(seen) > keep:
        seen.popitem(last=False)
    return True


def enqueue(event, p):
    if event in ("installation", "installation_repositories"):
        every = p.get("repositories") or []
        added = p.get("repositories_added", []) + (every if p["action"] == "created" else [])
        removed = p.get("repositories_removed", []) + (every if p["action"] == "deleted" else [])
        for r in added:
            # accept its events now; the queue runs them after the onboarding
            REPOS.setdefault(r["full_name"], "")
            submit({"kind": "onboard", "repo": r["full_name"]})
        for r in removed:
            REPOS.pop(r["full_name"], None)
            submit({"kind": "offboard", "repo": r["full_name"]})
        return
    repo = p.get("repository", {}).get("full_name")
    if repo not in REPOS:
        return
    if event == "pull_request" and p["action"] == "closed":
        m = re.fullmatch(r"feature/issue-(\d+)", p["pull_request"]["head"]["ref"])
        if m and p["pull_request"]["user"]["login"] == ME:  # our PRs only
            submit({"kind": "drop-worktree", "repo": repo, "n": int(m.group(1))})
        return
    n = p.get("issue", {}).get("number")
    # `labeled` only: an issue created with the label fires `opened` *and* `labeled`,
    # both before the first job sets a phase — reacting to both plans it twice.
    if event == "issues" and p["action"] == "labeled":
        if p["label"]["name"] == TRIGGER and not labels_of(repo, n) & set(PHASES):
            submit({"kind": "start", "repo": repo, "n": n,
                      "title": p["issue"]["title"], "body": p["issue"]["body"] or ""})
    elif event == "issue_comment" and p["action"] == "created":
        user = p["comment"]["user"]["login"].lower()
        # approvals and replies alike: only people who can write to *this* repo
        if user != ME and labels_of(repo, n) & set(PHASES) and can_write(repo, user):
            submit({"kind": "continue", "repo": repo, "n": n, "body": p["comment"]["body"]})


if __name__ == "__main__":
    if not APP_ID or not Path(APP_KEY).exists():
        sys.exit("set APP_ID and APP_KEY (path to the App's private key .pem) in .env")
    sync()

    if len(sys.argv) > 2:
        # dry run, no webhook: `agent.py owner/repo 12 [comment...]`
        repo, n = sys.argv[1], int(sys.argv[2])
        if len(sys.argv) > 3:
            handle({"kind": "continue", "repo": repo, "n": n, "body": " ".join(sys.argv[3:])})
        else:
            iss = json.loads(gh(repo, "issue", "view", str(n), "--json", "title,body"))
            handle({"kind": "start", "repo": repo, "n": n,
                    "title": iss["title"], "body": iss["body"] or ""})
        sys.exit()

    # log(), not print(): stdout is block-buffered under systemd, stderr is not,
    # so a print() banner never reaches the journal.
    log(f"listening on {HOST or '*'}:{PORT} for {', '.join(REPOS) or 'no repos yet'} (bot user: {ME})")
    HTTPServer((HOST, PORT), Hook).serve_forever()
