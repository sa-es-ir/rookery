//! `rookery`: Rookery's flow — plan, approve, a fresh claude session per task, the orchestrator
//! ticks and commits, an advisory review — on a local git repo. No GitHub, no server.
mod approve;
mod claude;
mod input;
mod logo;
mod plan;
mod review;
mod steer;
mod text;

use clap::{CommandFactory, Parser, Subcommand};
use serde_json::json;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use steer::{git_text, steer};

/// the oldest claude this was checked against: the server's pinned version
const MIN_CLAUDE: [u64; 3] = [2, 1, 110];

#[derive(Parser)]
#[command(name = "rookery", version, about = "Plan, approve, and let Claude Code implement it task by task, in a local git repo.",
          long_about = "Plan, approve, and let Claude Code implement it task by task, in a local git repo.\n\nRun `rookery` with no command to type commands at a prompt.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Describe a change; rookery plans it in its own worktree and asks, or shows the tasks
    New {
        /// What to change, in a few words
        title: String,
        /// More detail: the issue's body
        #[arg(short = 'm', long = "message", conflicts_with = "file")]
        body: Option<String>,
        /// Read the body from a markdown file
        #[arg(short = 'f', long)]
        file: Option<PathBuf>,
        /// The branch to start from and read CLAUDE.md and .rookery/ from [default: the checked-out branch]
        #[arg(long)]
        base: Option<String>,
        /// Let the agent use your own MCP servers (never the repo's .mcp.json)
        #[arg(long)]
        mcp: bool,
    },
    /// Answer an item's questions, or change its plan; rookery plans again in the same session
    Reply {
        /// The item's number
        n: u64,
        /// Your answer
        text: String,
        /// Let the agent use your own MCP servers (never the repo's .mcp.json)
        #[arg(long)]
        mcp: bool,
    },
    /// Run an item's tasks, one fresh session each, then its after steps and the review; nothing is pushed
    Approve {
        /// The item's number
        n: u64,
        /// Stop before a call once the run has spent this many dollars [default: max_usd in ~/.rookery/config.json, else no cap]
        #[arg(long)]
        max_usd: Option<f64>,
        /// Let the agent use your own MCP servers (never the repo's .mcp.json)
        #[arg(long)]
        mcp: bool,
    },
    /// Show an item: its phase, branch, commits, tasks and the review
    Show {
        /// The item's number
        n: u64,
    },
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
    // git says C:/x on Windows, and a join there adds \: native separators, so paths read as one
    let native = |s: String| PathBuf::from(if cfg!(windows) { s.replace('/', "\\") } else { s });
    let root = native(git_text(dir, &["rev-parse", "--show-toplevel"]).map_err(|_| "this is not a git repo".to_string())?);
    let common = native(git_text(dir, &["rev-parse", "--path-format=absolute", "--git-common-dir"])?);
    let key = claude::sid(&root.to_string_lossy())[..8].to_string();
    Ok(Repo { state: common.join("rookery"), root, key })
}

/// `--base`, else the checked-out branch -> (branch, its commit).
pub fn base_of(root: &Path, base: Option<String>) -> Result<(String, String), String> {
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

/// Colour on a terminal, unless NO_COLOR is set.
pub fn colour() -> bool {
    use std::io::IsTerminal;
    std::io::stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty())
}

/// `s` in the SGR colour `code` (`1` bold, `2` dim, `31` red …), or plain when `colour()` is off.
pub fn paint(code: &str, s: &str) -> String {
    if colour() { format!("\x1b[{code}m{s}\x1b[0m") } else { s.to_string() }
}

