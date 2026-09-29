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
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
MAX_BODY = 26 * 1024 * 1024  # GitHub caps a webhook payload at 25 MB
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
        if not s:
            return js(401, {"error": "not signed in"})
        if method == "POST" and not hmac.compare_digest(headers.get("X-CSRF-Token", ""), s["csrf"]):
            return js(403, {"error": "missing or wrong X-CSRF-Token"})
        if method == "POST" and path == "/auth/logout":
            with closing(agent.db()) as con, con:
                con.execute("DELETE FROM sessions WHERE id = ?", (digest(s["sid"]),))
            return 204, [cookie("rookery_session", "", 0)], b""
        if method == "GET" and path == "/api/me":
            return js(200, {"login": s["login"], "csrf": s["csrf"], "app": agent.ME.removesuffix("[bot]")})
        if method == "GET" and path == "/api/repos":
            return js(200, repos(s))
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
    if not CLIENT_ID or not CLIENT_SECRET:
        log("CLIENT_ID/CLIENT_SECRET unset — the webhook works, sign-in to the UI will not")
    srv = ThreadingHTTPServer((agent.HOST, agent.PORT), Handler)
    srv.daemon_threads = True
    # log(), not print(): stdout is block-buffered under systemd, stderr is not,
    # so a print() banner never reaches the journal.
    log(f"listening on {agent.HOST or '*'}:{agent.PORT} for {', '.join(agent.REPOS) or 'no repos yet'} "
        f"(bot user: {agent.ME}, UI at {PUBLIC_URL}/)")
    srv.serve_forever()
