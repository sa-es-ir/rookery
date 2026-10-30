//! `rookery ls`, `drop` and `log`: the repo's items, retention and the run log.
use crate::plan::{dirty, item, load, phase_paint, save};
use crate::steer::{git, git_text};
use crate::text::read_tasks;
use crate::{Repo, paint, repo};
use serde_json::{Value, json};
use std::fs;
use std::path::PathBuf;

fn here() -> Result<Repo, String> {
    repo(&std::env::current_dir().map_err(|e| e.to_string())?)
}

/// `rookery ls`: every item that is not dropped, with its phase and tasks.
pub fn ls() -> Result<(), String> {
    let r = here()?;
    let mut items: Vec<Value> = fs::read_dir(r.state.join("items")).into_iter().flatten().flatten()
        .filter_map(|e| fs::read_to_string(e.path().join("state.json")).ok())
        .filter_map(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|st| st["phase"] != "dropped").collect();
    if items.is_empty() {
        println!("No items in this repo. Start one: rookery new \"what to change\"");
        return Ok(());
    }
    items.sort_by_key(|st| st["n"].as_u64());
    for st in items {
        let n = st["n"].as_u64().unwrap_or(0);
        let tasks = fs::read_to_string(item(&r, n).join("tasks.md")).map(|t| read_tasks(&t)).unwrap_or_default();
        let done = if tasks.is_empty() { String::new() } else { format!("{}/{} tasks", tasks.iter().filter(|t| t.1).count(), tasks.len()) };
        let phase = st["phase"].as_str().unwrap_or("");
        // padded before it is painted: the colour codes would count as width
        println!("{} {}{} {:<10} {}",paint("1", &format!("{:>4}", format!("#{n}"))), phase_paint(phase), " ".repeat(18usize.saturating_sub(phase.len())),
                 done, st["title"].as_str().unwrap_or(""));
    }
    Ok(())
}

/// The branch's commits that are on no other branch and no remote: work that exists only there.
fn only_on(root: &std::path::Path, branch: &str) -> Result<bool, String> {
    // --exclude names a branch without refs/heads/, as --branches reads it
    let out = git_text(root, &["rev-list", "-1", &format!("refs/heads/{branch}"), "--not",
                               &format!("--exclude={branch}"), "--branches", "--remotes"])?;
    Ok(!out.is_empty())
}

/// `rookery drop`: the item's worktree and branch go, and the item leaves `ls`. Refused while it
/// runs, and, unless --force, while the worktree has uncommitted changes or the branch has
/// commits that are on no other branch and no remote (merge it first): retention never deletes
/// work. Its number is never used again, so no later item resumes its sessions.
pub fn drop_item(n: u64, force: bool) -> Result<(), String> {
    let r = here()?;
    let mut st = load(&r, n)?;
    let lock = fs::OpenOptions::new().create(true).truncate(false).write(true).open(item(&r, n).join("lock")).map_err(|e| e.to_string())?;
    if matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock)) {
        return Err(format!("#{n} is running in another rookery now. Wait for it to end, or stop it with Ctrl-C there."));
    }
    let (branch, wt) = (st["branch"].as_str().unwrap_or("").to_string(), PathBuf::from(st["worktree"].as_str().unwrap_or("")));
    let has_branch = git(&r.root, &["rev-parse", "--verify", "--quiet", &format!("refs/heads/{branch}")]).is_ok();
    if !force {
        if wt.is_dir() && let Ok(d) = dirty(&wt) && !d.is_empty() {
            return Err(format!("#{n}'s worktree has {} uncommitted change(s) ({}). Commit or discard them in {}, \
                                or drop it anyway: rookery drop {n} --force", d.len(), d[..d.len().min(5)].join(", "), wt.display()));
        }
        if has_branch && only_on(&r.root, &branch)? {
            return Err(format!("{branch} has commits that are on no other branch and no remote, so dropping #{n} would lose them. \
                                Merge it into {} (or push it) first, or drop it anyway: rookery drop {n} --force",
                               st["base"].as_str().unwrap_or("your branch")));
        }
    }
    if wt.exists() {
        git(&r.root, &["worktree", "remove", "--force", &wt.to_string_lossy()])?;
    }
    git(&r.root, &["worktree", "prune"])?;
    if has_branch {
        git(&r.root, &["branch", "-D", &branch])?;
    }
    st["phase"] = json!("dropped");
    save(&r, &st)?;
    println!("Dropped #{n}: removed {}{}. Its plan stays in {}.", wt.display(),
             if has_branch { format!(" and the branch {branch}") } else { String::new() }, item(&r, n).display());
    Ok(())
}

/// How long ago `at` (epoch seconds) was, in a few characters.
fn ago(at: f64) -> String {
    let s = (crate::claude::now() - at).max(0.0) as u64;
    match s {
        0..60 => format!("{s} s ago"),
        60..3600 => format!("{} min ago", s / 60),
        3600..86400 => format!("{} h ago", s / 3600),
        _ => format!("{} d ago", s / 86400),
    }
}

