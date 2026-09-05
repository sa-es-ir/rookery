# Deploying Rookery to a Linux server

Ubuntu 24.04 LTS. The orchestrator is stdlib-only Python; everything hard is in the two
CLIs it shells out to (`gh`, `claude`). Written against a real deploy — the gotchas at
the bottom each cost an hour, read them before you start.

Replace `OWNER/REPO` and `SERVER_IP` throughout. Each block says which machine and which
user it runs as.

## 0. Before you start

- A GitHub PAT — classic with scope **`repo`** (add **`workflow`** only if a task might
  edit `.github/workflows/*`, **`read:org`** only if the repo is in an org). Fine-grained
  equivalent: Contents RW, Issues RW, Pull requests RW, Metadata R.
- The **target** repo the agent will work on. Never this one.
- **Check the CPU first — see [AVX2](#avx2-the-two-hour-one) below.** A VPS with a generic
  `qemu64` CPU model cannot run current Claude Code at all.

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
scp agent.py test_agent.py root@SERVER_IP:/opt/rookery/
scp -r agents root@SERVER_IP:/opt/rookery/
scp .env root@SERVER_IP:/opt/rookery/
scp ~/.claude/.credentials.json root@SERVER_IP:/home/rookery/.claude/.credentials.json
```

Skipped on purpose: `repos.json` (paths are machine-specific — written in step 8),
`work/`, `ui/`, `.git`.

The credentials copy saves you the interactive login. It logs the server in as the same
account as your laptop, sharing one seat; use a separate account if concurrent sessions
trip a limit.

## 4. Hand ownership over — server, root

```bash
chown -R rookery:rookery /opt/rookery /home/rookery
chmod 600 /home/rookery/.claude/.credentials.json
```

`chown` all of `/home/rookery`, not just the two directories — anything root creates
there later (`repos/`, `.ssh/`) otherwise fails with a confusing `Permission denied` on
clone.

Everything from here runs as `rookery`.

## 5. GitHub auth — server, rookery

```bash
su - rookery
read -rs TOKEN && echo "$TOKEN" | gh auth login --with-token && unset TOKEN
gh auth setup-git          # so raw `git push` from the worktree authenticates
gh auth status
gh api user --jq .login    # this is ME — filtered out of approvals so the bot can't approve itself
```

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

## 8. Target repo and config — server, rookery

```bash
gh repo clone OWNER/REPO /home/rookery/repos/REPO

cat > /opt/rookery/repos.json <<'EOF'
{
  "OWNER/REPO": { "path": "/home/rookery/repos/REPO", "base": "main" }
}
EOF

gh repo view OWNER/REPO --json nameWithOwner --jq .nameWithOwner   # must equal the key, exactly
git -C /home/rookery/repos/REPO symbolic-ref --short HEAD          # must equal "base"
```

**The key is case-sensitive.** `enqueue()` tests `repo not in REPOS` against the webhook's
`full_name`, so a key of `sa-es-ir/llama` silently drops every event for
`sa-es-ir/Llama`. CLI dry runs still pass, because `gh` is case-insensitive — which is
exactly why this hides.

Check `/opt/rookery/.env`: `ALLOWLIST` holds the logins allowed to comment `approve`, and
note `WEBHOOK_SECRET` for step 10.

## 9. Test before any webhook — server, rookery

```bash
cd /opt/rookery
python3 test_agent.py                    # no network, pure asserts
python3 agent.py OWNER/REPO 12           # plan issue 12 -> posts a comment, sets claude:awaiting-approval
python3 agent.py OWNER/REPO 12 approve   # task loop -> push -> draft PR
```

Same `handle()` as the webhook; only the transport differs. If these pass, the agent is
deployed and only delivery is left.

## 10. Webhook — server, rookery

`agent.py` binds loopback, which GitHub cannot reach. One-line change:

```bash
sed -i 's/HTTPServer(("127.0.0.1", PORT)/HTTPServer(("", PORT)/' /opt/rookery/agent.py
```

GitHub → target repo → **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `http://SERVER_IP:8787/` |
| Content type | `application/json` |
| Secret | `WEBHOOK_SECRET` from `.env` |
| Events | *Let me select* → **Issues** + **Issue comments** |

`ufw` is inactive on a fresh 24.04, so 8787 is already open; a provider-side cloud
firewall may still need the rule. HTTP is cleartext — the HMAC authenticates the payload,
it does not encrypt it. Put Caddy in front once this handles a real repo.

Prefer no open port? Leave the bind alone and tunnel instead:

```bash
npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8787/
```

## 11. Run — server, rookery

```bash
cd /opt/rookery && tmux new -s rookery 'python3 agent.py'
# detach: ctrl-b d   ·   reattach: tmux attach -t rookery
```

Label an issue `claude:trigger`; `job: start` in the log means the chain is live.

Surviving reboots — `/etc/systemd/system/rookery.service`:

```ini
[Service]
User=rookery
WorkingDirectory=/opt/rookery
ExecStart=/usr/bin/python3 /opt/rookery/agent.py
Restart=always
Environment=PATH=/home/rookery/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

The explicit `PATH` matters — systemd does not read `.bashrc`, and without
`~/.local/bin` first it would find the system `claude`.

---

## Gotchas

### AVX2 (the two-hour one)

Claude Code **≥ 2.1.120** ships a 215MB Bun/JavaScriptCore binary (`bin/claude.exe`, an
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
3. Pin **2.1.110**, the last release with `bin: cli.js`, which runs on Node's V8 and does
   not care. `2.1.120` is the first with the binary.

With (3), a bare `npm install -g @anthropic-ai/claude-code` silently reinstalls the broken
build and puts you right back here. Keep the pin.

### Hangs are silent by design

`sh()` has no timeout and inherits stdin, so any prompt or spin blocks forever with
nothing on screen. Worth changing:

```python
p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                   encoding="utf-8", timeout=1800, stdin=subprocess.DEVNULL)
```

A `TimeoutExpired` becomes an issue comment plus `claude:error` through `handle()`,
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

- `ghp_xxx` in a guide is a placeholder. `gh auth login --with-token` answers
  `HTTP 401: Bad credentials` if you paste it literally.
- `git clone` prompting for a username means `gh auth setup-git` did not run as this user.
  `gh repo clone` works regardless.
- With NodeSource Node, the npm package installs under `/usr/lib/node_modules`, not
  `/usr/local/lib/node_modules` — deleting the wrong tree looks like a failed reinstall.
- A phase label left over from a crashed run blocks re-triggering:
  `gh issue edit N --repo OWNER/REPO --remove-label claude:planning`.
- Re-commenting `approve` after a failure resumes at the first unticked task; everything
  finished before it is already committed.
