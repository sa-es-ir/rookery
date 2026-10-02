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
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
import urllib.request
import uuid
from contextlib import closing
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
# comma-separated owner/name: serve only these of the App's repos. For a local
# instance next to the server, so the two never work the same repo; unset = all.
ONLY = {r.strip() for r in os.environ.get("REPOS_ONLY", "").split(",") if r.strip()}
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")  # "" = all interfaces, for a public webhook

# threadName is the repo on a worker thread, so parallel repos' `#n` lines stay apart
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s", datefmt="%H:%M:%S")
log = logging.info

TRIGGER = "rookery:trigger"
PHASES = ["rookery:planning", "rookery:awaiting-input", "rookery:awaiting-approval",
          "rookery:implementing", "rookery:reviewing", "rookery:done", "rookery:error"]
APPROVE = re.compile(r"^\s*(approve|approved|lgtm|go ahead)\b", re.I)
MARKER = re.compile(r"^STATUS:[ \t]*(\S+)[ \t]*$", re.M)
TASK_LINE = re.compile(r"^[ \t]*[-*] \[( |x)(\] .+)$", re.M | re.I)
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
MAX_TASKS = 15
# CLAUDE.md goes to the CLI as one argv entry, and Linux caps one at 128 KB
CLAUDE_MD_MAX = 64 * 1024
# every skill and agent name: it is rendered into prompts ("Use the `rk:x` skill") and --agent
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# frontmatter may say no more than this — `hooks:` would run shell here, `mcpServers:`
# reach out from here, `permissionMode:` loosen what the deny rules hold
SKILL_KEYS = ("name", "description", "allowed-tools")
AGENT_KEYS = ("name", "description", "tools", "model", "skills")
MODELS = ("haiku", "sonnet", "opus")
# the built-in workflow: today's pipeline, no skills. Whatever .rookery/workflow.json on
# base leaves out — a slot, or a key in one — comes from here.
WORKFLOW = {"plan": {"agent": "planmaker", "skills": [], "instructions": ""},
            "task": {"agent": None, "skills": [], "instructions": ""},
            "review": {"agent": "reviewer", "skills": [], "instructions": ""}}

queues = {}                  # repo -> its job queue; a worker thread per queue
qlock = threading.Lock()     # guards queues, so a worker never exits past a new job
seen = collections.OrderedDict()  # recent X-GitHub-Delivery ids; GitHub redelivers
seenlock = threading.Lock()  # the HTTP server is threaded: deliveries can race
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

def sh(*args, cwd=None, check=True, env=None, input=None, track=None):
    """track=repo registers the process as that repo's running job's, so cancel()
    can kill it; a job already cancelled never starts it."""
    with alock:
        a = active.get(track)
        if a and a["cancel"]:
            raise RuntimeError(f"Cancelled from the Rookery UI by @{a['cancel']}.")
        # own process group on POSIX, so kill() takes claude's children with it.
        # Never the parent's stdin: `claude -p` reads it even with the prompt in argv.
        p = subprocess.Popen(args, cwd=cwd, env=env, text=True, encoding="utf-8",
                             stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=bool(track))
        if a:
            a["proc"] = p
    try:
        out, err = p.communicate(input)
    finally:
        if a:
            a["proc"] = None
    if check and p.returncode:
        raise RuntimeError(f"{args[0]} failed ({p.returncode}): {err.strip() or out.strip()}")
    return out.strip()


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
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, login TEXT, repos TEXT,
            csrf TEXT, expires REAL);
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
# active[repo] is the job its worker is running: {"job": id, "proc": Popen|None,
# "cancel": login|None}. One worker per repo, so one entry per repo; alock guards
# proc/cancel against the HTTP thread that cancels.

active = {}
alock = threading.Lock()
fetchlocks = collections.defaultdict(threading.Lock)   # repo -> held around `git fetch`, see fetch()