/// One step in a few words, for `rookery log <n>`; None for the ones a reader does not need.
fn describe(s: &Value) -> Option<String> {
    let f = |k: &str| s[k].as_str().map(String::from).unwrap_or_else(|| s[k].to_string());
    Some(match s["what"].as_str()? {
        "base" => format!("steering from {} at {}", f("base"), s["sha"].as_str().unwrap_or("").get(..12).unwrap_or("")),
        "phase" => format!("phase {}", f("phase")),
        "claude" => format!("claude {} → {}  ${:.2} · {:.0} s{}", f("key"), f("status"), s["usd"].as_f64().unwrap_or(0.0),
                            s["secs"].as_f64().unwrap_or(0.0), s["agent"].as_str().map(|a| format!("  (rk:{a})")).unwrap_or_default()),
        "task" => format!("task {}/{}: {}", s["i"], s["of"], crate::text::summary(s["text"].as_str().unwrap_or(""))),
        "commit" => format!("commit {}", f("sha")),
        "reverted" => format!("reverted {} path(s) the agent left", s["paths"].as_array().map_or(0, Vec::len)),
        "step" => format!("step {} ({})", f("agent"), f("mode")),
        "posted" => format!("review {}", f("verdict")),
        "skipped" => "already reviewed: no call".into(),
        "remembered" => format!("notes updated, {} characters", s["chars"]),
        "error" | "cancelled" => format!("{}: {}", s["what"].as_str()?, s["error"].as_str().unwrap_or("").lines().next().unwrap_or("")),
        _ => return None,
    })
}

/// `rookery log`: the last runs, one line each; with <n>, every run of that item and its steps.
pub fn log(n: Option<u64>) -> Result<(), String> {
    let r = here()?;
    let text = fs::read_to_string(r.state.join("runs.jsonl")).unwrap_or_default();
    // runs in the order they started; a run's steps are on lines with its id
    let mut runs: Vec<(u64, Vec<Value>)> = vec![];
    for s in text.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok()) {
        let id = s["run"].as_u64().unwrap_or(0);
        match runs.iter_mut().find(|(r, _)| *r == id) {
            Some((_, steps)) => steps.push(s),
            None => runs.push((id, vec![s])),
        }
    }
    runs.retain(|(_, steps)| n.is_none_or(|n| steps[0]["n"].as_u64() == Some(n)));
    if runs.is_empty() {
        println!("{}", if n.is_some() { "No runs of that item." } else { "No runs in this repo yet." });
        return Ok(());
    }
    let shown = if n.is_some() { runs.len() } else { runs.len().min(20) };
    for (_, steps) in &runs[runs.len() - shown..] {
        let (first, last) = (&steps[0], &steps[steps.len() - 1]);
        let kind = steps.iter().find(|s| s["what"] == "begin").and_then(|s| s["kind"].as_str()).unwrap_or("run");
        let status = if steps.iter().any(|s| s["what"] == "cancelled") { paint("33", "cancelled") }
                     else if steps.iter().any(|s| s["what"] == "error") { paint("31", "failed") } else { paint("32", "ok") };
        let usd: f64 = steps.iter().filter(|s| s["what"] == "claude").filter_map(|s| s["usd"].as_f64()).sum();
        let secs = last["at"].as_f64().unwrap_or(0.0) - first["at"].as_f64().unwrap_or(0.0);
        let phase = steps.iter().rev().find(|s| s["what"] == "phase").and_then(|s| s["phase"].as_str())
            .map(|p| format!("  → {p}")).unwrap_or_default();
        let who = match first["n"].as_u64() { Some(0) | None => "    ".to_string(), Some(n) => format!("{:>4}", format!("#{n}")) };
        println!("{}  {} {:<8} {status}  {}{phase}", paint("2", &format!("{:>10}", ago(first["at"].as_f64().unwrap_or(0.0)))),
                 paint("1", &who), kind, paint("2", &format!("${usd:.2} · {secs:.0} s")));
        if n.is_some() {
            for s in steps.iter().filter_map(describe) {
                println!("              {}", paint("2", &s));
            }
        }
    }
    if n.is_none() && runs.len() > shown {
        println!("{}", paint("2", &format!("({} older runs; rookery log <n> shows every run of one item)", runs.len() - shown)));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_on_finds_work_nowhere_else() {
        let d = std::env::temp_dir().join(format!("rookery-drop-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        let g = |a: &[&str]| { git(&d, &[&["-c", "user.name=t", "-c", "user.email=t@t"][..], a].concat()).unwrap(); };
        g(&["init", "-q", "-b", "main"]);
        g(&["commit", "-q", "--allow-empty", "-m", "base"]);
        g(&["branch", "rookery/1-x"]);
        assert!(!only_on(&d, "rookery/1-x").unwrap(), "nothing past base");
        g(&["checkout", "-q", "rookery/1-x"]);
        g(&["commit", "-q", "--allow-empty", "-m", "#1 task 1: x"]);
        assert!(only_on(&d, "rookery/1-x").unwrap(), "a task commit on no other branch");
        g(&["checkout", "-q", "main"]);
        g(&["merge", "-q", "--ff-only", "rookery/1-x"]);
        assert!(!only_on(&d, "rookery/1-x").unwrap(), "merged: safe to drop");
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn steps_read_in_a_few_words() {
        assert_eq!(describe(&json!({"what": "claude", "key": "task-0", "status": "task-done", "usd": 0.123, "secs": 41.6, "agent": null})).unwrap(),
                   "claude task-0 → task-done  $0.12 · 42 s");
        assert_eq!(describe(&json!({"what": "cancelled", "error": "Cancelled with Ctrl-C.\nmore"})).unwrap(), "cancelled: Cancelled with Ctrl-C.");
        assert!(describe(&json!({"what": "begin", "kind": "plan"})).is_none());
        assert_eq!(ago(crate::claude::now() - 7200.0), "2 h ago");
    }
}
