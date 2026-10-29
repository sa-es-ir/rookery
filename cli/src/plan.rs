//! `rookery new` and `rookery reply`: agent.py's plan() on a local item. The item's state is
//! items/<n>/state.json (the server's phase labels, without `rookery:`); its plan files sit beside
//! it, outside the worktree, never committed.
use crate::claude::{self, Job};
use crate::steer::{self, git, git_text};
use crate::text::{cap, fmt, prompt, read_tasks};
use crate::{Repo, base_of, paint, repo};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};

/// agent.py's plan() says this at the end of a plan; here the next command is printed instead
const REPLY_APPROVE: &str = "Reply `approve` to implement.";

/// The next item number. `next` holds it, and making items/<n> claims it: two `new`s at once
/// never share a number, and a lost `next` never reuses one.
pub fn claim(state: &Path) -> Result<u64, String> {
    let items = state.join("items");
    fs::create_dir_all(&items).map_err(|e| e.to_string())?;
    let mut n = fs::read_to_string(state.join("next")).ok().and_then(|t| t.trim().parse().ok()).unwrap_or(1u64).max(1);
    loop {
        match fs::create_dir(items.join(n.to_string())) {
            Ok(()) => break,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => n += 1,
            Err(e) => return Err(format!("could not make item {n}: {e}")),
        }
    }
    fs::write(state.join("next"), format!("{}\n", n + 1)).map_err(|e| e.to_string())?;
    Ok(n)
}

/// A branch-safe slug of the title: ASCII letters and digits, dashes between, at most 40
/// characters, cut at a dash. A title with none of those gives "".
pub fn slug(title: &str) -> String {
    let mut s = String::new();
    for c in title.chars().flat_map(char::to_lowercase) {
        if c.is_ascii_alphanumeric() {
            s.push(c);
        } else if !s.is_empty() && !s.ends_with('-') {
            s.push('-');
        }
    }
    if s.len() > 40 {
        s.truncate(40);
        if let Some(at) = s.rfind('-') {
            s.truncate(at);
        }
    }
    s.trim_end_matches('-').to_string()
}

/// The command that starts item `st` again as a new one: same title, body (items/<n>/issue.md
/// in `dir`) and base. What a failed or stopped item offers, since it takes no reply.
pub fn again(st: &Value, dir: &Path) -> String {
    let body = if st["body"].as_str().is_some_and(|b| !b.is_empty()) { format!(" -f \"{}\"", dir.join("issue.md").display()) } else { String::new() };
    format!("rookery new \"{}\"{body} --base {}", st["title"].as_str().unwrap_or(""), st["base"].as_str().unwrap_or(""))
}

/// A reply re-plans only an item that waits for one, as the server takes replies only in the
/// awaiting-* phases. Any other phase says what happened and what to do instead.
pub fn may_reply(st: &Value, again: &str) -> Result<(), String> {
    let n = st["n"].as_u64().unwrap_or(0);
    match st["phase"].as_str().unwrap_or("") {
        "awaiting-input" | "awaiting-approval" => Ok(()),
        "planning" => Err(format!(
            "#{n} is still planning. Either another rookery is planning it now, or its last plan call was \
             stopped before it ended (Ctrl-C, a closed terminal).\n\nIf one is running, wait for it. If not, \
             start the item again:\n  {again}")),
        "error" if st["approved"] == true => Err(format!(
            "#{n}'s plan is approved, so a reply cannot change it. Its last run failed:\n  {}\n\n\
             Fix the cause if it is yours, then run it again from where it stopped:\n  rookery approve {n}",
            st["error"].as_str().unwrap_or("(no detail was recorded)"))),
        "error" => Err(format!(
            "#{n} failed, so it takes no reply. What happened:\n  {}\n\nFix the cause if it is yours, then start the item again:\n  {again}",
            st["error"].as_str().unwrap_or("(no detail was recorded)"))),
        p => Err(format!("#{n} is {p}: its plan is approved, so a reply cannot change it.")),
    }
}

