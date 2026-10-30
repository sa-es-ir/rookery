#!/usr/bin/env python3
"""GitHub issue -> Claude Code agent, for every repo the GitHub App is installed on.

State lives on GitHub (installations, labels, comments) and in the target repo
(agent-plans/<n>/, or beside the worktree for a repo that keeps them out). The only local state is a clone per repo, a git worktree
per issue, and rookery.db (the per-repo daily usage behind the caps, and the run
log the UI reads). Claude
session ids are derived from repo#issue/key, so no session state is persisted.
"""
import base64
import calendar
import collections
import hashlib
import hmac
import html
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
import tempfile
import textwrap
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, contextmanager
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
# shared with the local CLI (cli/): every text a claude call is built from, and the defaults
# and verdict tables replies are held to. One copy, so wording changes once for both.
DEFAULTS = json.loads((HERE / "prompts" / "defaults.json").read_text(encoding="utf-8"))

# the `claude` CLI must use the claude.ai login; a stale key in the env fails it.
# Another backend's key reaches it only through provider_env().
os.environ.pop("ANTHROPIC_API_KEY", None)
# Backends behind the `claude` CLI. "claude" is its own /login: no
# overrides at all. Others: an Anthropic-compatible endpoint, the .env var holding its key,
# what the CLI's aliases (agents' `model: opus`, tasks' `--model sonnet`, memory's haiku)
# resolve to there, and USD per million tokens (input, cache hit, output) at peak, so
# USD_CAP can trip early, never late. The CLI itself prices any model as Claude (S-b).
# `anthropic` is Claude by API key: the aliases are Claude models already, and total_cost_usd is
# its price. Its key is CLAUDE_API_KEY, not ANTHROPIC_API_KEY: that one is popped above, so it
# never beats the /login. In prompts/defaults.json: the local CLI (`rookery config`) uses the same.
PROVIDERS = DEFAULTS["providers"]
PROVIDER = os.environ.get("PROVIDER", "claude")   # the default until the admin page stores one
# owners the operator's subscription (`claude`) may run for; unset, the ADMINS logins
OWN = {x.strip().lower() for x in (os.environ.get("OWN") or os.environ.get("ADMINS", "")).split(",") if x.strip()}
# every provider's key is kept here, out of os.environ: the claude subprocess inherits it
KEYS = {n: os.environ.pop(p["key"], "") for n, p in PROVIDERS.items() if p}
# seals the owners' own keys in rookery.db (seal()); popped like the keys. Unset, no owner key can
# be added or read: the keys page says so, and an owner's selected key fails its jobs (NoKey).
KEY_SECRET = os.environ.pop("KEY_SECRET", "")


def ready(name):
    """A provider a call could run on: known, and its key (if it needs one) is in .env."""
    return name in PROVIDERS and (not PROVIDERS[name] or bool(KEYS.get(name)))


def provider_env(name, key=None):
    """-> the env overrides for one claude call on `name`, with `key` (an owner's own) or else the
    operator's; {} for the CLI's own login. Raises on a backend that can't run: a misconfigured
    one must never fall back to the operator's login."""
    if name not in PROVIDERS:
        raise RuntimeError(f"provider {name!r} is not one of {', '.join(PROVIDERS)}")
    p = PROVIDERS[name]
    if not p:
        return {}
    if not (key := key or KEYS.get(name)):
        raise RuntimeError(f"provider {name} needs {p['key']} in .env")
    return {"ANTHROPIC_BASE_URL": p["url"], "ANTHROPIC_API_KEY": key,
            **{f"ANTHROPIC_DEFAULT_{a.upper()}_MODEL": m for a, m in p.get("models", {}).items()}}


provider_env(PROVIDER)   # fails startup, not the first job
# GitHub credentials reach gh/git through env= per call, never os.environ, so the
# claude subprocess (which inherits os.environ) cannot read them.
for k in ("GH_TOKEN", "GITHUB_TOKEN"):
    os.environ.pop(k, None)
APP_ID = os.environ.get("APP_ID", "")
APP_KEY = os.environ.get("APP_KEY", str(HERE / "app.pem"))
SECRET = os.environ["WEBHOOK_SECRET"].encode()
# per repo per UTC day; a run is one claude call. RUN_CAP/USD_CAP override them.
RUN_CAP = int(os.environ.get("RUN_CAP", "30"))
USD_CAP = float(os.environ.get("USD_CAP", "20"))
PAUSED = HERE / "PAUSED"  # operator switch: while this file exists, no claude call starts
# comma-separated owner/name: serve only these of the App's repos. For a local
# instance next to the server, so the two never work the same repo; unset = all.
ONLY = {r.strip() for r in os.environ.get("REPOS_ONLY", "").split(",") if r.strip()}
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")  # "" = all interfaces, for a public webhook
# where the browser reaches us: the portal's links in comments, GitHub's OAuth callback (web.py)
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

# threadName is the repo on a worker thread, so parallel repos' `#n` lines stay apart
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s", datefmt="%H:%M:%S")
log = logging.info


def prompt(name):
    return (HERE / "prompts" / f"{name}.md").read_text(encoding="utf-8")


TRIGGER = "rookery:trigger"
REVIEW_LABEL = "rookery:pr-review"   # on a PR: review it once. Never a PHASES label on a PR (handle()).
# the name people use for Rookery: a plain machine account, no token for it on the box
AGENT_USER = os.environ.get("AGENT_USER", "rookery-agent")
MENTION = re.compile(rf"(?<![\w-])@{re.escape(AGENT_USER)}(?![\w-])", re.I)
# a label on the wrong kind of item: say so and remove it, so adding the right one is a clean event
WRONG_LABEL = {
    TRIGGER: f"`{TRIGGER}` plans an issue. It does nothing on a pull request, so I removed it. "
             f"To get a review of this pull request, add `{REVIEW_LABEL}` or mention `@{AGENT_USER}`.",
    REVIEW_LABEL: f"`{REVIEW_LABEL}` reviews a pull request. It does nothing on an issue, so I removed it. "
                  f"To plan this issue, add `{TRIGGER}`.",
}
# an owner with nothing to run on (NoKey): not in a code block, so the link works
NO_KEY = ("Agent error: {why} for **{owner}**. Add a key at {url}/portal.html#keys and select it. "
          f"Then remove the `rookery:error` and `{TRIGGER}` labels and add `{TRIGGER}` again. "
          "If you approved the plan before this error, comment `approve` again.")
NO_KEY_PR = ("Agent error: {why} for **{owner}**. Add a key at {url}/portal.html#keys and select it. "
             f"Then add `{REVIEW_LABEL}` again, or mention `@{AGENT_USER}` again.")
# a mention made only of these words asks for a review and says nothing more: no note
ASKING = {"please", "pls", "plz", "can", "could", "you", "kindly", "review", "re-review", "rereview", "again",
          "once", "more", "one", "another", "time", "this", "it", "the", "pr", "pull", "request", "ptal", "now",
          "take", "a", "look", "check", "do", "thanks", "thank", "thx"}
PHASES = ["rookery:planning", "rookery:awaiting-input", "rookery:awaiting-approval",
          "rookery:implementing", "rookery:reviewing", "rookery:done", "rookery:changes-wanted",
          "rookery:error"]
APPROVE = re.compile(r"^\s*(approve|approved|lgtm|go ahead)\b", re.I)
# alone on its line; markdown bold or code around it is tolerated (prod: `**STATUS: review-ok**`,
# and the backticks the prompts quote it in, copied as is)
MARKER = re.compile(r"^(?:\*\*|`)?STATUS:(?:\*\*)?[ \t]*([\w-]+)[ \t]*(?:\*\*|`)?[ \t]*$", re.M)
# a reply fenced whole, marker inside (prod: deepseek's plan posted as one code block); unwrapped
# only when no other fence is inside, so a reply that starts and ends with code is left alone
FENCED = re.compile(r"```[\w-]*\n((?:(?!```).)*?)\n?```", re.S)
TASK_LINE = re.compile(r"^[ \t]*[-*] \[( |x)(\] .+)$", re.M | re.I)
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
MAX_TASKS = DEFAULTS["max_tasks"]
MAX_STEPS = DEFAULTS["max_steps"]   # `after` steps in .rookery/workflow.json; per issue, like MAX_TASKS, and apart from it
# `with` in .rookery/workflow.json: the companion repos an issue may also change. Each is a
# worktree per issue and a block in every prompt.
MAX_WITH = 3
REPO = re.compile(r"[\w.-]+/[\w.-]+")
# CLAUDE.md goes to the CLI as one argv entry, and Linux caps one at 128 KB
CLAUDE_MD_MAX = DEFAULTS["claude_md_max"]
# every skill and agent name: it is rendered into prompts ("Use the `rk:x` skill") and --agent
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# frontmatter may say no more than this — `hooks:` would run shell here, `mcpServers:`
# reach out from here, `permissionMode:` loosen what the deny rules hold. `argument-hint`
# only labels a skill, `disable-model-invocation` only keeps the model from calling it.
SKILL_KEYS = tuple(DEFAULTS["skill_keys"])
AGENT_KEYS = tuple(DEFAULTS["agent_keys"])
MODELS = tuple(DEFAULTS["models"])
# the built-in workflow: today's pipeline, no skills. Whatever .rookery/workflow.json on
# base leaves out — a slot, or a key in one — comes from here.
WORKFLOW = DEFAULTS["workflow"]
# one `after` step: runs between the task loop and the review. `agent` and `mode` are required.
STEP = DEFAULTS["step"]
# mode -> the STATUS values a step in it may reply; anything else fails the job
MODES = {k: tuple(v) for k, v in DEFAULTS["modes"].items()}
# a follow-up's reply (follow_up()): the worktree, not the word, decides what happens next
FOLLOW_VERDICTS = tuple(DEFAULTS["follow_verdicts"])
CONFLICT = re.compile(r"^(?:<{7}|>{7})(?: |$)", re.M)   # a marker git leaves in a conflicted file
# <owner>/.rookery: that owner's defaults — workflow.json, agents/, skills/ at its root — between
# a repo's own config and the built-in. Read only while the App is installed on it (it is in REPOS).
ORG = ".rookery"
# what the portal may change, under config_dir(): a PR touching anything else is not the portal's
CONFIG_FILE = re.compile(r"workflow\.json|agents/[a-z0-9][a-z0-9-]*\.md")
# a PR review's diff goes into its prompt up to this many characters (about 25k tokens); past
# it, whole files are left out and the reviewer reads them. Lockfiles never go in.
DIFF_CAP = DEFAULTS["diff_cap"]
LOCKFILES = (":(exclude)*package-lock.json", ":(exclude)*.lock")
# the PR body folds each task under a one-line summary of this many characters, and the plan
# under one line, cut at PLAN_CAP: GitHub refuses a body over 65536 characters
SUMMARY_CAP = DEFAULTS["summary_cap"]
PLAN_CAP = 30_000
# repo memory: one notes file per repo, written by a `memory` job after each job, read by pin().
# MEMORY=0 turns off both, for the whole instance.
MEMORY = os.environ.get("MEMORY", "1") != "0"
MEMORY_CAP = DEFAULTS["memory_cap"]             # characters of notes
MEMORY_REWRITES = DEFAULTS["memory_rewrites"]   # extra memory calls to ask for shorter notes, before truncating
DIGEST_CAP = DEFAULTS["digest_cap"]             # characters of one job's digest, what the memory call reads
# a second OS user every `claude` call runs as (DEPLOY.md §5b): it can't read app.pem, .env or
# rookery.db, so an injected agent can't mint tokens for every installation. Its group shares
# the worktrees with us, never the clone's .git — its config and hooks run when we commit.
# "" = the agent runs as this user (local dev, Windows).
RUN_AS = os.environ.get("RUN_AS", "")
if RUN_AS:
    os.umask(0o002)   # what we check out in a worktree stays writable by RUN_AS's group
# where the CLI writes session transcripts, <sid>.jsonl under projects/<cwd>/
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(f"~{RUN_AS}").expanduser() / ".claude")

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


def memory_file(repo):
    """repo's notes: on this box only, never in the target repo."""
    return WORKSPACE / "memory" / (repo.replace("/", "_") + ".md")


def pdir(n):
    """Per-issue state, inside the target repo, committed with the change."""
    return f"agent-plans/{n}"


def plans(c, wt):
    """-> (issue n's plan dir, how the prompts name it): pdir() in the worktree, or — for a repo
    whose `local_plans` is on — plans-<n>/ beside the worktree in wt_root (shared with RUN_AS like
    the worktrees), never committed, removed with the worktree. A plan already in either place
    wins, so flipping the setting mid-issue never strands one; outside first, so an agent writing
    pdir() by mistake cannot move a plan back into the repo."""
    inside, outside = wt / pdir(c.n), wt_root(c.repo) / f"plans-{c.n}"
    if outside.exists() or not inside.exists() and repo_settings(c.repo)["local_plans"]:
        shared(outside.parent)
        outside.mkdir(exist_ok=True)
        return outside, str(outside.absolute())
    return inside, pdir(c.n)


@contextmanager
def frozen(p):
    """p (a tasks.md) as it was before the block, whatever the agent in it did: only tick()
    changes the task list. Git reverts it inside the worktree; beside it, nothing else would."""
    was = p.read_bytes() if p.exists() else None
    try:
        yield
    finally:
        if was is not None:
            p.write_bytes(was)


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


def api(method, path, bearer, data=None):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=None if data is None else json.dumps(data).encode(),
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


def unlabel(repo, n, label):
    """REST, never `gh pr edit`: gh 2.45 (Ubuntu's) asks for classic Projects there, which GitHub
    now refuses, so the edit fails whole. An issue number or a PR's, alike. Never raises."""
    try:
        api("DELETE", f"/repos/{repo}/issues/{n}/labels/{urllib.parse.quote(label, safe='')}", token(repo))
    except Exception as e:
        log(f"{repo}#{n} removing {label} failed: {e}")


