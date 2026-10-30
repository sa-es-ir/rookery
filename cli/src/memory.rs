//! Repo memory: agent.py's digest() and remember() on a local repo. One notes file per repo,
//! <git-common-dir>/rookery/memory.md, read at each run's start into every call's system prompt
//! (`memory-note`), and written by the orchestrator from a checked reply after each plan, reply,
//! approve, ask or review that ends ok or failed. The model never writes the file, and its call
//! has no tools and an empty cwd. `"memory": false` in ~/.rookery/config.json turns it all off.
use crate::claude::{self, Job};
use crate::steer::{self, Steer};
use crate::text::{DEFAULTS, cap, fmt, prompt};
use crate::{Repo, paint, repo};
use regex::Regex;
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

static NOTES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)<notes>(.*?)</notes>").unwrap());

/// The last run this command made, which the notes learn from when it ends: (repo, n, run).
static LAST: Mutex<Option<(Repo, u64, u64)>> = Mutex::new(None);

pub fn file(r: &Repo) -> PathBuf {
    r.state.join("memory.md")
}

fn on() -> bool {
    crate::config::load().map_or(true, |c| c.get("memory") != Some(&Value::Bool(false)))
}

pub fn ran(r: &Repo, n: u64, run: u64) {
    *LAST.lock().unwrap() = Some((r.clone(), n, run));
}

/// The notes for a run's calls and what its `base` step says of them. Left out, never failing
/// the run, when CLAUDE.md, the notes and the longest slot instructions would pass the cap.
pub fn load(r: &Repo, s: &Steer) -> (String, Value) {
    let notes = if on() { fs::read_to_string(file(r)).unwrap_or_default() } else { String::new() };
    if notes.is_empty() {
        return (notes, Value::Null);
    }
    let most = steer::slots(&s.workflow).iter().map(|(_, x)| x["instructions"].as_str().unwrap_or("").len()).max().unwrap_or(0);
    if s.claude_md.len() + notes.len() + most > cap("claude_md_max") as usize {
        return (String::new(), json!("left out: over the 64 KB limit"));
    }
    let n = notes.len();
    (notes, json!(n))
}

/// Where claude keeps its transcripts: projects/<cwd>/<session>.jsonl under this.
fn claude_home() -> PathBuf {
    match std::env::var_os("CLAUDE_CONFIG_DIR") {
        Some(d) => d.into(),
        None => PathBuf::from(std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap_or_default()).join(".claude"),
    }
}

/// `s` cut to `n` characters.
fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// agent.py's digest(): one run's claude calls, read back from their transcripts, small enough for
/// haiku. Each tool call (the tool and its path, command or pattern), the first lines of each failed
/// one, each session's last reply. Never file contents or successful output: they are big, and they
/// are where injected text would come from.
pub fn digest(log: &Path, run: u64, projects: &Path) -> String {
    let calls: Vec<Value> = fs::read_to_string(log).unwrap_or_default().lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(|s| s["run"] == run && s["what"] == "claude").collect();
    let mut out = vec![];
    for s in calls {
        let Some(sid) = s["sid"].as_str() else { continue };
        let found = fs::read_dir(projects).into_iter().flatten().flatten()
            .map(|d| d.path().join(format!("{sid}.jsonl"))).find(|p| p.is_file());
        let Some(text) = found.and_then(|p| fs::read(p).ok()).map(|b| String::from_utf8_lossy(&b).into_owned()) else { continue };
        let mut lines = vec![format!("## {} (STATUS: {})", s["key"].as_str().unwrap_or(""), s["status"].as_str().unwrap_or(""))];
        let mut reply = String::new();
        for raw in text.lines() {
            let Ok(o) = serde_json::from_str::<Value>(raw) else { continue };
            for x in o["message"]["content"].as_array().into_iter().flatten() {
                match x["type"].as_str() {
                    Some("tool_use") => {
                        let i = &x["input"];
                        let what = ["file_path", "path", "command", "pattern", "url"].iter().find_map(|k| i[k].as_str())
                            .map_or_else(|| i.to_string(), String::from);
                        lines.push(cut(&format!("- {}: {}", x["name"].as_str().unwrap_or(""), what.split_whitespace().collect::<Vec<_>>().join(" ")), 200));
                    }
                    Some("tool_result") if x["is_error"] == true => {
                        let body = match &x["content"] {
                            Value::Array(a) => a.iter().map(|b| b["text"].as_str().unwrap_or("")).collect::<Vec<_>>().join("\n"),
                            Value::String(s) => s.clone(),
                            v => v.to_string(),
                        };
                        lines.extend(body.lines().take(5).map(|l| cut(&format!("    ! {l}"), 200)));
                    }
                    Some("text") if o["type"] == "assistant" => reply = x["text"].as_str().unwrap_or("").to_string(),
                    _ => {}
                }
            }
        }
        lines.push(format!("Reply:\n{}", cut(&reply, 3000)));
        out.push(lines.join("\n"));
    }
    let d = out.join("\n\n");
    let max = cap("digest_cap") as usize;
    // ponytail: keeps the tail, where a failure is; a per-call budget if long runs lose their start
    if d.len() <= max { d } else { format!("(earlier calls cut)\n\n{}", claude::tail(&d, max)) }
}