pub fn item(r: &Repo, n: u64) -> PathBuf {
    r.state.join("items").join(n.to_string())
}

pub fn load(r: &Repo, n: u64) -> Result<Value, String> {
    let t = fs::read_to_string(item(r, n).join("state.json")).map_err(|_| format!("there is no item #{n} in this repo"))?;
    serde_json::from_str(&t).map_err(|e| format!("items/{n}/state.json: {e}"))
}

pub fn save(r: &Repo, st: &Value) -> Result<(), String> {
    let n = st["n"].as_u64().unwrap();
    fs::write(item(r, n).join("state.json"), serde_json::to_string_pretty(st).unwrap() + "\n").map_err(|e| e.to_string())
}

/// The item's worktree, made from its base commit the first time. Made again on its branch if
/// someone removed the directory; `-b`, never `-B`: an existing branch is never reset.
pub fn worktree(r: &Repo, st: &Value) -> Result<PathBuf, String> {
    let wt = PathBuf::from(st["worktree"].as_str().unwrap());
    if !wt.exists() {
        git(&r.root, &["worktree", "prune"])?;
        let (branch, w) = (st["branch"].as_str().unwrap(), wt.to_string_lossy());
        if git(&r.root, &["rev-parse", "--verify", "--quiet", &format!("refs/heads/{branch}")]).is_ok() {
            git(&r.root, &["worktree", "add", &w, branch])?;
        } else {
            git(&r.root, &["worktree", "add", "-b", branch, &w, st["sha"].as_str().unwrap()])?;
        }
    }
    Ok(wt)
}

/// agent.py's keep_plans_only(keep=()): the plan files live outside the worktree here, so
/// nothing in it is the plan's. Reverts whatever the agent left, never raises over it (a reply
/// loop calls this again and again) -> the reverted paths.
pub fn keep_plans_only(wt: &Path) -> Result<Vec<String>, String> {
    // -uall: plain --porcelain collapses an untracked tree to its top dir. -z: paths as they
    // are (no quoting), and a rename's two sides as two fields, new first.
    let out = git(wt, &["status", "--porcelain", "-uall", "-z"])?;
    let out = String::from_utf8_lossy(&out);
    let mut fields = out.split('\0').filter(|f| f.len() > 3);
    let mut reverted = vec![];
    while let Some(f) = fields.next() {
        let (code, path) = (&f[..2], &f[3..]);
        let mut paths = vec![path];
        if code.starts_with(['R', 'C']) {
            paths.extend(fields.next());
        }
        for p in paths {
            // clean takes untracked files; restore (no-overlay) puts index and file back to
            // HEAD, and drops a staged file that HEAD does not have
            match code {
                "??" => git(wt, &["clean", "-fdq", "--", p])?,
                _ => git(wt, &["restore", "--source=HEAD", "--staged", "--worktree", "--", p])?,
            };
            reverted.push(p.to_string());
        }
    }
    Ok(reverted)
}

/// The phase in its colour: red failed, yellow waits on the user, green otherwise.
pub fn phase_paint(phase: &str) -> String {
    paint(match phase { "error" => "1;31", "awaiting-input" | "changes-wanted" => "1;33", _ => "1;32" }, phase)
}

/// An agent's markdown on the terminal, its headings bold.
pub fn print_md(text: &str) {
    for line in text.lines() {
        println!("{}", if line.starts_with('#') { paint("1", line) } else { line.to_string() });
    }
}

pub fn set_phase(r: &Repo, job: &Job, st: &mut Value, phase: &str) -> Result<(), String> {
    st["phase"] = json!(phase);
    if phase != "error" {
        st.as_object_mut().unwrap().remove("error");
    }
    job.step("phase", json!({"phase": phase}));
    save(r, st)
}

