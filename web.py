"""Rookery's HTTP side: the webhook, GitHub login, the control-plane API, the UI files.

One ThreadingHTTPServer. POST / is the webhook (HMAC-checked, as before); /auth/*
is GitHub OAuth through the App; /api/* is JSON for ui/portal.html, and needs a
session. `route()` is the whole app as a function of (method, url, headers, body),
so test_agent.py drives it without a socket.

Who sees what: at login, the user's GitHub token lists the repos of *their*
installations of the App; the session keeps that list and the token is dropped.
A repo is visible while it is on that list and in agent.REPOS.
The list is fixed for the session; signing in again refreshes it (GitHub skips the
consent screen once authorised). The App's Setup URL points at /auth/login, so installing
it or changing its repos lands back here signed in with the new list.
"""
import collections
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent
from agent import log

# The App's OAuth client. Popped like GH_TOKEN: the claude subprocess inherits os.environ.
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.pop("CLIENT_SECRET", "")
# where the browser reaches us; GitHub redirects to PUBLIC_URL/auth/callback
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{agent.PORT}").rstrip("/")
SECURE = PUBLIC_URL.startswith("https://")  # the Secure cookie flag; off only over plain http
TTL = 8 * 3600  # a session, and GitHub's own lifetime for a user token
UI = agent.HERE / "ui"
DOCS = agent.HERE / "docs"  # product docs as markdown, rendered by ui/docs.html; flat like ui/
MAX_BODY = 26 * 1024 * 1024  # GitHub caps a webhook payload at 25 MB
# GitHub logins that may open /admin.html's API; anyone else gets a 404 there, signed in or not
ADMINS = {x.strip().lower() for x in os.environ.get("ADMINS", "").split(",") if x.strip()}
HEADERS = [("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
           ("Referrer-Policy", "same-origin")]


# --- responses ---------------------------------------------------------------

def js(code, obj):
    return code, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], json.dumps(obj).encode()


def text(code, msg):
    return code, [("Content-Type", "text/plain; charset=utf-8")], msg.encode()


def cookie(name, value, age):
    return ("Set-Cookie", f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax"
                          + ("; Secure" if SECURE else ""))


# --- sessions ----------------------------------------------------------------
# Server-side rows, keyed by sha256 of the cookie: rookery.db is readable by the
# agent (same user, same workspace), so it must hold nothing a browser could replay.

def digest(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def new_session(login, repos):
    """-> the cookie value. Also sweeps expired rows."""
    sid, now = secrets.token_urlsafe(32), time.time()
    with closing(agent.db()) as con, con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (digest(sid), login, json.dumps(sorted(repos)), secrets.token_urlsafe(24), now + TTL))
    return sid


def session(jar):
    sid = jar.get("rookery_session")
    if not sid:
        return None
    with closing(agent.db()) as con:
        row = con.execute("SELECT login, repos, csrf FROM sessions WHERE id = ? AND expires > ?",
                          (digest(sid.value), time.time())).fetchone()
    return row and {"sid": sid.value, "login": row[0], "repos": json.loads(row[1]), "csrf": row[2]}


def visible(s):
    return [r for r in s["repos"] if r in agent.REPOS]


def admin(s):
    return bool(s) and s["login"].lower() in ADMINS


# --- GitHub login ------------------------------------------------------------

def login():
    state = secrets.token_urlsafe(24)
    url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": CLIENT_ID, "redirect_uri": PUBLIC_URL + "/auth/callback", "state": state})
    return 302, [("Location", url), cookie("rookery_state", state, 600)], b""