def set_phase(c, phase):
    stale =[l for l in labels_of(c.repo, c.n) if l in PHASES and l != phase]
    args = ["issue", "edit", str(c.n), "--add-label", phase]
    for l in stale:
        args += ["--remove-label", l]
    # GitHub's GraphQL 500s now and then ("Something went wrong while executing your query"),
    # and a lost label strands the issue: a reply to rookery:error does nothing. The edit is
    # idempotent, so it is retried; a comment never is, since a retry could post it twice.
    for wait in (5, 20, None):
        try:
            gh(c.repo, *args)
            break
        except RuntimeError:
            if wait is None:
                raise
            log(f"#{c.n} phase {phase} failed — retrying in {wait}s")
            time.sleep(wait)
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
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS users (login TEXT PRIMARY KEY, first REAL, last REAL, logins INTEGER);
        CREATE TABLE IF NOT EXISTS keys (id INTEGER PRIMARY KEY, owner TEXT, provider TEXT,
            label TEXT, secret TEXT, last4 TEXT, added REAL, by TEXT);
    """)
    return con


class NoKey(RuntimeError):
    """Nothing to run an owner's calls on: no selected key, no override, not in OWN; or the selected
    key cannot be read. handle() posts NO_KEY / NO_KEY_PR, with the keys page's link."""
    def __init__(self, owner, why="no API key"):
        super().__init__(f"{why} for {owner}")
        self.owner, self.why = owner, why


def mac(ct):
    return hmac.new(hmac.new(KEY_SECRET.encode(), b"mac", hashlib.sha256).digest(), ct.encode(),
                    hashlib.sha256).hexdigest()


def seal(plain):
    """-> `<base64 ciphertext>.<hex HMAC>` of an owner's key, encrypt-then-MAC. The stdlib has no
    cipher, so openssl encrypts, as it signs jwt(); the key goes in on stdin and KEY_SECRET in env=
    for that one call, never argv. The MAC key is derived, so it is not the cipher's password."""
    ct = sh("openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", "env:K", "-a", "-A",
            env={**os.environ, "K": KEY_SECRET}, input=plain)
    return f"{ct}.{mac(ct)}"


def unseal(blob):
    """-> the key. ValueError with no KEY_SECRET, or a blob changed, or sealed under another one."""
    ct, _, tag = blob.rpartition(".")
    if not KEY_SECRET or not hmac.compare_digest(tag, mac(ct)):
        raise ValueError("the stored key does not match KEY_SECRET")
    return sh("openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "env:K", "-a", "-A",
              env={**os.environ, "K": KEY_SECRET}, input=ct + "\n")


# per repo, set on the portal (POST /api/repos/<o>/<r>/settings, can_write()): one JSON row
# `repo:<owner/repo>` in `settings`, never in the target repo. Whatever the row leaves out is this.
REPO_SETTINGS = {"local_plans": True,    # plan.md, tasks.md, decisions.md beside the worktree (plans())
                 "review_prs": False}    # review every same-repo PR once, when it is opened (enqueue())


def repo_settings(repo):
    with closing(db()) as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (f"repo:{repo}",)).fetchone()
    return {**REPO_SETTINGS, **(json.loads(row[0]) if row else {})}


def fallback(owner):
    """The operator's provider for `owner`: its `provider:<owner>` override, else (only for an
    owner in OWN) the stored default, else PROVIDER. None: the operator pays for none of this
    owner's calls. Never the subscription (`claude`) for an owner outside OWN."""
    with closing(db()) as con:
        s = dict(con.execute("SELECT key, value FROM settings WHERE key IN (?, 'provider')", (f"provider:{owner}",)))
    name = s.get(f"provider:{owner}")
    if owner.lower() in OWN:
        return name or s.get("provider", PROVIDER)
    return None if name == "claude" else name


def provider_for(repo):
    """-> (provider, key id, key) for the repo owner's calls: the owner's selected key (who pays,
    chooses), else fallback(), else NoKey. id and key are None on the operator's own keys. Per
    owner, never per actor: one issue has several, and must stay on one backend."""
    owner = repo.split("/")[0]
    with closing(db()) as con:
        k = con.execute("SELECT k.id, k.provider, k.secret FROM settings s JOIN keys k ON k.id = s.value "
                        "WHERE s.key = ? AND k.owner = ?", (f"key:{owner}", owner)).fetchone()
    if k:
        try:
            return k[1], k[0], unseal(k[2])
        except (ValueError, RuntimeError):
            raise NoKey(owner, "the selected API key cannot be read") from None
    if name := fallback(owner):
        return name, None, None
    raise NoKey(owner)


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


def step(repo, what, /, **detail):
    """Append to the running job's log; outside a job (dry-run helpers, tests) a no-op. `/`:
    a companion's steps carry `repo` in their detail."""
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
    elif RUN_AS:
        # p is sudo (root's, not ours to signal); with !use_pty claude stays in its session
        subprocess.run(["sudo", "-n", "-u", RUN_AS, "pkill", "-KILL", "-s", str(p.pid)], capture_output=True)
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


def cost_of(out, name):
    """-> USD of one call: the CLI's own figure on Claude, the provider's `price` table
    elsewhere. A model the table lacks raises — no quiet guess."""
    try:
        d = json.loads(out)
    except ValueError:
        return 0.0
    price = PROVIDERS[name].get("price")
    if not price:
        return float(d.get("total_cost_usd") or 0)
    usd = 0.0
    for m, u in (d.get("modelUsage") or {}).items():
        if m not in price:
            raise RuntimeError(f"no {name} price for model {m!r}: add it to providers in prompts/defaults.json")
        i, hit, o = price[m]
        # a cache write is billed as a plain input token there
        usd += ((u.get("inputTokens", 0) + u.get("cacheCreationInputTokens", 0)) * i
                + u.get("cacheReadInputTokens", 0) * hit + u.get("outputTokens", 0) * o) / 1e6
    return usd


# --- claude ------------------------------------------------------------------

def worktree(c, home=None):
    """Per-issue branch + working dir, namespaced by repo, so nothing collides. With `home`, c is
    a companion of home's issue (mates()): the names carry the home's name (tag())."""
    wt = wt_root(c.repo) / tag(c, home)
    if not wt.exists():
        log(f"#{c.n} creating worktree {wt} from origin/{c.base}")
        shared(wt.parent)
        fetch(c.repo, c.path, c.base)
        sh("git", "worktree", "add", "-B", f"feature/{tag(c, home)}", str(wt),
           f"origin/{c.base}", cwd=c.path)
        sealed(wt)
    return wt


def tag(c, home=None):
    """An issue's worktree dir, and its branch after `feature/`. A companion's is never
    `issue-<n>`: that repo has its own issue n, and drop_worktree() keys off that name."""
    return f"{home.repo.split('/')[1]}-issue-{c.n}" if home else f"issue-{c.n}"


def mates(c):
    """This job's companions (`with`, as pin() read it), each a Ctx of its own repo with the
    home's issue number. Only for the home's issue worktree: never a PR review's or memory's."""
    return [Ctx(w["repo"], c.n, clone_dir(w["repo"]), w["base"]) for w in (active.get(c.repo) or {}).get("with", [])]


def mate_wts(c):
    """(companion, its worktree) for each companion whose worktree exists."""
    return [(m, wt) for m in mates(c) if (wt := wt_root(m.repo) / tag(m, c)).exists()]


def shared(d):
    """wt_root: RUN_AS edits the worktrees under it through the group. setgid, so what either
    user makes there stays in that group."""
    d.mkdir(parents=True, exist_ok=True)
    if RUN_AS:
        shutil.chown(d, group=RUN_AS)
        os.chmod(d, 0o2775)


def sealed(wt):
    """The worktree's `.git` file names the gitdir our git trusts: RUN_AS may not rewrite it."""
    if RUN_AS:
        os.chmod(wt / ".git", 0o644)


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


def workflow(text, where, under=None):
    """.rookery/workflow.json -> every slot with every key, `under` (the owner's workflow, else
    WORKFLOW) filling what the file leaves out, plus its `after` steps — `under`'s unless the
    file has its own — and its `with`, the file's own only: an owner-wide list would let every
    repo write every repo. An invalid file fails the job: silently running the default is worse."""
    under = under or {**WORKFLOW, "after": []}
    try:
        given = json.loads(text) if text else {}
    except ValueError as e:
        raise RuntimeError(f"{where} is not valid JSON: {e}")
    if not isinstance(given, dict) or given.keys() - WORKFLOW.keys() - {"after", "with"}:
        raise RuntimeError(f"{where}: the top level is an object with only {', '.join(WORKFLOW)}, after, with.")
    if not isinstance(given.get("after", []), list) or len(given.get("after", [])) > MAX_STEPS:
        raise RuntimeError(f"{where}: `after` is a list of at most {MAX_STEPS} steps.")
    mates = given.get("with", [])
    if not isinstance(mates, list) or len(mates) > MAX_WITH \
            or not all(isinstance(r, str) and REPO.fullmatch(r) for r in mates) or len(set(mates)) < len(mates):
        raise RuntimeError(f"{where}: `with` is a list of at most {MAX_WITH} different repos, each `owner/name`.")

    def one(slot, s, default):
        if not isinstance(s, dict) or s.keys() - default.keys():
            raise RuntimeError(f"{where}: `{slot}` is an object with only {', '.join(default)}.")
        s = {**default, **s}
        if not (s["agent"] is None or isinstance(s["agent"], str)) or not isinstance(s["instructions"], str) \
                or not isinstance(s["skills"], list) or not all(isinstance(k, str) for k in s["skills"]):
            raise RuntimeError(f"{where}: in `{slot}`, `agent` is a name or null, `skills` a list of "
                               "names, `instructions` a string.")
        if default is STEP and (not s["agent"] or not isinstance(s["mode"], str) or s["mode"] not in MODES):
            raise RuntimeError(f"{where}: `{slot}` needs an `agent` and a `mode`, {' or '.join(MODES)}.")
        for name in s["skills"] + ([] if s["agent"] is None else [s["agent"]]):
            if not NAME.fullmatch(name):
                raise RuntimeError(f"{where}: `{name}` in `{slot}` — a name must match `{NAME.pattern}`.")
        return s

    wf = {slot: one(slot, given.get(slot, {}), under[slot]) for slot in WORKFLOW}
    wf["after"] = [one(f"after[{i}]", s, STEP) for i, s in enumerate(given["after"])] \
        if "after" in given else under["after"]
    # a step's commit subject and PR section carry its agent's name: two would collide
    if len(names := [s["agent"] for s in wf["after"]]) > len(set(names)):
        raise RuntimeError(f"{where}: `after` names an agent twice; each step needs its own.")
    wf["with"] = mates
    return wf


def slots(wf):
    """(label, slot) for every slot and `after` step in wf — what steer() checks and assembles."""
    return [*((k, wf[k]) for k in WORKFLOW), *((f"after[{i}]", s) for i, s in enumerate(wf["after"]))]


def fetch(repo, path, base):
    """Under the repo's lock: the portal's workflow view fetches from an HTTP thread while
    the repo's worker may be fetching too, and two fetches race on the same ref lock."""
    with fetchlocks[repo]:
        sh("git", "fetch", "origin", base, cwd=path, env=auth(repo))


def pin(c):
    """Read what steers the agent — root CLAUDE.md, .rookery/workflow.json, skills — from a
    freshly fetched origin/<base>, once at job start, for every call in the job. Not from
    the worktree: those are the files a task agent edits mid-run. A change to any of it
    steers the bot only after a human merges it. A bad input fails the job here. An owner with
    nothing to run on fails first (NoKey): no fetch, no plugin, no label, no claude call."""
    prov, kid, _ = provider_for(c.repo)
    a = active[c.repo]
    plug = WORKSPACE / "plugins" / c.repo.replace("/", "_") / f"job-{a['job']}"
    a["plugin"] = plug   # set first, so finish() removes it whatever steer() gets to
    a.update(steer(c.repo, c.path, c.base, plug))
    # once per job, like CLAUDE.md, so every call in it reads the same notes, and the log names them
    notes = memory_file(c.repo).read_text(encoding="utf-8") if MEMORY and memory_file(c.repo).exists() else ""
    most = max(len(s["instructions"].encode()) for _, s in slots(a["workflow"]))
    mds = a["claude_md"] + "".join(w["claude_md"] for w in a["with"])
    if notes and len((mds + notes).encode()) + most > CLAUDE_MD_MAX:
        # steer() already failed the job if CLAUDE.md alone were over: the notes are the extra
        log(f"#{c.n} repo notes left out: CLAUDE.md, notes and instructions would pass {CLAUDE_MD_MAX // 1024} KB")
        notes, said = "", "left out: over the 64 KB limit"
    else:
        said = f"{len(notes)}, {hashlib.sha256(notes.encode()).hexdigest()[:12]}" if notes else ""
    a["memory"] = notes
    step(c.repo, "base", branch=c.base, sha=a["sha"], provider=prov, **({"key": kid} if kid else {}),
         skills=sorted(a["sources"]), sources=a["sources"],
         agents=sorted(n for n, src in a["agent_sources"].items() if src != "built-in"),
         **({"org": f"{a['org']['repo']}@{a['org']['sha']}"} if a["org"] else {}),
         **({"with": [f"{w['repo']}@{w['sha']}" for w in a["with"]]} if a["with"] else {}),
         **({"skipped": a["skipped"]} if a["skipped"] else {}),
         **({"memory": said} if said else {}))
    log(f"#{c.n} pinned origin/{c.base} at {a['sha'][:12]}: {len(a['claude_md'])} chars of CLAUDE.md, "
        f"skills {a['sources']}")


def oid(cwd, sha, p):
    """The object at sha:p, "" when there is none."""
    return sh("git", "rev-parse", "--verify", "--quiet", f"{sha}:{p}", cwd=cwd, check=False)


def blob(cwd, sha, p):
    return sh("git", "cat-file", "blob", o, cwd=cwd) if (o := oid(cwd, sha, p)) else ""