/// The notes between <notes> tags of a checked reply, or why there are none.
fn notes_in(status: &str, text: &str, what: &str) -> Result<String, String> {
    if status != "memory-updated" {
        return Err(format!("{what} replied `STATUS: {status}`; it replies memory-updated"));
    }
    NOTES.captures(text).map(|m| m[1].trim().to_string()).ok_or_else(|| format!("{what} has no <notes>…</notes>; the old notes stay"))
}

/// Notes still over the cap after the rewrites: cut at the last line break under it, never half a bullet.
fn fit(notes: String, max: usize) -> String {
    if notes.chars().count() <= max {
        return notes;
    }
    let head = cut(&notes, max);
    head.rfind('\n').map_or(head.clone(), |at| head[..at].to_string())
}

/// After the command: fold its run's digest into the notes. A side channel: a failure is one dim
/// line, never the command's, and a cancelled run teaches nothing.
pub fn after() {
    let Some((r, n, source)) = LAST.lock().unwrap().take() else { return };
    if !on() || crate::cancel::cancelled() {
        return;
    }
    let d = digest(&r.state.join("runs.jsonl"), source, &claude_home().join("projects"));
    if d.is_empty() {
        return; // no claude call to learn from (an "Already reviewed"), or no transcript
    }
    println!("\n{}", paint("2", "Updating the repo notes …"));
    match remember(&r, n, source, &d) {
        Ok(Some(chars)) => println!("{}", paint("2", &format!("notes: updated, {chars} characters (rookery memory)"))),
        Ok(None) => println!("{}", paint("2", "notes: unchanged")),
        Err(e) => println!("{}", paint("2", &format!("notes: not updated: {}", e.lines().next().unwrap_or("")))),
    }
}

/// agent.py's remember(): one haiku call with no tools in an empty dir, then the orchestrator
/// checks the reply and writes the file. -> the new notes' length, or None when unchanged.
fn remember(r: &Repo, n: u64, source: u64, d: &str) -> Result<Option<usize>, String> {
    let run = (claude::now() * 1000.0) as u64;
    let mut job = Job {
        repo_key: r.key.clone(), n, run,
        scratch: std::env::temp_dir().join(format!("rookery-{}-memory-{run}", r.key)),
        log: Some(r.state.join("runs.jsonl")),
        ..Default::default()
    };
    job.step("begin", json!({"kind": "memory", "source": source}));
    let out = (|| {
        let cwd = job.scratch.join("cwd"); // nothing to read, nothing to edit
        fs::create_dir_all(&cwd).map_err(|e| e.to_string())?;
        let f = file(r);
        let old = fs::read_to_string(&f).unwrap_or_default();
        let name = r.root.file_name().map_or(String::new(), |s| s.to_string_lossy().to_string());
        let (max, slot) = (cap("memory_cap") as usize, &DEFAULTS["memory_slot"]);
        let p = fmt(&prompt("remember"), &[("repo", name.clone()), ("cap", max.to_string()), ("notes", old), ("digest", d.into())]);
        let (status, text) = claude::claude(&mut job, &p, "memory", false, slot, true, &cwd)?;
        if status == "memory-unchanged" {
            return Ok(None);
        }
        let mut notes = notes_in(&status, &text, "the memory call")?;
        for _ in 0..cap("memory_rewrites") {
            if notes.chars().count() <= max {
                break;
            }
            let p = fmt(&prompt("rewrite"), &[("repo", name.clone()), ("cap", max.to_string()),
                                              ("n", notes.chars().count().to_string()), ("notes", notes.clone())]);
            let (status, text) = claude::claude(&mut job, &p, "memory-rewrite", false, slot, true, &cwd)?;
            notes = notes_in(&status, &text, "the memory rewrite")?;
        }
        let notes = fit(notes, max); // length never fails the job
        let tmp = f.with_extension("tmp"); // a read never sees half a file
        fs::write(&tmp, &notes).and_then(|_| fs::rename(&tmp, &f)).map_err(|e| e.to_string())?;
        let chars = notes.chars().count();
        job.step("remembered", json!({"chars": chars, "source": source}));
        Ok(Some(chars))
    })();
    let _ = fs::remove_dir_all(&job.scratch);
    out.inspect_err(|e: &String| {
        let (what, e) = crate::cancel::why(e.clone());
        job.step(what, json!({"error": e}));
    })
}