def exchange(code):
    """OAuth code -> the user's token. Used by callback() and then dropped."""
    req = urllib.request.Request("https://github.com/login/oauth/access_token", method="POST",
                                 headers={"Accept": "application/json"}, data=urllib.parse.urlencode(
            {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code,
             "redirect_uri": PUBLIC_URL + "/auth/callback"}).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if "access_token" not in body:
        raise RuntimeError(f"GitHub refused the code: {body.get('error_description') or body}")
    return body["access_token"]


def callback(q, jar):
    state, code, want = q.get("state", [""])[0], q.get("code", [""])[0], jar.get("rookery_state")
    if not code or not want or not hmac.compare_digest(state, want.value):
        return text(400, "Sign-in failed: the login state did not match. Start again at /auth/login.")
    tok = exchange(code)
    who = agent.api("GET", "/user", tok)["login"]
    repos = {r["full_name"]
             for i in agent.api_all("/user/installations", tok, "installations")
             for r in agent.api_all(f"/user/installations/{i['id']}/repositories", tok, "repositories")}
    log(f"{who} signed in ({len(repos)} repos in their installations)")
    with closing(agent.db()) as con, con:
        con.execute("INSERT INTO users VALUES (?, ?, ?, 1) ON CONFLICT(login) DO UPDATE "
                    "SET last = excluded.last, logins = logins + 1", (who, time.time(), time.time()))
    if old := jar.get("rookery_session"):   # a refresh replaces the session, it doesn't add one
        with closing(agent.db()) as con, con:
            con.execute("DELETE FROM sessions WHERE id = ?", (digest(old.value),))
    return 302, [("Location", "/portal.html"), cookie("rookery_session", new_session(who, repos), TTL),
                 cookie("rookery_state", "", 0)], b""


# --- API ---------------------------------------------------------------------

def repos(s):
    out = []
    for r in visible(s):
        runs, usd = agent.usage(r)
        out.append({"repo": r, "base": agent.REPOS[r], "runs": runs, "usd": round(usd, 4),
                    "run_cap": agent.RUN_CAP, "usd_cap": agent.USD_CAP})
    return {"repos": out, "paused": agent.PAUSED.exists()}


JOB = """SELECT id, repo, n, act, COALESCE(title, (SELECT title FROM jobs t WHERE t.repo = j.repo
         AND t.n = j.n AND t.title IS NOT NULL ORDER BY t.id DESC LIMIT 1)), started, ended, status
         FROM jobs j"""


def summary(row, steps):
    """A job plus what its steps say: the task it reached, its cost, its last error."""
    run = dict(zip(("id", "repo", "n", "act", "title", "started", "ended", "status"), row))
    run.update(task=None, usd=0.0, error=None)
    for what, d in steps:
        if what == "task":
            run["task"] = d
        elif what == "claude":
            run["usd"] = round(run["usd"] + d.get("usd", 0), 4)
        elif what == "error":
            run["error"] = d["message"]
    return run


def runs(s, repo=None):
    mine = [r for r in visible(s) if repo in (None, r)]
    if not mine:
        return []
    with closing(agent.db()) as con:
        rows = con.execute(f"{JOB} WHERE repo IN ({','.join('?' * len(mine))}) ORDER BY id DESC LIMIT 100",
                           mine).fetchall()
        ids = [r[0] for r in rows]
        steps = con.execute(f"SELECT job, what, detail FROM steps WHERE job IN ({','.join('?' * len(ids))}) "
                            "ORDER BY rowid", ids).fetchall() if ids else []
    by = {}
    for job, what, d in steps:
        by.setdefault(job, []).append((what, json.loads(d)))
    return [summary(r, by.get(r[0], [])) for r in rows]


def run(s, job):
    with closing(agent.db()) as con:
        row = con.execute(f"{JOB} WHERE id = ?", (job,)).fetchone()
        steps = [(at, what, json.loads(d)) for at, what, d in
                 con.execute("SELECT at, what, detail FROM steps WHERE job = ? ORDER BY rowid", (job,))]
    if not row or row[1] not in visible(s):
        return None   # someone else's run is indistinguishable from no run
    return {**summary(row, [(w, d) for _, w, d in steps]),
            "steps": [{"at": at, "what": w, **d} for at, w, d in steps]}


def workflow(s, repo):
    """A visible repo's effective workflow, from agent.steer(): the job's own validator, on a
    freshly fetched base, into a throwaway plugin dir. None for a repo the session can't see:
    its custom personas and instructions are that tenant's."""
    if repo not in visible(s):
        return None
    base, path = agent.REPOS[repo], agent.clone_dir(repo)
    out = {"repo": repo, "base": base, "error": None, "agent_user": agent.AGENT_USER}
    if not base or not path.exists():
        return {**out, "error": "Rookery is still cloning this repo. Try again in a minute."}
    with tempfile.TemporaryDirectory() as plug:
        try:
            got = agent.steer(repo, path, base, Path(plug))
        except Exception as e:   # whatever would fail the next job at start
            got = {"error": str(e)}
    # the editor's starting point, even (above all) when the config does not validate
    out["config"] = {"dir": agent.config_dir(repo), "files": agent.config_files(repo)}
    if "error" in got:
        return {**out, "error": got["error"]}
    model = lambda a: got["agents"][a].get("model") if a else "sonnet"   # claude()'s choice, same rule
    wf, owf = got["workflow"], got["org_workflow"] or {**agent.WORKFLOW, "after": []}
    # which layer set it: the repo's own workflow.json, its owner's .rookery, or the built-in
    origin = lambda mine, owners, builtin: "repo" if mine != owners else "org" if owners != builtin else "built-in"
    return {**out, "sha": got["sha"], "claude_md": len(got["claude_md"].encode()), "org": got["org"],
            "slots": {k: {**wf[k], "model": model(wf[k]["agent"]), "from": origin(wf[k], owf[k], v)}
                      for k, v in agent.WORKFLOW.items()},
            "after": [{**v, "model": model(v["agent"]), "from": origin(wf["after"], owf["after"], [])}
                      for v in wf["after"]],
            "agents": {n: {"description": m.get("description", ""), "source": got["agent_sources"][n]}
                       for n, m in got["agents"].items()},
            "skills": {n: {"source": got["sources"][n], "description": d} for n, d in got["skills"].items()}}


def memory(s, repo):
    """A visible repo's notes, their size, and the run that last wrote them. None for a repo the
    session can't see: the notes are that tenant's."""
    if repo not in visible(s):
        return None
    f = agent.memory_file(repo)
    notes = f.read_text(encoding="utf-8") if f.exists() else ""
    with closing(agent.db()) as con:
        last = con.execute("SELECT j.id, j.ended FROM jobs j JOIN steps s ON s.job = j.id WHERE j.repo = ? "
                           "AND s.what = 'remembered' ORDER BY j.id DESC LIMIT 1", (repo,)).fetchone()
    return {"repo": repo, "enabled": agent.MEMORY, "notes": notes, "chars": len(notes), "cap": agent.MEMORY_CAP,
            "run": last and notes and {"id": last[0], "at": last[1]} or None}


def catalog():
    """skills/: each SKILL.md's description, plus slot and source from the README's table,
    which is where the pinned upstream commit is written down. "same" repeats the row above."""
    rows, src = {}, ""
    for m in re.finditer(r"^\| `([\w-]+)` \| (\w+) \| (.+?) \|$", (agent.HERE / "skills" / "README.md")
                         .read_text(encoding="utf-8"), re.M):
        src = src if m.group(3) == "same" else m.group(3)
        link = re.search(r"\[([^]]+)]\((https://[^)]+)\)", src)
        rows[m.group(1)] = {"slot": m.group(2), "source": link.group(1) if link else src,
                            "url": link and link.group(2)}
    return [{"name": p.parent.name, **rows.get(p.parent.name, {"slot": None, "source": None, "url": None}),
             "description": agent.frontmatter(p, agent.SKILL_KEYS, p.parent.name).get("description", "")}
            for p in sorted((agent.HERE / "skills").glob("*/SKILL.md"))]


def admin_view():
    """Everything /admin.html shows: providers, the default, per-owner overrides, who signed in."""
    now = time.time()
    with closing(agent.db()) as con:
        rows = dict(con.execute("SELECT key, value FROM settings WHERE key LIKE 'provider%'"))
        users = con.execute("SELECT login, first, last, logins FROM users ORDER BY last DESC").fetchall()
        # each login's newest session, swept 8h after it ends: its repo list and whether it is live
        last = {l: (e, json.loads(r)) for l, r, e in con.execute("SELECT login, repos, expires FROM sessions ORDER BY expires")}
    overrides = {k.split(":", 1)[1]: v for k, v in rows.items() if ":" in k}
    count = collections.Counter(r.split("/")[0] for r in agent.REPOS)
    return {"default": rows.get("provider", agent.PROVIDER),
            "providers": [{"name": n, "ready": agent.ready(n), **({"key": p["key"]} if p else {})}
                          for n, p in agent.PROVIDERS.items()],
            "overrides": overrides,
            "owners": [{"owner": o, "repos": count[o]} for o in sorted(set(count) | set(overrides))],
            "users": [{"login": l, "first": f, "last": t, "logins": n, "live": l in last and last[l][0] > now,
                       "repos": len([r for r in last[l][1] if r in agent.REPOS]) if l in last else None}
                      for l, f, t, n in users]}


def set_provider(owner, name, by):
    """owner None = the default (never deleted); name None = drop that owner's override."""
    key = f"provider:{owner}" if owner else "provider"
    with closing(agent.db()) as con, con:
        if name is None:
            con.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, name))
    log(f"provider for {owner or 'default'} -> {name or 'default'} by {by}")


