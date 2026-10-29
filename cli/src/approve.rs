//! `rookery approve` and `rookery show`: agent.py's implement(), after_steps() and the review
//! from publish(), on a local item. A fresh session per task; the orchestrator ticks tasks.md and
//! commits with the user's own git identity. Nothing is pushed: the branch is the user's.
use crate::claude::{self, Job};
use crate::plan::{again, item, keep_plans_only, load, phase_paint, print_md, save, set_phase, worktree};
use crate::review::render_review;
use crate::steer::{self, git, git_text};
use crate::text::{DEFAULTS, cap, fmt, prompt, read_tasks, summary, tick};
use crate::{base_of, paint, repo};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs;
use std::path::Path;

/// what steers the agent; a branch that changes it says so (agent.py's STEERING)
const STEERING: [&str; 3] = [".rookery", ".claude", "CLAUDE.md"];

/// The server's approve phases: awaiting-approval, changes-wanted (all ticked: the checks and
/// the review again) and error (resume at the first unticked task). `running`: another rookery
/// holds the item's lock. An implementing or reviewing item nobody holds was stopped mid-run, as
/// the server's interrupted job is; it resumes like an error. Any refusal says what to do.
pub fn may_approve(st: &Value, running: bool, again: &str) -> Result<(), String> {
    let n = st["n"].as_u64().unwrap_or(0);
    let approved = st["approved"] == true;
    match st["phase"].as_str().unwrap_or("") {
        "awaiting-approval" | "changes-wanted" => Ok(()),
        "error" if approved => Ok(()),
        "implementing" | "reviewing" if approved && !running => Ok(()),
        "implementing" | "reviewing" => Err(format!("#{n} is running in another rookery now. Wait for it to end, then look at it with `rookery show {n}`.")),
        "error" => Err(format!(
            "#{n} failed while it was planned, so it has no approved plan to run. What happened:\n  {}\n\n\
             Fix the cause if it is yours, then start the item again:\n  {again}",
            st["error"].as_str().unwrap_or("(no detail was recorded)"))),
        "awaiting-input" => Err(format!("#{n} waits for your answers: its plan is not ready to approve. \
                                         Read them with `rookery show {n}`, then answer:\n  rookery reply {n} \"your answers\"")),
        "planning" => Err(format!("#{n} is still planning. Either another rookery is planning it now, or its last plan call was \
                                   stopped before it ended.\n\nIf one is running, wait for it. If not, start the item again:\n  {again}")),
        "done" => Err(format!("#{n} is done: every task is committed and the review wants no changes. \
                               See it with `rookery show {n}`, then merge its branch yourself.")),
        p => Err(format!("#{n} is {p}, so there is nothing to approve.")),
    }
}

/// The subjects of the commits on the worktree's branch since base: a write step whose commit
/// is there already ran, and a re-approve skips it.
fn on_branch(wt: &Path, base: &str) -> Result<HashSet<String>, String> {
    Ok(git_text(wt, &["log", "--format=%s", &format!("{base}..HEAD")])?.lines().map(String::from).collect())
}

/// A write step whose `#<n> step <agent>` commit is on the branch already ran: skipped.
fn skipped(n: u64, step: &Value, done: &HashSet<String>) -> bool {
    step["mode"] == "write" && done.contains(&format!("#{n} step {}", step["agent"].as_str().unwrap_or("")))
}

/// agent.py's frozen(): tasks.md as it was before `f`, whatever the agent did to it. Only
/// tick() changes the task list, and nothing reverts it out here but this.
fn frozen<T>(p: &Path, f: impl FnOnce() -> T) -> T {
    let was = fs::read(p).ok();
    let out = f();
    if let Some(b) = was {
        let _ = fs::write(p, b);
    }
    out
}