def begin(c, act, title=None):
    with closing(db()) as con, con:
        job = con.execute("INSERT INTO jobs (repo, n, act, title, started, status) VALUES (?, ?, ?, ?, ?, 'running')",
                          (c.repo, c.n, act, title, time.time())).lastrowid
    active[c.repo] = {"job": job, "proc": None, "cancel": None}


def step(repo, what, **detail):
    """Append to the running job's log; outside a job (dry-run helpers, tests) a no-op."""
    if a := active.get(repo):
        with closing(db()) as con, con:
            con.execute("INSERT INTO steps VALUES (?, ?, ?, ?)", (a["job"], time.time(), what, json.dumps(detail)))


def finish(repo, status):
    if a := active.pop(repo, None):
        with closing(db()) as con, con:
            con.execute("UPDATE jobs SET ended = ?, status = ? WHERE id = ?", (time.time(), status, a["job"]))
        if a.get("plugin"):
            # never fails the job over cleanup; main() clears whatever this leaves
            shutil.rmtree(a["plugin"], ignore_errors=True)


def cancel(repo, job, by):
    """Stop repo's running job if it is `job`: flag it, so no further claude call
    starts, and kill the call in flight. False when that job is not running."""
    with alock:
        a = active.get(repo)
        if not a or a["job"] != job:
            return False
        a["cancel"] = by
        if a["proc"]:
            kill(a["proc"])
    log(f"{repo} job {job} cancelled by {by}")
    return True


def kill(p):
    """The whole tree: claude's own tool subprocesses would otherwise outlive it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        os.killpg(p.pid, signal.SIGKILL)


def check_caps(repo):
    """Before every claude call, so a running implementation stops after the task it
    is on — that task is committed, and `approve` later resumes at the next one.
    ponytail: checked before the call, so one call can overshoot USD_CAP by its own cost."""
    if (a := active.get(repo)) and a["cancel"]:
        raise RuntimeError(f"Cancelled from the Rookery UI by @{a['cancel']}.")
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
        fetch(c.repo, c.path, c.base)
        sh("git", "worktree", "add", "-B", f"feature/issue-{c.n}", str(wt),
           f"origin/{c.base}", cwd=c.path)
    return wt


def frontmatter(p, keys, where):
    """p's top-level frontmatter keys -> raw value, continuation lines folded in. Any key
    outside `keys` fails the job. The CLI reads the file itself; this only gates it."""
    m = re.match(r"\s*---[ \t]*\r?\n(.*?)\r?\n---", p.read_text(encoding="utf-8-sig"), re.S) \
        if p.exists() else None
    meta, key = {}, None
    # dedent: YAML takes a uniformly indented block as the top level, so must we
    for line in textwrap.dedent(m.group(1)).splitlines() if m else []:
        if not line.strip() or line.startswith("#"):
            continue
        if line[0].isspace() or line.startswith("- ") or line == "-":   # belongs to the key above
            if key:
                meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key not in keys:
            raise RuntimeError(f"{where}: frontmatter `{key}` is not allowed — only {', '.join(keys)}.")
        meta[key] = "" if val in (">", "|", ">-", "|-") else val.strip("'\"")
    return meta


def parse_agent(p, where):
    """An agent file held to the frontmatter allowlist. The CLI reads the persona itself
    from the plugin, as `rk:<name>`; Rookery only needs the model, for the run log."""
    meta = frontmatter(p, AGENT_KEYS, where)
    if not NAME.fullmatch(p.stem) or meta.get("name", p.stem) != p.stem:
        raise RuntimeError(f"{where}: an agent's name must match `{NAME.pattern}` and its filename.")
    if meta.get("model", MODELS[0]) not in MODELS:
        raise RuntimeError(f"{where}: `model` must be one of {', '.join(MODELS)}.")
    return meta


def workflow(text, where):
    """.rookery/workflow.json -> every slot with every key, WORKFLOW filling what the file
    leaves out. An invalid file fails the job: silently running the default is worse."""
    try:
        given = json.loads(text) if text else {}
    except ValueError as e:
        raise RuntimeError(f"{where} is not valid JSON: {e}")
    if not isinstance(given, dict) or given.keys() - WORKFLOW.keys():
        raise RuntimeError(f"{where}: the top level is an object with only {', '.join(WORKFLOW)} "
                           "(`after` steps are not built yet).")
    wf = {}
    for slot, default in WORKFLOW.items():
        s = given.get(slot, {})
        if not isinstance(s, dict) or s.keys() - default.keys():
            raise RuntimeError(f"{where}: `{slot}` is an object with only {', '.join(default)}.")
        s = wf[slot] = {**default, **s}
        if not (s["agent"] is None or isinstance(s["agent"], str)) or not isinstance(s["instructions"], str) \
                or not isinstance(s["skills"], list) or not all(isinstance(k, str) for k in s["skills"]):
            raise RuntimeError(f"{where}: in `{slot}`, `agent` is a name or null, `skills` a list of "
                               "names, `instructions` a string.")
        for name in s["skills"] + ([] if s["agent"] is None else [s["agent"]]):
            if not NAME.fullmatch(name):
                raise RuntimeError(f"{where}: `{name}` in `{slot}` — a name must match `{NAME.pattern}`.")
    return wf


def fetch(repo, path, base):
    """Under the repo's lock: the portal's workflow view fetches from an HTTP thread while
    the repo's worker may be fetching too, and two fetches race on the same ref lock."""
    with fetchlocks[repo]:
        sh("git", "fetch", "origin", base, cwd=path, env=auth(repo))


