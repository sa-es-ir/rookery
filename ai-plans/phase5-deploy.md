# Phase 5 deploy — rookery.codesimple.dev

**Written:** 29 September 2026, as a handoff to a fresh session. **Deployed the same day:**
all steps (0–8) are done, marked on each heading. Spec: [PRD-Phases.md](PRD-Phases.md) Phase 5; what was built and tested:
[phase5-notes.md](phase5-notes.md). The Phase 1–4 server setup is [../DEPLOY.md](../DEPLOY.md).

Goal: the Phase 5 code (portal, API, run log, cancel) running on the existing server,
reachable at **https://rookery.codesimple.dev**, with GitHub's webhook going there too and
port 8787 closed to the internet.

## Decisions already made

- **Cloudflare proxy + host nginx on port 80.** No Cloudflare Tunnel (the user rejected it
  as needless complexity), no Caddy, no certbot.
- **SSL mode stays Flexible — accepted by the user.** Browser → Cloudflare is HTTPS;
  Cloudflare → server is plain HTTP on port 80. That hop carries session cookies, the OAuth
  code at sign-in, and webhook payloads (issue text, private repos included) in cleartext.
  The HMAC authenticates payloads, it does not hide them. Upgrade path is at the bottom.
- **Because of Flexible, nginx must NOT redirect port 80 to https.** Cloudflare sends every
  request to the origin as http; a redirect there loops forever.
- **The streaming project goes.** The user no longer needs it; its nginx container holds
  port 80 today. Stop it, don't delete its files yet.
- `/` now serves the landing page, `/portal.html` the portal, the sign-in callback lands on
  `/portal.html` (local change, not yet committed — see step 0).

## Server facts (checked 29 Sep 2026, read-only)