/// agent.py's commit_all(), one repo: everything the agent left, --allow-empty (a verify task
/// may change nothing), with the user's own git identity -> the short sha.
fn commit(job: &Job, wt: &Path, subject: &str, detail: Value) -> Result<String, String> {
    git(wt, &["add", "-A"])?;
    git(wt, &["commit", "-q", "--allow-empty", "-m", subject])?;
    let sha = git_text(wt, &["rev-parse", "--short", "HEAD"])?;
    let mut d = detail;
    d["sha"] = json!(sha);
    job.step("commit", d);
    Ok(sha)
}

/// ~/.rookery/config.json's `max_usd`, when there is one.
fn config_max_usd() -> Result<Option<f64>, String> {
    match crate::config::load()?.get("max_usd") {
        None | Some(Value::Null) => Ok(None),
        Some(m) => m.as_f64().map(Some).ok_or_else(|| format!("{}: `max_usd` is a number of dollars", crate::config::path().display())),
    }
}

/// The run's sections in items/<n>/out/, in the order the server puts them in the PR body.
fn sections(d: &Path) -> Vec<(String, String)> {
    let out = d.join("out");
    // in the order they were written, which is the steps' order (a skipped write step: its last run's)
    let mut steps: Vec<_> = fs::read_dir(&out).into_iter().flatten().flatten()
        .map(|e| (e.metadata().and_then(|m| m.modified()).ok(), e.file_name().to_string_lossy().to_string()))
        .filter(|(_, f)| f.starts_with("step-") && f.ends_with(".md")).collect();
    steps.sort();
    std::iter::once("unverified.md".to_string()).chain(steps.into_iter().map(|(_, f)| f)).chain(["review.md".to_string()])
        .filter_map(|f| fs::read_to_string(out.join(&f)).ok().map(|t| (f, t))).collect()
}