fn doctor(base: Option<String>) -> Result<(), String> {
    let mut failed = 0;
    let mut say = |ok: Option<bool>, what: &str, detail: String| {
        failed += (ok == Some(false)) as u32;
        let (mark, code) = match ok { Some(true) => ("✓ ok", "32"), Some(false) => ("✗ FAIL", "1;31"), None => ("! warn", "33") };
        println!("{} {} {detail}", paint(code, &format!("{mark:<6}")), paint("1", &format!("{what:<9}")));
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
    if failed > 0 {
        return Err(format!("{failed} check(s) failed"));
    }
    println!("\n{}", paint("32", "All checks passed."));
    Ok(())
}

fn run(cmd: Cmd) -> Result<(), String> {
    match cmd {
        Cmd::Doctor { base } => doctor(base),
        Cmd::New { title, body, file, base, mcp } => {
            let body = match file {
                Some(f) => std::fs::read_to_string(&f).map_err(|e| format!("{}: {e}", f.display()))?,
                None => body.unwrap_or_default(),
            };
            plan::new(&title, &body, base, mcp)
        }
        Cmd::Reply { n, text, mcp } => plan::reply(n, &text, mcp),
        Cmd::Approve { n, max_usd, mcp } => approve::approve(n, max_usd, mcp),
        Cmd::Show { n } => approve::show(n),
    }
}

const GUIDE: &str = "\
1. Describe a change. rookery writes a plan and a task list, and asks when something is unclear.
2. Answer its questions, or approve the plan. Nothing is edited before you approve.
3. Each task runs in a fresh Claude session in its own worktree; rookery commits each one.
4. An advisory review runs at the end. Nothing is pushed: the branch is yours.";

/// The logo with the name beside it, all at once, when stdout is a colour terminal wide enough:
/// 28 characters wide, from 70 columns; none below that, piped or under NO_COLOR.
fn banner() -> bool {
    if !colour() {
        return false;
    }
    if !matches!(crossterm::terminal::size(), Ok((w, _)) if w >= 70) {
        return false;
    }
    let art = logo::LOGO;
    let mid = art.len() / 2;
    println!();
    for (i, line) in art.iter().enumerate() {
        match i {
            _ if i + 1 == mid => println!("  {line}  \x1b[1mrookery\x1b[0m {}", env!("CARGO_PKG_VERSION")),
            _ if i == mid => println!("  {line}  \x1b[2mplan · approve · task by task\x1b[0m"),
            _ => println!("  {line}"),
        }
    }
    println!();
    true
}

/// The top of the prompt: what rookery does, where it is, and the commands that exist now.
fn guide() {
    if !banner() {
        println!("rookery {}: {}\n", env!("CARGO_PKG_VERSION"), Cli::command().get_about().unwrap());
    }
    println!("{}", paint("1", "How it works"));
    for l in GUIDE.lines() {
        println!("  {l}");
    }
    println!();
    let here = std::env::current_dir().unwrap_or_default();
    match repo(&here).and_then(|r| base_of(&r.root, None).map(|(b, _)| (r, b))) {
        Ok((r, b)) => println!("{}     {} {}", paint("1", "Repo"), paint("36", &r.root.display().to_string()), paint("2", &format!("(base {b})"))),
        Err(e) => println!("{}     {}", paint("1", "Repo"), paint("33", &format!("none: {e}. Start rookery inside a git repo."))),
    }
    println!("\n{}  {}", paint("1", "Commands"), paint("2", "(type /<command> here, or run `rookery <command>` from a shell)"));
    for (name, about) in commands() {
        println!("  {} {about}", paint("38;2;168;204;104", &format!("{name:<9}"))); // the logo's light green
    }
    println!();
}

/// What can be typed at the prompt, as (/name, what it does): clap's commands, then help and exit.
fn commands() -> Vec<(String, String)> {
    let mut all: Vec<_> = Cli::command().get_subcommands()
        .map(|c| (format!("/{}", c.get_name()), c.get_about().map(|a| a.to_string()).unwrap_or_default())).collect();
    all.push(("/help".into(), "Show a command's options: /help <command>".into()));
    all.push(("/exit".into(), "Leave rookery (or Ctrl-D)".into()));
    all
}

/// A typed line -> args: whitespace splits, a quote at the start of a word groups to its match.
/// No escapes: a prompt line, not a shell. A quote inside a word (`don't`) is a letter.
fn words(line: &str) -> Result<Vec<String>, String> {
    let (mut out, mut cur, mut quote, mut any) = (vec![], String::new(), None, false);
    for ch in line.chars() {
        match (quote, ch) {
            (Some(q), c) if c == q => quote = None,
            (Some(_), c) => cur.push(c),
            (None, '"' | '\'') if !any => (quote, any) = (Some(ch), true),
            (None, c) if c.is_whitespace() => if any { out.push(std::mem::take(&mut cur)); any = false },
            (None, c) => (cur.push(c), any = true).1,
        }
    }
    if quote.is_some() { return Err("a quote is not closed".into()) }
    if any { out.push(cur) }
    Ok(out)
}

/// `rookery` alone: the guide, then one command per line until exit or end of input. On a
/// terminal each line is typed in the box (`input`); piped, it is read plainly after `rookery> `.
fn prompt() {
    use std::io::IsTerminal;
    guide();
    let boxed = std::io::stdin().is_terminal() && std::io::stdout().is_terminal();
    let cmds = commands();
    let placeholder = format!("Type / for commands, e.g. {} \"what to change\"", cmds[0].0);
    let (mut lines, mut history) = (std::io::stdin().lock().lines(), Vec::<String>::new());
    loop {
        let line = if boxed {
            match input::line(&history, &placeholder, &cmds) {
                Ok(Some(l)) => l,
                Ok(None) => return,
                Err(e) => { eprintln!("rookery: {e}"); return }
            }
        } else {
            print!("rookery> ");
            let _ = std::io::stdout().flush();
            let Some(Ok(l)) = lines.next() else { println!(); return };
            l
        };
        if history.last() != Some(&line) { history.push(line.clone()) }
        let mut args = match words(&line) {
            Ok(a) => a,
            Err(e) => { eprintln!("rookery: {e}"); continue }
        };
        if args.first().is_some_and(|a| a == "rookery") { args.remove(0); }
        // `/new` as the menu shows it; `new` works too, as from a shell
        if let Some(a) = args.first_mut().filter(|a| a.starts_with('/')) { a.remove(0); }
        match args.first().map(String::as_str) {
            None => continue,
            Some("exit" | "quit") => return,
            _ => {}
        }
        match Cli::try_parse_from(std::iter::once("rookery".to_string()).chain(args)) {
            Ok(Cli { cmd: Some(cmd) }) => if let Err(e) = run(cmd) { eprintln!("rookery: {e}") },
            Ok(Cli { cmd: None }) => {}
            Err(e) => { let _ = e.print(); }
        }
    }
}

fn main() -> ExitCode {
    let Some(cmd) = Cli::parse().cmd else {
        prompt();
        return ExitCode::SUCCESS;
    };
    match run(cmd) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("rookery: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn words_split_like_a_prompt() {
        assert_eq!(super::words(r#"reply 3 "use sqlite" don't 'a b'"#).unwrap(), ["reply", "3", "use sqlite", "don't", "a b"]);
        assert_eq!(super::words(r#"new """#).unwrap(), ["new", ""]);
        assert!(super::words(r#"new "open"#).is_err());
        assert!(super::words("   ").unwrap().is_empty());
    }

    #[test]
    fn logo_lines_are_even() {
        let esc = regex::Regex::new("\x1b\\[[0-9;]*m").unwrap();
        assert_eq!(super::logo::LOGO.len(), 14);
        for l in super::logo::LOGO {
            assert_eq!(esc.replace_all(l, "").chars().count(), 28, "{l:?}");
            assert!(l.ends_with("\x1b[0m"));
        }
    }
}
