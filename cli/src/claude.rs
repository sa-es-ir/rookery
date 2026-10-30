//! One `claude` call, as agent.py's claude() makes it, minus the server's parts (sudo, owner keys,
//! daily caps). The harness is the user's own `claude`: on its own login, or on the provider and key
//! `rookery config` set (config.rs).
use crate::text::{fmt, marker, prompt};
use serde_json::{Value, json};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;
use uuid::Uuid;

/// the agent never publishes. Deny rules beat bypassPermissions, and match the command after a
/// PreToolUse hook rewrites it: the user's RTK hook turns `git push` into `rtk git push`, and
/// `rtk proxy <cmd>` runs <cmd> unrewritten (spike S5: all three denied with these five).
pub const DENY: [&str; 5] = ["Bash(git push:*)", "Bash(gh:*)", "Bash(rtk git push:*)", "Bash(rtk gh:*)", "Bash(rtk proxy:*)"];

/// Everything one run's calls share; steering is read once, at the run's start.
#[derive(Default)]
pub struct Job {
    pub repo_key: String, // hash8 of the repo's path: sessions of two repos never collide
    pub n: u64,
    pub run: u64,
    pub base: String,
    pub claude_md: String,
    pub memory: String,
    pub sources: std::collections::BTreeMap<String, String>,
    pub agents: std::collections::BTreeMap<String, crate::steer::Meta>,
    pub plugin: Option<PathBuf>,
    pub add_dirs: Vec<PathBuf>,
    pub scratch: PathBuf, // this run's own dir: the system prompt file goes here
    pub max_usd: f64,
    pub spent: f64,
    pub mcp: bool,
    pub log: Option<PathBuf>, // runs.jsonl
}

impl Job {
    /// Append a step to runs.jsonl; without a log (doctor) a no-op.
    pub fn step(&self, what: &str, detail: Value) {
        let Some(log) = &self.log else { return };
        let mut line = json!({"run": self.run, "n": self.n, "at": now(), "what": what});
        line.as_object_mut().unwrap().extend(detail.as_object().cloned().unwrap_or_default());
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(log) {
            let _ = writeln!(f, "{line}");
        }
    }

    /// The run's first step, which `rookery log` reads its kind from. It is also the run the
    /// repo notes learn from once the command ends (memory::after()).
    pub fn begin(&self, r: &crate::Repo, kind: &str) {
        self.step("begin", json!({"kind": kind}));
        crate::memory::ran(r, self.n, self.run);
    }

    /// agent.py's pin(): what steers the agent, from `base` at `sha`, read once at the run's
    /// start, never from the worktree; the repo notes with it. Logged as the `base` step.
    /// -> the workflow.
    pub fn pin(&mut self, r: &crate::Repo, base: &str, sha: &str) -> Result<Value, String> {
        let plug = self.scratch.join("plugin");
        let s = crate::steer::steer(&r.root, base, sha, &crate::steer::rookery_home().join("defaults"), &plug)?;
        let (memory, said) = crate::memory::load(r, &s);
        self.step("base", json!({"base": base, "sha": sha, "claude_md": s.claude_md.len(), "sources": s.sources,
                                 "agents": s.agent_sources, "memory": said}));
        (self.base, self.claude_md, self.memory, self.sources, self.agents, self.plugin) =
            (base.into(), s.claude_md, memory, s.sources, s.agents, Some(plug));
        Ok(s.workflow)
    }
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// agent.py's NS: a session id is uuid5(NS, "<repo>#<n>/<key>"), so re-running resumes it.
pub fn sid(name: &str) -> String {
    Uuid::new_v5(&Uuid::NAMESPACE_DNS, name.as_bytes()).to_string()
}

/// `claude` on PATH, the way a shell finds it. Rust's Command finds only `claude.exe` on
/// Windows, and npm installs a `claude.cmd` (spike S1), so PATHEXT is walked here.
pub fn bin() -> Result<PathBuf, String> {
    let exts: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT").unwrap_or(".EXE;.CMD;.BAT".into()).split(';')
            .filter(|e| matches!(e.to_ascii_lowercase().as_str(), ".exe" | ".cmd" | ".bat" | ".com"))
            .map(String::from).collect()
    } else {
        vec![String::new()]
    };
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .flat_map(|d| exts.iter().map(move |e| d.join(format!("claude{e}"))))
        .find(|p| p.is_file())
        .ok_or_else(|| "claude is not on PATH. Install Claude Code first: https://code.claude.com/docs/en/setup".into())
}