/// `rookery memory`: the notes the agents read at each run's start, or `--clear` to forget them.
pub fn memory(clear: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let f = file(&r);
    if clear {
        return match fs::remove_file(&f) {
            Ok(()) => Ok(println!("Cleared the repo notes ({}).", f.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(println!("There are no repo notes to clear.")),
            Err(e) => Err(format!("{}: {e}", f.display())),
        };
    }
    if !on() {
        println!("{} \"memory\": false in {}: runs neither read nor write notes.", paint("33", "off:"), crate::config::path().display());
    }
    match fs::read_to_string(&f) {
        Ok(t) => {
            println!("{}  {}\n", paint("1", "Repo notes"), paint("2", &format!("({} characters, {})", t.chars().count(), f.display())));
            crate::plan::print_md(&t);
            println!("\n{}", paint("2", "Advisory: the code and CLAUDE.md win over them. Forget them with: rookery memory --clear"));
        }
        Err(_) => println!("No repo notes yet. Rookery writes them after each run, at {}.", f.display()),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_keeps_calls_errors_and_the_last_reply() {
        let d = std::env::temp_dir().join(format!("rookery-digest-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(d.join("projects/C--repo")).unwrap();
        let log = d.join("runs.jsonl");
        fs::write(&log, [
            json!({"run": 5, "what": "claude", "key": "task-0", "status": "task-done", "sid": "s1"}),
            json!({"run": 5, "what": "commit"}),
            json!({"run": 6, "what": "claude", "key": "plan", "status": "questions", "sid": "s2"}),   // another run
        ].map(|v| v.to_string()).join("\n")).unwrap();
        fs::write(d.join("projects/C--repo/s1.jsonl"), [
            json!({"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "cargo   test\n --all"}}]}}),
            json!({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": true, "content": "error[E0433]\nline 2"},
                {"type": "tool_result", "content": "SECRET FILE CONTENTS"}]}}),
            json!({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "src/a.rs"}},
                {"type": "text", "text": "Done.\nSTATUS: task-done"}]}}),
        ].map(|v| v.to_string()).join("\n") + "\nnot json\n").unwrap();
        let got = digest(&log, 5, &d.join("projects"));
        assert_eq!(got, "## task-0 (STATUS: task-done)\n- Bash: cargo test --all\n    ! error[E0433]\n    ! line 2\n- Read: src/a.rs\n\
                         Reply:\nDone.\nSTATUS: task-done");
        assert!(digest(&log, 6, &d.join("projects")).is_empty(), "no transcript: skipped");
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn notes_are_checked_and_fit() {
        assert_eq!(notes_in("memory-updated", "x <notes>\n- a\n</notes> y", "m").unwrap(), "- a");
        assert!(notes_in("memory-updated", "no tags", "m").unwrap_err().contains("old notes stay"));
        assert!(notes_in("maybe", "<notes>a</notes>", "m").is_err());
        assert_eq!(fit("- one\n- two\n- three".into(), 12), "- one\n- two", "cut at a line break");
        assert_eq!(fit("short".into(), 12), "short");
    }
}
