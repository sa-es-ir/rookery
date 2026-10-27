//! One `claude` call, as agent.py's claude() makes it, minus the server's parts (sudo, providers,
//! daily caps). The harness is the user's own `claude`, logged in however they log in.
use crate::text::{fmt, marker, prompt};
use serde_json::{Value, json};
use std::fs;
use std::io::Write;
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
    let mut cmd: Vec<String> = ["-p", "--output-format", "json", "--permission-mode", "bypassPermissions",
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
    if job.max_usd > 0.0 && job.spent >= job.max_usd {
        return Err(format!("This run hit its --max-usd of ${:.2} (spent ${:.2}). Nothing more ran; \
                            run it again with a higher --max-usd to go on.", job.max_usd, job.spent));
    }
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
    let mut out = run(&cmd, &p, cwd);
    if out.as_ref().is_err_and(|e| !resume && e.contains("already in use")) {
        // an earlier run made this session (it crashed, or this is a re-run): pick it up
        let at = cmd.iter().position(|a| a == "--session-id").unwrap();
        cmd[at] = "--resume".into();
        out = run(&cmd, &p, cwd);
    }
    let (usd, text) = match &out {
        Ok(v) => (v["total_cost_usd"].as_f64().unwrap_or(0.0), v["result"].as_str().unwrap_or("").to_string()),
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
                              "model": model, "sid": sid, "secs": started.elapsed().as_secs_f64()}));
    out?;
    m.ok_or_else(|| format!("no STATUS marker in agent reply:\n{}", tail(&text, 1500)))
}

fn tail(s: &str, n: usize) -> &str {
    let mut at = s.len().saturating_sub(n);
    while !s.is_char_boundary(at) {
        at += 1;
    }
    &s[at..]
}

/// Run claude with the prompt on stdin -> its JSON. A failed call says what claude said.
fn run(cmd: &[String], input: &str, cwd: &Path) -> Result<Value, String> {
    let mut child = Command::new(bin()?).args(cmd).current_dir(cwd)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("could not start claude: {e}"))?;
    let mut stdin = child.stdin.take().unwrap();
    let input = input.to_string();
    // from a thread: a prompt bigger than the pipe's buffer would block us while claude writes
    let writer = std::thread::spawn(move || stdin.write_all(input.as_bytes()));
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let _ = writer.join();
    let stdout = String::from_utf8_lossy(&out.stdout);
    let json: Option<Value> = serde_json::from_str(stdout.trim()).ok();
    if !out.status.success() || json.as_ref().is_some_and(|j| j["is_error"] == true) {
        // with --output-format json, an API error ("Invalid API key") is the result, on stdout
        let said = json.as_ref().and_then(|j| j["result"].as_str()).map(String::from)
            .unwrap_or_else(|| String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr }).trim().to_string());
        return Err(format!("claude failed ({}): {said}", out.status.code().unwrap_or(-1)));
    }
    json.ok_or_else(|| format!("claude replied with no JSON: {}", tail(&stdout, 500)))
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
        for flag in ["--strict-mcp-config", "--setting-sources", "--plugin-dir=plug", "--session-id"] {
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
    fn session_ids_match_the_server() {
        // agent.py: uuid.uuid5(NS, "o/r#12/plan") with NS = 6ba7b810-9dad-11d1-80b4-00c04fd430c8
        assert_eq!(sid("o/r#12/plan"), "dd590912-484d-5f45-b801-a6c166393b59");
    }
}