/// `rookery approve`: the task loop, the `after` steps and the review, as the server runs them
/// on an `approve` comment. A failure leaves every finished task committed; approve resumes.
pub fn approve(n: u64, max_usd: Option<f64>, mcp: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let mut st = load(&r, n)?;
    let d = item(&r, n);
    // held for the whole run, released by the OS when this process ends however it ends
    let lock = fs::OpenOptions::new().create(true).truncate(false).write(true).open(d.join("lock")).map_err(|e| e.to_string())?;
    let running = matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock));
    may_approve(&st, running, &again(&st, &d))?;
    let tasks_md = d.join("tasks.md");
    if fs::read_to_string(&tasks_md).map(|t| read_tasks(&t)).unwrap_or_default().is_empty() {
        return Err(format!("#{n} has no tasks in {}, so there is nothing to implement.", tasks_md.display()));
    }
    let (max_usd, from) = match max_usd {
        Some(m) => (m, "--max-usd"),
        None => (config_max_usd()?.unwrap_or(0.0), "config.json"),
    };
    st["approved"] = json!(true);
    save(&r, &st)?;
    let run = (claude::now() * 1000.0) as u64;
    let mut job = Job {
        repo_key: r.key.clone(), n, run, mcp, max_usd,
        add_dirs: vec![d.clone()],
        scratch: std::env::temp_dir().join(format!("rookery-{}-{n}-{run}", r.key)),
        log: Some(r.state.join("runs.jsonl")),
        ..Default::default()
    };
    let branch = st["branch"].as_str().unwrap().to_string();
    println!("{} {}  {}", paint("1", &format!("#{n}")), st["title"].as_str().unwrap_or(""), paint("2", &format!(
        "({branch}; {})", if max_usd > 0.0 { format!("--max-usd ${max_usd:.2} from {from}") } else { "no --max-usd cap".into() })));
    let started = std::time::Instant::now();
    // where the run is, for the error: what is committed and where approve picks up
    let mut at = String::from("start");
    let mut ran: Vec<String> = vec![];
    let out = (|| {
        // what steers the agent: the base branch as it is now, read once, never the worktree
        let (base, sha) = base_of(&r.root, st["base"].as_str().map(String::from))?;
        let plug = job.scratch.join("plugin");
        let s = steer::steer(&r.root, &base, &sha, &steer::rookery_home().join("defaults"), &plug)?;
        job.step("base", json!({"base": base, "sha": sha, "claude_md": s.claude_md.len(), "sources": s.sources, "agents": s.agent_sources}));
        (job.base, job.claude_md, job.sources, job.agents, job.plugin) = (base.clone(), s.claude_md, s.sources, s.agents, Some(plug));
        let wf = s.workflow;
        fs::create_dir_all(d.join("out")).map_err(|e| e.to_string())?;
        set_phase(&r, &job, &mut st, "implementing")?;
        let wt = worktree(&r, &st)?;
        let dd = d.display().to_string();
        let args = |more: &[(&'static str, String)]| -> Vec<(&'static str, String)> {
            let mut a = vec![("n", n.to_string()), ("d", dd.clone()), ("base", base.clone()), ("source", format!("task #{n}")),
                             ("since", base.clone()), ("handoff", "a branch to review and merge".into()), ("report", "the run's report".into())];
            a.extend_from_slice(more);
            a
        };

        let max = cap("max_tasks") as usize;
        let mut converged = false;
        for _ in 0..=max {
            let tasks = read_tasks(&fs::read_to_string(&tasks_md).map_err(|_| "tasks.md is missing".to_string())?);
            if tasks.len() > max {
                return Err(format!("{} tasks exceeds the {max}-task cap: split the item or shorten the plan", tasks.len()));
            }
            let Some((i, (off, _, text))) = tasks.iter().enumerate().find(|(_, t)| !t.1) else {
                converged = true;
                break;
            };
            at = format!("task {} ({})", i + 1, summary(text));
            println!("\n{} {}", paint("1", &format!("Task {}/{}", i + 1, tasks.len())), summary(text));
            job.step("task", json!({"i": i + 1, "of": tasks.len(), "text": text}));
            let p = fmt(&prompt("task"), &args(&[("task", text.clone())]));
            // tasks.md is the orchestrator's: whatever the agent did to it is undone before the tick
            let (status, msg) = frozen(&tasks_md, || claude::claude(&mut job, &p, &format!("task-{i}"), false, &wf["task"], false, &wt))?;
            if status != "task-done" && status != "task-unverified" {
                return Err(format!("task {} ({text}) reported {status}:\n\n{msg}", i + 1));
            }
            let now = fs::read_to_string(&tasks_md).map_err(|e| e.to_string())?;
            fs::write(&tasks_md, tick(&now, *off)).map_err(|e| e.to_string())?;
            job.step("tick", json!({"i": i + 1, "of": tasks.len()}));
            let sha = commit(&job, &wt, &format!("#{n} task {}: {text}", i + 1), json!({"i": i + 1}))?;
            let mark = if status == "task-done" { paint("32", "✓") } else { paint("33", "⚠") };
            println!("{mark} {}  {}", paint("2", &format!("committed {sha}")), msg.lines().next().unwrap_or(""));
            if status == "task-unverified" {
                // a check this machine cannot run is the user's to run, not a failed run
                let p = d.join("out").join("unverified.md");
                let was = fs::read_to_string(&p).unwrap_or_else(|_| "## ⚠️ Not verified here".into());
                fs::create_dir_all(p.parent().unwrap()).map_err(|e| e.to_string())?;
                fs::write(&p, format!("{was}\n\n**Task {}.** {msg}", i + 1)).map_err(|e| e.to_string())?;
                ran.push("unverified.md".into());
            }
        }
        if !converged {
            return Err("the task loop did not converge".into());
        }

        // agent.py's after_steps(): checks may not edit and re-run every time; a write step is
        // committed, and skipped once its commit is on the branch. Neither adds tasks.
        at = "steps".into();
        let mut wanting = vec![];
        let after = wf["after"].as_array().unwrap();
        let done = if after.is_empty() { HashSet::new() } else { on_branch(&wt, &base)? };
        for s in after {
            let (name, mode) = (s["agent"].as_str().unwrap(), s["mode"].as_str().unwrap());
            let subject = format!("#{n} step {name}");
            if skipped(n, s, &done) {
                println!("\n{} {name} (write): committed in an earlier run, skipped", paint("2", "Step"));
                continue;
            }
            println!("\n{} {name} ({mode})", paint("1", "Step"));
            at = format!("step {name}");
            job.step("step", json!({"agent": name, "mode": mode}));
            let p = fmt(&prompt(mode), &args(&[]));
            let said = frozen(&tasks_md, || claude::claude(&mut job, &p, &format!("step-{name}"), false, s, true, &wt));
            // enforced, not asked: whatever a check leaves in the worktree is reverted, and flagged
            let reverted = if mode == "check" { keep_plans_only(&wt)? } else { vec![] };
            if !reverted.is_empty() {
                job.step("reverted", json!({"agent": name, "paths": reverted}));
            }
            let (status, mut msg) = said?;
            let modes: Vec<&str> = DEFAULTS["modes"][mode].as_array().unwrap().iter().filter_map(Value::as_str).collect();
            if !modes.contains(&status.as_str()) {
                return Err(format!("step {name} replied `STATUS: {status}`; a {mode} step replies {}.", modes.join(" or ")));
            }
            if status == "failed" {
                return Err(format!("step {name} reported failed:\n\n{msg}"));
            }
            let skills: Vec<&str> = s["skills"].as_array().unwrap().iter().filter_map(Value::as_str).collect();
            if mode == "check" {
                let by = std::iter::once(name).chain(skills.iter().copied()).map(|k| format!("rk:{k}")).collect::<Vec<_>>().join(" · ");
                msg = render_review(&status, &msg, &by, "check_verdicts", "###", Some(&wt))?;
            }
            if !reverted.is_empty() {
                msg += &format!("\n_Rookery reverted this check's edits to {}: a check is read-only._\n", reverted.join(", "));
            }
            if mode == "write" {
                commit(&job, &wt, &subject, json!({"step": name}))?;
            } else if status == "changes-needed" || !reverted.is_empty() {
                wanting.push(name.to_string());
            }
            let mut c = name.chars();
            let title = c.next().map_or(String::new(), |f| f.to_uppercase().chain(c).collect());
            let f = format!("step-{name}.md");
            fs::write(d.join("out").join(&f), format!("## 🤖 {title} agent · {mode}{}\n\n{msg}",
                                                       skills.iter().map(|k| format!(" · rk:{k}")).collect::<String>()))
                .map_err(|e| e.to_string())?;
            ran.push(f);
        }

        // the review: advisory, once, read-only; changes-needed flags the item, never fails it
        at = "the review".into();
        set_phase(&r, &job, &mut st, "reviewing")?;
        println!("\n{} {}", paint("1", "Review"), paint("2", &wf["review"]["agent"].as_str().map_or("sonnet".into(), |a| format!("rk:{a}"))));
        let p = fmt(&prompt("review"), &args(&[]));
        let said = frozen(&tasks_md, || claude::claude(&mut job, &p, "review", false, &wf["review"], true, &wt));
        let reverted = keep_plans_only(&wt)?;
        if !reverted.is_empty() {
            job.step("reverted", json!({"agent": "review", "paths": reverted}));
        }
        let (verdict, text) = said?;
        let rs = &wf["review"];
        let by = rs["agent"].as_str().into_iter().chain(rs["skills"].as_array().unwrap().iter().filter_map(Value::as_str))
            .map(|k| format!("rk:{k}")).collect::<Vec<_>>().join(" · ");
        let review = render_review(&verdict, &text, &by, "review_verdicts", "###", Some(&wt))?;
        fs::write(d.join("out").join("review.md"), format!("## 🤖 Reviewer agent\n\n{review}")).map_err(|e| e.to_string())?;
        ran.push("review.md".into());
        if verdict != "review-ok" {
            wanting.push("the reviewer".into());
        }
        let steers = git_text(&wt, &[&["diff", "--name-only", &format!("{base}...HEAD"), "--"][..], &STEERING].concat())?;
        let phase = if wanting.is_empty() { "done" } else { "changes-wanted" };
        set_phase(&r, &job, &mut st, phase)?;
        Ok((phase, wanting, steers, wt))
    })();
    drop(lock);
    let _ = fs::remove_dir_all(&job.scratch);
    let cost = format!("${:.2} · {:.0} s", job.spent, started.elapsed().as_secs_f64());
    let (phase, wanting, steers, wt) = match out {
        Ok(o) => o,
        Err(e) => {
            job.step("error", json!({"error": e}));
            st["error"] = json!(e);
            let _ = set_phase(&r, &job, &mut st, "error");
            println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint("error"), paint("2", &cost));
            println!("{}\n{e}\n", paint("1", "What happened"));
            let resume = match at.as_str() {
                "start" => "Nothing ran.".to_string(),
                t if t.starts_with("task") => format!("{} failed; every task before it is committed on {branch}, \
                                                       and `rookery approve {n}` resumes there.", t[..1].to_uppercase() + &t[1..]),
                t => format!("Every task is committed on {branch}; {t} failed. `rookery approve {n}` runs the `after` \
                              steps and the review again (a write step already committed is skipped)."),
            };
            println!("{}\n{resume} Fix the cause above if it is yours, then run:\n  rookery approve {n}", paint("1", "What you can do"));
            return Err(format!("#{n} failed"));
        }
    };
    println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint(phase), paint("2", &cost));
    for (_, t) in sections(&d).into_iter().filter(|(f, _)| ran.contains(f)) {
        print_md(&t);
        println!();
    }
    if !steers.is_empty() {
        println!("{} this branch changes agent configuration ({}). It steers every later run once merged: read it first.\n",
                 paint("33", "warning:"), steers.lines().collect::<Vec<_>>().join(", "));
    }
    if !wanting.is_empty() {
        println!("Changes wanted by {}: see their sections above.", wanting.join(", "));
    }
    println!("{} {branch} in {}. Nothing is pushed.", paint("1", "branch:"), wt.display());
    println!("{} rookery show {n}{}", paint("1", "next:"), if wanting.is_empty() { String::new() } else {
        format!("   {}", paint("2", &format!("(fix it on the branch, then rookery approve {n} runs the checks and the review again)")))
    });
    Ok(())
}