/// The prompt and argv for one call, before it runs: what the tests check.
pub fn argv(job: &Job, prompt_: &str, key: &str, resume: bool, slot: &Value, per_job: bool) -> (Vec<String>, String, String, Option<String>) {
    let sid = sid(&format!("{}#{}/{key}{}", job.repo_key, job.n, if per_job { format!("/run-{}", job.run) } else { String::new() }));
    let skills: Vec<&str> = slot["skills"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
    // a skill in the plugin alone almost never fires; named in the prompt, it does
    let mut p = std::iter::once(prompt_.to_string())
        .chain(skills.iter().map(|k| fmt(&prompt("use-skill"), &[("name", k.to_string())])))
        .collect::<Vec<_>>().join("\n\n");
    // the repo's own skills, unnamed by this slot, are offered; never the catalog's
    let offer: Vec<String> = job.sources.iter().filter(|(k, src)| *src != "catalog" && !skills.contains(&k.as_str()))
        .map(|(k, _)| format!("`rk:{k}`")).collect();
    if !offer.is_empty() {
        p += &format!("\n\n{}", fmt(&prompt("offer-skills"), &[("names", offer.join(", "))]));
    }
    // streamed, so the model's reasoning shows while it works: a summary, never the raw thinking,
    // billed the same either way (display changes what comes back, not what is spent)
    let mut cmd: Vec<String> = ["-p", "--output-format", "stream-json", "--verbose", "--thinking-display", "summarized",
                                "--permission-mode", "bypassPermissions",
                                // never the target repo's settings: its hooks would run, its skills and
                                // CLAUDE.md come from the worktree. The user's own settings are theirs.
                                "--setting-sources", "user"].map(String::from).into();
    if !job.mcp {
        // the repo's .mcp.json is never loaded either way (--setting-sources); this keeps the
        // user's own servers out too, unless they asked for them with --mcp
        cmd.push("--strict-mcp-config".into());
    }
    cmd.extend(slot["args"].as_array().into_iter().flatten().filter_map(Value::as_str).map(String::from));
    if let Some(plug) = &job.plugin {
        cmd.push(format!("--plugin-dir={}", plug.display()));
    }
    cmd.extend(job.add_dirs.iter().map(|d| format!("--add-dir={}", d.display())));
    let inst = slot["instructions"].as_str().unwrap_or("");
    let rules: Vec<String> = [
        (!job.claude_md.is_empty()).then(|| format!("Contents of CLAUDE.md (the repo's own instructions, from {}):\n\n{}", job.base, job.claude_md)),
        (!job.memory.is_empty()).then(|| prompt("memory-note") + &job.memory),
        (!inst.is_empty()).then(|| format!("Instructions for this step (.rookery/workflow.json, from {}):\n\n{inst}", job.base)),
    ].into_iter().flatten().collect();
    let system = (!rules.is_empty()).then(|| prompt("system") + &rules.join("\n\n"));
    cmd.push("--disallowed-tools".into());
    cmd.extend(DENY.map(String::from));
    let agent = slot["agent"].as_str();
    match agent {
        // a plugin agent: its persona and `model:` come from agents/<name>.md
        Some(a) => cmd.extend(["--agent".into(), format!("rk:{a}")]),
        // no agent (implementation tasks, by default), so the model is set here
        None => cmd.extend(["--model".into(), slot["model"].as_str().unwrap_or("sonnet").into()]),
    }
    cmd.extend([if resume { "--resume" } else { "--session-id" }.into(), sid.clone()]);
    (cmd, p, sid, system)
}

/// -> (STATUS, the reply without it). Checks --max-usd first, logs the call either way.
pub fn claude(job: &mut Job, prompt_: &str, key: &str, resume: bool, slot: &Value, per_job: bool, cwd: &Path)
              -> Result<(String, String), String> {
    if crate::cancel::cancelled() {
        return Err(crate::cancel::CANCELLED.into());
    }
    if job.max_usd > 0.0 && job.spent >= job.max_usd {
        return Err(format!("This run hit its --max-usd of ${:.2} (spent ${:.2}). Nothing more ran; \
                            run it again with a higher --max-usd to go on.", job.max_usd, job.spent));
    }
    // read once per call and used for its env, its price and its step, as agent.py's claude() does
    let (provider, api_key) = crate::config::active()?;
    let env = crate::config::env(&provider, api_key.as_deref());
    let (mut cmd, p, sid, system) = argv(job, prompt_, key, resume, slot, per_job);
    if let Some(s) = system {
        // a file, never argv: Windows caps a whole command line at 32,767 characters, CLAUDE.md
        // alone may be 64 KB, and a .cmd shim cannot carry its newlines
        fs::create_dir_all(&job.scratch).map_err(|e| e.to_string())?;
        let f = job.scratch.join(format!("system-{key}.md"));
        fs::write(&f, s).map_err(|e| e.to_string())?;
        let at = cmd.iter().position(|a| a == "--disallowed-tools").unwrap();
        cmd.splice(at..at, ["--append-system-prompt-file".into(), f.display().to_string()]);
    }
    let started = Instant::now();
    let mut out = run(&cmd, &p, cwd, &env);
    if out.as_ref().is_err_and(|e| e.contains("--thinking-display")) {
        // a claude older than the flag: the same call, without the summaries
        let at = cmd.iter().position(|a| a == "--thinking-display").unwrap();
        cmd.drain(at..at + 2);
        out = run(&cmd, &p, cwd, &env);
    }
    if out.as_ref().is_err_and(|e| !resume && e.contains("already in use")) {
        // an earlier run made this session (it crashed, or this is a re-run): pick it up
        let at = cmd.iter().position(|a| a == "--session-id").unwrap();
        cmd[at] = "--resume".into();
        out = run(&cmd, &p, cwd, &env);
    }
    // a model the provider's price table lacks fails the call: no quiet guess
    if let Some(e) = out.as_ref().ok().and_then(|v| crate::config::cost(v, &provider).err()) {
        out = Err(e);
    }
    let (usd, text) = match &out {
        Ok(v) => (crate::config::cost(v, &provider).unwrap_or(0.0), v["result"].as_str().unwrap_or("").to_string()),
        Err(_) => (0.0, String::new()),
    };
    job.spent += usd;
    let m = marker(&text);
    let status = m.as_ref().map_or(if out.is_ok() { "no-marker" } else { "failed" }, |m| m.0.as_str());
    let model = match slot["agent"].as_str() {
        Some(a) => job.agents.get(a).and_then(|m| m.get("model")).cloned().map_or(Value::Null, Value::from),
        None => json!(slot["model"].as_str().unwrap_or("sonnet")),
    };
    job.step("claude", json!({"key": key, "status": status, "usd": usd, "agent": slot["agent"], "skills": slot["skills"],
                              "model": model, "sid": sid, "provider": provider, "secs": started.elapsed().as_secs_f64()}));
    out?;
    m.ok_or_else(|| format!("no STATUS marker in agent reply:\n{}", tail(&text, 1500)))
}

pub fn tail(s: &str, n: usize) -> &str {
    let mut at = s.len().saturating_sub(n);
    while !s.is_char_boundary(at) {
        at += 1;
    }
    &s[at..]
}

/// the most rows one summary takes on a terminal; past it, the rest is `…`
const THINK_ROWS: usize = 6;

/// A call's reasoning summaries, dim. On a terminal the latest replaces the one before it in place,
/// and the call's end collapses them all to one line; piped, each prints in full. Nothing shows
/// when the model or provider omits them.
#[derive(Default)]
struct Thinking {
    rows: u16,
    count: usize,
}

impl Thinking {
    fn live() -> bool {
        use std::io::IsTerminal;
        std::io::stdout().is_terminal()
    }

    fn show(&mut self, event: &Value) {
        for b in event["message"]["content"].as_array().into_iter().flatten().filter(|b| b["type"] == "thinking") {
            let t = b["thinking"].as_str().unwrap_or("").replace("**", "");
            if t.trim().is_empty() {
                continue;
            }
            self.count += 1;
            if !Self::live() {
                println!("{}", crate::paint("2", &format!("  ∴ {}", t.trim().replace('\n', "\n    "))));
                continue;
            }
            // every row fits the width, so the rows printed are the rows to erase
            let w = crossterm::terminal::size().map_or(80, |(w, _)| w as usize).saturating_sub(5).max(20);
            let mut rows = wrap(&t, w);
            if rows.len() > THINK_ROWS {
                rows.truncate(THINK_ROWS - 1);
                rows.push("…".into());
            }
            self.erase();
            for (i, r) in rows.iter().enumerate() {
                println!("{}", crate::paint("2", &format!("{} {r}", if i == 0 { "  ∴" } else { "   " })));
            }
            self.rows = rows.len() as u16;
        }
    }

    fn erase(&mut self) {
        use crossterm::{cursor, terminal::{Clear, ClearType}};
        if self.rows > 0 {
            let _ = crossterm::execute!(std::io::stdout(), cursor::MoveUp(self.rows), cursor::MoveToColumn(0), Clear(ClearType::FromCursorDown));
            self.rows = 0;
        }
    }

    fn done(&mut self, secs: f64) {
        if Self::live() && self.count > 0 {
            self.erase();
            println!("{}", crate::paint("2", &format!("  ∴ thought {}× · {secs:.0} s", self.count)));
        }
    }
}

/// `t`'s non-blank lines word-wrapped to `w` characters, a longer word split.
/// ponytail: counts chars, so a wide (CJK, emoji) one overflows its row; unicode-width if that bites
fn wrap(t: &str, w: usize) -> Vec<String> {
    let mut out = vec![];
    for para in t.lines().filter(|l| !l.trim().is_empty()) {
        let mut cur = String::new();
        for word in para.split_whitespace() {
            for piece in word.chars().collect::<Vec<_>>().chunks(w) {
                let n = cur.chars().count();
                if n > 0 && n + 1 + piece.len() > w {
                    out.push(std::mem::take(&mut cur));
                }
                if !cur.is_empty() {
                    cur.push(' ');
                }
                cur.extend(piece);
            }
        }
        if !cur.is_empty() {
            out.push(cur);
        }
    }
    out
}

/// Run claude with the prompt on stdin -> its result event (the JSON `--output-format json` would
/// give), showing the reasoning on the way. A failed call says what claude said. `env` is
/// config::env(): empty on the login, which leaves the user's env as it is.
fn run(cmd: &[String], input: &str, cwd: &Path, env: &[(String, String)]) -> Result<Value, String> {
    let mut c = Command::new(bin()?);
    if !env.is_empty() {
        // a bearer token of the user's own would be sent in place of the provider's key
        c.env_remove("ANTHROPIC_AUTH_TOKEN").envs(env.iter().map(|(k, v)| (k, v)));
    }
    crate::cancel::detach(&mut c);
    let mut child = c.args(cmd).current_dir(cwd)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("could not start claude: {e}"))?;
    // Ctrl-C kills this call and all it started; dropped once it ends, which takes its orphans
    let _tree = crate::cancel::guard(&child);
    let mut stdin = child.stdin.take().unwrap();
    let input = input.to_string();
    // from a thread: a prompt bigger than the pipe's buffer would block us while claude writes
    let writer = std::thread::spawn(move || stdin.write_all(input.as_bytes()));
    // and stderr: a full pipe there would stall claude while stdout is read
    let mut stderr = child.stderr.take().unwrap();
    let errs = std::thread::spawn(move || { let mut s = String::new(); let _ = stderr.read_to_string(&mut s); s });
    let (mut result, mut raw, mut thinking, started) = (None, String::new(), Thinking::default(), Instant::now());
    for line in BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok) {
        match serde_json::from_str::<Value>(&line) {
            Ok(e) if e["type"] == "result" => result = Some(e),
            Ok(e) if e["type"] == "assistant" => thinking.show(&e),
            Ok(_) => {}
            Err(_) => raw += &format!("{line}\n"),
        }
    }
    thinking.done(started.elapsed().as_secs_f64());
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = writer.join();
    let stderr = errs.join().unwrap_or_default();
    if crate::cancel::cancelled() {
        return Err(crate::cancel::CANCELLED.into());
    }
    if !status.success() || result.as_ref().is_some_and(|j| j["is_error"] == true) {
        // an API error ("Invalid API key") is the result event, on stdout
        let said = result.as_ref().and_then(|j| j["result"].as_str()).map(String::from)
            .unwrap_or_else(|| if stderr.trim().is_empty() { raw.trim() } else { stderr.trim() }.to_string());
        return Err(format!("claude failed ({}): {said}", status.code().unwrap_or(-1)));
    }
    result.ok_or_else(|| format!("claude replied with no result: {}", tail(&raw, 500)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn argv_holds_the_invariants() {
        let mut job = Job { repo_key: "abcd1234".into(), n: 7, run: 3, base: "main".into(), claude_md: "Use tabs.".into(),
                            plugin: Some("plug".into()), ..Default::default() };
        job.sources.insert("verify".into(), ".rookery/skills".into());
        job.sources.insert("repo-recon".into(), ".claude/skills".into());
        job.sources.insert("changelog".into(), "catalog".into());
        let slot = json!({"agent": "planmaker", "skills": ["repo-recon"], "instructions": "One migration per task."});
        let (cmd, p, sid, system) = argv(&job, "p", "plan", false, &slot, false);
        for flag in ["--strict-mcp-config", "--setting-sources", "--plugin-dir=plug", "--session-id", "--thinking-display"] {
            assert!(cmd.contains(&flag.to_string()), "{flag}");
        }
        assert_eq!(&cmd[cmd.iter().position(|a| a == "--disallowed-tools").unwrap() + 1..][..5], DENY);
        assert_eq!(cmd[cmd.iter().position(|a| a == "--agent").unwrap() + 1], "rk:planmaker");
        assert!(!cmd.contains(&"--model".to_string()), "an agent's model is its own");
        assert_eq!(sid, super::sid("abcd1234#7/plan"), "re-running resumes the same session");
        assert_eq!(p, format!("p\n\nUse the `rk:repo-recon` skill.\n\n{}",
                              fmt(&prompt("offer-skills"), &[("names", "`rk:verify`".into())])), "named; the repo's others offered");
        let system = system.unwrap();
        assert!(system.starts_with(&prompt("system")) && system.contains("Use tabs.") && system.ends_with("One migration per task."));

        job.mcp = true;
        let (cmd, _, sid2, system) = argv(&job, "p", "task-0", true, &json!({"agent": null, "skills": [], "instructions": ""}), true);
        assert!(!cmd.contains(&"--strict-mcp-config".to_string()) && cmd.contains(&"--resume".to_string()));
        assert_eq!(cmd[cmd.iter().position(|a| a == "--model").unwrap() + 1], "sonnet", "tasks grind on sonnet");
        assert_eq!(sid2, super::sid("abcd1234#7/task-0/run-3"), "per_job: this run's own");
        assert!(system.unwrap().ends_with("Use tabs."));
    }

    #[test]
    fn thinking_wraps_to_the_width() {
        assert_eq!(wrap("Checking the tests now\n\nthen abcdefghij", 10), ["Checking", "the tests", "now", "then", "abcdefghij"]);
        assert_eq!(wrap("x abcdefghijklm", 5), ["x", "abcde", "fghij", "klm"], "a word past the width is split");
        assert!(wrap("a b c d e f g h i j k l m n o p q r s t u v w x y z", 20).iter().all(|r| r.chars().count() <= 20));
    }

    #[test]
    fn session_ids_match_the_server() {
        // agent.py: uuid.uuid5(NS, "o/r#12/plan") with NS = 6ba7b810-9dad-11d1-80b4-00c04fd430c8
        assert_eq!(sid("o/r#12/plan"), "dd590912-484d-5f45-b801-a6c166393b59");
    }
}
