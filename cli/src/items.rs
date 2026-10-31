//! `rookery ls`, `drop` and `log`: the repo's items, retention and the run log.
use crate::plan::{dirty, item, load, phase_paint, save};
use crate::steer::{git, git_text};
use crate::text::{fit, read_tasks};
use crate::{Repo, approve, paint, repo};
use serde_json::{Value, json};
use std::fs;
use std::path::PathBuf;

fn here() -> Result<Repo, String> {
    repo(&std::env::current_dir().map_err(|e| e.to_string())?)
}

/// One item on the board.
pub struct Card {
    pub n: u64,
    pub title: String,
    pub phase: String,
    pub tasks: String,
    pub next: String,
}

/// The board's groups, in order: Running (another rookery holds the lock), Needs you, Done. An
/// empty group is left out.
pub fn board(r: &Repo) -> Vec<(&'static str, Vec<Card>)> {
    let mut items: Vec<(Value, std::time::SystemTime)> = fs::read_dir(r.state.join("items")).into_iter().flatten().flatten()
        .filter_map(|e| {
            let p = e.path().join("state.json");
            let st = serde_json::from_str::<Value>(&fs::read_to_string(&p).ok()?).ok()?;
            Some((st, fs::metadata(&p).and_then(|m| m.modified()).ok()?))
        })
        .filter(|(st, _)| st["phase"] != "dropped").collect();
    items.sort_by_key(|(st, _)| std::cmp::Reverse(st["n"].as_u64()));
    let mut groups: Vec<(&'static str, Vec<Card>)> = vec![("Running", vec![]), ("Needs you", vec![]), ("Done", vec![])];
    for (st, at) in items {
        let n = st["n"].as_u64().unwrap_or(0);
        let d = item(r, n);
        // no lock file: never run; held: running now, as drop_item checks
        let running = fs::OpenOptions::new().write(true).open(d.join("lock"))
            .is_ok_and(|f| matches!(f.try_lock(), Err(fs::TryLockError::WouldBlock)));
        let tasks = fs::read_to_string(d.join("tasks.md")).map(|t| read_tasks(&t)).unwrap_or_default();
        let phase = st["phase"].as_str().unwrap_or("").to_string();
        let mut done = if tasks.is_empty() { String::new() } else { format!("{}/{} tasks", tasks.iter().filter(|t| t.1).count(), tasks.len()) };
        if phase == "done" {
            let days = at.elapsed().map_or(0, |e| e.as_secs() / 86400);
            let age = match days { 0 => "today".into(), 1 => "1 day ago".into(), d => format!("{d} days ago") };
            done = if done.is_empty() { age } else { format!("{done} · {age}") };
        }
        let g = if running { 0 } else if phase == "done" { 2 } else { 1 };
        groups[g].1.push(Card { n, title: st["title"].as_str().unwrap_or("").into(), next: approve::next(&st, running), phase, tasks: done });
    }
    groups.retain(|(_, cards)| !cards.is_empty());
    groups
}

/// The board, under group headers. Without colour (piped, NO_COLOR) one plain line per item and no
/// headers, so `grep` reads it; a running item says so in its last column.
pub fn print_board(groups: &[(&str, Vec<Card>)]) {
    let cards = || groups.iter().flat_map(|(_, c)| c);
    let tw = cards().map(|c| c.title.chars().count()).max().unwrap_or(0).min(40);
    let kw = cards().map(|c| c.tasks.chars().count()).max().unwrap_or(0);
    let on = crate::colour();
    for (name, cards) in groups {
        if on {
            println!("  {}", paint("1", name));
        }
        for c in cards {
            let next = if *name == "Running" && !on { "running" } else { &c.next };
            // padded before it is painted: the colour codes would count as width
            println!("{}{}  {}  {}{}  {}  {}", if on { "    " } else { "" }, paint("1", &format!("{:<4}", format!("#{}", c.n))),
                     fit(&c.title, tw), phase_paint(&c.phase), " ".repeat(18usize.saturating_sub(c.phase.len())),
                     fit(&c.tasks, kw), paint("2", next));
        }
        if on {
            println!();
        }
    }
}

/// `rookery ls`: every item that is not dropped, as the board.
pub fn ls() -> Result<(), String> {
    let b = board(&here()?);
    if b.is_empty() {
        println!("No items in this repo. Start one: rookery new \"what to change\"");
    }
    print_board(&b);
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
    fn board_groups_by_what_each_needs() {
        let d = std::env::temp_dir().join(format!("rookery-board-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        let r = Repo { root: d.clone(), state: d.clone(), key: "k".into() };
        let add = |n: u64, phase: &str| {
            fs::create_dir_all(item(&r, n)).unwrap();
            fs::write(item(&r, n).join("state.json"), json!({"n": n, "phase": phase, "approved": true, "title": format!("t{n}")}).to_string()).unwrap();
        };
        for (n, p) in [(1, "done"), (2, "awaiting-approval"), (3, "implementing"), (4, "dropped"), (5, "implementing"), (6, "error")] {
            add(n, p);
        }
        fs::write(item(&r, 5).join("tasks.md"), "- [x] a\n- [ ] b\n").unwrap();
        let held = fs::OpenOptions::new().create(true).truncate(false).write(true).open(item(&r, 5).join("lock")).unwrap();
        held.lock().unwrap();
        let got: Vec<(&str, Vec<u64>)> = board(&r).iter().map(|(g, c)| (*g, c.iter().map(|c| c.n).collect())).collect();
        assert_eq!(got, [("Running", vec![5]), ("Needs you", vec![6, 3, 2]), ("Done", vec![1])], "a stopped run needs you; dropped is gone");
        let b = board(&r);
        assert_eq!((b[0].1[0].tasks.as_str(), b[0].1[0].next.as_str()), ("1/2 tasks", ""));
        assert_eq!(b[2].1[0].tasks, "today");
        drop(held);
        let _ = fs::remove_dir_all(&d);
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