def untar(cwd, sha, src, dest, keep):
    """src at sha into dest, the members keep() takes -> their names."""
    tar = subprocess.run(["git", "archive", f"{sha}:{src}"], cwd=cwd, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        # "data": no link or path out of dest
        t.extractall(dest, filter="data", members=(got := [m for m in t if keep(m)]))
    return [m.name for m in got]


def org_of(repo):
    """repo's owner's .rookery repo, when the App is installed on it: (name, clone, sha) of a
    freshly fetched base — else None, and no owner defaults apply."""
    org = repo.split("/")[0] + "/" + ORG
    if org not in REPOS:
        return None
    if not REPOS[org] or not clone_dir(org).exists():   # never run without them, even once
        raise RuntimeError(f"Rookery is still cloning {org}, which holds this owner's defaults. "
                           "Try again in a minute.")
    fetch(org, clone_dir(org), REPOS[org])
    return org, clone_dir(org), sh("git", "rev-parse", f"origin/{REPOS[org]}", cwd=clone_dir(org))


def steer(repo, path, base, plug, sha=None):
    """pin()'s work, minus the job: fetch base, validate everything that steers the agent,
    assemble the plugin at `plug`. The portal's workflow view calls this too, so the page
    and the next run cannot disagree; propose() calls it at an unpushed `sha`, so a config
    PR that would fail the next job at start is never opened. Raises on the first bad input.

    Precedence, first wins: the repo's own (.rookery/, .claude/skills), its owner's
    <owner>/.rookery (org_of()), then Rookery's built-in agents, catalog and WORKFLOW."""
    if not sha:
        fetch(repo, path, base)
        sha = sh("git", "rev-parse", f"origin/{base}", cwd=path)
    org = org_of(repo)
    md = blob(path, sha, "CLAUDE.md")
    owf = workflow(blob(org[1], org[2], "workflow.json"), f"workflow.json in {org[0]}") if org else None
    wf = workflow(blob(path, sha, ".rookery/workflow.json"), f".rookery/workflow.json on {base}", owf)
    mates = []   # (repo, clone, sha) of each companion in `with`, at its own freshly fetched base
    for r in wf["with"]:
        why = ("is the repo itself" if r == repo else "has another owner" if r.split("/")[0] != repo.split("/")[0]
               else "is not a repo Rookery is installed on" if r not in REPOS or r.split("/")[1] == ORG else "")
        if why:
            raise RuntimeError(f".rookery/workflow.json on {base}: `with` names {r}, which {why}. A companion is "
                               "another repo of the same owner, with the Rookery App installed on it.")
        if not REPOS[r] or not clone_dir(r).exists():   # never run without it, even once
            raise RuntimeError(f"Rookery is still cloning {r}, which `with` names. Try again in a minute.")
        fetch(r, clone_dir(r), REPOS[r])
        mates.append((r, clone_dir(r), sh("git", "rev-parse", f"origin/{REPOS[r]}", cwd=clone_dir(r))))
    mds = [blob(p, at, "CLAUDE.md") for _, p, at in mates]
    for slot, s in slots(wf):
        if (size := len((md + "".join(mds) + s["instructions"]).encode())) > CLAUDE_MD_MAX:
            also = (" and the companions' CLAUDE.md" if any(mds) else "") \
                + (f" plus the `{slot}` instructions in .rookery/workflow.json" if s["instructions"] else "")
            raise RuntimeError(f"CLAUDE.md{also} on {base} is {size // 1024} KB; Rookery passes it to "
                               f"the agent in one argument, which is capped at {CLAUDE_MD_MAX // 1024} KB. "
                               "Shorten it on the base branch, then comment again.")
    # one plugin per job (the CLI loads exactly one per --plugin-dir), so all in it is `rk:<name>`.
    # Only skills/ and agents/: hooks/, .mcp.json or commands/ from any source never get in.
    (plug / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "rk"}', encoding="utf-8")
    skills = plug / "skills"
    skills.mkdir(exist_ok=True)

    # built-in agents, then the owner's, then .rookery/agents on base: a later one of the same
    # name replaces the role. Top-level *.md only: the CLI reads nothing else from agents/.
    shutil.copytree(HERE / "agents", plug / "agents")
    by = {p.stem: "built-in" for p in (plug / "agents").glob("*.md")}
    top = lambda m: m.isfile() and "/" not in m.name and m.name.endswith(".md")
    for cwd, at, src, label in ([(org[1], org[2], "agents", org[0])] if org else []) \
            + [(path, sha, ".rookery/agents", ".rookery/agents")]:
        if oid(cwd, at, src):
            by.update((Path(f).stem, label) for f in untar(cwd, at, src, plug / "agents", top))
    # the first source to bring a name wins: .rookery/skills, .claude/skills, the owner's, the catalog
    won = {}
    for cwd, at, src, label in [(path, sha, s, s) for s in (".rookery/skills", ".claude/skills")] \
            + ([(org[1], org[2], "skills", org[0])] if org else []):
        if oid(cwd, at, src):
            untar(cwd, at, src, skills, lambda m: m.name.split("/")[0] not in won)
            for d in skills.iterdir():
                if d.is_dir():
                    won.setdefault(d.name, label)
    # the catalog only as far as this repo enabled it; names are NAME-checked, so no `..`
    for name in sorted({k for _, s in slots(wf) for k in s["skills"]} - won.keys()):
        if (HERE / "skills" / name).is_dir():
            shutil.copytree(HERE / "skills" / name, skills / name)
            won[name] = "catalog"
    # a companion only adds, never replaces: a name the home's assembly has stays the home's, so a
    # frontend `reviewer` can never become a backend run's reviewer. Between companions, first wins.
    skipped = set()
    for r, p, at_ in mates:
        label = f"with:{r}"

        def new(name, have):   # logs what the companion lost; its own second source is no loss
            if name in have and have[name] != label:
                skipped.add(f"{name} ({r})")
            return name not in have
        if oid(p, at_, ".rookery/agents"):
            by.update((Path(f).stem, label) for f in untar(
                p, at_, ".rookery/agents", plug / "agents", lambda m: top(m) and new(Path(m.name).stem, by)))
        for src in (".rookery/skills", ".claude/skills"):
            if oid(p, at_, src):
                untar(p, at_, src, skills, lambda m: new(m.name.split("/")[0], won))
                for d in skills.iterdir():
                    if d.is_dir():
                        won.setdefault(d.name, label)
    # where a file came from, for the error that names it
    at = lambda src, sub, f: (f"{sub}/{f} in {src.removeprefix('with:')}" if org and src == org[0]
                              or src.startswith("with:") else f"{src}/{f} on {base}")
    described = {}
    for name, src in sorted(won.items()):
        where = at(src, "skills", name) if src != "catalog" else f"catalog skill {name}"
        if not NAME.fullmatch(name):
            raise RuntimeError(f"{where}: a skill name must match `{NAME.pattern}`.")
        described[name] = frontmatter(skills / name / "SKILL.md", SKILL_KEYS, where).get("description", "")
    agents = {p.stem: parse_agent(p, at(by[p.stem], "agents", p.name) if by[p.stem] != "built-in"
                                  else f"agent {p.stem}") for p in sorted((plug / "agents").glob("*.md"))}
    # --agent with a name that does not exist is silently ignored (S2a): never pass one
    for slot, s in slots(wf):
        for kind, names, have in (("agent", [s["agent"]] if s["agent"] else [], agents),
                                  ("skill", s["skills"], won)):
            if missing := [x for x in names if x not in have]:
                src = f"workflow.json in {org[0]}" if owf and dict(slots(owf)).get(slot) == s \
                    else f".rookery/workflow.json on {base}"
                raise RuntimeError(f"{src}: `{slot}` names the {kind} `{missing[0]}`, and there is no "
                                   f"{kind} by that name.")
    return {"sha": sha, "claude_md": md, "workflow": wf, "agents": agents, "agent_sources": by,
            "sources": won, "skills": described, "org": org and {"repo": org[0], "sha": org[2]},
            "org_workflow": owf, "skipped": sorted(skipped),
            "with": [{"repo": r, "base": REPOS[r], "sha": at_, "claude_md": m} for (r, _, at_), m in zip(mates, mds)]}


def config_dir(repo):
    """Where repo keeps its own config: .rookery/, or the root of an owner's .rookery repo,
    whose root is the owner's defaults (org_of())."""
    return "" if repo.split("/")[1] == ORG else ".rookery/"


def config_files(repo):
    """repo's config files on origin/<base> as of its last fetch, CONFIG_FILE path -> text:
    what the portal's editor starts from, read even when the config does not validate."""
    path, d, at = clone_dir(repo), config_dir(repo), f"origin/{REPOS[repo]}"
    agents = sh("git", "ls-tree", "--name-only", f"{at}:{d}agents", cwd=path, check=False).splitlines()
    # not blob(): sh() strips, and an editor saving the file untouched must save the same bytes
    raw = lambda f: subprocess.run(["git", "cat-file", "blob", f"{at}:{d}{f}"], cwd=path,
                                   capture_output=True).stdout.decode("utf-8", "replace")
    return {f: t for f in ["workflow.json"] + [f"agents/{a}" for a in agents]
            if CONFIG_FILE.fullmatch(f) and (t := raw(f))}


def propose(repo, files, by):
    """Open a pull request on repo that sets `files` (CONFIG_FILE path -> text, None deletes)
    in its config_dir(), on a freshly fetched base. The portal's only way to change what steers
    Rookery: a human merges it, so pin()'s "from reviewed base" holds. The commit is built in a
    throwaway index, so neither the clone's checkout nor a worktree is touched, and steer() runs
    at it first: a PR that would fail the next job at start is never opened. -> the PR's URL."""
    base, path, d = REPOS[repo], clone_dir(repo), config_dir(repo)
    if not files or not all(isinstance(f, str) and CONFIG_FILE.fullmatch(f) for f in files):
        raise ValueError(f"only workflow.json and agents/<name>.md under {d or 'the repo root'} can change here")
    fetch(repo, path, base)
    head = sh("git", "rev-parse", f"origin/{base}", cwd=path)
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        sh("git", "read-tree", head, cwd=path, env=env)
        for f, text in files.items():
            if text is None:
                sh("git", "update-index", "--force-remove", d + f, cwd=path, env=env)
                continue
            # through a file, byte for byte: a text-mode pipe would turn \n into \r\n on Windows
            (Path(tmp) / "blob").write_bytes(text.encode())
            o = sh("git", "hash-object", "-w", "--no-filters", str(Path(tmp) / "blob"), cwd=path)
            sh("git", "update-index", "--add", "--cacheinfo", f"100644,{o},{d + f}", cwd=path, env=env)
        tree = sh("git", "write-tree", cwd=path, env=env)
        if tree == sh("git", "rev-parse", f"{head}^{{tree}}", cwd=path):
            raise ValueError(f"that is already what {base} has")
        names = ", ".join(d + f for f in files)
        commit = sh("git", "commit-tree", tree, "-p", head, "-m", f"Rookery config: {names}",
                    "-m", f"Proposed from the Rookery portal by @{by}.", cwd=path)
        steer(repo, path, base, Path(tmp) / "plugin", sha=commit)
    branch = f"rookery/config-{uuid.uuid4().hex[:8]}"
    # like implement()'s push: to a URL, so it carries the App's token whatever origin is
    sh("git", "push", f"https://github.com/{repo}.git", f"{commit}:refs/heads/{branch}", cwd=path, env=auth(repo))
    log(f"{repo} config PR from {branch} for {by}: {names}")
    return gh(repo, "pr", "create", "--base", base, "--head", branch, "--title", f"Rookery config: {names}",
              "--body-file", "-", input=CONFIG_WARNING + f"Proposed from the Rookery portal by @{by}. Rookery "
              f"checked it against `{base}` the way a job checks at start; it changes nothing until "
              "someone merges it.\n\n" + "\n".join(f"- {'deletes' if t is None else 'sets'} `{d + f}`"
                                                    for f, t in files.items()))


def claude(c, prompt, key="plan", resume=False, slot="task", per_job=False, wt=None):
    """One session per key: planning is one long thread, each task is its own. `slot`
    picks the workflow slot — its agent, the skills to name, the instructions to append —
    by name, or is an `after` step itself. `per_job` makes the session this job's own: the
    review and `after` steps re-run whole on every approve, and a resumed one repeats its
    last verdict instead of looking again (prod: a gate kept "failing" on a file now there).
    `wt` runs it elsewhere than the issue's worktree: a PR review's pr_worktree()."""
    a = active.get(c.repo) or {}
    sid = str(uuid.uuid5(NS, f"{c.repo}#{c.n}/{key}" + (f"/job-{a.get('job')}" if per_job else "")))
    s = slot if isinstance(slot, dict) else (a.get("workflow") or WORKFLOW)[slot]
    # the companions' worktrees beside the issue's; a PR review is of one PR, in one repo
    both = [] if slot == "pr-review" else mate_wts(c)
    if both:
        prompt += "\n\n" + WITH_REPOS.format(repos="\n".join(
            f"- `{m.repo}` at `{w.absolute()}`, base `{m.base}`. Its change is "
            f"`git -C {w.absolute()} diff origin/{m.base}...HEAD`." for m, w in both)) \
            + (WITH_PLAN if slot == "plan" else "")
    # a skill in the plugin alone almost never fires (S5: 0/3); named in the prompt, 3/3
    prompt = "\n\n".join([prompt] + [USE_SKILL.format(name=k) for k in s["skills"]])
    # the repo's and owner's own skills, unnamed by this slot, are offered, not ordered: a repo
    # that has skills but no workflow.json still gets them used where they fit. Not the
    # catalog's: those are in the plugin only because some other slot enabled them.
    if offer := [k for k, src in sorted((a.get("sources") or {}).items())
                 if src != "catalog" and k not in s["skills"]]:
        prompt += "\n\n" + OFFER_SKILLS.format(names=", ".join(f"`rk:{k}`" for k in offer))
    # the prompt goes on stdin, never argv: Linux caps one argv entry at 128 KB, and a PR
    # review's diff alone may be DIFF_CAP characters (prod: "Argument list too long")
    cmd = ["claude", "-p", "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           # none of the target repo's own settings: its hooks would run on this box, and
           # its skills and CLAUDE.md would come from the worktree — pin() reads them from base
           "--setting-sources", "user",
           # no MCP at all: without this the target repo's .mcp.json spawns its servers,
           # and the CLI login's claude.ai connectors reach every tenant's agent
           "--strict-mcp-config", *s.get("args", [])]
    if a.get("plugin"):
        cmd.append(f"--plugin-dir={a['plugin']}")
    if (out := wt_root(c.repo) / f"plans-{c.n}").exists():   # plans() put this issue's plan outside cwd
        cmd.append(f"--add-dir={out.absolute()}")
    cmd += [f"--add-dir={w.absolute()}" for _, w in both]
    mds = {w["repo"]: w for w in a.get("with") or []}
    # the slot's instructions land after the persona, which --agent puts in the system prompt
    rules = [f"Contents of CLAUDE.md (the repo's own instructions, from {c.base}):\n\n{a['claude_md']}"
             if a.get("claude_md") else "",
             *(f"Contents of CLAUDE.md in {m.repo} (a companion repo's own instructions, from {m.base}), for "
               f"its files only:\n\n{mds[m.repo]['claude_md']}" for m, _ in both if mds[m.repo]["claude_md"]),
             MEMORY_NOTE + a["memory"] if a.get("memory") else "",
             f"Instructions for this step (.rookery/workflow.json, from {c.base}):\n\n{s['instructions']}"
             if s["instructions"] else ""]
    if any(rules):
        # `=`: a value that starts with "-" stays a value
        cmd.append("--append-system-prompt=" + SYSTEM + "\n\n".join(filter(None, rules)))
    # guardrail: the agent never publishes. Deny rules beat bypassPermissions. They match the
    # command after hooks rewrite it, so the operator's RTK hook (DEPLOY.md §7) turning
    # `git push` into `rtk git push` would slip past the first two without the rtk forms.
    # `rtk proxy <cmd>` runs <cmd> raw, never rewritten, so it would slip past all four.
    cmd += ["--disallowed-tools", "Bash(git push:*)", "Bash(gh:*)", "Bash(rtk git push:*)", "Bash(rtk gh:*)",
            "Bash(rtk proxy:*)"]
    if s["agent"]:
        # a plugin agent, from agents/*.md via pin(); its `model:` is honoured (S2b). Its
        # `tools` list is a role hint, not a boundary — the deny rules above and the checks
        # in plan() are what actually enforce.
        cmd += ["--agent", f"rk:{s['agent']}"]
        model = (a.get("agents") or {}).get(s["agent"], {}).get("model")
    else:
        # no agent (implementation tasks, by default), so the model is set here
        model = s.get("model", "sonnet")
        cmd += ["--model", model]
    if RUN_AS:
        # sudo resets the env (nothing of .env reaches the agent) and PATH; the umask keeps
        # what the agent writes in a worktree writable by us, who tick, commit and clean up
        cmd = ["sudo", "-n", "-H", "-u", RUN_AS, "--", "sh", "-c",
               'umask 002; export PATH="$HOME/.local/bin:$PATH"; exec "$@"', "sh", *cmd]
    check_caps(c.repo)
    log(f"#{c.n} claude {key}{' (resume)' if resume else ''} — this takes a while...")
    # read once: the admin page can switch it mid-call, and the env and the price must agree
    prov, kid, secret = provider_for(c.repo)
    # None = inherit, as before providers: claude runs the exact env it always ran.
    # Through sudo these need the sudoers env_keep line (DEPLOY.md §5b).
    env = {**os.environ, **pe} if (pe := provider_env(prov, secret)) else None
    wt, out, m, usd = wt or worktree(c), "", None, 0.0
    try:
        try:
            out = sh(*cmd, *(["--resume", sid] if resume else ["--session-id", sid]), cwd=wt, env=env, input=prompt,
                     track=c.repo)
        except RuntimeError as e:
            # a previous run already created this session (it crashed, or we are being
            # re-run): the id is derived from repo#issue/key, so pick that session up.
            if resume or "already in use" not in str(e):
                raise
            log(f"#{c.n} session {sid} exists — resuming it")
            out = sh(*cmd, "--resume", sid, cwd=wt, env=env, input=prompt, track=c.repo)
        usd = cost_of(out, prov)   # an unpriced model raises here; the run still counts below
        text = json.loads(out).get("result", "")
        m = MARKER.search(text)
    finally:
        # a failed call still counts as a run; its cost is lost with its stdout
        record(c.repo, usd)
        # sid: digest() finds the session's transcript by it
        step(c.repo, "claude", key=key, status=m.group(1) if m else "no-marker" if out else "failed",
             usd=usd, agent=s["agent"], skills=s["skills"], model=model, provider=prov,
             **({"key": kid} if kid else {}), sid=sid)
    log(f"#{c.n} claude {key} -> {m.group(1) if m else 'NO STATUS MARKER'}")
    if not m:
        raise RuntimeError(f"no STATUS marker in agent reply:\n{text[-1500:]}")
    return m.group(1), unmark(text, m)


def unmark(text, m):
    """The reply without its STATUS line, and without a fence around the whole of it."""
    reply = (text[:m.start()] + text[m.end():]).strip()
    f = FENCED.fullmatch(reply)
    return f.group(1).strip() if f else reply


# Named roles live in agents/*.md and reach the CLI as plugin agents, `--agent rk:<name>`
# (pin(), claude()). They hold the standing rules of a role; the templates below hold
# the job. Implementation tasks run as no agent at all — the default harness with the
# deny rules is the point.

PLAN = prompt("plan")
REPLY = prompt("reply")
TASK = prompt("task")
REVIEW = prompt("review")
CHECK = prompt("check")
WRITE = prompt("write")
PR_REVIEW = prompt("pr-review")
FOLLOW_UP = prompt("follow-up")
FOLLOW_MERGED = prompt("follow-merged")
FOLLOW_CONFLICTS = prompt("follow-conflicts")
FOLLOW_EMPTY = (f"Mention `@{AGENT_USER}` with what to change or what to answer. For example: "
                f"`@{AGENT_USER} pull the latest from the base branch and resolve the conflicts`.")
NO_PR = "This issue has no open pull request, so there is nothing to follow up on. Open a new issue for further work."
INTERRUPTED = ("Agent error: Rookery restarted while this job was running, so the job stopped. "
               "The tasks that finished are committed. Comment `approve` to continue at the next task, "
               "or mention @{} again to repeat the follow-up.").format(AGENT_USER)

# what pin() reads from base: a PR that changes any of it steers Rookery once merged
STEERING = (".rookery", ".claude", "CLAUDE.md")
CONFIG_WARNING = ("> ⚠ This PR changes agent configuration (`.rookery/`, `.claude/` or the root "
                  "`CLAUDE.md`). Once merged, it steers how Rookery works on this repo.\n\n")

# after the prompt of every issue call, when `with` lists companions (claude())
WITH_REPOS = prompt("with-repos")
WITH_PLAN = prompt("with-plan")
# after PLAN or REPLY, when the issue names a repo of the owner that `with` does not list (named())
NOT_MOUNTED = prompt("not-mounted")
# the issue names a repo `with` does not list: once per repo per issue, before the plan call
NOT_LISTED = """This issue names `{repo}`, but Rookery can change only `{home}`: `.rookery/workflow.json` on `{base}` does not list it in `with`.

**To change both repos:** {install}add it and merge, then reply on this issue.
- In the portal: {url}/portal.html#workflow → pick `{name}` → drag `{short}` from the shelf onto **Repos** → open the pull request.
- Or by hand on `{base}`: `.rookery/workflow.json` ⟶ `"with": {listed}`

Your next reply re-plans with both repos. Approving the plan before that changes only `{name}`; the plan lists what `{short}` needs, for a person or for a later issue.
<!-- rookery:with {repo} -->"""
NOT_INSTALLED = "install the Rookery App on `{repo}` (https://github.com/apps/{app}/installations/new), then "

# one line per skill the slot enables in .rookery/workflow.json, after the slot's prompt
USE_SKILL = prompt("use-skill")
OFFER_SKILLS = prompt("offer-skills")

# what claude() puts above a repo's notes in the appended system prompt
MEMORY_NOTE = prompt("memory-note")
# what claude() puts above all of the appended system prompt: the CLI's own header for a
# discovered CLAUDE.md. Without it haiku dropped a format rule 2/9, with it 0/5 (S8).
SYSTEM = prompt("system")

REMEMBER = prompt("remember")
REWRITE = prompt("rewrite")

# the memory call is not a workflow slot: haiku, one turn, no tools, no agent, no plugin
MEMORY_SLOT = DEFAULTS["memory_slot"]


# --- tasks -------------------------------------------------------------------

def read_tasks(d):
    """-> [(offset of the checkbox char, done?, text)] in file order, from plan dir d."""
    p = d / "tasks.md"
    if not p.exists():
        raise RuntimeError("tasks.md is missing — the plan produced no task list")
    text = p.read_text(encoding="utf-8")
    return [(m.start(1), m.group(1).lower() == "x", m.group(2)[2:].strip())
            for m in TASK_LINE.finditer(text)]


def tick(d, at):
    """The orchestrator marks tasks done, never the agent."""
    p = d / "tasks.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text[:at] + "x" + text[at + 1:], encoding="utf-8")


# --- jobs --------------------------------------------------------------------

def keep_plans_only(c, wt, keep=None):
    """Planning writes plans() and nothing else — revert the rest. `keep=()`
    reverts everything, for a step that may not edit at all. -> the reverted paths.

    The PLAN prompt asks for this; here it is enforced, because a prompt is not
    a guard. Every call to plan() reverts what the previous one left behind, so
    calling it again — which a long question-and-answer thread does repeatedly —
    can never accumulate stray edits ahead of the task loop.
    """
    keep = (pdir(c.n) + "/",) if keep is None else keep
    reverted = []
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
        log(f"#{c.n} agent touched {path} — reverting")
        # `clean` takes untracked files and whole untracked dirs; `checkout`
        # restores modified and deleted tracked ones.
        sh("git", *(["clean", "-fdq"] if code == "??" else ["checkout"]), "--", path, cwd=wt)
        reverted.append(path)
    return reverted


def named(c, text):
    """-> the home owner's repos that `text` names and this job does not mount. A regex, not the
    model: `owner/name`, `owner/name#12` or its github.com URL, outside quotes and code. Another
    owner's repo (`facebook/react#1` in a bug report) is ordinary text, and so is a bare name:
    a repo called `api` or `web` would match ordinary words."""
    owner, canon = c.repo.split("/")[0], {r.lower(): r for r in REPOS}
    found = re.findall(rf"(?<![\w.@/-])(?:(?:https?://)?github\.com/)?{re.escape(owner)}/([\w.-]+)", unquoted(text), re.I)
    have = {c.repo.lower()} | {m.repo.lower() for m in mates(c)}
    out = {}   # lower-case -> as REPOS spells it, else as first written
    for r in (f"{owner}/{f.rstrip('.').removesuffix('.git')}" for f in found):
        if r.lower() not in have and r.split("/")[1] not in ("", ORG):
            out.setdefault(r.lower(), canon.get(r.lower(), r))
    return list(out.values())


def notice(c, repos):
    """NOT_LISTED on the issue, before the plan call, so it is there in seconds: once per repo per
    issue, since its hidden marker in an earlier comment is enough. No label, no claude call.
    ponytail: any comment with the marker counts, not only the bot's; a copied marker only mutes a hint."""
    said = gh(c.repo, "issue", "view", str(c.n), "--json", "comments", "--jq", ".comments[].body", check=False)
    listed = [m.repo for m in mates(c)]
    for r in repos:
        if f"<!-- rookery:with {r} -->" in said:
            continue
        comment(c, NOT_LISTED.format(repo=r, home=c.repo, base=c.base, url=PUBLIC_URL, name=c.repo.split("/")[1],
                                     short=r.split("/")[1], listed=json.dumps(listed + [r]),
                                     install="" if r in REPOS else NOT_INSTALLED.format(repo=r, app=ME.removesuffix("[bot]"))))
        step(c.repo, "named", repo=r)


def framing(c):
    """What the task, review and step prompts call the work, its diff and where their words go.
    The local CLI says "task #<n>", "<base>", "a branch to review and merge", "the run's report"."""
    return {"source": f"GitHub issue #{c.n}", "since": f"origin/{c.base}",
            "handoff": "a draft pull request", "report": "the pull request body"}


def plan(c, title, body, resume):
    set_phase(c, "rookery:planning")
    wt = worktree(c)
    for m in mates(c):   # mounted for every issue: the planner decides whether the issue needs it
        worktree(m, c)
    d = plans(c, wt)[1]
    source = f"GitHub issue #{c.n}"   # the local CLI says "task #<n>" and "this machine"
    prompt = (REPLY.format(source=source, body=body, d=d) if resume
              else PLAN.format(source=source, repo=c.repo, title=title, body=body, d=d, max=MAX_TASKS,
                               box="a Linux server"))
    if missing := named(c, f"{title or ''}\n{body}"):
        notice(c, missing)
        many = len(missing) > 1
        prompt += "\n" + NOT_MOUNTED.format(repos=", ".join(f"`{r}`" for r in missing),
                                            them="them" if many else "it", each="each of them" if many else "it")
    status, msg = claude(c, prompt, resume=resume, slot="plan")
    # a plan beside the worktree: nothing in the worktree is the plan's
    keep_plans_only(c, wt, keep=(d + "/",) if d == pdir(c.n) else ())
    for _, mw in mate_wts(c):   # planning changes no companion
        keep_plans_only(c, mw, keep=())
    if status == "plan-ready" and not read_tasks(plans(c, wt)[0]):
        # nothing to approve: an approve would open a PR with no commits, which gh refuses
        status, msg = "questions", (msg.replace("Reply `approve` to implement.", "").rstrip()
                                    + "\n\nThe plan has no tasks, so there is nothing to approve. "
                                      "Reply to change the plan, or close the issue.")
    comment(c, msg)
    set_phase(c, "rookery:awaiting-input" if status == "questions" else "rookery:awaiting-approval")


def implement(c):
    """One fresh session per unticked task. Re-running resumes where it stopped."""
    set_phase(c, "rookery:implementing")
    wt, said, unverified = worktree(c), {}, []
    for m in mates(c):   # `with` as of this job: a companion merged since the plan is mounted too
        worktree(m, c)
    p, d = plans(c, wt)
    if not read_tasks(p):   # before any claude call: the review and push would end in no PR
        raise RuntimeError("the plan has no tasks, so there is nothing to implement — reply to change it")
    for _ in range(MAX_TASKS + 1):
        tasks = read_tasks(p)
        if len(tasks) > MAX_TASKS:
            raise RuntimeError(f"{len(tasks)} tasks exceeds the {MAX_TASKS}-task cap — "
                               "split the issue or shorten the plan")
        todo = next(((i, at, text) for i, (at, is_done, text) in enumerate(tasks) if not is_done), None)
        if todo is None:
            break
        i, at, text = todo
        log(f"#{c.n} task {i + 1}/{len(tasks)}: {text}")
        step(c.repo, "task", i=i + 1, of=len(tasks), text=text)
        status, msg = claude(c, TASK.format(n=c.n, d=d, task=text, base=c.base, **framing(c)),
                             key=f"task-{i}")
        if status not in ("task-done", "task-unverified"):
            raise RuntimeError(f"task {i + 1} ({text}) reported {status}:\n\n{msg}")
        tick(p, at)
        step(c.repo, "tick", i=i + 1, of=len(tasks))
        # a verify-only task changes nothing in the worktree when the plans are outside it
        commit_all(c, wt, f"#{c.n} task {i + 1}: {text}", empty=True, i=i + 1)
        log(f"#{c.n} committed task {i + 1}")
        said[i] = ("✅" if status == "task-done" else "⚠️", msg)
        if status == "task-unverified":
            unverified.append(f"**Task {i + 1}.** {msg}")
    else:
        raise RuntimeError("task loop did not converge")
    # every task, not only this run's: a run that resumes after a failure or a restart
    # still lists what the earlier runs did (their replies died with them)
    done = [fold(f"{said.get(i, ('✅',))[0]} {i + 1}. {summary(text)}",
                 f"> {text}" + (f"\n\n{said[i][1]}" if i in said else ""))
            for i, (_, _, text) in enumerate(read_tasks(p))]
    # a check this box cannot run is the human's to run, not a failed run
    lead = ["## ⚠️ Not verified here\n\n" + "\n\n".join(unverified)] if unverified else []
    publish(c, wt, [*lead, "## Tasks", *done] if done else [])


def commit_all(c, wt, subject, empty=False, merge=False, **detail):
    """Commit what the agent left in the home worktree and in each companion's -> [(repo, sha)],
    logged as `commit` steps. A companion commits only when it changed, under the home's full
    name: `#12` there would be its own issue 12. The home commits when it changed, or `empty`
    (--allow-empty), or `merge` (the merge in progress, --no-edit), or when nothing else did:
    then git's "nothing to commit" fails the job, as it always has."""
    made = []
    for m, mw in mate_wts(c):
        if sh("git", "status", "--porcelain", cwd=mw):
            sh("git", "add", "-A", cwd=mw)
            sh("git", "commit", "-m", c.repo + subject, cwd=mw)
            made.append((m.repo, mw))
    sh("git", "add", "-A", cwd=wt)
    if empty or merge or not made or sh("git", "status", "--porcelain", cwd=wt):
        sh("git", "commit", *(["--no-edit"] if merge else ["--allow-empty"] * empty + ["-m", subject]), cwd=wt)
        made.insert(0, (c.repo, wt))
    made = [(r, sh("git", "rev-parse", "--short", "HEAD", cwd=w)) for r, w in made]
    for r, sha in made:   # where each commit went, in the run log
        step(c.repo, "commit", **detail, sha=sha, **({"repo": r} if r != c.repo else {}))
    return made


def summary(text):
    """A task's first sentence, plain and cut to SUMMARY_CAP: the line a reader scans."""
    s = re.split(r"(?<=\.)\s", text.replace("`", ""), maxsplit=1)[0]
    return s if len(s) <= SUMMARY_CAP else s[:SUMMARY_CAP - 1].rsplit(" ", 1)[0] + "…"


def fold(title, body):
    """Collapsed on GitHub: `title` is plain text (a <summary> renders no markdown), `body` is."""
    return f"<details><summary>{html.escape(title)}</summary>\n\n{body}\n\n</details>"


def publish(c, wt, done, reply=""):
    """implement()'s tail, from the `after` steps on; follow_up() runs it too, so the two
    cannot drift: the checks, the review, the push, then the draft PR — or, when the branch
    already has an open one, this run's sections as a comment on it. `done` leads the PR
    text, `reply` the issue comment."""
    after = (active[c.repo].get("workflow") or {}).get("after", [])
    sections, wanting = after_steps(c, wt, after)

    branch = f"feature/issue-{c.n}"
    assert branch != c.base

    set_phase(c, "rookery:reviewing")
    p, d = plans(c, wt)
    with frozen(p / "tasks.md"):
        verdict, review = claude(c, REVIEW.format(n=c.n, d=d, base=c.base, **framing(c)), key="review", slot="review", per_job=True)
    revert_all(c, wt)   # read-only too; a write step on a later run would commit its edits
    s = (active[c.repo].get("workflow") or WORKFLOW)["review"]
    review = render_review(verdict, review, " · ".join(f"rk:{k}" for k in [s["agent"], *s["skills"]]),
                           verdicts=REVIEW_VERDICTS, level="###", wt=wt)

    # a companion with no commit for this issue gets no push and no PR
    parts = [(m, mw, f"feature/{tag(m, c)}") for m, mw in mate_wts(c)
             if sh("git", "rev-list", "-1", f"origin/{m.base}..HEAD", cwd=mw)]
    log(f"#{c.n} pushing {branch}{''.join(f', {m.repo} {b}' for m, _, b in parts)} and opening draft PRs")
    for m, mw, b in [*parts, (c, wt, branch)]:
        assert b != m.base
        # an explicit https URL, so the push is the App's even when origin is ssh
        sh("git", "push", f"https://github.com/{m.repo}.git", f"{b}:{b}", cwd=mw, env=auth(m.repo))
        # a push to a URL updates no remote-tracking ref; record it, so unpushed()
        # sees this work as safe on GitHub
        sh("git", "update-ref", f"refs/remotes/origin/{b}", b, cwd=mw)
        step(c.repo, "push", branch=b, **({"repo": m.repo} if m is not c else {}))
    title = json.loads(gh(c.repo, "issue", "view", str(c.n), "--json", "title"))["title"]
    # what steers the agent changes only by a human merging it (pin()): say so where they merge
    steers = lambda m, mw: sh("git", "diff", "--name-only", f"origin/{m.base}...HEAD", "--", *STEERING, cwd=mw)
    open_pr = lambda m, b: gh(m.repo, "pr", "view", b, "--json", "state,url", "--jq",
                              'select(.state == "OPEN") | .url', check=False)
    # approve again after a run that opened the PR (a check flagged it): `gh pr create`
    # refuses a second PR for the branch, so this run's sections go on that PR as a comment
    url = open_pr(c, branch)

    def part(m, mw, home):
        # never `Closes #<n>`: in the companion, #<n> is its own issue n
        log_ = sh("git", "log", "--no-merges", "--reverse", "--format=- %s", f"origin/{m.base}..HEAD", cwd=mw)
        return ((CONFIG_WARNING if steers(m, mw) else "") + f"Part of {c.repo}#{c.n}. Merge it together with "
                + (home or f"the pull request for {c.repo}#{c.n}") + f".\n\n## Commits\n\n{log_}")
    links, new = [], []
    for m, mw, b in parts:   # first, so the home's body can list them
        if u := open_pr(m, b):
            gh(m.repo, "pr", "comment", u, "--body-file", "-", input=part(m, mw, url))
        else:
            u = gh(m.repo, "pr", "create", "--draft", "--base", m.base, "--head", b, "--title", title,
                   "--body-file", "-", input=part(m, mw, url))
            new.append((m, mw, u))
        step(c.repo, "pr", url=u, repo=m.repo)
        links.append(f"- `{m.repo}`: {u}")
    with_ = ["## Companion PRs\n\nMerge these together with this one.\n\n" + "\n".join(links)] if links else []

    def body(lead=()):
        return ((CONFIG_WARNING if steers(c, wt) else "")
                + "\n\n".join([*lead, *done, *with_, *sections])
                + f"\n\n## 🤖 Reviewer agent\n\n{review}\n\nCloses #{c.n}"
                # the commit every steering input was read at; GitHub links owner/repo@sha
                f"\n\n---\nworkflow: {c.repo}@{active[c.repo]['sha']}"
                + (f" · steps: {', '.join(s['agent'] for s in after)}" if after else ""))
    if url:
        gh(c.repo, "pr", "comment", url, "--body-file", "-", input=body())
    else:
        # the plan leads the PR once, folded; a local_plans plan is on GitHub nowhere else
        plan = (p / "plan.md").read_text(encoding="utf-8").strip() if (p / "plan.md").exists() else ""
        if len(plan) > PLAN_CAP:
            plan = plan[:PLAN_CAP] + f"\n\n_…cut at {PLAN_CAP} characters._"
        url = gh(c.repo, "pr", "create", "--draft", "--base", c.base, "--head", branch, "--title", title,
                 "--body-file", "-", input=body([fold("📋 Plan", plan)] if plan else []))
        for m, mw, u in new:   # opened before the home's PR existed: now they can name it
            # REST, never `gh pr edit` (see unlabel())
            api("PATCH", f"/repos/{m.repo}/pulls/{u.rsplit('/', 1)[1]}", token(m.repo), {"body": part(m, mw, url)})
    # The review and the checks are advisory: the PR opens either way, so the work is
    # always on GitHub rather than stranded in a local worktree. A bad verdict only flags.
    step(c.repo, "pr", url=url)
    if verdict != "review-ok":
        wanting.append("the reviewer")
    lead = (f"{reply}\n\nUpdated: {url}" if reply else f"Draft PR: {url}") + "".join(f"\nAnd in {l[2:]}" for l in links)
    comment(c, lead if not wanting else
            f"{lead}\n\nChanges wanted by {', '.join(wanting)} — see their sections on the PR.")
    # the run finished: an advisory verdict is not a failure, so not rookery:error
    set_phase(c, "rookery:changes-wanted" if wanting else "rookery:done")


def revert_all(c, wt):
    """keep_plans_only(keep=()) on the home worktree and on each companion's: a step that may not
    edit, edited nowhere. -> the reverted paths, a companion's as `<repo>:<path>`."""
    return keep_plans_only(c, wt, keep=()) + [f"{m.repo}:{p}" for m, mw in mate_wts(c)
                                              for p in keep_plans_only(c, mw, keep=())]


def after_steps(c, wt, after):
    """The workflow's `after` steps, in order, between the task loop and the review ->
    (their PR sections, the check steps that want changes). A check step may not edit and
    re-runs every time; a write step's work is committed by the orchestrator, and a re-run
    skips it once that commit is on the branch. Neither adds tasks, so the loop's bound holds."""
    if not after:
        return [], []
    on_branch = set(sh("git", "log", "--format=%s", f"origin/{c.base}..HEAD", cwd=wt).splitlines())
    sections, wanting = [], []
    p, d = plans(c, wt)
    for s in after:
        name, mode = s["agent"], s["mode"]
        subject = f"#{c.n} step {name}"
        if mode == "write" and subject in on_branch:
            log(f"#{c.n} step {name} already committed — skipping")
            continue
        step(c.repo, "step", agent=name, mode=mode)
        with frozen(p / "tasks.md"):   # neither mode adds tasks
            status, msg = claude(c, (CHECK if mode == "check" else WRITE).format(n=c.n, d=d, base=c.base, **framing(c)),
                                 key=f"step-{name}", slot=s, per_job=True)
        # enforced, not asked: whatever a check leaves in the worktree is reverted, and flagged
        reverted = revert_all(c, wt) if mode == "check" else []
        if reverted:
            step(c.repo, "reverted", agent=name, paths=reverted)
        if status not in MODES[mode]:
            raise RuntimeError(f"step {name} replied `STATUS: {status}`; a {mode} step replies "
                               f"{' or '.join(MODES[mode])}.")
        if status == "failed":
            raise RuntimeError(f"step {name} reported failed:\n\n{msg}")
        if mode == "check":
            msg = render_review(status, msg, " · ".join(f"rk:{k}" for k in [name, *s["skills"]]),
                                verdicts=CHECK_VERDICTS, level="###", wt=wt)
        if reverted:
            msg += f"\n_Rookery reverted this check's edits to {', '.join(reverted)}: a check is read-only._\n"
        if mode == "write":
            commit_all(c, wt, subject, empty=True, step=name)
        elif status == "changes-needed" or reverted:
            wanting.append(name)
        # an icon and "agent", so a reader sees whose words these are, not a part of the PR
        sections.append(f"## 🤖 {name[0].upper()}{name[1:]} agent · {mode}"
                        + "".join(f" · rk:{k}" for k in s["skills"]) + f"\n\n{msg}")
    return sections, wanting


def at_head(c, home=None):
    """follow_up()'s worktree, at its branch's head on GitHub, not the local branch: a person may
    have pushed to it since. Re-added there when missing, moved up --ff-only, never reset —
    worktree()'s -B would reset it to base. A companion's branch is on GitHub only once a run
    committed there; until then it is worktree()'s, from base."""
    t = tag(c, home)
    branch, wt = f"feature/{t}", wt_root(c.repo) / t
    if not home:
        fetch(c.repo, c.path, branch)
    else:
        with fetchlocks[c.repo]:   # fetch()'s lock; a branch not on GitHub yet is no error
            sh("git", "fetch", "origin", branch, cwd=c.path, env=auth(c.repo), check=False)
        if not sh("git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=c.path, check=False):
            return worktree(c, home)
    if not wt.exists():
        shared(wt.parent)
        sh("git", "worktree", "prune", cwd=c.path)
        mine = sh("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=c.path, check=False)
        sh("git", "worktree", "add", *([str(wt), branch] if mine else ["-b", branch, str(wt), f"origin/{branch}"]),
           cwd=c.path)
        sealed(wt)
    sh("git", "merge", "--abort", cwd=wt, check=False)   # a cancelled follow-up's merge, else nothing
    try:
        sh("git", "merge", "--ff-only", f"origin/{branch}", cwd=wt)
    except RuntimeError as e:
        raise RuntimeError(f"{branch} here and on GitHub have both moved, and Rookery never force-pushes:\n\n{e}") from None
    return wt


def follow_up(c, job, note):
    """A mention on the issue once its draft PR is open: an answer, or a change on
    feature/issue-<n> that goes through the checks and the review again, as approving again
    does. The agent says which (FOLLOW_VERDICTS); the worktree holds it to that. The
    orchestrator brings the PR's head and base in, checks the conflicts are gone, and commits."""
    if job.get("comment"):   # "picked up", as review_pr() does. Cosmetic, so never fatal.
        try:
            api("POST", f"/repos/{c.repo}/issues/comments/{job['comment']}/reactions", token(c.repo),
                {"content": "eyes"})
        except Exception as e:
            log(f"#{c.n} 👀 failed: {e}")
    if not note:
        return comment(c, FOLLOW_EMPTY)
    branch = f"feature/issue-{c.n}"
    pr = json.loads(gh(c.repo, "pr", "view", branch, "--json", "state,url,mergeable,mergeStateStatus",
                       check=False) or "{}")
    if pr.get("state") != "OPEN":   # merged, closed or never opened; after a merge the worktree is gone too
        return comment(c, NO_PR)
    wt = at_head(c)
    for m in mates(c):
        at_head(m, c)
    # base only when the PR needs it, or the note names it: a question stays a question.
    # ponytail: GitHub's view, UNKNOWN for a while after a push to base; a local
    # `git merge-tree --write-tree` (git 2.38+) if that bites.
    fetch(c.repo, c.path, c.base)
    merge, conflicts = "", []
    if pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "BEHIND" \
            or re.search(rf"(?<![\w/-]){re.escape(c.base)}(?![\w/-])", note, re.I):
        before = sh("git", "rev-parse", "HEAD", cwd=wt)
        try:
            sh("git", "merge", "--no-ff", "--no-edit", "-m", f"#{c.n} merge {c.base}", f"origin/{c.base}", cwd=wt)
        except RuntimeError:
            if not (conflicts := sh("git", "diff", "--name-only", "--diff-filter=U", cwd=wt).splitlines()):
                raise
        step(c.repo, "merge", base=c.base, conflicts=conflicts)
        if conflicts:
            merge = FOLLOW_CONFLICTS.format(base=c.base, files="\n".join(f"- {f}" for f in conflicts))
        elif sh("git", "rev-parse", "HEAD", cwd=wt) != before:
            merge = FOLLOW_MERGED.format(base=c.base)
    p, d = plans(c, wt)
    try:
        with frozen(p / "tasks.md"):   # a follow-up adds no tasks, so every tick still reduces the open count
            status, msg = claude(c, FOLLOW_UP.format(
                source=f"GitHub issue #{c.n}", handoff=f"its draft pull request\n{pr['url']} is open",
                who="Someone with write access commented on the issue", since=f"origin/{c.base}",
                posts="posts that text on the issue", d=d, base=c.base, note=note, merge=merge),
                key="follow-up", per_job=True, wt=wt)
        if status not in FOLLOW_VERDICTS:
            raise RuntimeError(f"the follow-up replied `STATUS: {status}`; it replies {', '.join(FOLLOW_VERDICTS)}.")
        if status == "failed":
            raise RuntimeError(f"the follow-up reported failed:\n\n{msg}")
        if status == "answered" and conflicts:
            raise RuntimeError("the follow-up replied `STATUS: answered` with merge conflicts still open in "
                               f"{', '.join(conflicts)}.")
        # checked here, not asked: a marker left in would ship
        if left := [f for f in conflicts if (wt / f).is_file()
                    and CONFLICT.search((wt / f).read_text(encoding="utf-8", errors="replace"))]:
            raise RuntimeError(f"the follow-up replied `STATUS: changed`, but conflict markers are still in "
                               f"{', '.join(left)}.")
    except Exception:
        # nothing half-merged or half-done stays for the next follow-up to commit
        sh("git", "merge", "--abort", cwd=wt, check=False)
        revert_all(c, wt)
        raise
    said = msg
    if status == "changed":
        # the merge commit carries the resolution
        merging = bool(sh("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", cwd=wt, check=False))
        if merging or any(sh("git", "status", "--porcelain", cwd=w) for w in [wt, *(mw for _, mw in mate_wts(c))]):
            commit_all(c, wt, f"#{c.n} follow-up: {' '.join(note.splitlines()[0].split())[:60]}", merge=merging)
        else:
            status = "answered"   # no diff at all: it was an answer after all
    elif reverted := revert_all(c, wt):   # an answer changes nothing
        step(c.repo, "reverted", paths=reverted)
        said += f"\n\n_Rookery reverted the edits to {', '.join(reverted)}: an answer changes nothing._"
    # unpushed, not this run's: a commit an earlier failed follow-up left is published now
    if sh("git", "rev-parse", "HEAD", cwd=wt) == sh("git", "rev-parse", f"origin/{branch}", cwd=wt) \
            and not any(unpushed(mw, "HEAD") for _, mw in mate_wts(c)):
        return comment(c, said)
    publish(c, wt, ["## 🤖 Follow-up\n\n> " + note.replace("\n", "\n> ") + f"\n\n{said}"], reply=said)


# --- pull request review -----------------------------------------------------

def unquoted(body):
    """The comment without its quoted lines and fenced code: what the commenter wrote themselves."""
    text = re.sub(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1|\Z)", "", body or "", flags=re.M | re.S)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def mention(body):
    """The comment without its @rookery-agent, or None when it does not address Rookery.
    Quoted lines and code do not count: a reply quoting a request is not a second one."""
    text = unquoted(body)
    if not MENTION.search(re.sub(r"`[^`\n]*`", "", text)):
        return None
    note = MENTION.sub("", text).strip()
    # prod: "@rookery-agent review again" re-reviewed an unchanged head. Only asking is no note.
    return "" if set(re.findall(r"[\w-]+", note.lower())) <= ASKING else note


def pr_worktree(c, base):
    """The PR's head, detached, at wt_root/pr-<n>, with origin/<base> fetched for the diff.
    Never committed to, so removing it can never lose work, and unpushed() has no say."""
    fetch(c.repo, c.path, f"+refs/pull/{c.n}/head:refs/remotes/origin/pr/{c.n}")
    fetch(c.repo, c.path, base)
    wt = drop_pr_worktree(c)
    shared(wt.parent)
    sh("git", "worktree", "add", "--detach", str(wt), f"origin/pr/{c.n}", cwd=c.path)
    sealed(wt)
    return wt


def drop_pr_worktree(c):
    wt = wt_root(c.repo) / f"pr-{c.n}"
    if wt.exists():
        sh("git", "worktree", "remove", "--force", str(wt), cwd=c.path, check=False)
    if wt.exists():   # a leftover git no longer knows about
        rmtree(wt)
    sh("git", "worktree", "prune", cwd=c.path, check=False)
    return wt


def pr_diff(wt, base):
    """The PR's change, for its review prompt: the stat whole, then the diff without lockfiles,
    cut at the last file boundary before DIFF_CAP, naming the files cut. Run here, not by the
    agent, so the operator's RTK hook (which drops hunk lines) never sees it."""
    rng, only = f"origin/{base}...HEAD", ("--", ".", *LOCKFILES)
    stat = sh("git", "diff", "--stat", rng, cwd=wt)
    files = re.split(r"(?m)^(?=diff --git )", sh("git", "diff", rng, *only, cwd=wt))[1:]   # one chunk per file
    names = sh("git", "diff", "--name-only", rng, *only, cwd=wt).splitlines()             # in the same order
    keep, size = [], 0
    for f in files:
        if size + len(f) > DIFF_CAP:
            break
        keep.append(f)
        size += len(f)
    out = (f"`git diff --stat {rng}`:\n\n```\n{stat}\n```\n\n`git diff {rng}`, lockfiles left out:\n\n"
           f"```diff\n{''.join(keep).rstrip()}\n```")
    if left := names[len(keep):]:
        out += (f"\n\nThe diff above stops at {DIFF_CAP:,} characters. These files are changed too and are not "
                "in it; read them with Read:\n\n" + "\n".join(f"- {n}" for n in left))
    return out


def reviewed_at(repo, n, head):
    """True when an earlier pr-review job on repo#n finished ok and posted at this head."""
    with closing(db()) as con:
        rows = con.execute("SELECT s.detail FROM jobs j JOIN steps s ON s.job = j.id WHERE j.repo = ? AND j.n = ? "
                           "AND j.act = 'pr-review' AND j.status = 'ok' AND s.what = 'posted'", (repo, n)).fetchall()
    return any(json.loads(d).get("head") == head for d, in rows)


def review_pr(c, job):
    """One advisory review on PR c.n, posted as a review of type comment. Any PR of the repo,
    not only Rookery's; never a fork's."""
    # "queued and running": on the mention, or on the PR itself for a label or review request.
    # Cosmetic, so never fatal.
    on = f"issues/comments/{job['comment']}" if job.get("comment") else f"issues/{c.n}"
    if job.get("comment") or job.get("label") or job.get("requested") or job.get("auto"):
        try:
            api("POST", f"/repos/{c.repo}/{on}/reactions", token(c.repo), {"content": "eyes"})
        except Exception as e:
            log(f"#{c.n} 👀 failed: {e}")
    pr = json.loads(gh(c.repo, "pr", "view", str(c.n), "--json",
                       "title,body,baseRefName,isCrossRepository,state,headRefOid"))
    if pr["isCrossRepository"]:
        raise RuntimeError("Rookery does not review pull requests from forks yet.")
    if pr["state"] != "OPEN":
        raise RuntimeError(f"This pull request is {pr['state'].lower()}; Rookery reviews open ones only.")
    head = pr["headRefOid"]
    # prod: one head reviewed twice, 4 minutes apart, for $1.11. A note is a new question.
    if not job.get("note") and reviewed_at(c.repo, c.n, head):
        step(c.repo, "skipped", head=head)
        gh(c.repo, "pr", "comment", str(c.n), "--body-file", "-",
           input=f"Already reviewed at `{head[:7]}`. Push a change, or mention @{AGENT_USER} with a note, "
                 "to review again.")
        return
    note = f"\nThe person who asked for this review wrote:\n\n{job['note']}\n" if job.get("note") else ""
    s = (active[c.repo].get("workflow") or WORKFLOW)["pr-review"]
    by = " · ".join(f"rk:{k}" for k in [s["agent"], *s["skills"]])
    try:
        wt = pr_worktree(c, pr["baseRefName"])
        # the local CLI's `rookery review` says "the commits on <ref> …", "<base>", "shows"
        verdict, text = claude(c, PR_REVIEW.format(what=f"pull request #{c.n} in {c.repo}", who="Someone with write access",
                                                   title=pr["title"], body=pr["body"] or "", note=note,
                                                   head="the pull request's head", since=f"origin/{pr['baseRefName']}",
                                                   posts="posts", wt=Path(wt).absolute(),
                                                   diff=pr_diff(wt, pr["baseRefName"])),
                               key="pr-review", slot="pr-review", per_job=True, wt=wt)
        review = render_review(verdict, text, by, wt=wt)   # before the worktree goes: it checks each `where`
    finally:
        drop_pr_worktree(c)
    gh(c.repo, "pr", "review", str(c.n), "--comment", "--body-file", "-", input=review)
    step(c.repo, "posted", verdict=verdict, head=head)


# PR review verdicts → the header of the posted review; the order is best first
PR_VERDICTS = DEFAULTS["pr_verdicts"]
# the implement-flow reviewer and checks keep two: implement(), MODES and rookery:changes-wanted key off them
REVIEW_VERDICTS = DEFAULTS["review_verdicts"]
CHECK_VERDICTS = DEFAULTS["check_verdicts"]
SEVERITY = DEFAULTS["severity"]
REVIEW_JSON = re.compile(r"<review>\s*(?:```(?:json)?\s*)?(.*?)\s*(?:```\s*)?</review>", re.S)
# cut, never failed: the review is already paid for
REVIEW_CAPS = DEFAULTS["review_caps"]
WHERE = re.compile(r"([^\s,;:()]+)(?::L?(\d+)(?:-L?\d+)?)?")   # path, optional :line or :line-line
UNCONFIRMED = "❔ Rookery did not find this file or line in the code. Check it by hand."


def located(wt, where):
    """False when a finding's `where` names a file, or a line past its end, that is not in wt:
    a guess, not something the reviewer read. Only tokens with a / or . count as paths, so
    prose in `where` passes. ponytail: checks wt only, so a finding on a file the change
    deletes reads as unconfirmed; check base too if that turns out to be common."""
    root = Path(wt).resolve()
    for p, line in WHERE.findall(str(where or "").replace("`", "")):
        p = p.rstrip(".")
        if "/" not in p and "." not in p:
            continue
        f = (root / p).resolve()
        if not f.is_relative_to(root) or not f.exists():
            return False
        if line and (f.is_dir() or not 1 <= int(line) <= len(f.read_bytes().splitlines())):
            return False
    return True


def render_review(verdict, text, by, verdicts=PR_VERDICTS, level="##", wt=None):
    """A reviewer's STATUS and <review> JSON → the markdown posted on the PR, or into the
    PR body under `level`. A reply off that shape raises: parsed, never inferred, like the
    STATUS marker. With `wt`, each finding's `where` is checked against it (located())
    and marked ❔ when it is not there: flagged, never dropped."""
    def bad(why):
        return RuntimeError(f"{by}'s reply {why}. Its reply ended:\n\n{text[-1500:]}")
    best = next(iter(verdicts))   # best first
    if verdict not in verdicts:
        raise bad(f"has `STATUS: {verdict}`; it replies {', '.join(verdicts)}")
    m = REVIEW_JSON.search(text)
    if not m:
        raise bad("has no <review> JSON")
    # a model sometimes drops the last `}` after a whole findings list: a closer added, never content
    for end in ("", "}"):
        try:
            r = json.loads(m.group(1) + end)
            break
        except ValueError as e:
            err = err if end else e
    else:
        raise bad(f"has <review> JSON that does not parse ({err})")
    fs = r.get("findings") if isinstance(r, dict) else None
    if not isinstance(r, dict) or not isinstance(r.get("summary"), str) or not isinstance(fs, list) or not all(
            isinstance(f, dict) and f.get("severity") in SEVERITY and isinstance(f.get("title"), str) for f in fs):
        raise bad("has <review> JSON without a summary, or with a finding that has no title or severity")
    if verdict == best and any(f["severity"] == "high" for f in fs):
        raise bad(f"says {best} but has a high finding")

    def cut(s, cap, cell=False):
        s = str(s or "").strip()
        if cell:   # one table cell: one line, and a | would end the cell
            s = " ".join(s.split()).replace("|", "\\|")
        return s if len(s) <= cap else s[:cap - 1].rstrip() + "…"
    # the count first, so "nothing to fix" is plain before anyone reads on
    tally = "".join(f" · {icon} {k} {sev}" for sev, icon in SEVERITY.items()
                    if (k := sum(f["severity"] == sev for f in fs)))
    lead = f"🔎 **{len(fs)} finding{'s' * (len(fs) != 1)}**{tally}" if fs else "✅ **No findings**"
    out = [f"{level} {verdicts[verdict]}", lead, cut(r['summary'], REVIEW_CAPS['summary'])]
    if answer := cut(r.get("answer"), REVIEW_CAPS["answer"]):
        out.append("> **Your note:** " + answer.replace("\n", "\n> "))
    shown = fs[:REVIEW_CAPS["findings"]]
    if shown:
        rows, unsure = ["| # | Severity | Where | Finding |", "|---|---|---|---|"], False
        for i, f in enumerate(shown, 1):
            where = cut(f.get("where"), REVIEW_CAPS["where"], cell=True).replace("`", "")
            mark = " ❔" if wt and not located(wt, f.get("where")) else ""   # the raw where: a cut one could miss
            unsure = unsure or bool(mark)
            rows.append(f"| {i} | {SEVERITY[f['severity']]} {f['severity']} | {f'`{where}`' if where else ''}{mark} "
                        f"| {cut(f['title'], REVIEW_CAPS['title'], cell=True)} |")
        if len(fs) > len(shown):
            rows.append(f"\n{len(fs) - len(shown)} more findings not shown.")
        if unsure:
            rows.append(f"\n{UNCONFIRMED}")
        out.append("\n".join(rows))
        details = [f"**{i}. {cut(f['title'], REVIEW_CAPS['title'], cell=True)}**\n\n"
                   f"{cut(f['detail'], REVIEW_CAPS['detail'])}" for i, f in enumerate(shown, 1) if f.get("detail")]
        if details:
            out.append("<details><summary>Details</summary>\n\n" + "\n\n".join(details) + "\n\n</details>")
    out.append(f"<sub>{by}</sub>")
    return "\n\n".join(out) + "\n"


def wrong_label(job):
    """No run log, no claude call, no cap. TRIGGER can only be wrong on a PR, REVIEW_LABEL only on
    an issue, and on a PR it is `gh pr …`, never `gh issue …` (it cannot resolve a PR number)."""
    kind, n = "pr" if job["label"] == TRIGGER else "issue", str(job["n"])
    log(f"{job['repo']}#{n} {job['label']} on the wrong kind of item: removing it")
    gh(job["repo"], kind, "comment", n, "--body-file", "-", input=WRONG_LABEL[job["label"]])
    unlabel(job["repo"], n, job["label"])


def pr_cleanup(c, job):
    """Whatever triggered the review, gone: a label or a request still there means queued or
    running. Never fails the job over it."""
    if job.get("label"):
        unlabel(c.repo, c.n, REVIEW_LABEL)
    if job.get("requested"):
        # the review is the App's, not AGENT_USER's, so GitHub would show the request pending forever
        try:
            api("DELETE", f"/repos/{c.repo}/pulls/{c.n}/requested_reviewers", token(c.repo),
                {"reviewers": [AGENT_USER]})
        except Exception as e:
            log(f"#{c.n} removing the review request failed: {e}")


# --- repo memory -------------------------------------------------------------

def digest(job):
    """One job's claude calls, read back from their transcripts, small enough for haiku: each
    tool call (the tool and its path, command or pattern), the first lines of each failed one,
    each session's last reply. Never file contents or successful output: they are big, and
    they are where injected text would come from."""
    with closing(db()) as con:
        calls = [json.loads(d) for d, in con.execute(
            "SELECT detail FROM steps WHERE job = ? AND what = 'claude' ORDER BY rowid", (job,))]
    out = []
    for s in calls:
        sid, text = s.get("sid"), ""
        if sid and RUN_AS:   # the CLI makes each project dir 0700 and transcript 0600: found and read as RUN_AS
            text = sh("sudo", "-n", "-u", RUN_AS, "find", str(CLAUDE_HOME / "projects"), "-mindepth", "2",
                      "-maxdepth", "2", "-name", f"{sid}.jsonl", "-exec", "cat", "{}", ";", "-quit", check=False)
        elif sid and (found := sorted((CLAUDE_HOME / "projects").glob(f"*/{sid}.jsonl"))):
            text = found[0].read_text(encoding="utf-8", errors="replace")
        if not text:
            log(f"job {job}: no transcript for claude {s.get('key')} — skipped")
            continue
        lines, reply = [f"## {s.get('key')} (STATUS: {s.get('status')})"], ""
        for raw in text.splitlines():
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            content = (o.get("message") or {}).get("content") if isinstance(o, dict) else None
            for x in content if isinstance(content, list) else []:
                kind = x.get("type") if isinstance(x, dict) else None
                if kind == "tool_use":
                    i = x.get("input") if isinstance(x.get("input"), dict) else {}
                    what = next((i[k] for k in ("file_path", "path", "command", "pattern", "url")
                                 if isinstance(i.get(k), str)), json.dumps(i))
                    lines.append(f"- {x.get('name')}: {' '.join(what.split())}"[:200])
                elif kind == "tool_result" and x.get("is_error"):
                    body = x.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                    lines += [f"    ! {l}"[:200] for l in str(body).splitlines()[:5]]
                elif kind == "text" and o.get("type") == "assistant":
                    reply = x.get("text", "")
        out.append("\n".join(lines + [f"Reply:\n{reply[:3000]}"]))
    d = "\n\n".join(out)
    # ponytail: keeps the tail, where a failure is; a per-call budget if long jobs lose their start
    return d if len(d) <= DIGEST_CAP else "(earlier calls cut)\n\n" + d[-DIGEST_CAP:]


def remember(job):
    """The `memory` job: fold one finished job's digest into its repo's notes. The orchestrator
    writes the file, from a checked reply; the model never touches it. A side channel: a failure
    is logged in its own run, never commented on the issue or labelled."""
    c = ctx(job["repo"], job["n"])
    d = digest(job["job"])
    if not d:   # no claude call to learn from (a skipped review), or no transcript
        log(f"{c.repo} job {job['job']}: nothing to remember")
        return
    begin(c, "memory")
    f = memory_file(c.repo)
    try:
        old = f.read_text(encoding="utf-8") if f.exists() else ""
        with tempfile.TemporaryDirectory() as tmp:   # its cwd: nothing to read, nothing to edit
            os.chmod(tmp, 0o755)   # mkdtemp's 0700 would shut RUN_AS out of its own cwd
            status, text = claude(c, REMEMBER.format(repo=c.repo, cap=MEMORY_CAP, notes=old, digest=d),
                                  key="memory", slot=MEMORY_SLOT, per_job=True, wt=Path(tmp))
            if status == "memory-updated":
                m = re.search(r"<notes>(.*?)</notes>", text, re.S)
                if not m:
                    raise RuntimeError("the memory reply has no <notes>…</notes>; the old notes stay")
                notes = m.group(1).strip()
                for _ in range(MEMORY_REWRITES):
                    if len(notes) <= MEMORY_CAP:
                        break
                    rstatus, rtext = claude(c, REWRITE.format(repo=c.repo, cap=MEMORY_CAP, n=len(notes),
                                                             notes=notes),
                                            key="memory-rewrite", slot=MEMORY_SLOT, per_job=True, wt=Path(tmp))
                    if rstatus != "memory-updated":
                        raise RuntimeError(f"the memory rewrite replied `STATUS: {rstatus}`; it replies "
                                           "memory-updated")
                    m = re.search(r"<notes>(.*?)</notes>", rtext, re.S)
                    if not m:
                        raise RuntimeError("the memory rewrite has no <notes>…</notes>; the old notes stay")
                    notes = m.group(1).strip()
                if len(notes) > MEMORY_CAP:   # still too long: the job must not fail on length
                    log(f"{c.repo}: the rewritten notes are {len(notes):,} characters; "
                        f"truncated to {MEMORY_CAP:,}")
                    notes = notes[:MEMORY_CAP].rsplit("\n", 1)[0]   # never half a bullet
                f.parent.mkdir(parents=True, exist_ok=True)
                tmpf = f.with_suffix(".tmp")   # a portal read never sees half a file
                tmpf.write_text(notes, encoding="utf-8")
                tmpf.replace(f)
                step(c.repo, "remembered", chars=len(notes),
                     sha=hashlib.sha256(notes.encode()).hexdigest()[:12], source=job["job"])
            elif status != "memory-unchanged":
                raise RuntimeError(f"the memory call replied `STATUS: {status}`; it replies memory-updated "
                                   "or memory-unchanged")
        finish(c.repo, "ok")
    except Exception as e:
        log(f"{c.repo} memory FAILED: {e}")
        cancelled = (active.get(c.repo) or {}).get("cancel")
        step(c.repo, "error", message=(f"Cancelled from the Rookery UI by @{cancelled}." if cancelled
                                       else str(e))[-2000:])
        finish(c.repo, "cancelled" if cancelled else "failed")


# --- repo stats: the portal's first tab --------------------------------------
# A: git log on the clone. B: our own run log. C: a few GitHub calls. Nothing is stored.
# ponytail: computed on request and cached 5 min; compute ahead if git log gets slow on a huge repo.

STATS_TTL = 300
_stats = {}   # (repo, days) -> (when, base sha, A and C). B is our own db, read fresh every time.


def stats(repo, days=90):
    """The portal's Stats tab for repo over the last `days`, or {"error"} while it is cloning.
    A hit skips the fetch as well as the GitHub calls: the numbers are up to STATS_TTL old."""
    base, path = REPOS[repo], clone_dir(repo)
    if not base or not path.exists():
        return {"error": "Rookery is still cloning this repo. Try again in a minute."}
    now = time.time()
    since = now - days * 86400
    at, sha, got = _stats.get((repo, days), (0, None, None))
    if now - at > STATS_TTL:
        try:
            fetch(repo, path, base)
        except Exception as e:   # stats from the last fetch beat no stats
            log(f"{repo} stats: fetch failed, using the last one: {e}")
        sha = sh("git", "rev-parse", f"origin/{base}", cwd=path)
        got = {**history(path, base, days, now), **github(repo, since)}
        if b := got["bot"]:
            lines = got["added"] + got["removed"]
            b.update(pr_share=b["prs"] / b["merged"] if b["merged"] else None,
                     line_share=min(1, b["lines"] / lines) if lines else None)
        for k, (t, *_) in list(_stats.items()):
            if now - t > STATS_TTL:
                _stats.pop(k, None)
        _stats[(repo, days)] = (now, sha, got)
    return {"repo": repo, "base": base, "sha": sha, "days": days, **got, "rookery": runlog(repo, since)}


def history(path, base, days, now):
    """A: base's non-merge commits in the window. Author names only, never emails."""
    out = sh("git", "log", f"origin/{base}", "--no-merges", f"--since={days}.days",
             "--format=%x00%an%x09%ct", "--numstat", cwd=path)
    # 30 days -> 5 weeks, 90 -> 13 weeks, 365 -> 12 months; the oldest week can be part of one
    n, width = (12, days * 86400 / 12) if days > 90 else (-(-days // 7), 7 * 86400)
    trend, authors, files = [0] * n, {}, {}
    for c in out.split("\0")[1:]:
        head, *rows = c.strip().split("\n")
        name, ct = head.rsplit("\t", 1)
        trend[n - 1 - min(n - 1, max(0, int((now - int(ct)) // width)))] += 1
        a = authors.setdefault(name, {"name": name, "commits": 0, "added": 0, "removed": 0})
        a["commits"] += 1
        for r in filter(None, rows):
            plus, minus, f = r.split("\t", 2)
            plus, minus = (0 if x == "-" else int(x) for x in (plus, minus))   # "-": a binary file
            a["added"] += plus
            a["removed"] += minus
            fl = files.setdefault(f, {"path": f, "commits": 0, "churn": 0})
            fl["commits"] += 1
            fl["churn"] += plus + minus
    return {"commits": sum(trend), "authors": len(authors),
            "added": sum(a["added"] for a in authors.values()), "removed": sum(a["removed"] for a in authors.values()),
            "unit": "month" if days > 90 else "week",
            "trend": [{"from": int(now - (n - i) * width), "commits": k} for i, k in enumerate(trend)],
            "top_authors": sorted(authors.values(), key=lambda a: (-a["commits"], -a["added"] - a["removed"]))[:5],
            "top_files": sorted(files.values(), key=lambda f: (-f["churn"], -f["commits"]))[:8]}


def runlog(repo, since):
    """B: what Rookery did on repo in the window, from the run log."""
    with closing(db()) as con:
        jobs = con.execute("SELECT n, act, status FROM jobs WHERE repo = ? AND started >= ?", (repo, since)).fetchall()
        steps = con.execute("SELECT what, detail FROM steps WHERE what IN ('claude', 'pr', 'posted') AND job IN "
                            "(SELECT id FROM jobs WHERE repo = ? AND started >= ?)", (repo, since)).fetchall()
    usd, prs, verdicts = 0, set(), collections.Counter()
    for what, d in steps:
        d = json.loads(d)
        if what == "claude":
            usd += d.get("usd", 0)
        elif what == "pr":
            prs.add(d["url"])   # approving again comments on the open PR: the same URL, not a new PR
        else:
            verdicts[d["verdict"]] += 1
    return {"runs": dict(collections.Counter(s for *_, s in jobs)),
            "issues": len({n for n, act, _ in jobs if act == "plan"}), "prs": len(prs),
            "reviews": sum(verdicts.values()), "verdicts": dict(verdicts), "usd": round(usd, 4),
            "per_pr": round(usd / len(prs), 4) if prs else None}


PULSE_TTL = 3600
_pulse = [0, None]   # (when, merged count) — one GitHub search per owner, so cached longer than stats


def pulse():
    """The landing page's numbers: instance-wide totals only — no repo, owner, title or login."""
    with closing(db()) as con:
        steps = dict(con.execute("SELECT what, COUNT(*) FROM steps WHERE what IN "
                                 "('claude', 'tick', 'commit', 'posted') GROUP BY what"))
        prs = con.execute("SELECT COUNT(DISTINCT json_extract(detail, '$.url')) FROM steps WHERE what = 'pr'").fetchone()[0]
        issues = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT repo, n FROM jobs WHERE act = 'plan')").fetchone()[0]
    now = time.time()
    if now - _pulse[0] > PULSE_TTL:
        owners = {r.split("/")[0]: r for r in REPOS}   # one installation per owner, so any repo's token
        q = f"is:pr is:merged author:app/{ME.removesuffix('[bot]')}"
        try:
            # ponytail: one search per owner, fine to a few dozen owners; record `merged` on the closed event past that
            merged = sum(api("GET", "/search/issues?per_page=1&q=" + urllib.parse.quote(f"{q} user:{o}"),
                             token(r))["total_count"] for o, r in owners.items())
        except Exception as e:
            log(f"pulse: merged search failed: {e}")
            merged = _pulse[1]   # the last good count beats none
        _pulse[:] = now, merged
    return {"repos": len(REPOS), "agents": len(list((HERE / "agents").glob("*.md"))),
            "skills": len(list((HERE / "skills").glob("*/SKILL.md"))),
            "providers": len(PROVIDERS),   # what a run can use: an owner brings a key the operator lacks
            "issues": issues, "prs": prs, "merged": _pulse[1], "reviews": steps.get("posted", 0),
            "tasks": steps.get("tick", 0), "commits": steps.get("commit", 0), "calls": steps.get("claude", 0)}


def github(repo, since):
    """C: GitHub's own numbers. A part that fails is None; it never fails the page."""
    day = time.strftime("%Y-%m-%d", time.gmtime(since))
    # merged PRs, not commits: Rookery commits as the server's git user, and a squash merge changes the sha
    merged = lambda *a: json.loads(gh(repo, "pr", "list", "--state", "merged", "--search", f"merged:>={day}",
                                      "--json", "additions,deletions", "--limit", "500", *a))
    def bot():
        every, mine = merged(), merged("--author", f"app/{ME.removesuffix('[bot]')}")
        return {"merged": len(every), "prs": len(mine), "lines": sum(p["additions"] + p["deletions"] for p in mine)}

    def facts():
        r = api("GET", f"/repos/{repo}", token(repo))
        # GitHub's open_issues_count counts open PRs too
        return {"stars": r["stargazers_count"], "forks": r["forks_count"], "open_issues": r["open_issues_count"]}
    out = {}
    for k, f in (("facts", facts), ("languages", lambda: api("GET", f"/repos/{repo}/languages", token(repo))),
                 ("bot", bot)):
        try:
            out[k] = f()
        except Exception as e:
            log(f"{repo} stats: {k} failed: {e}")
            out[k] = None
    return out


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
    for l in PHASES + [TRIGGER, REVIEW_LABEL]:
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
    """The issue's PR is closed: its worktree has nothing left to do, nor have its companions'.
    Those are found on disk, in every repo of the owner, not from today's `with`: it may have
    changed since. Each is kept while it has unpushed commits."""
    home = Ctx(repo, n, clone_dir(repo), "")
    for r in [repo] + [r for r in REPOS if r != repo and r.split("/")[0] == repo.split("/")[0]]:
        t = tag(home, None if r == repo else home)
        wt, clone, branch = wt_root(r) / t, clone_dir(r), f"feature/{t}"
        if not wt.exists():
            continue
        if unpushed(clone, branch):
            log(f"{repo}#{n} {r} {branch} has unpushed commits — keeping {wt}")
            continue
        sh("git", "worktree", "remove", "--force", str(wt), cwd=clone)
        if r == repo and (out := wt_root(repo) / f"plans-{n}").exists():   # a plan kept out of the repo goes with it
            rmtree(out)
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
    memory_file(repo).unlink(missing_ok=True)
    with closing(db()) as con, con:
        con.execute("DELETE FROM settings WHERE key = ?", (f"repo:{repo}",))
    log(f"{repo} removed {clone}, its worktrees, its notes and its settings")


# --- dispatch ----------------------------------------------------------------

def refused(c, job, what):
    """Approve and follow-up write in every companion, so they need write access there too: a
    writer on the backend must not let a backend-only person make Rookery branch in the frontend.
    Replies need only the home: planning writes nothing in a companion. The phase stays."""
    if "by" not in job:   # a dry run: the operator's own, from the box
        return False
    if no := [m.repo for m in mates(c) if not can_write(m.repo, job["by"])]:
        step(c.repo, "refused", by=job["by"], repos=no)
        comment(c, f"{what} needs write access on {', '.join(f'`{r}`' for r in no)} too: this issue can change "
                   f"{'those repos' if len(no) > 1 else 'that repo'} (`with` in `.rookery/workflow.json`). "
                   "Ask someone with write access on every one of them.")
        return True
    return False


def handle(job):
    if job["kind"] == "onboard":
        return onboard(job["repo"])
    if job["kind"] == "offboard":
        return offboard(job["repo"])
    if job["kind"] == "drop-worktree":
        return drop_worktree(job["repo"], job["n"])
    if job["kind"] == "memory":
        return remember(job)
    if job["kind"] == "wrong-label":
        return wrong_label(job)
    c = ctx(job["repo"], job["n"])
    log(f"{c.repo}#{c.n} job: {job['kind']}")
    pr, learn = job["kind"] == "pr-review", None
    try:
        if job["kind"] == "start":
            begin(c, "plan", job["title"])
            pin(c)
            plan(c, job["title"], job["body"], resume=False)
        elif pr:
            begin(c, "pr-review", job.get("title"))
            pin(c)
            review_pr(c, job)
        else:
            phase = labels_of(c.repo, c.n)
            # approving again after an error resumes at the first unticked task; after
            # changes-wanted every box is ticked, so it re-runs the checks and the review
            # prod: a quote-reply to the plan starts with "> …"; only the commenter's own words count
            if APPROVE.match(unquoted(job["body"])) and phase & {"rookery:awaiting-approval", "rookery:error",
                                                       "rookery:changes-wanted"}:
                begin(c, "implement")
                pin(c)
                if not refused(c, job, "`approve`"):
                    implement(c)
            elif phase & {"rookery:awaiting-input", "rookery:awaiting-approval"}:
                begin(c, "reply")
                pin(c)
                plan(c, None, job["body"], resume=True)
            # once the PR is open, only a mention means anything: ordinary talk costs nothing.
            # The phase is read here, when the job runs, not when the comment came in. rookery:error
            # too, so a failed follow-up can be asked again; follow_up() refuses without an open PR.
            elif (note := mention(job["body"])) is not None and phase & {"rookery:changes-wanted", "rookery:done",
                                                                         "rookery:error"}:
                begin(c, "follow-up")
                pin(c)
                if not refused(c, job, "A follow-up"):
                    follow_up(c, job, note)
        learn = (active.get(c.repo) or {}).get("job")
        finish(c.repo, "ok")
    except Exception as e:
        log(f"#{c.n} FAILED: {e}")
        # a killed claude fails with its own noise; say what actually happened
        cancelled = (active.get(c.repo) or {}).get("cancel")
        # a failure teaches too (its traps); a cancel was a person's call, not the repo's
        learn = None if cancelled else (active.get(c.repo) or {}).get("job")
        msg = f"Cancelled from the Rookery UI by @{cancelled}." if cancelled else str(e)
        step(c.repo, "error", message=msg[-2000:])
        say = (NO_KEY_PR if pr else NO_KEY).format(why=e.why, owner=e.owner, url=PUBLIC_URL) \
            if isinstance(e, NoKey) and not cancelled else f"Agent error:\n\n```\n{msg}\n```"
        try:
            if pr:
                # never set_phase() on a PR: `gh issue` cannot see it, and a phase label
                # there would let a later `approve` comment run implement() on it
                gh(c.repo, "pr", "comment", str(c.n), "--body-file", "-", input=say)
            else:
                comment(c, say)
                set_phase(c, "rookery:error")
        finally:
            finish(c.repo, "cancelled" if cancelled else "failed")
    finally:
        if pr:
            pr_cleanup(c, job)
    if learn and MEMORY:
        # on the repo's own queue: one job at a time per repo, so two writes never race
        submit({"kind": "memory", "repo": c.repo, "n": c.n, "job": learn})


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
        q.task_done()
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
    sender = (p.get("sender") or {}).get("login", "").lower()
    # a PR review spends the repo's cap: the same gate as `approve`
    may = lambda user: user != ME and can_write(repo, user)
    me = lambda u: (u or {}).get("login", "").lower() == AGENT_USER.lower()
    if event == "pull_request":
        pr, act = p["pull_request"], p["action"]
        review = lambda **k: submit({"kind": "pr-review", "repo": repo, "n": pr["number"], "title": pr["title"],
                                     "by": sender, **k})
        if act == "closed":
            m = re.fullmatch(r"feature/issue-(\d+)", pr["head"]["ref"])
            if m and pr["user"]["login"] == ME:  # our PRs only
                submit({"kind": "drop-worktree", "repo": repo, "n": int(m.group(1))})
        elif act == "labeled" and p["label"]["name"] == REVIEW_LABEL and may(sender):
            review(label=True)
        elif act == "labeled" and p["label"]["name"] == TRIGGER and sender != ME:
            submit({"kind": "wrong-label", "repo": repo, "n": pr["number"], "label": TRIGGER})
        elif act == "review_requested" and me(p.get("requested_reviewer")) and may(sender):
            review(requested=True)
        # no may(): a writer turned review_prs on, and a same-repo head means its author can push.
        # Not Rookery's own (its reviewer ran already), never a fork's (review_pr() refuses them).
        elif act == "opened" and pr["user"]["login"] != ME and (pr["head"].get("repo") or {}).get("full_name") == repo \
                and repo_settings(repo)["review_prs"]:
            review(auto=True)
        return
    n = p.get("issue", {}).get("number")
    # `labeled` only: an issue created with the label fires `opened` *and* `labeled`,
    # both before the first job sets a phase — reacting to both plans it twice.
    # Assigning AGENT_USER (on a repo that made it a collaborator) is the same as the label.
    if event == "issues" and (p["action"] == "labeled" and p["label"]["name"] == TRIGGER
                              or p["action"] == "assigned" and me(p.get("assignee"))):
        if not labels_of(repo, n) & set(PHASES):
            submit({"kind": "start", "repo": repo, "n": n,
                      "title": p["issue"]["title"], "body": p["issue"]["body"] or ""})
    elif event == "issues" and p["action"] == "labeled" and p["label"]["name"] == REVIEW_LABEL and sender != ME:
        # no may(): adding a label already needs triage, and the reply spends nothing
        submit({"kind": "wrong-label", "repo": repo, "n": n, "label": REVIEW_LABEL})
    elif event == "issue_comment" and p["action"] == "created" and "pull_request" in p["issue"]:
        # on a PR only a mention means anything: never a `continue` job, phase label or not
        user = p["comment"]["user"]["login"].lower()
        if (note := mention(p["comment"]["body"])) is not None and may(user):
            submit({"kind": "pr-review", "repo": repo, "n": n, "title": p["issue"]["title"], "by": user,
                    "note": note, "comment": p["comment"].get("id")})
    elif event == "issue_comment" and p["action"] == "created":
        user = p["comment"]["user"]["login"].lower()
        # approvals and replies alike: only people who can write to *this* repo
        if user != ME and labels_of(repo, n) & set(PHASES) and can_write(repo, user):
            submit({"kind": "continue", "repo": repo, "n": n, "body": p["comment"]["body"],
                    "comment": p["comment"].get("id"), "by": user})


def main():
    if not APP_ID or not Path(APP_KEY).exists():
        sys.exit("set APP_ID and APP_KEY (path to the App's private key .pem) in .env")
    sync()

    if len(sys.argv) > 2:
        # dry run, no webhook: `agent.py owner/repo 12 [comment...]`; a comment with @AGENT_USER
        # reviews 12 if it is a PR, as that comment on a PR would (minus the 👀: there is no comment
        # id), and on an issue is a follow-up
        repo, n = sys.argv[1], int(sys.argv[2])
        if (note := mention(" ".join(sys.argv[3:]))) is not None \
                and gh(repo, "pr", "view", str(n), "--json", "number", check=False):
            handle({"kind": "pr-review", "repo": repo, "n": n, "note": note})
        elif len(sys.argv) > 3:
            handle({"kind": "continue", "repo": repo, "n": n, "body": " ".join(sys.argv[3:])})
        else:
            iss = json.loads(gh(repo, "issue", "view", str(n), "--json", "title,body"))
            handle({"kind": "start", "repo": repo, "n": n,
                    "title": iss["title"], "body": iss["body"] or ""})
        if repo in queues:   # the memory job handle() queued: let it finish before exiting
            queues[repo].join()
        sys.exit()

    # a job still "running" in the log died with the previous process
    with closing(db()) as con, con:
        dead = con.execute("SELECT repo, n, act FROM jobs WHERE status = 'running'").fetchall()
        con.execute("UPDATE jobs SET status = 'interrupted', ended = ? WHERE status = 'running'", (time.time(),))
    # its phase label outlived it: `rookery:implementing` takes no `approve`, so the issue would sit
    # there for good. rookery:error does, and resumes at the first unticked task
    for repo, n, act in dead:
        if act in ("implement", "follow-up") and repo in REPOS:
            try:
                c = ctx(repo, n)
                comment(c, INTERRUPTED)
                set_phase(c, "rookery:error")
            except Exception as e:
                log(f"{repo}#{n} interrupted, could not say so: {e}")
    shutil.rmtree(WORKSPACE / "plugins", ignore_errors=True)   # and so did its per-job plugin dirs
    # a key taken out of .env after the admin page stored its provider: those jobs fail at the call
    with closing(db()) as con:
        for k, v in con.execute("SELECT key, value FROM settings WHERE key LIKE 'provider%'"):
            if not ready(v):
                log(f"{k} is {v}, which cannot run ({PROVIDERS.get(v, {}).get('key', 'unknown provider')}): "
                    "its jobs will fail until the key is back in .env or the admin page changes it")
            elif v == "claude" and ":" in k and k.split(":", 1)[1].lower() not in OWN:
                log(f"{k} is claude, but that owner is not in OWN: its jobs fail until the owner selects a key "
                    "or the admin page changes it")
        if not KEY_SECRET and (n := con.execute("SELECT COUNT(*) FROM keys").fetchone()[0]):
            log(f"KEY_SECRET is not set: {n} owner keys cannot be read, and their owners' jobs will fail")
    import web
    web.serve()


if __name__ == "__main__":
    # run as the `agent` module that web.py imports, not as a second copy named __main__
    import agent
    agent.main()