- `root@185.208.172.15`, Ubuntu 24.04.3, Python 3.12.3, `ufw` inactive.
- **SSH from this Windows box only works with Windows OpenSSH** (the key is in the Windows
  ssh-agent; Git Bash's own `ssh` gets "Permission denied"). From the PowerShell tool:
  `& "$env:SystemRoot\System32\OpenSSH\ssh.exe" -o BatchMode=yes root@185.208.172.15 '<cmd>'`,
  and `scp.exe` from the same folder.
- `/opt/rookery` holds the **Phase 1–4** code: `agent.py`, `test_agent.py`, `agents/`,
  `app.pem`, `work/`, old `*.bak-pre-phase2/3` files. No `web.py`, no `ui/`.
- systemd unit `rookery` (active), runs as user `rookery`, listens on `0.0.0.0:8787`
  because `.env` has `HOST=` — **twice**. No `PUBLIC_URL`, `CLIENT_ID`, `CLIENT_SECRET`.
- The App's webhook currently points at `http://185.208.172.15:8787/`.
- Docker:
  - `/root/streaming/docker-compose.yml` — services `srs`, `watermark`, `nginx`
    (`nginx:stable-alpine`, publishes `80:80`). Only `nginx` is running. **Remove this.**
  - `aspire-dashboard` (ports 18888, 4317) — a separate compose project (`root_default`).
    **Leave it alone.**
- Nothing listens on 443. `nginx` is not installed on the host. `cloudflared` not installed.
- Rookery's `rookery.db` needs no migration: `agent.py` runs `CREATE TABLE IF NOT EXISTS`
  for `jobs`, `steps`, `sessions` at startup.

## Cloudflare (done)

- A record `rookery` → `185.208.172.15`, **Proxied**. Verified: the hostname resolves to
  Cloudflare and requests reach the server — they currently get a 404 from the old
  streaming nginx (`nginx/1.28.2` footer).
- No Origin Certificate, no Configuration Rule (skipped on purpose — see Decisions).
- To check if deliveries fail later: **Security → Bots → Bot Fight Mode** can 403 GitHub's
  webhook POSTs. If Recent Deliveries shows a 403 with a Cloudflare page, turn it off.

## Steps

Who does what: **[user]** = a dashboard or a secret; **[claude]** = can be done from here.

### 0. Commit the local changes [claude] — DONE (b8f0b48)

Branch `phase5-local` has uncommitted work: `ui/index.html`, `ui/portal.html` (mobile
layout), `web.py`, `test_agent.py`, `README.md` (`/` = landing page). Commit it so the
server runs a known commit. `claude-gh-perms.txt` is an unrelated untracked file — leave it
out.

Tests: `REPOS_ONLY= python test_agent.py` → `ok`. Plain `python test_agent.py` fails on this
machine because the local `.env` sets `REPOS_ONLY`, which leaks into the tests (known,
unfixed; the fix is clearing it at the top of `test_agent.py`).

### 1. GitHub App settings [user] — DONE

App settings → General:
- **Callback URL:** add `https://rookery.codesimple.dev/auth/callback` (keep the
  `http://127.0.0.1:8787/auth/callback` one for local testing).
- **Do not change the Webhook URL yet** — that is step 6.

The server can reuse the local `.env`'s `CLIENT_ID` / `CLIENT_SECRET` (same App). Step 3
copies them without printing them. A separate secret for the server is optional
(App settings → *Generate a new client secret*); then the user puts it in the server's
`.env` themselves.

### 2. Stop the streaming stack [claude, server root] — DONE

```bash
cd /root/streaming && docker compose down     # removes its containers + network; files stay
ss -lntp | grep ':80 '                        # want: nothing
docker ps                                     # want: only aspire-dashboard
```

### 3. Copy the code and set `.env` [claude] — DONE (server tests ok, service up)

From the repo root, PowerShell tool:

```powershell
$o = "$env:SystemRoot\System32\OpenSSH"; $h = "root@185.208.172.15"
& "$o\ssh.exe" $h 'cd /opt/rookery && cp agent.py agent.py.bak-pre-phase5 && cp test_agent.py test_agent.py.bak-pre-phase5 && mkdir -p ui'
& "$o\scp.exe" agent.py web.py test_agent.py "${h}:/opt/rookery/"
& "$o\scp.exe" -r agents "${h}:/opt/rookery/"
& "$o\scp.exe" ui/index.html ui/portal.html ui/styles.css ui/rookery.png "${h}:/opt/rookery/ui/"
```

Not copied on purpose: `ui/index2.html` (unlinked older landing page), `ui/tui/` (the
server only serves files directly in `ui/`), `.env`, `work/`.

`.env` additions. Copy the OAuth pair from the local `.env` without echoing it — from Git
Bash, calling the **Windows** `ssh.exe` (it can reach the Windows agent), stripping CRs:

```bash
grep -E '^(CLIENT_ID|CLIENT_SECRET)=' .env | tr -d '\r' \
  | "/c/Windows/System32/OpenSSH/ssh.exe" -o BatchMode=yes root@185.208.172.15 'cat >> /opt/rookery/.env'
```

If that can't reach the agent, the user runs `nano /opt/rookery/.env` themselves. Then, on
the server:

```bash
echo 'PUBLIC_URL=https://rookery.codesimple.dev' >> /opt/rookery/.env
grep -cE '^(CLIENT_ID|CLIENT_SECRET|PUBLIC_URL)=' /opt/rookery/.env   # want: 3
grep -c '^REPOS_ONLY' /opt/rookery/.env                               # want: 0
```

Keep `HOST=` for now — the webhook still arrives on `IP:8787` until step 6.
`PUBLIC_URL` being `https://` turns on the `Secure` cookie flag; that is correct under
Flexible too, since the browser's side is HTTPS.

```bash
chown -R rookery:rookery /opt/rookery && chmod 600 /opt/rookery/.env /opt/rookery/app.pem
sudo -u rookery python3 /opt/rookery/test_agent.py        # want: ok
systemctl restart rookery
journalctl -u rookery -n 20 --no-pager                    # want: listening on *:8787 ... UI at https://rookery.codesimple.dev/
```

A restart is deaf for ~20 s (startup `sync()`); a webhook in that window shows 502 in
Recent Deliveries and must be redelivered by hand.

### 4. nginx on port 80 [claude, server root] — DONE

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
ss -lntp | grep nginx                   # want: :80, owned by nginx (not docker-proxy)
```

### 5. Verify [claude, then user] — curl checks DONE (200/200/200/401)

From this machine (`curl.exe` in PowerShell; in Git Bash set `MSYS_NO_PATHCONV=1` or `/`
arguments get rewritten to `C:/Program Files/Git/`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://rookery.codesimple.dev/             # 200
curl -s https://rookery.codesimple.dev/ | grep -o '<title>[^<]*'                     # Rookery — agents that work your issue queue
curl -s -o /dev/null -w '%{http_code}\n' https://rookery.codesimple.dev/portal.html  # 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' https://rookery.codesimple.dev/   # 401 = HMAC rejected it: correct
```

**[user]** in a browser: landing page → *Open portal* → *Sign in with GitHub* → back on
`/portal.html` with their repos listed. Also check it on a phone.

### 6. Move the webhook [user] — DONE (204)

App settings → General → **Webhook URL** `https://rookery.codesimple.dev/` → Save. Then
Advanced → Recent Deliveries → **Redeliver** the latest → want **204**.
- 401 → the secret doesn't match `WEBHOOK_SECRET` (it didn't change, so unlikely).
- 403 with a Cloudflare page → Bot Fight Mode (see Cloudflare section).