/// `rookery show`: where the item is, what is committed, its tasks and its sections.
pub fn show(n: u64) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let st = load(&r, n)?;
    let d = item(&r, n);
    let (phase, base, branch) = (st["phase"].as_str().unwrap_or(""), st["base"].as_str().unwrap_or(""), st["branch"].as_str().unwrap_or(""));
    println!("{} {}  {}\n", paint("1", &format!("#{n}")), st["title"].as_str().unwrap_or(""), phase_paint(phase));
    println!("{}   {branch}  {}", paint("1", "branch"), paint("2", &format!("(from {base} at {})", st["sha"].as_str().unwrap_or("").get(..12).unwrap_or(""))));
    println!("{} {}", paint("1", "worktree"), st["worktree"].as_str().unwrap_or(""));
    println!("{}    {}", paint("1", "plan"), d.join("plan.md").display());
    let log = git_text(&r.root, &["log", "--reverse", "--format=%h %s", &format!("{base}..refs/heads/{branch}")]).unwrap_or_default();
    let commits: Vec<&str> = log.lines().collect();
    println!("\n{}", paint("1", &format!("Commits since {base} ({})", commits.len())));
    for c in &commits {
        println!("  {c}");
    }
    let tasks = fs::read_to_string(d.join("tasks.md")).map(|t| read_tasks(&t)).unwrap_or_default();
    println!("\n{}", paint("1", &format!("Tasks ({}/{} done)", tasks.iter().filter(|t| t.1).count(), tasks.len())));
    for (i, (_, done, t)) in tasks.iter().enumerate() {
        println!("  {} {:>2}. {t}", if *done { paint("32", "[x]") } else { "[ ]".into() }, i + 1);
    }
    for (_, t) in sections(&d) {
        println!();
        print_md(&t);
    }
    if phase == "awaiting-input" {
        if let Ok(q) = fs::read_to_string(d.join("out").join("plan.md")) {
            println!();
            print_md(&q);
        }
    }
    if let Some(e) = st["error"].as_str() {
        println!("\n{}\n{e}", paint("1", "What happened"));
    }
    let next = match phase {
        "awaiting-input" => format!("rookery reply {n} \"your answers\""),
        "awaiting-approval" => format!("rookery approve {n}"),
        "error" if st["approved"] == true => match tasks.iter().position(|t| !t.1) {
            Some(i) => format!("rookery approve {n}   (resumes at task {})", i + 1),
            None => format!("rookery approve {n}   (runs the after steps and the review again)"),
        },
        "error" => again(&st, &d),
        "changes-wanted" => format!("rookery approve {n}   (runs the checks and the review again)"),
        "done" => format!("merge {branch} into {base} yourself"),
        _ => String::new(),
    };
    if !next.is_empty() {
        println!("\n{} {next}", paint("1", "next:"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn approve_only_in_the_servers_phases() {
        let st = |phase: &str, approved: bool| json!({"n": 5, "phase": phase, "approved": approved, "title": "t", "body": "",
                                                      "base": "main", "error": "no network"});
        for p in ["awaiting-approval", "changes-wanted"] {
            assert!(may_approve(&st(p, false), false, "").is_ok(), "{p}");
        }
        assert!(may_approve(&st("error", true), false, "").is_ok(), "a failed run resumes");
        let e = may_approve(&st("error", false), false, "rookery new \"t\" --base main").unwrap_err();
        assert!(e.contains("no network") && e.ends_with("--base main"), "a failed plan is started again: {e}");
        for p in ["implementing", "reviewing"] {
            assert!(may_approve(&st(p, true), false, "").is_ok(), "{p}: stopped mid-run, nobody holds it");
            assert!(may_approve(&st(p, true), true, "").unwrap_err().contains("another rookery"), "{p}");
        }
        for p in ["awaiting-input", "planning", "done", ""] {
            assert!(may_approve(&st(p, true), false, "").is_err(), "{p}");
        }
        assert!(may_approve(&st("awaiting-input", false), false, "").unwrap_err().contains("rookery reply 5"));
    }

    #[test]
    fn a_write_step_runs_once() {
        let d = std::env::temp_dir().join(format!("rookery-steps-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        let g = |a: &[&str]| { git(&d, &[&["-c", "user.name=t", "-c", "user.email=t@t"][..], a].concat()).unwrap(); };
        g(&["init", "-q", "-b", "main"]);
        g(&["commit", "-q", "--allow-empty", "-m", "#3 step docs"]);   // on base: not this branch's
        g(&["checkout", "-q", "-b", "rookery/3"]);
        assert!(on_branch(&d, "main").unwrap().is_empty());
        g(&["commit", "-q", "--allow-empty", "-m", "#3 task 1: x"]);
        g(&["commit", "-q", "--allow-empty", "-m", "#3 step changelog"]);
        let done = on_branch(&d, "main").unwrap();
        assert!(done.contains("#3 step changelog") && !done.contains("#3 step docs") && done.len() == 2, "{done:?}");
        let step = |agent: &str, mode: &str| json!({"agent": agent, "mode": mode});
        assert!(skipped(3, &step("changelog", "write"), &done), "committed here: skipped on re-approve");
        assert!(!skipped(3, &step("changelog", "check"), &done), "a check re-runs every time");
        assert!(!skipped(3, &step("docs", "write"), &done) && !skipped(4, &step("changelog", "write"), &done));
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn frozen_undoes_the_agent() {
        let p = std::env::temp_dir().join(format!("rookery-frozen-{}.md", std::process::id()));
        fs::write(&p, "- [ ] a\n").unwrap();
        let got = frozen(&p, || { fs::write(&p, "- [x] a\n- [ ] more\n").unwrap(); 7 });
        assert_eq!((got, fs::read_to_string(&p).unwrap()), (7, "- [ ] a\n".to_string()));
        let _ = fs::remove_file(&p);
    }
}
