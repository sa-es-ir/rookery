# Deploying Rookery to a Linux server

Ubuntu 24.04 LTS. The orchestrator is stdlib-only Python; everything hard is in the two
CLIs it shells out to (`gh`, `claude`). Written against a real deploy — the gotchas at
the bottom each cost an hour, read them before you start.

Replace `OWNER/REPO` and `SERVER_IP` throughout. Each block says which machine and which
user it runs as.

## 0. Before you start

- A **GitHub App** (step 5): permissions Contents RW, Issues RW, Pull requests RW,
  Metadata R; events Issues + Issue comment; installed on the target repo. Its App ID,
  private key `.pem` and webhook secret are all the GitHub credentials the box gets —
  no PAT, no `gh auth login`.
- The **target** repo the agent will work on. Never this one.
- **Check the CPU before you pick the server — see [AVX2](#avx2-the-two-hour-one) below.**
  A VPS with a generic `qemu64` CPU model (`/proc/cpuinfo` says `Common KVM processor`)
  cannot run current Claude Code at all, and is stuck on the 4.6 models. On a candidate
  box, before anything else:

  ```bash
  grep -m1 flags /proc/cpuinfo | tr ' ' '\n' | grep -cE '^(avx2|sse4_2)$'   # want 2
  ```

  `0` means ask the provider for `host-passthrough` or pick another server now — it is
  a hypervisor setting, not something to fix after the deploy.

## 1. Packages — server, root

```bash
ssh root@SERVER_IP
apt update && apt install -y gh tmux
```

Python 3.12, git and curl ship with 24.04. `gh` from universe (2.45) is new enough for
every call `agent.py` makes.

Node 22+ is required by the `claude` npm package — 24.04's `npm` pulls Node 18, which
fails in confusing ways:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
node --version    # v22.x
```

## 2. Service user — server, root

```bash
adduser --disabled-password --gecos "" rookery
mkdir -p /opt/rookery /home/rookery/.claude
```

Run as a normal user, not root: `claude` refuses `--permission-mode bypassPermissions`
under uid 0, which is what `agent.py` uses.

## 3. Copy the files — your machine

```bash
scp agent.py web.py test_agent.py root@SERVER_IP:/opt/rookery/
scp -r agents root@SERVER_IP:/opt/rookery/
ssh root@SERVER_IP mkdir -p /opt/rookery/ui
scp ui/index.html ui/portal.html ui/styles.css ui/rookery.png root@SERVER_IP:/opt/rookery/ui/
scp .env root@SERVER_IP:/opt/rookery/
scp path/to/your-app.private-key.pem root@SERVER_IP:/opt/rookery/app.pem
scp ~/.claude/.credentials.json root@SERVER_IP:/home/rookery/.claude/.credentials.json
```

Skipped on purpose: `work/` (clones are made on the server — step 8), `ui/index2.html` and
`ui/tui/` (the server only serves files directly in `ui/`), `.git`.

The credentials copy saves you the interactive login for first light only — the token it
carries rotates, and your laptop refreshing it revokes the server's copy mid-run (see
[the gotcha](#oauth-access-token-has-been-revoked)). Run `claude` → `/login` on the
server once it works, ideally on an account of its own rather than sharing your seat.

## 4. Hand ownership over — server, root

```bash
chown -R rookery:rookery /opt/rookery /home/rookery
chmod 600 /home/rookery/.claude/.credentials.json /opt/rookery/app.pem /opt/rookery/.env
```

`chown` all of `/home/rookery`, not just the two directories — anything root creates
there later (`repos/`, `.ssh/`) otherwise fails with a confusing `Permission denied` on
clone.

Everything from here runs as `rookery`.

## 5. GitHub auth — the App, not a login

GitHub → Settings → Developer settings → **GitHub Apps → New**: webhook URL
`https://HOSTNAME/` (nginx, step 10), content type JSON, a fresh secret; the permissions
and events from step 0. **Callback URL** `https://HOSTNAME/auth/callback` for portal
sign-in (add `http://127.0.0.1:8787/auth/callback` too to test the portal locally).
**Setup URL** (under *Post installation*) `https://HOSTNAME/auth/login` with *Redirect on
update* ticked, and *Request user authorization (OAuth) during installation* **unticked** —
it would send GitHub to the callback without our `state` and the sign-in would fail. After
an install or a repo change the user lands back in the portal with the new list.
Generate a private key and a client secret, then **Install** it on the target repo.

`/opt/rookery/.env` gets these values (the secret replaces any old per-repo one):

```bash
APP_ID=1234567
APP_KEY=/opt/rookery/app.pem
WEBHOOK_SECRET=<the App's webhook secret>
CLIENT_ID=<the App's client id>
CLIENT_SECRET=<the App's client secret>
PUBLIC_URL=https://HOSTNAME
```

`PUBLIC_URL` being `https://` sets the `Secure` flag on the session cookie.

Per job, `agent.py` signs a JWT with the key (`openssl`), exchanges it for a one-hour
installation token, and hands that to `gh` and `git` through `env=` — never
`os.environ`, so the `claude` subprocess cannot read it. Comments, pushes and PRs all
come from `<slug>[bot]`. `ME` in `agent.py` is that login: if your slug is not
`rookery-gha`, change it there.

Do **not** run `gh auth login` on the box. Left logged in, `~/.config/gh/hosts.yml` is a
long-lived personal token the agent could read; `gh auth status` should say
"not logged in". Who may approve is not configured here: anyone with write, maintain or
admin on the issue's repo can, looked up per comment.

Known ceiling: the agent runs as the same user that can read `app.pem`, and the key
outranks any token minted from it. The deny rules stop `gh` and `git push`, not
`openssl` + `curl`. Closing that needs the sandbox, not this file.

## 6. Git identity — server, rookery

```bash
git config --global user.name  "rookery-agent"
git config --global user.email "you@example.com"
```

Not optional. The orchestrator commits after every task and git refuses without an
identity — you would only find out fifteen minutes into a run.

## 7. Claude CLI — server, rookery

```bash
npm install -g --prefix ~/.local @anthropic-ai/claude-code@2.1.110
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
which claude && claude --version
```

**The version pin is load-bearing on CPUs without AVX2** — see the gotcha below. On a
modern CPU, drop the pin; keep `--prefix ~/.local` either way, so no root is needed.

Gate before going further:

```bash
timeout 120 claude -p 'say ok' --output-format json </dev/null
```

JSON with a `"result"` field means the whole chain works — this is the exact call
`agent.py` makes. If it asks for auth, run plain `claude` once, finish the prompts,
`/exit`. That writes `~/.claude.json`, the onboarding flag the credentials file does not
cover.

## 8. Target repos — GitHub, not the server

There is no repo list to write. At startup `sync()` asks the App for every repository it
is installed on, clones each one over https into `$WORKSPACE/repos/<owner>_<repo>`
(`WORKSPACE` defaults to `/opt/rookery/work`), creates the `rookery:*` labels, and takes
the base branch from the repo's `default_branch`. After that, `installation` and
`installation_repositories` events keep the list current — no restart:

- **Add a repo** — App settings → *Install App* → the gear on your account →
  *Repository access*. With *Only select repositories*, pick it there; with
  *All repositories*, a new repo is added the moment it is created. The journal shows
  `OWNER/REPO live, base main`.
- **Remove a repo** — deselect it (or uninstall the App). Rookery stops accepting its
  events at once, then deletes the clone and every worktree — unless a worktree branch has
  commits that are not on GitHub, in which case it logs `NOT removed — unpushed commits`
  and keeps everything. Push or delete that branch by hand, then remove the directories.
- **A PR closes** (merged or not) — the issue's worktree is removed, with the same
  unpushed-commits check.

A repo with no commits yet has no base to branch from: give it a first commit (a README)
before triggering an issue there.

For one-off `gh` calls as the App (there is no `gh` login on the box):

```bash
tok() { (cd /opt/rookery && python3 -c "import agent; print(agent.token('$1'))"); }
GH_TOKEN=$(tok OWNER/REPO) gh issue list --repo OWNER/REPO
```

Check `/opt/rookery/.env`: note `WEBHOOK_SECRET` for step 10. Optional: `RUN_CAP` (default
10 `claude` calls) and `USD_CAP` (default 20) per repo per UTC day. Usage is in
`$WORKSPACE/rookery.db`:

```bash
python3 -c "import agent; print(agent.usage('OWNER/REPO'))"   # (runs, usd) today, from /opt/rookery
touch /opt/rookery/PAUSED     # stop every new claude call, all repos; rm it to resume
```

## 9. Test before any webhook — server, rookery

```bash
cd /opt/rookery
python3 test_agent.py                    # no network, pure asserts
python3 agent.py OWNER/REPO 12           # plan issue 12 -> posts a comment, sets rookery:awaiting-approval
python3 agent.py OWNER/REPO 12 approve   # task loop -> push -> draft PR
```

Same `handle()` as the webhook; only the transport differs. If these pass, the agent is
deployed and only delivery is left.

## 10. Listen — server, root

`agent.py` binds `127.0.0.1:8787` by default and stays there: nginx on port 80 fronts it,
and Cloudflare fronts nginx. Leave `HOST` unset — `HOST=` (empty) binds all interfaces
and exposes the plain `http.server` on 8787.

Run it under systemd from the start — the unit file and `systemctl` need **root**, the
process itself runs as `rookery` via `User=`:

```bash
tee /etc/systemd/system/rookery.service >/dev/null <<'EOF'
[Service]
User=rookery
WorkingDirectory=/opt/rookery
ExecStart=/usr/bin/python3 -u /opt/rookery/agent.py
Restart=always
Environment=PATH=/home/rookery/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now rookery
journalctl -u rookery -n 20 --no-pager     # want: listening on 127.0.0.1:8787 for OWNER/REPO ... UI at https://HOSTNAME/
```

Every later change to `agent.py` or `.env` needs both halves — copy it up, then restart,
as **root** (`rookery` is not a sudoer):

```bash
scp agent.py web.py root@SERVER_IP:/opt/rookery/                 # your machine
chown rookery:rookery /opt/rookery/agent.py /opt/rookery/web.py  # server, root
systemctl restart rookery                                        # server, root
```

Editing the file alone changes nothing — the running process keeps the old code, and
module-level values like `ME` are read once at startup. (`REPOS` is rebuilt from the
App at every start and kept current by events after that.)

Kill any foreground `python3 agent.py` or `tmux` session first, or the unit dies on
`Address already in use`. `systemctl is-active rookery` and a `pid=` in
`ss -lntp | grep 8787` matching `systemctl show -p MainPID rookery` tell the unit apart
from a leftover manual run — a port probe cannot, both answer identically.

`User=` makes systemd set `HOME=/home/rookery` itself, so `claude` finds
`~/.claude/.credentials.json`. The explicit `PATH` is the only environment you must
supply: systemd does not read `.bashrc`, and without `~/.local/bin` first it would find
the system `claude`.

### nginx on port 80 — server, root

Cloudflare DNS: an A record `HOSTNAME` → `SERVER_IP`, **Proxied**, SSL/TLS mode
**Flexible** (browser → Cloudflare is HTTPS, Cloudflare → server is plain http on 80).

```bash
apt install -y nginx
rm /etc/nginx/sites-enabled/default
tee /etc/nginx/sites-available/rookery >/dev/null <<'EOF'
# Cloudflare (SSL mode Flexible) -> here over http. No https redirect: it would loop.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 26m;           # GitHub webhooks go up to 25 MB; nginx's default is 1 MB

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF
ln -s /etc/nginx/sites-available/rookery /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

**Never add an https redirect on port 80 while SSL mode is Flexible** — Cloudflare sends
every request to the origin as http, so it loops. Flexible leaves the Cloudflare → server
hop in cleartext (cookies, the OAuth code, webhook payloads); moving to Full (strict) with
a Cloudflare Origin Certificate is in [ai-plans/phase5-deploy.md](ai-plans/phase5-deploy.md).

### Prove it is reachable before touching GitHub

From your own machine. In PowerShell use `curl.exe` — bare `curl` is `Invoke-WebRequest`:

```bash
curl.exe -s -o NUL -w "%{http_code}\n" https://HOSTNAME/                    # 200, landing page
curl.exe -s -o NUL -w "%{http_code}\n" https://HOSTNAME/portal.html         # 200
curl.exe -s -o NUL -w "%{http_code}\n" -X POST -d '{}' https://HOSTNAME/    # 401 = HMAC rejected it: correct
curl.exe -s -m 5 http://SERVER_IP:8787/                                     # must fail: 8787 is loopback only
```

| Result | Meaning |
|---|---|
| `401` on the POST | **Working.** The HMAC rejected an unsigned body — exactly right. |
| `502` | nginx is up, the app is not (down, or still in its ~20 s startup `sync()`). |
| Cloudflare `521` / `522` | Cloudflare cannot reach port 80: nginx is down, or something else holds 80. |

## 11. Deliver — GitHub

Nothing per repo: the App's own webhook (step 5) delivers every installed repo's events.
**Delete any old per-repo webhook** pointing at the same URL — its secret no longer
matches, so it only adds red `401`s.

App settings → **Advanced → Recent Deliveries** should show green `204`s (a `ping`
redelivered after the restart is a quick check); a red `401` means the App's secret does
not match `WEBHOOK_SECRET` in `.env`; a `403` with a Cloudflare page means **Security →
Bots → Bot Fight Mode** is blocking GitHub — turn it off.

Portal: `https://HOSTNAME/` → *Open portal* → *Sign in with GitHub* → back on
`/portal.html` with your repos listed.

Now label a **fresh** issue `rookery:trigger` — `enqueue()` ignores any issue that already
carries a `rookery:*` phase label, so the issues you exercised by CLI in step 9 will not
re-fire (see the gotchas). `job: start` in `journalctl -u rookery -f` means the chain is
live end to end.

Port 80 answers anyone, not only Cloudflare: restricting it to Cloudflare's IP ranges
(nginx `allow`/`deny` or `ufw`) is not done yet.

---

## Gotchas

### AVX2 (the two-hour one)

Claude Code **≥ 2.1.113** ships a Bun/JavaScriptCore binary (215–240MB) (`bin/claude.exe`, an
ELF despite the name) that assumes a modern x86-64 baseline. On a VPS whose CPU model is
a generic `qemu64`, it does not fail — it **spins at 100% CPU forever**.
`claude --version` answers instantly because it exits before JIT warmup, while
`claude --help` and `claude -p` hang with no output, no network traffic, and no error.
`sh()` captures stdout, so the symptom is a run parked at
`claude plan — this takes a while...` indefinitely.

Diagnose in ten seconds:

```bash
grep -m1 flags /proc/cpuinfo | tr ' ' '\n' | grep -cE '^(avx2|sse4_2)$'   # 0 = broken
claude --help >/dev/null 2>&1 & sleep 5; ps -o pcpu,comm -p $!; kill $!   # ~100% = spinning
```

Fixes, best first:

1. Ask the host to set the VM's CPU model to `host-passthrough`. This is hypervisor-side;
   you cannot change it from inside the guest.
2. Move to a provider that exposes AVX2 (Hetzner, Vultr, DigitalOcean, Scaleway).
3. Pin **2.1.110** (2.1.112 is the last release with `bin: cli.js`, which runs on Node's
   V8 and does not care; `2.1.113` is the first with the binary — checked with
   `npm view @anthropic-ai/claude-code@<v> bin`).

With (3), a bare `npm install -g @anthropic-ai/claude-code` silently reinstalls the broken
build and puts you right back here. Keep the pin. (Without `--prefix ~/.local` it fails
anyway: npm's default prefix is `/usr`, which `rookery` cannot write.)

**(3) costs you the current models.** The pinned CLI resolves `opus`/`sonnet` to
`claude-opus-4-6`/`claude-sonnet-4-6`. A full id on the command line
(`--model claude-sonnet-5-5`) works on it, but a full id in an agent's `model:`
frontmatter is silently ignored — the call falls back to the default model — so the
`planmaker`/`reviewer` roles cannot be moved to a newer model without code changes.
Only (1) or (2) gets you the latest CLI and models with no code change.

Rolling back after a bad upgrade — no service restart needed, each call spawns a fresh
`claude`:

```bash
npm install -g --prefix ~/.local @anthropic-ai/claude-code@2.1.110   # server, rookery
timeout 90 claude -p 'reply with ok' --output-format json </dev/null
```

### The bot ignores my comments

You comment, GitHub's **Recent Deliveries** shows a green `204`, and nothing happens — no
reply, no phase flicker, and `journalctl -u rookery | grep 'job:'` shows no `job: continue`.

The 204 means nothing: `do_POST` answers *before* calling `enqueue()`, so that a slow job
never trips GitHub's 10s delivery timeout. Every authenticated payload reports green
whether it was acted on or dropped.

`enqueue()` drops an `issue_comment` unless all three hold — check them in this order:

```bash
journalctl -u rookery --no-pager | grep 'listening on'   # "(bot user: ...)" must be the App's, not you
journalctl -u rookery --no-pager | grep 'permission lookup'   # a failed write-access check denies
GH_TOKEN=$(tok OWNER/REPO) gh api repos/OWNER/REPO/collaborators/YOU/permission -q .permission  # admin or write
GH_TOKEN=$(tok OWNER/REPO) gh issue view N --repo OWNER/REPO --json labels   # some rookery:* phase label
```

Then the App itself: its **Issue comment** event must be ticked (App settings →
Permissions & events). With only **Issues**, a label still fires `job: start`, which
makes the agent look alive while every comment is silently lost.

Do not "fix" this by dropping the `ME` check. The bot comments after every planning round;
without the filter its own comment re-enters `enqueue()` and loops forever, and it can
approve itself.

### "I approve it" is not an approval

`APPROVE` is `^\s*(approve|approved|lgtm|go ahead)\b` matched with `.match()`, so the
comment must **begin** with one of those words. `approve` and `Approve, ship it` work;
`I approve it` does not, and neither does `approves it` — the `\b` requires the word to
end there.

It does not fail loudly, which is the confusing part. On `rookery:awaiting-approval` a
non-matching comment falls through to the `elif` and is fed back as developer feedback —
another planning round, another comment, no implementation. It looks like the agent
ignored you when it actually re-planned.

### `rookery:error` also means "the reviewer wants changes"

A run that ends `claude review -> changes-needed` pushes the branch, opens the draft PR,
and *then* sets `rookery:error`. Nothing crashed — the label is overloaded, because the
review is advisory and only flags.

Tell the two apart by the comment on the issue, not the label: a real failure posts
`Agent error:` with a traceback; a `changes-needed` verdict posts a short "the reviewer
wants changes" line pointing at the **Review** section of the PR body.

Commenting `approve` again on that issue resumes the task loop at the first unticked box —
with every box already ticked, that is a no-op and it simply re-reviews. Fix the flagged
code yourself, or reply with feedback to get another planning round.

### `OAuth access token has been revoked`

Mid-run, `claude failed (1)` with `api_error_status: 401` and
`"OAuth access token has been revoked."` — the `.credentials.json` copied in step 3 is a
snapshot of a **rotating** token. Your laptop refreshing it, or any re-login on that
account, revokes the server's copy. Permanent, not transient, and it recurs.

Log the server in on its own instead:

```bash
su - rookery
claude            # /login — prints a URL, authorize it in a browser, paste the code back
timeout 120 claude -p 'say ok' --output-format json </dev/null    # the step 7 gate again
```

Then comment `approve` on the issue: it is on `rookery:error` with every task before the
failing one already committed, so the loop resumes at the first unticked box.

The credentials copy is a convenience for first light. If the revocation keeps recurring,
two machines are contending for one seat — give the bot its own account.

### Hangs are silent by design

`sh()` has no timeout and inherits stdin, so any prompt or spin blocks forever with
nothing on screen. Worth changing:

```python
p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                   encoding="utf-8", timeout=1800, stdin=subprocess.DEVNULL)
```

A `TimeoutExpired` becomes an issue comment plus `rookery:error` through `handle()`,
instead of a run that never returns.

### Debugging a hang, in order

```bash
timeout 60 claude --debug --verbose -p 'say ok'    # NO pipe — `| tail` buffers everything until exit
timeout 30 claude -p 'say ok' </dev/null; echo $?  # instant error = waiting on a prompt; 124 = blocked elsewhere
curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' https://api.anthropic.com/v1/messages
node -e "fetch('https://api.anthropic.com/v1/messages',{method:'POST',body:'{}'}).then(r=>console.log(r.status))"
strace -f -tt -o /tmp/cl.trace claude --help       # no connect() and no blocked read = userspace spin
```

A `405` from that curl in under a second means the network and the endpoint are fine.
Anthropic does not serve sanctioned regions, so `curl https://ipinfo.io/json` is worth one
look — but a geo-block is a fast 403, never a hang.

### Smaller ones

- `git fetch` prompting for a username, or `Repository not found` on a private repo, means
  the clone's origin is ssh or the App is not installed on that repo.
- A repo that left the installation while the service was down keeps its clone: removal
  is event-driven only. Delete `$WORKSPACE/repos/<owner>_<repo>` and
  `$WORKSPACE/<owner>_<repo>` by hand.
- `sign failed` / `Could not open file` from `openssl` at the first job: `APP_KEY` points at
  a missing or unreadable `.pem` — it must be owned by `rookery`.
- With NodeSource Node, the npm package installs under `/usr/lib/node_modules`, not
  `/usr/local/lib/node_modules` — deleting the wrong tree looks like a failed reinstall.
- A phase label left over from a crashed run blocks re-triggering:
  `gh issue edit N --repo OWNER/REPO --remove-label rookery:planning`.
- Re-commenting `approve` after a failure resumes at the first unticked task; everything
  finished before it is already committed.
- A restart is deaf for the ~20s startup `sync()` takes: a comment in that window gets a
  `502` in Recent Deliveries and GitHub does not retry it. Hit **Redeliver** — dedupe on
  `X-GitHub-Delivery` only drops ids the server actually received.
- `hit its daily cap` as the error comment: the repo used `RUN_CAP` claude calls or
  `USD_CAP` dollars today (UTC). Wait for 00:00 UTC or raise the cap in `.env` and restart,
  then comment `approve` again.