### 7. Close port 8787 [claude, server root] — DONE (8787 refused from outside, site 200)

Only after step 6 shows 204:

```bash
sed -i '/^HOST=$/d' /opt/rookery/.env && grep -c '^HOST' /opt/rookery/.env   # want: 0
systemctl restart rookery
journalctl -u rookery -n 5 --no-pager                 # want: listening on 127.0.0.1:8787
```

From this machine: `curl -s -m 5 http://185.208.172.15:8787/` must fail (connection
refused), while `https://rookery.codesimple.dev/` still answers 200. Redeliver one webhook
again → 204.

### 8. Docs and status [claude] — DONE

- `DEPLOY.md`: drop the "Phase 5 is not deployed" banner; add `web.py` + `ui/` to the copy
  step; replace step 10/11's `HOST=` + `http://SERVER_IP:8787/` webhook with the nginx +
  Cloudflare setup above; add `PUBLIC_URL` / `CLIENT_ID` / `CLIENT_SECRET` and the callback
  URL.
- `ai-plans/phase5-notes.md` and `PRD-Phases.md` status line: Phase 5 deployed.
- `CLAUDE.md` Style section: "1–4 are deployed, 5 has run locally only" → all 5 deployed.
- Commit.

## Rollback

```bash
cd /opt/rookery && cp agent.py.bak-pre-phase5 agent.py && cp test_agent.py.bak-pre-phase5 test_agent.py
echo 'HOST=' >> .env && chown rookery:rookery agent.py test_agent.py .env
systemctl restart rookery
```

Webhook URL back to `http://185.208.172.15:8787/`. `web.py` and `ui/` can stay; the old
`agent.py` does not import them. Streaming, if ever needed again:
`systemctl stop nginx && cd /root/streaming && docker compose up -d`.

## Still open after this (not part of the deploy)

Before the link goes to anyone else — carried since Phase 1 (`phase3-4-notes.md` "Next steps"):
1. `gh auth logout` as `rookery` — the agent can read that token.
2. SSH hardening: `PasswordAuthentication no`, `PermitRootLogin prohibit-password`. Root is
   being password-brute-forced. Confirm a second key session works before closing the first.
3. Delete Llama's old per-repo webhook (only produces 401s).
4. `app.pem` is readable by the agent — run `claude` as a separate user (small fix) or the
   sandbox (real fix).

Also:
- Port 80 on the origin answers anyone, not just Cloudflare — someone hitting the IP gets the
  same app over plain http. Restricting 80 to Cloudflare's IP ranges (nginx `allow`/`deny`
  or `ufw`) closes that; not done.
- Cleanup from local testing: throwaway repos (`gh repo delete sa-es-ir/rookery-throwaway{,-2}`),
  the second App private key generated for this PC, `/root/streaming` files once sure.
- Licence check (PRD-Phases §1.1) is still not recorded as done.

## Upgrade path: Flexible → Full (strict)

When the cleartext hop stops being acceptable (~10 minutes):
1. Cloudflare: SSL/TLS → Origin Server → *Create Certificate* (RSA, 15 years). Save the cert
   and key as `origin-cert.pem` / `origin-key.pem` in the repo root (`*.pem` is gitignored),
   copy to `/etc/ssl/rookery.pem` / `/etc/ssl/rookery.key` (`chmod 600` the key), delete
   the local copies.
2. nginx: add a `listen 443 ssl` server with `ssl_certificate` / `ssl_certificate_key` and
   the same `location /` block; turn the port 80 block into
   `return 301 https://rookery.codesimple.dev$request_uri;`.
3. Cloudflare: Rules → Configuration Rules → hostname equals `rookery.codesimple.dev` →
   SSL **Full (strict)**. Do 2 before 3, and never the redirect without 3 — Flexible plus
   an origin redirect loops.