def pin(c):
    """Read what steers the agent — root CLAUDE.md, .rookery/workflow.json, skills — from a
    freshly fetched origin/<base>, once at job start, for every call in the job. Not from
    the worktree: those are the files a task agent edits mid-run. A change to any of it
    steers the bot only after a human merges it. A bad input fails the job here."""
    a = active[c.repo]
    plug = WORKSPACE / "plugins" / c.repo.replace("/", "_") / f"job-{a['job']}"
    a["plugin"] = plug   # set first, so finish() removes it whatever steer() gets to
    a.update(steer(c.repo, c.path, c.base, plug))
    step(c.repo, "base", branch=c.base, sha=a["sha"], skills=sorted(a["sources"]), sources=a["sources"])
    log(f"#{c.n} pinned origin/{c.base} at {a['sha'][:12]}: {len(a['claude_md'])} chars of CLAUDE.md, "
        f"skills {a['sources']}")


def steer(repo, path, base, plug):
    """pin()'s work, minus the job: fetch base, validate everything that steers the agent,
    assemble the plugin at `plug`. The portal's workflow view calls this too, so the page
    and the next run cannot disagree. Raises on the first bad input."""
    fetch(repo, path, base)
    sha = sh("git", "rev-parse", f"origin/{base}", cwd=path)
    oid = lambda p: sh("git", "rev-parse", "--verify", "--quiet", f"{sha}:{p}", cwd=path, check=False)
    blob = lambda p: sh("git", "cat-file", "blob", o, cwd=path) if (o := oid(p)) else ""
    md = blob("CLAUDE.md")
    wf = workflow(blob(".rookery/workflow.json"), f".rookery/workflow.json on {base}")
    for slot, s in wf.items():
        if (size := len((md + s["instructions"]).encode())) > CLAUDE_MD_MAX:
            also = f" plus the `{slot}` instructions in .rookery/workflow.json" if s["instructions"] else ""
            raise RuntimeError(f"CLAUDE.md{also} on {base} is {size // 1024} KB; Rookery passes it to "
                               f"the agent in one argument, which is capped at {CLAUDE_MD_MAX // 1024} KB. "
                               "Shorten it on the base branch, then comment again.")
    # one plugin per job (the CLI loads exactly one per --plugin-dir), so all in it is `rk:<name>`.
    # Only skills/ and agents/: hooks/, .mcp.json or commands/ from any source never get in.
    (plug / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "rk"}', encoding="utf-8")
    skills = plug / "skills"
    skills.mkdir(exist_ok=True)
    shutil.copytree(HERE / "agents", plug / "agents")
    # the first source to bring a name wins: .rookery/skills, .claude/skills, then the catalog
    won = {}
    for src in (".rookery/skills", ".claude/skills"):
        if oid(src):
            tar = subprocess.run(["git", "archive", f"{sha}:{src}"], cwd=path,
                                 capture_output=True, check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                # "data": no link or path out of skills/
                t.extractall(skills, filter="data", members=[m for m in t if m.name.split("/")[0] not in won])
            for d in skills.iterdir():
                if d.is_dir():
                    won.setdefault(d.name, src)
    # the catalog only as far as this repo enabled it; names are NAME-checked, so no `..`
    for name in sorted({k for s in wf.values() for k in s["skills"]} - won.keys()):
        if (HERE / "skills" / name).is_dir():
            shutil.copytree(HERE / "skills" / name, skills / name)
            won[name] = "catalog"
    described = {}
    for name, src in sorted(won.items()):
        where = f"{src}/{name} on {base}" if src != "catalog" else f"catalog skill {name}"
        if not NAME.fullmatch(name):
            raise RuntimeError(f"{where}: a skill name must match `{NAME.pattern}`.")
        described[name] = frontmatter(skills / name / "SKILL.md", SKILL_KEYS, where).get("description", "")
    agents = {p.stem: parse_agent(p, f"agent {p.stem}") for p in sorted((plug / "agents").glob("*.md"))}
    # --agent with a name that does not exist is silently ignored (S2a): never pass one
    for slot, s in wf.items():
        for kind, names, have in (("agent", [s["agent"]] if s["agent"] else [], agents),
                                  ("skill", s["skills"], won)):
            if missing := [x for x in names if x not in have]:
                raise RuntimeError(f".rookery/workflow.json on {base}: `{slot}` names the {kind} "
                                   f"`{missing[0]}`, and there is no {kind} by that name.")
    return {"sha": sha, "claude_md": md, "workflow": wf, "agents": agents, "sources": won,
            "skills": described}


def claude(c, prompt, key="plan", resume=False, slot="task"):
    """One session per key: planning is one long thread, each task is its own. `slot`
    picks the workflow slot: its agent, the skills to name, the instructions to append."""
    sid = str(uuid.uuid5(NS, f"{c.repo}#{c.n}/{key}"))
    a = active.get(c.repo) or {}
    s = (a.get("workflow") or WORKFLOW)[slot]
    # a skill in the plugin alone almost never fires (S5: 0/3); named in the prompt, 3/3
    prompt = "\n\n".join([prompt] + [USE_SKILL.format(name=k) for k in s["skills"]])
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           # none of the target repo's own settings: its hooks would run on this box, and
           # its skills and CLAUDE.md would come from the worktree — pin() reads them from base
           "--setting-sources", "user",
           # no MCP at all: without this the target repo's .mcp.json spawns its servers,
           # and the CLI login's claude.ai connectors reach every tenant's agent
           "--strict-mcp-config"]
    if a.get("plugin"):
        cmd.append(f"--plugin-dir={a['plugin']}")
    # the slot's instructions land after the persona, which --agent puts in the system prompt
    rules = [f"Contents of CLAUDE.md (the repo's own instructions, from {c.base}):\n\n{a['claude_md']}"
             if a.get("claude_md") else "",
             f"Instructions for this step (.rookery/workflow.json, from {c.base}):\n\n{s['instructions']}"
             if s["instructions"] else ""]
    if any(rules):
        # `=`: a value that starts with "-" stays a value. The header is the CLI's own for a
        # discovered CLAUDE.md: without it haiku dropped a format rule 2/9, with it 0/5 (S8).
        cmd.append("--append-system-prompt=Codebase and user instructions are shown below. Be sure to "
                   "adhere to these instructions. IMPORTANT: These instructions OVERRIDE any default "
                   "behavior and you MUST follow them exactly as written.\n\n" + "\n\n".join(filter(None, rules)))
    # guardrail: the agent never publishes. Deny rules beat bypassPermissions. They match the
    # command after hooks rewrite it, so the operator's RTK hook (DEPLOY.md §7) turning
    # `git push` into `rtk git push` would slip past the first two without the rtk forms.
    cmd += ["--disallowed-tools", "Bash(git push:*)", "Bash(gh:*)", "Bash(rtk git push:*)", "Bash(rtk gh:*)"]
    if s["agent"]:
        # a plugin agent, from agents/*.md via pin(); its `model:` is honoured (S2b). Its
        # `tools` list is a role hint, not a boundary — the deny rules above and the checks
        # in plan() are what actually enforce.
        cmd += ["--agent", f"rk:{s['agent']}"]
        model = (a.get("agents") or {}).get(s["agent"], {}).get("model")
    else:
        # no agent (implementation tasks, by default), so the model is set here
        cmd += ["--model", "sonnet"]
        model = "sonnet"
    check_caps(c.repo)
    log(f"#{c.n} claude {key}{' (resume)' if resume else ''} — this takes a while...")
    wt, out, m = worktree(c), "", None
    try:
        try:
            out = sh(*cmd, *(["--resume", sid] if resume else ["--session-id", sid]), cwd=wt, track=c.repo)
        except RuntimeError as e:
            # a previous run already created this session (it crashed, or we are being
            # re-run): the id is derived from repo#issue/key, so pick that session up.
            if resume or "already in use" not in str(e):
                raise
            log(f"#{c.n} session {sid} exists — resuming it")
            out = sh(*cmd, "--resume", sid, cwd=wt, track=c.repo)
        text = json.loads(out).get("result", "")
        m = MARKER.search(text)
    finally:
        # a failed call still counts as a run; its cost is lost with its stdout
        record(c.repo, cost_of(out))
        step(c.repo, "claude", key=key, status=m.group(1) if m else "no-marker" if out else "failed",
             usd=cost_of(out), agent=s["agent"], skills=s["skills"], model=model)
    log(f"#{c.n} claude {key} -> {m.group(1) if m else 'NO STATUS MARKER'}")
    if not m:
        raise RuntimeError(f"no STATUS marker in agent reply:\n{text[-1500:]}")
    return m.group(1), (text[:m.start()] + text[m.end():]).strip()


