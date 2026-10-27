//! `rookery`: Rookery's flow — plan, approve, a fresh claude session per task, the orchestrator
//! ticks and commits, an advisory review — on a local git repo. No GitHub, no server.
mod claude;
mod steer;
mod text;

use clap::{Parser, Subcommand};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use steer::{git_text, steer};

/// the oldest claude this was checked against: the server's pinned version
const MIN_CLAUDE: [u64; 3] = [2, 1, 110];

#[derive(Parser)]
#[command(name = "rookery", version, about = "Plan, approve, and let Claude Code implement it task by task, in a local git repo.")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Check git, claude and this repo's steering, then make one small claude call (about $0.01)
    Doctor {
        /// The branch to read CLAUDE.md and .rookery/ from [default: the checked-out branch]
        #[arg(long)]
        base: Option<String>,
    },
}

/// The repo rookery runs in. Its state lives in the git common dir, so it is never committed,
/// every worktree shares it, and it goes when the repo goes.
pub struct Repo {
    pub root: PathBuf,
    pub state: PathBuf,
    pub key: String,
}

pub fn repo(dir: &Path) -> Result<Repo, String> {
    let root = PathBuf::from(git_text(dir, &["rev-parse", "--show-toplevel"]).map_err(|_| "this is not a git repo".to_string())?);
    let common = git_text(dir, &["rev-parse", "--path-format=absolute", "--git-common-dir"])?;
    let key = claude::sid(&root.to_string_lossy())[..8].to_string();
    Ok(Repo { state: PathBuf::from(common).join("rookery"), root, key })
}

/// `--base`, else the checked-out branch -> (branch, its commit).
fn base_of(root: &Path, base: Option<String>) -> Result<(String, String), String> {
    let base = match base {
        Some(b) => b,
        None => match git_text(root, &["rev-parse", "--abbrev-ref", "HEAD"])? {
            b if b == "HEAD" => return Err("HEAD is detached: name the base branch with --base".into()),
            b => b,
        },
    };
    let sha = git_text(root, &["rev-parse", "--verify", "--quiet", &format!("{base}^{{commit}}")])
        .map_err(|_| format!("{base} is not a branch or commit here"))?;
    Ok((base, sha))
}

fn version(s: &str) -> Option<[u64; 3]> {
    let mut it = s.split_whitespace().next()?.split('.').map(|x| x.parse().ok());
    Some([it.next()??, it.next()??, it.next()??])
}

fn doctor(base: Option<String>) -> Result<(), String> {
    let mut failed = 0;
    let mut say = |ok: Option<bool>, what: &str, detail: String| {
        failed += (ok == Some(false)) as u32;
        println!("{:<5} {what:<9} {detail}", match ok { Some(true) => "ok", Some(false) => "FAIL", None => "warn" });
    };
    let here = std::env::current_dir().map_err(|e| e.to_string())?;
    match git_text(&here, &["--version"]) {
        Ok(v) => say(Some(true), "git", v),
        Err(e) => say(Some(false), "git", e),
    }
    let bin = claude::bin();
    match &bin {
        Ok(b) => {
            let v = std::process::Command::new(b).arg("--version").output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
            match v.as_deref().ok().and_then(version) {
                Some(v) if v >= MIN_CLAUDE => say(Some(true), "claude", format!("{} {}", b.display(), v.map(|x| x.to_string()).join("."))),
                Some(v) => say(Some(false), "claude", format!("{} is {}; rookery needs {} or later (`claude update`)",
                                                             b.display(), v.map(|x| x.to_string()).join("."), MIN_CLAUDE.map(|x| x.to_string()).join("."))),
                None => say(Some(false), "claude", format!("{} --version did not answer: {v:?}", b.display())),
            }
        }
        Err(e) => say(Some(false), "claude", e.clone()),
    }
    if std::env::var_os("ANTHROPIC_API_KEY").is_some() {
        say(None, "env", "ANTHROPIC_API_KEY is set: claude uses it, not your /login".into());
    }

    let mut job = claude::Job { scratch: std::env::temp_dir().join(format!("rookery-doctor-{}", std::process::id())), ..Default::default() };
    match repo(&here) {
        Err(e) => say(None, "repo", format!("{e}: only the claude call is checked")),
        Ok(r) => {
            let (base, sha) = base_of(&r.root, base)?;
            say(Some(true), "repo", format!("{}, base {base} at {}, state in {}", r.root.display(), &sha[..12], r.state.display()));
            let dirty = git_text(&r.root, &["status", "--porcelain"])?.lines().count();
            if dirty > 0 {
                say(None, "checkout", format!("{dirty} uncommitted changes: a run starts from {base}'s last commit and does not see them"));
            }
            let plug = job.scratch.join("plugin");
            match steer(&r.root, &base, &sha, &steer::rookery_home().join("defaults"), &plug) {
                Ok(s) => {
                    let own: Vec<_> = s.agent_sources.iter().filter(|(_, src)| *src != "built-in").map(|(n, src)| format!("{n} ({src})")).collect();
                    let after: Vec<_> = s.workflow["after"].as_array().unwrap().iter()
                        .map(|a| format!("{} ({})", a["agent"].as_str().unwrap(), a["mode"].as_str().unwrap())).collect();
                    say(Some(true), "steering", format!(
                        "CLAUDE.md {} KB; after steps: {}; repo agents: {}; skills: {}",
                        s.claude_md.len().div_ceil(1024), if after.is_empty() { "none".into() } else { after.join(", ") },
                        if own.is_empty() { "none".into() } else { own.join(", ") },
                        if s.sources.is_empty() { "none".into() } else { s.sources.iter().map(|(k, v)| format!("{k} ({v})")).collect::<Vec<_>>().join(", ") }));
                    job.plugin = Some(plug);
                    job.base = base;
                    job.agents = s.agents;
                }
                Err(e) => say(Some(false), "steering", e),
            }
        }
    }
    if bin.is_ok() {
        // the memory call's shape: haiku, one turn, no tools. It proves the login, the plugin
        // and the system prompt file, and costs about a cent.
        let slot = json!({"agent": null, "skills": [], "model": "haiku", "args": ["--max-turns", "1", "--tools", ""],
                          "instructions": "rookery doctor is checking that this harness works."});
        let cwd = job.scratch.join("cwd");
        std::fs::create_dir_all(&cwd).map_err(|e| e.to_string())?;
        let t = std::time::Instant::now();
        match claude::claude(&mut job, "Reply with exactly this line and nothing else:\nSTATUS: ok", "doctor", false, &slot, false, &cwd) {
            Ok((s, _)) if s == "ok" => say(Some(true), "call", format!("haiku answered in {:.1} s for ${:.4}", t.elapsed().as_secs_f64(), job.spent)),
            Ok((s, _)) => say(Some(false), "call", format!("haiku replied STATUS: {s}, not ok")),
            Err(e) => say(Some(false), "call", e),
        }
    }
    let _ = std::fs::remove_dir_all(&job.scratch);
    if failed > 0 { Err(format!("{failed} check(s) failed")) } else { Ok(()) }
}

fn main() -> ExitCode {
    let r = match Cli::parse().cmd {
        Cmd::Doctor { base } => doctor(base),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("rookery: {e}");
            ExitCode::FAILURE
        }
    }
}