# --- routing -----------------------------------------------------------------

def route(method, url, headers, body=b""):
    """-> (status, [(header, value)], body). The webhook (POST /) is not here: see Handler."""
    u = urllib.parse.urlsplit(url)
    path, jar = u.path, SimpleCookie(headers.get("Cookie", ""))
    if method == "GET" and path == "/auth/login":
        return login()
    if method == "GET" and path == "/auth/callback":
        return callback(urllib.parse.parse_qs(u.query), jar)

    if path.startswith("/api/") or path == "/auth/logout":
        s = session(jar)
        if path.startswith("/api/admin") and not admin(s):
            return js(404, {"error": "no such endpoint"})   # not even "sign in": the page is unlinked
        if not s:
            return js(401, {"error": "not signed in"})
        if method in ("POST", "DELETE") and not hmac.compare_digest(headers.get("X-CSRF-Token", ""), s["csrf"]):
            return js(403, {"error": "missing or wrong X-CSRF-Token"})
        if method == "POST" and path == "/auth/logout":
            with closing(agent.db()) as con, con:
                con.execute("DELETE FROM sessions WHERE id = ?", (digest(s["sid"]),))
            return 204, [cookie("rookery_session", "", 0)], b""
        if method == "GET" and path == "/api/me":
            return js(200, {"login": s["login"], "csrf": s["csrf"], "app": agent.ME.removesuffix("[bot]")})
        if method == "GET" and path == "/api/repos":
            return js(200, repos(s))
        if method == "GET" and (m := re.fullmatch(r"/api/repos/([\w.-]+/[\w.-]+)/workflow", path)):
            w = workflow(s, m.group(1))
            return js(200, w) if w else js(404, {"error": "no such repo"})
        if method == "POST" and (m := re.fullmatch(r"/api/repos/([\w.-]+/[\w.-]+)/config", path)):
            repo = m.group(1)
            if repo not in visible(s):
                return js(404, {"error": "no such repo"})
            # what steers a repo is changed only by a PR its writers merge; proposing one needs write too
            if not agent.can_write(repo, s["login"]):
                return js(403, {"error": f"changing the config needs write access to {repo}"})
            try:
                files = json.loads(body)["files"]
                if not isinstance(files, dict) or not all(isinstance(t, (str, type(None))) for t in files.values()):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                return js(400, {"error": 'expected {"files": {"<path>": "<text>" or null}}'})
            try:
                return js(201, {"url": agent.propose(repo, files, s["login"])})
            except Exception as e:   # the check the next job would fail, or git/gh saying no
                return js(422, {"error": str(e)})
        if method in ("GET", "DELETE") and (m := re.fullmatch(r"/api/repos/([\w.-]+/[\w.-]+)/memory", path)):
            mem = memory(s, m.group(1))
            if not mem:
                return js(404, {"error": "no such repo"})
            if method == "GET":
                return js(200, mem)
            # the notes steer every run on the repo: clearing them needs what cancel needs
            if not agent.can_write(mem["repo"], s["login"]):
                return js(403, {"error": f"clearing the notes needs write access to {mem['repo']}"})
            agent.memory_file(mem["repo"]).unlink(missing_ok=True)
            log(f"{mem['repo']} notes cleared by {s['login']}")
            return 204, [], b""
        if method == "GET" and path == "/api/admin":
            return js(200, admin_view())
        if method == "POST" and path == "/api/admin/provider":
            try:
                d = json.loads(body)
                owner, name = d["owner"], d["provider"]
                # the default is never dropped: provider null needs an owner
                if not (owner is None or isinstance(owner, str) and re.fullmatch(r"[\w.-]+", owner)) \
                        or not (name is None and owner or isinstance(name, str)):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                return js(400, {"error": 'expected {"owner": null or "<owner>", "provider": "<name>", '
                                         'or null to drop an owner\'s override}'})
            if name is not None and not agent.ready(name):
                return js(422, {"error": f"{name} cannot run: unknown, or its key is not in .env"})
            set_provider(owner, name, s["login"])
            return js(200, admin_view())
        if method == "GET" and path == "/api/skills":
            return js(200, {"skills": catalog()})
        if method == "GET" and path == "/api/runs":
            return js(200, {"runs": runs(s, urllib.parse.parse_qs(u.query).get("repo", [None])[0])})
        if method == "GET" and (m := re.fullmatch(r"/api/runs/(\d+)", path)):
            r = run(s, int(m.group(1)))
            return js(200, r) if r else js(404, {"error": "no such run"})
        if method == "POST" and (m := re.fullmatch(r"/api/runs/(\d+)/cancel", path)):
            r = run(s, int(m.group(1)))
            if not r:
                return js(404, {"error": "no such run"})
            # seeing a repo is not steering it: cancel needs what `approve` needs
            if not agent.can_write(r["repo"], s["login"]):
                return js(403, {"error": f"cancelling needs write access to {r['repo']}"})
            if not agent.cancel(r["repo"], r["id"], s["login"]):
                return js(409, {"error": "that run is not running"})
            return js(202, {"cancelled": r["id"]})
        return js(404, {"error": "no such endpoint"})

    if method == "GET" and (m := re.fullmatch(r"/docs/([\w-]+\.md)", path)) and (DOCS / m.group(1)).is_file():
        return 200, [("Content-Type", "text/markdown; charset=utf-8"), ("Cache-Control", "no-cache")], \
            (DOCS / m.group(1)).read_bytes()
    if method == "GET":
        name = "index.html" if path == "/" else path[1:]
        # a flat whitelist of ui/ files: no subdirectories, so no way out of ui/
        if re.fullmatch(r"[\w-]+\.(html|css|png|js)", name) and (UI / name).is_file():
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return 200, [("Content-Type", kind), ("Cache-Control", "no-cache")], (UI / name).read_bytes()
    return text(404, "not found")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply(self.safe("GET"))

    def do_DELETE(self):
        self.reply(self.safe("DELETE"))

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        if size > MAX_BODY:
            return self.reply(text(413, "too large"))
        raw = self.rfile.read(size)
        if urllib.parse.urlsplit(self.path).path != "/":
            return self.reply(self.safe("POST", raw))
        sig = "sha256=" + hmac.new(agent.SECRET, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, self.headers.get("X-Hub-Signature-256", "")):
            return self.reply((401, [], b""))
        self.reply((204, [], b""))
        if not agent.fresh(self.headers.get("X-GitHub-Delivery")):
            log(f"duplicate delivery {self.headers['X-GitHub-Delivery']} — ignored")
            return
        try:
            agent.enqueue(self.headers.get("X-GitHub-Event", ""), json.loads(raw))
        except Exception as e:
            print("dispatch error:", e, file=sys.stderr)

    def safe(self, method, body=b""):
        try:
            return route(method, self.path, self.headers, body)
        except Exception as e:
            log(f"{method} {urllib.parse.urlsplit(self.path).path} failed: {e}")
            return text(500, "internal error")

    def reply(self, res):
        code, headers, body = res
        self.send_response(code)
        for k, v in HEADERS + headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve():
    # once: people signed in before the users table existed, from their live (or unswept) sessions
    with closing(agent.db()) as con, con:
        con.execute("INSERT OR IGNORE INTO users SELECT login, MIN(expires) - ?, MAX(expires) - ?, COUNT(*) "
                    "FROM sessions GROUP BY login", (TTL, TTL))
    if not CLIENT_ID or not CLIENT_SECRET:
        log("CLIENT_ID/CLIENT_SECRET unset — the webhook works, sign-in to the UI will not")
    srv = ThreadingHTTPServer((agent.HOST, agent.PORT), Handler)
    srv.daemon_threads = True
    # log(), not print(): stdout is block-buffered under systemd, stderr is not,
    # so a print() banner never reaches the journal.
    log(f"listening on {agent.HOST or '*'}:{agent.PORT} for {', '.join(agent.REPOS) or 'no repos yet'} "
        f"(bot user: {agent.ME}, UI at {PUBLIC_URL}/)")
    srv.serve_forever()
