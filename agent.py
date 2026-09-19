#!/usr/bin/env python3
"""GitHub issue -> Claude Code agent. Laptop, smee tunnel, one or more repos.

State lives on GitHub (labels + comments) and in the target repo
(agent-plans/<n>/). The only local state is a git worktree per issue; Claude
session ids are derived from repo#issue/key, so nothing is persisted between
runs.
"""
import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines() if (HERE / ".env").exists() else []:
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

# {"owner/name": {"path": "C:/src/name", "base": "main"}, ...}
REPOS = json.loads((HERE / "repos.json").read_text(encoding="utf-8")) if (HERE / "repos.json").exists() else {}
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
ALLOW = {u.strip().lower() for u in os.environ["ALLOWLIST"].split(",") if u.strip()}
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")  # "" = all interfaces, for a public webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.info

TRIGGER = "claude:trigger"
PHASES = ["claude:planning", "claude:awaiting-input", "claude:awaiting-approval",
          "claude:implementing", "claude:reviewing", "claude:done", "claude:error"]
APPROVE = re.compile(r"^\s*(approve|approved|lgtm|go ahead)\b", re.I)
MARKER = re.compile(r"^STATUS:[ \t]*(\S+)[ \t]*$", re.M)
TASK_LINE = re.compile(r"^[ \t]*[-*] \[( |x)(\] .+)$", re.M | re.I)
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
MAX_TASKS = 15

jobs: "queue.Queue" = queue.Queue()
ME = "rookery-gha[bot]"  # the App's bot login — filtered so we never answer ourselves


class Ctx(NamedTuple):
    """One issue in one configured repo — threaded through everything."""
    repo: str   # owner/name
    n: int      # issue number
    path: Path  # local clone of repo
    base: str   # base branch


def ctx(repo, n):
    cfg = REPOS[repo]
    return Ctx(repo, int(n), Path(cfg["path"]), cfg.get("base", "main"))


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


def labels_of(repo, n):
    return {l["name"] for l in json.loads(gh(repo, "issue", "view", str(n), "--json", "labels"))["labels"]}


def set_phase(c, phase):
    stale = [l for l in labels_of(c.repo, c.n) if l in PHASES and l != phase]
    args = ["issue", "edit", str(c.n), "--add-label", phase]
    for l in stale:
        args += ["--remove-label", l]
    gh(c.repo, *args)
    log(f"#{c.n} phase: {phase}")


# --- claude ------------------------------------------------------------------

def worktree(c):
    """Per-issue branch + working dir, namespaced by repo, so nothing collides."""
    wt = WORKSPACE / c.repo.replace("/", "_") / f"issue-{c.n}"
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
    log(f"#{c.n} claude {key}{' (resume)' if resume else ''} — this takes a while...")
    wt = worktree(c)
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
        status, msg = claude(c, TASK.format(n=c.n, d=pdir(c.n), task=text, base=c.base),
                             key=f"task-{i}")
        if status != "task-done":
            raise RuntimeError(f"task {i + 1} ({text}) reported {status}:\n\n{msg}")
        tick(wt, c.n, at)
        sh("git", "add", "-A", cwd=wt)
        sh("git", "commit", "-m", f"#{c.n} task {i + 1}: {text}", cwd=wt)
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
    title = json.loads(gh(c.repo, "issue", "view", str(c.n), "--json", "title"))["title"]
    url = gh(c.repo, "pr", "create", "--draft", "--base", c.base, "--head", branch,
             "--title", title, "--body-file", "-",
             input="\n\n".join(done) + f"\n\n## Review\n\n{review}\n\nCloses #{c.n}")
    # The review is advisory: the PR opens either way, so the work is always on
    # GitHub rather than stranded in a local worktree. A bad verdict only flags.
    ok = verdict == "review-ok"
    comment(c, f"Draft PR: {url}" if ok else
            f"Draft PR: {url}\n\nThe reviewer wants changes — see **Review** in the PR body.")
    set_phase(c, "claude:done" if ok else "claude:error")


def handle(job):
    c = ctx(job["repo"], job["n"])
    log(f"{c.repo}#{c.n} job: {job['kind']}")
    try:
        if job["kind"] == "start":
            plan(c, job["title"], job["body"], resume=False)
            return
        phase = labels_of(c.repo, c.n)
        # approving again after an error resumes at the first unticked task
        if APPROVE.match(job["body"]) and phase & {"claude:awaiting-approval", "claude:error"}:
            implement(c)
        elif phase & {"claude:awaiting-input", "claude:awaiting-approval"}:
            plan(c, None, job["body"], resume=True)
    except Exception as e:
        log(f"#{c.n} FAILED: {e}")
        comment(c, f"Agent error:\n\n```\n{e}\n```")
        set_phase(c, "claude:error")


def worker():
    while True:
        handle(jobs.get())


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
        try:
            enqueue(self.headers.get("X-GitHub-Event", ""), json.loads(raw))
        except Exception as e:
            print("dispatch error:", e, file=sys.stderr)

    def log_message(self, *a):
        pass


def enqueue(event, p):
    repo = p.get("repository", {}).get("full_name")
    if repo not in REPOS:
        return
    n = p.get("issue", {}).get("number")
    # `labeled` only: an issue created with the label fires `opened` *and* `labeled`,
    # both before the first job sets a phase — reacting to both plans it twice.
    if event == "issues" and p["action"] == "labeled":
        if p["label"]["name"] == TRIGGER and not labels_of(repo, n) & set(PHASES):
            jobs.put({"kind": "start", "repo": repo, "n": n,
                      "title": p["issue"]["title"], "body": p["issue"]["body"] or ""})
    elif event == "issue_comment" and p["action"] == "created":
        user = p["comment"]["user"]["login"].lower()
        if user != ME and user in ALLOW and labels_of(repo, n) & set(PHASES):
            jobs.put({"kind": "continue", "repo": repo, "n": n, "body": p["comment"]["body"]})


if __name__ == "__main__":
    if not REPOS:
        sys.exit("repos.json is empty or missing — see repos.example.json")
    if not APP_ID or not Path(APP_KEY).exists():
        sys.exit("set APP_ID and APP_KEY (path to the App's private key .pem) in .env")
    for repo in REPOS:
        for l in PHASES + [TRIGGER]:
            gh(repo, "label", "create", l, "--color", "5319e7", check=False)

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

    threading.Thread(target=worker, daemon=True).start()
    # log(), not print(): stdout is block-buffered under systemd, stderr is not,
    # so a print() banner never reaches the journal.
    log(f"listening on {HOST or '*'}:{PORT} for {', '.join(REPOS)} (bot user: {ME})")
    HTTPServer((HOST, PORT), Hook).serve_forever()