/// `rookery new`: a new item from its base commit, then the first plan call.
pub fn new(title: &str, body: &str, base: Option<String>, mcp: bool) -> Result<(), String> {
    if title.trim().is_empty() {
        return Err("give the item a title: rookery new \"what to change\"".into());
    }
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let (base, sha) = base_of(&r.root, base)?;
    // raw, never git_text(): its trim takes the first line's leading space, and with it a letter
    // of that path (" M Directory.Packages.props" -> "irectory.Packages.props")
    let out = git(&r.root, &["status", "--porcelain", "-z"])?;
    let out = String::from_utf8_lossy(&out);
    let (mut fields, mut dirty) = (out.split('\0').filter(|f| !f.is_empty()), Vec::<String>::new());
    while let Some(f) = fields.next() {
        if f.starts_with(['R', 'C']) {
            fields.next(); // a rename's old side
        }
        dirty.extend(f.get(3..).map(String::from));
    }
    if !dirty.is_empty() {
        let more = if dirty.len() > 10 { format!(" and {} more", dirty.len() - 10) } else { String::new() };
        eprintln!("{} {} uncommitted change(s) here: {}{more}. The agent starts from {base}'s last commit and does not see them.",
                  paint("33", "warning:"), dirty.len(), dirty[..dirty.len().min(10)].join(", "));
    }
    let n = claim(&r.state)?;
    let slug = slug(title);
    let name = r.root.file_name().map_or("repo".into(), |s| s.to_string_lossy().to_string());
    let st = json!({
        "n": n, "title": title.trim(), "body": body, "base": base, "sha": sha,
        "branch": if slug.is_empty() { format!("rookery/{n}") } else { format!("rookery/{n}-{slug}") },
        "worktree": steer::rookery_home().join("worktrees").join(format!("{name}-{}", r.key)).join(n.to_string()),
        "phase": "planning", "made": claude::now(),
    });
    save(&r, &st)?;
    if !body.is_empty() {
        // for `again()`: a failed item is started over from the same body
        fs::write(item(&r, n).join("issue.md"), body).map_err(|e| e.to_string())?;
    }
    println!("{} {}  {}", paint("1", &format!("#{n}")), title.trim(), paint("2", &format!("({} from {base} at {})", st["branch"].as_str().unwrap(), &sha[..12])));
    plan(&r, n, body, false, mcp)
}

/// `rookery reply`: the answer goes into the same planning session.
pub fn reply(n: u64, text: &str, mcp: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let st = load(&r, n)?;
    may_reply(&st, &again(&st, &item(&r, n)))?;
    if text.trim().is_empty() {
        return Err(format!("say something: rookery reply {n} \"your answer\""));
    }
    plan(&r, n, text, true, mcp)
}