# Named roles live in agents/*.md and reach the CLI as plugin agents, `--agent rk:<name>`
# (pin(), claude()). They hold the standing rules of a role; the templates below hold
# the job. Implementation tasks run as no agent at all — the default harness with the
# deny rules is the point.

PLAN ="""You are planning work for GitHub issue #{n} in {repo}.

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

# one line per skill the slot enables in .rookery/workflow.json, after the slot's prompt
USE_SKILL = "Use the `rk:{name}` skill."


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
    set_phase(c, "rookery:planning")
    prompt = (REPLY.format(n=c.n, body=body, d=pdir(c.n)) if resume
              else PLAN.format(n=c.n, repo=c.repo, title=title, body=body,
                               d=pdir(c.n), max=MAX_TASKS))
    status, msg = claude(c, prompt, resume=resume, slot="plan")
    keep_plans_only(c, worktree(c))
    comment(c, msg)
    set_phase(c, "rookery:awaiting-input" if status == "questions" else "rookery:awaiting-approval")


def implement(c):
    """One fresh session per unticked task. Re-running resumes where it stopped."""
    set_phase(c, "rookery:implementing")
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

    set_phase(c, "rookery:reviewing")
    verdict, review = claude(c, REVIEW.format(n=c.n, d=pdir(c.n), base=c.base),
                             key="review", slot="review")

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
             input="\n\n".join(done) + f"\n\n## Review\n\n{review}\n\nCloses #{c.n}"
             # the commit every steering input was read at; GitHub links owner/repo@sha
             f"\n\n---\nworkflow: {c.repo}@{active[c.repo]['sha']}")
    # The review is advisory: the PR opens either way, so the work is always on
    # GitHub rather than stranded in a local worktree. A bad verdict only flags.
    step(c.repo, "pr", url=url)
    ok = verdict == "review-ok"
    comment(c, f"Draft PR: {url}" if ok else
            f"Draft PR: {url}\n\nThe reviewer wants changes — see **Review** in the PR body.")
    set_phase(c, "rookery:done" if ok else "rookery:error")


# --- repos: onboarding and retention ----------------------------------------

def sync():
    """Rebuild REPOS from every installation of the App — GitHub is the source of truth."""
    for inst in api_all("/app/installations", jwt()):
        tok = api("POST", f"/app/installations/{inst['id']}/access_tokens", jwt())["token"]
        for r in api_all("/installation/repositories", tok, "repositories"):
            if wanted(r["full_name"]):
                onboard(r["full_name"], r["default_branch"])


def wanted(repo):
    return not ONLY or repo in ONLY


def onboard(repo, base=None):
    """Make repo live: clone it once, create the labels, record its base branch."""
    base = base or api("GET", f"/repos/{repo}", token(repo))["default_branch"]
    path = clone_dir(repo)
    if not path.exists():
        log(f"{repo} cloning into {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", f"https://github.com/{repo}.git", str(path), env=auth(repo))
    for l in PHASES + [TRIGGER]:
        # ponytail: one-time claude:* -> rookery:* rename, keeps the label on in-flight issues;
        # delete once every repo has restarted on this code
        gh(repo, "label", "edit", l.replace("rookery:", "claude:"), "--name", l, check=False)
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
            pin(c)
            plan(c, job["title"], job["body"], resume=False)
        else:
            phase = labels_of(c.repo, c.n)
            # approving again after an error resumes at the first unticked task
            if APPROVE.match(job["body"]) and phase & {"rookery:awaiting-approval", "rookery:error"}:
                begin(c, "implement")
                pin(c)
                implement(c)
            elif phase & {"rookery:awaiting-input", "rookery:awaiting-approval"}:
                begin(c, "reply")
                pin(c)
                plan(c, None, job["body"], resume=True)
        finish(c.repo, "ok")
    except Exception as e:
        log(f"#{c.n} FAILED: {e}")
        # a killed claude fails with its own noise; say what actually happened
        cancelled = (active.get(c.repo) or {}).get("cancel")
        msg = f"Cancelled from the Rookery UI by @{cancelled}." if cancelled else str(e)
        step(c.repo, "error", message=msg[-2000:])
        try:
            comment(c, f"Agent error:\n\n```\n{msg}\n```")
            set_phase(c, "rookery:error")
        finally:
            finish(c.repo, "cancelled" if cancelled else "failed")


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


# --- webhook dispatch (the HTTP side is web.py) ---------------------------

def fresh(delivery, keep=1000):
    """False for a delivery id seen recently. In memory: a restart forgetting it is fine."""
    if not delivery:
        return True
    with seenlock:
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
        for r in [r for r in added if wanted(r["full_name"])]:
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


def main():
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

    # a job still "running" in the log died with the previous process
    with closing(db()) as con, con:
        con.execute("UPDATE jobs SET status = 'interrupted', ended = ? WHERE status = 'running'", (time.time(),))
    shutil.rmtree(WORKSPACE / "plugins", ignore_errors=True)   # and so did its per-job plugin dirs
    import web
    web.serve()


if __name__ == "__main__":
    # run as the `agent` module that web.py imports, not as a second copy named __main__
    import agent
    agent.main()