/// agent.py's plan(): steering from base, one plan-slot call, everything but the plan files
/// reverted, then awaiting-input or awaiting-approval. A failure leaves the item in `error`.
fn plan(r: &Repo, n: u64, body: &str, resume: bool, mcp: bool) -> Result<(), String> {
    let mut st = load(r, n)?;
    let run = (claude::now() * 1000.0) as u64;
    let mut job = Job {
        repo_key: r.key.clone(), n, run, mcp,
        add_dirs: vec![item(r, n)],
        scratch: std::env::temp_dir().join(format!("rookery-{}-{n}-{run}", r.key)),
        log: Some(r.state.join("runs.jsonl")),
        ..Default::default()
    };
    let started = std::time::Instant::now();
    let out = (|| {
        // what steers the agent: the base branch as it is now, read once, never the worktree
        let (base, sha) = base_of(&r.root, st["base"].as_str().map(String::from))?;
        let plug = job.scratch.join("plugin");
        let s = steer::steer(&r.root, &base, &sha, &steer::rookery_home().join("defaults"), &plug)?;
        job.step("base", json!({"base": base, "sha": sha, "claude_md": s.claude_md.len(), "sources": s.sources, "agents": s.agent_sources}));
        (job.base, job.claude_md, job.sources, job.agents, job.plugin) = (base, s.claude_md, s.sources, s.agents, Some(plug));
        set_phase(r, &job, &mut st, "planning")?;
        let wt = worktree(r, &st)?;
        let d = item(r, n).display().to_string();
        let source = format!("task #{n}");
        let p = if resume {
            fmt(&prompt("reply"), &[("source", source), ("body", body.into()), ("d", d)])
        } else {
            let repo = r.root.file_name().map_or(String::new(), |s| s.to_string_lossy().to_string());
            fmt(&prompt("plan"), &[("source", source), ("repo", repo), ("title", st["title"].as_str().unwrap().into()),
                                   ("body", body.into()), ("d", d), ("max", cap("max_tasks").to_string()), ("box", "this machine".into())])
        };
        println!("{}", paint("2", &format!("Planning with {} …", s.workflow["plan"]["agent"].as_str().map_or("sonnet".into(), |a| format!("rk:{a}")))));
        let said = claude::claude(&mut job, &p, "plan", resume, &s.workflow["plan"], false, &wt);
        // after every plan call, failed or not: planning writes the plan files and nothing else
        let reverted = keep_plans_only(&wt)?;
        if !reverted.is_empty() {
            job.step("reverted", json!({"paths": reverted}));
            eprintln!("{} the planner touched {} in the worktree; reverted.", paint("33", "note:"), reverted.join(", "));
        }
        let (status, msg) = said?;
        let msg = msg.replace(REPLY_APPROVE, "").trim_end().to_string();
        let tasks = || fs::read_to_string(item(r, n).join("tasks.md"))
            .map(|t| read_tasks(&t)).map_err(|_| "tasks.md is missing: the plan produced no task list".to_string());
        let out = item(r, n).join("out").join("plan.md");
        fs::create_dir_all(out.parent().unwrap()).map_err(|e| e.to_string())?;
        fs::write(&out, &msg).map_err(|e| e.to_string())?;
        let (phase, msg) = match status.as_str() {
            "questions" => ("awaiting-input", msg),
            // nothing to approve: approve would run no task and commit nothing
            "plan-ready" if tasks()?.is_empty() => ("awaiting-input", msg + "\n\nThe plan has no tasks, so there is nothing to approve. \
                                                                          Reply to change the plan, or drop the item."),
            "plan-ready" => ("awaiting-approval", msg),
            // parsed, never inferred: a status off the two is not taken for either
            s => return Err(format!("The planner ended its reply with `STATUS: {s}`. Rookery acts only on `questions` or \
                                     `plan-ready`, and does not guess which one the planner meant. Its reply is in {}.",
                                    out.display())),
        };
        fs::write(&out, &msg).map_err(|e| e.to_string())?;
        set_phase(r, &job, &mut st, phase)?;
        Ok((phase, msg, if phase == "awaiting-approval" { tasks()? } else { vec![] }))
    })();
    let _ = fs::remove_dir_all(&job.scratch);
    let cost = format!("${:.2} · {:.0} s", job.spent, started.elapsed().as_secs_f64());
    let (phase, msg, tasks) = match out {
        Ok(o) => o,
        Err(e) => {
            job.step("error", json!({"error": e}));
            st["error"] = json!(e);
            let _ = set_phase(r, &job, &mut st, "error");
            println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint("error"), paint("2", &cost));
            println!("{}\n{e}\n", paint("1", "What happened"));
            println!("{}\n#{n} takes no more replies. Fix the cause above if it is yours, then start the item again:\n  {}",
                     paint("1", "What you can do"), again(&st, &item(r, n)));
            return Err(format!("#{n} failed"));
        }
    };
    println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint(phase), paint("2", &cost));
    print_md(&msg);
    if phase == "awaiting-input" {
        println!("\n{} rookery reply {n} \"your answers\"", paint("1", "next:"));
        return Ok(());
    }
    println!("\n{}", paint("1", &format!("Tasks ({})", tasks.len())));
    for (i, (_, _, t)) in tasks.iter().enumerate() {
        println!("  {} {t}", paint("38;2;168;204;104", &format!("{:>2}.", i + 1)));
    }
    println!("\n{} {}", paint("2", "full plan:"), item(r, n).join("plan.md").display());
    println!("{} rookery approve {n}   {}", paint("1", "run:"), paint("2", &format!("(or rookery reply {n} \"…\" to change the plan)")));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs_are_branch_safe() {
        assert_eq!(slug("Add a --dry-run flag!"), "add-a-dry-run-flag");
        assert_eq!(slug("仓库 support for Ünicode"), "support-for-nicode");
        assert_eq!(slug("仓库"), "");
        let long = slug("make the importer retry on timeouts and log every attempt it makes");
        assert!(long.len() <= 40 && !long.ends_with('-') && long == "make-the-importer-retry-on-timeouts-and", "{long}");
    }

    #[test]
    fn items_count_up_and_never_reuse() {
        let d = std::env::temp_dir().join(format!("rookery-claim-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        assert_eq!((claim(&d).unwrap(), claim(&d).unwrap()), (1, 2));
        fs::remove_file(d.join("next")).unwrap();
        assert_eq!(claim(&d).unwrap(), 3, "a lost counter skips the items that exist");
        fs::write(d.join("next"), "junk").unwrap();
        assert_eq!(claim(&d).unwrap(), 4);
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn reply_only_while_awaiting() {
        let st = |phase: &str| json!({"n": 4, "phase": phase, "title": "Add x", "body": "", "base": "main", "error": "no network"});
        assert!(may_reply(&st("awaiting-input"), "").is_ok() && may_reply(&st("awaiting-approval"), "").is_ok());
        for p in ["planning", "implementing", "reviewing", "done", "changes-wanted", "error", ""] {
            assert!(may_reply(&st(p), "").is_err(), "{p}");
        }
        // a stopped or failed item says what happened and how to go on
        let again = again(&st("error"), Path::new("items/4"));
        assert_eq!(again, "rookery new \"Add x\" --base main", "no body, no -f");
        let e = may_reply(&st("error"), &again).unwrap_err();
        assert!(e.contains("no network") && e.ends_with(&again), "{e}");
        assert!(may_reply(&st("planning"), &again).unwrap_err().ends_with(&again));
        let mut failed_run = st("error");
        failed_run["approved"] = json!(true);
        assert!(may_reply(&failed_run, &again).unwrap_err().ends_with("rookery approve 4"), "a failed approve resumes, never starts over");
        let with_body = json!({"title": "t", "body": "b", "base": "dev"});
        assert_eq!(super::again(&with_body, Path::new("d")), format!("rookery new \"t\" -f \"{}\" --base dev", Path::new("d").join("issue.md").display()));
    }

    #[test]
    fn keep_plans_only_reverts_everything() {
        let d = std::env::temp_dir().join(format!("rookery-keep-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        let g = |a: &[&str]| { git(&d, a).unwrap(); };
        g(&["init", "-q"]);
        for f in ["a.txt", "b.txt", "c.txt", "d.txt"] {
            fs::write(d.join(f), f).unwrap();
        }
        g(&["add", "."]);
        g(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]);
        fs::write(d.join("a.txt"), "changed").unwrap(); // modified
        fs::remove_file(d.join("b.txt")).unwrap(); // deleted
        g(&["mv", "c.txt", "c2.txt"]); // renamed, staged
        fs::create_dir_all(d.join("new/deep")).unwrap();
        fs::write(d.join("new/deep/x.txt"), "x").unwrap(); // untracked, in a new tree
        fs::write(d.join("staged.txt"), "s").unwrap();
        g(&["add", "staged.txt"]); // added, not in HEAD
        fs::write(d.join("d.txt"), "staged change").unwrap();
        g(&["add", "d.txt"]);
        fs::write(d.join("d.txt"), "and another").unwrap(); // staged and modified
        let mut got = keep_plans_only(&d).unwrap();
        got.sort();
        assert_eq!(got, ["a.txt", "b.txt", "c.txt", "c2.txt", "d.txt", "new/deep/x.txt", "staged.txt"]);
        assert_eq!(git_text(&d, &["status", "--porcelain", "-uall"]).unwrap(), "");
        assert_eq!(fs::read_to_string(d.join("a.txt")).unwrap(), "a.txt");
        assert!(keep_plans_only(&d).unwrap().is_empty());
        fs::remove_dir_all(&d).unwrap();
    }
}
