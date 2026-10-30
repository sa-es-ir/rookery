//! `rookery approve`, `rookery ask` and `rookery show`: agent.py's implement(), after_steps(),
//! follow_up() and the review from publish(), on a local item. A fresh session per task; the orchestrator ticks tasks.md and
//! commits with the user's own git identity. Nothing is pushed: the branch is the user's.
use crate::claude::{self, Job};
use crate::plan::{again, dirty, item, keep_plans_only, load, phase_paint, print_md, save, set_phase, worktree};
use crate::review::render_review;
use crate::steer::{git, git_text};
use crate::text::{DEFAULTS, cap, fmt, names, prompt, read_tasks, summary, tick};
use crate::{Repo, base_of, cancel, paint, repo};
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

/// A follow-up needs a finished run: the server's needs an open pull request, which a run opens
/// once its review has run (`implemented`), even when the run failed after that.
pub fn may_ask(st: &Value, running: bool, again: &str) -> Result<(), String> {
    let n = st["n"].as_u64().unwrap_or(0);
    match st["phase"].as_str().unwrap_or("") {
        _ if running => Err(format!("#{n} is running in another rookery now. Wait for it to end, then ask.")),
        "done" | "changes-wanted" => Ok(()),
        "error" | "implementing" | "reviewing" if st["implemented"] == true => Ok(()),
        "error" | "implementing" | "reviewing" if st["approved"] == true => Err(format!(
            "#{n}'s run has not finished, so there is no change to follow up on yet. Finish it first:
  rookery approve {n}")),
        "awaiting-input" => Err(format!("#{n} waits for your answers to its plan. Read them with `rookery show {n}`, then answer:
                                           rookery reply {n} \"your answers\"")),
        "awaiting-approval" => Err(format!("#{n} is not implemented yet. Approve its plan first (rookery approve {n}), or change the                                             plan with rookery reply {n} \"…\".")),
        "planning" | "error" => Err(format!("#{n} has no approved plan, so there is nothing to follow up on. Start it again:
  {again}")),
        p => Err(format!("#{n} is {p}, so there is nothing to follow up on.")),
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

/// `--max-usd`, else `max_usd` in ~/.rookery/config.json, else no cap (0) -> (dollars, where from).
fn budget(max_usd: Option<f64>) -> Result<(f64, &'static str), String> {
    if let Some(m) = max_usd {
        return Ok((m, "--max-usd"));
    }
    match crate::config::load()?.get("max_usd") {
        None | Some(Value::Null) => Ok((0.0, "config.json")),
        Some(m) => m.as_f64().map(|m| (m, "config.json"))
            .ok_or_else(|| format!("{}: `max_usd` is a number of dollars", crate::config::path().display())),
    }
}

/// items/<n>/lock, held for a whole run and released by the OS when this process ends however it
/// ends -> (the lock, whether another rookery holds it).
fn hold(d: &Path) -> Result<(fs::File, bool), String> {
    let lock = fs::OpenOptions::new().create(true).truncate(false).write(true).open(d.join("lock")).map_err(|e| e.to_string())?;
    let running = matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock));
    Ok((lock, running))
}

fn job_for(r: &Repo, n: u64, mcp: bool, max_usd: f64) -> Job {
    let run = (claude::now() * 1000.0) as u64;
    Job {
        repo_key: r.key.clone(), n, run, mcp, max_usd,
        add_dirs: vec![item(r, n)],
        scratch: std::env::temp_dir().join(format!("rookery-{}-{n}-{run}", r.key)),
        log: Some(r.state.join("runs.jsonl")),
        ..Default::default()
    }
}

/// The task, review and step prompts' words for a local item (agent.py's framing(c) gives the server's).
fn framing(n: u64, d: &Path, base: &str) -> Vec<(&'static str, String)> {
    vec![("n", n.to_string()), ("d", d.display().to_string()), ("base", base.into()), ("source", format!("task #{n}")),
         ("since", base.into()), ("handoff", "a branch to review and merge".into()), ("report", "the run's report".into())]
}

/// What a change goes through before it is handed over, as agent.py's publish() minus the push:
/// the `after` steps, then the review. Approve runs it after the task loop, ask after a follow-up
/// that changed the branch. -> (phase, who wants changes, the steering paths the branch changes)
fn publish(r: &Repo, job: &mut Job, st: &mut Value, wt: &Path, wf: &Value, at: &mut String, ran: &mut Vec<String>)
           -> Result<(&'static str, Vec<String>, String), String> {
    let n = st["n"].as_u64().unwrap();
    let (d, base) = (item(r, n), job.base.clone());
    let tasks_md = d.join("tasks.md");
    let args = framing(n, &d, &base);

    // agent.py's after_steps(): checks may not edit and re-run every time; a write step is
    // committed, and skipped once its commit is on the branch. Neither adds tasks.
    *at = "steps".into();
    let mut wanting = vec![];
    let after = wf["after"].as_array().unwrap();
    let done = if after.is_empty() { HashSet::new() } else { on_branch(wt, &base)? };
    for s in after {
        let (name, mode) = (s["agent"].as_str().unwrap(), s["mode"].as_str().unwrap());
        let subject = format!("#{n} step {name}");
        if skipped(n, s, &done) {
            println!("\n{} {name} (write): committed in an earlier run, skipped", paint("2", "Step"));
            continue;
        }
        println!("\n{} {name} ({mode})", paint("1", "Step"));
        *at = format!("step {name}");
        job.step("step", json!({"agent": name, "mode": mode}));
        let p = fmt(&prompt(mode), &args);
        let said = frozen(&tasks_md, || claude::claude(job, &p, &format!("step-{name}"), false, s, true, wt));
        // enforced, not asked: whatever a check leaves in the worktree is reverted, and flagged
        let reverted = if mode == "check" { keep_plans_only(wt)? } else { vec![] };
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
            msg = render_review(&status, &msg, &by, "check_verdicts", "###", Some(wt))?;
        }
        if !reverted.is_empty() {
            msg += &format!("\n_Rookery reverted this check's edits to {}: a check is read-only._\n", reverted.join(", "));
        }
        if mode == "write" {
            commit(job, wt, &subject, json!({"step": name}))?;
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
    *at = "the review".into();
    set_phase(r, job, st, "reviewing")?;
    println!("\n{} {}", paint("1", "Review"), paint("2", &wf["review"]["agent"].as_str().map_or("sonnet".into(), |a| format!("rk:{a}"))));
    let p = fmt(&prompt("review"), &args);
    let said = frozen(&tasks_md, || claude::claude(job, &p, "review", false, &wf["review"], true, wt));
    let reverted = keep_plans_only(wt)?;
    if !reverted.is_empty() {
        job.step("reverted", json!({"agent": "review", "paths": reverted}));
    }
    let (verdict, text) = said?;
    let rs = &wf["review"];
    let by = rs["agent"].as_str().into_iter().chain(rs["skills"].as_array().unwrap().iter().filter_map(Value::as_str))
        .map(|k| format!("rk:{k}")).collect::<Vec<_>>().join(" · ");
    let review = render_review(&verdict, &text, &by, "review_verdicts", "###", Some(wt))?;
    fs::write(d.join("out").join("review.md"), format!("## 🤖 Reviewer agent\n\n{review}")).map_err(|e| e.to_string())?;
    ran.push("review.md".into());
    if verdict != "review-ok" {
        wanting.push("the reviewer".into());
    }
    let steers = git_text(wt, &[&["diff", "--name-only", &format!("{base}...HEAD"), "--"][..], &STEERING].concat())?;
    // the server's pull request is open from here: a follow-up may ask about it, and one that
    // changes the branch goes through the steps and the review again
    st["implemented"] = json!(true);
    st["reviewed"] = json!(git_text(wt, &["rev-parse", "HEAD"])?);
    let phase = if wanting.is_empty() { "done" } else { "changes-wanted" };
    set_phase(r, job, st, phase)?;
    Ok((phase, wanting, steers))
}

/// A run that failed or was cancelled: the item goes to `error`, and the user reads what
/// happened, where it stopped and the command that goes on from there. -> the command's error.
fn failed(r: &Repo, job: &Job, st: &mut Value, e: String, at: &str, cost: &str) -> String {
    let (n, branch) = (st["n"].as_u64().unwrap_or(0), st["branch"].as_str().unwrap_or("").to_string());
    let (what, e) = cancel::why(e);
    job.step(what, json!({"error": e}));
    st["error"] = json!(e);
    let _ = set_phase(r, job, st, "error");
    println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint("error"), paint("2", cost));
    println!("{}\n{e}\n", paint("1", "What happened"));
    let (resume, cmd) = match at {
        "start" => ("Nothing ran.".to_string(), format!("rookery approve {n}")),
        "the follow-up" => (format!("The follow-up kept nothing: its edits are reverted, and {branch} is as it was."),
                            format!("rookery ask {n} \"…\"")),
        t if t.starts_with("task") => (format!("{} did not finish; every task before it is committed on {branch}, \
                                                and `rookery approve {n}` resumes there.", t[..1].to_uppercase() + &t[1..]),
                                       format!("rookery approve {n}")),
        t => (format!("Everything before it is committed on {branch}; {t} did not finish. `rookery approve {n}` runs the \
                       `after` steps and the review again (a write step already committed is skipped)."), format!("rookery approve {n}")),
    };
    let then = if what == "cancelled" { "To go on, run:" } else { "Fix the cause above if it is yours, then run:" };
    println!("{}\n{resume} {then}\n  {cmd}", paint("1", "What you can do"));
    format!("#{n} {}", if what == "cancelled" { "was cancelled" } else { "failed" })
}

/// A run that reached the end: its sections in run order, then where the branch is and what next.
fn report(d: &Path, st: &Value, (phase, wanting, steers): (&str, Vec<String>, String), wt: &Path, ran: &[String], cost: &str) {
    let (n, branch) = (st["n"].as_u64().unwrap_or(0), st["branch"].as_str().unwrap_or(""));
    println!("\n{} {}  {}\n", paint("1", &format!("#{n}")), phase_paint(phase), paint("2", cost));
    for (_, t) in sections(d).into_iter().filter(|(f, _)| ran.contains(f)) {
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
        format!("   {}", paint("2", &format!("(rookery ask {n} \"…\" to have it fixed; or fix it on the branch, then rookery approve {n})")))
    });
}

/// `rookery approve`: the task loop, the `after` steps and the review, as the server runs them
/// on an `approve` comment. A failure leaves every finished task committed; approve resumes.
pub fn approve(n: u64, max_usd: Option<f64>, mcp: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let mut st = load(&r, n)?;
    let d = item(&r, n);
    let (lock, running) = hold(&d)?;
    may_approve(&st, running, &again(&st, &d))?;
    let tasks_md = d.join("tasks.md");
    if fs::read_to_string(&tasks_md).map(|t| read_tasks(&t)).unwrap_or_default().is_empty() {
        return Err(format!("#{n} has no tasks in {}, so there is nothing to implement.", tasks_md.display()));
    }
    let (max_usd, from) = budget(max_usd)?;
    st["approved"] = json!(true);
    save(&r, &st)?;
    let mut job = job_for(&r, n, mcp, max_usd);
    job.begin(&r, "approve");
    let branch = st["branch"].as_str().unwrap().to_string();
    println!("{} {}  {}", paint("1", &format!("#{n}")), st["title"].as_str().unwrap_or(""), paint("2", &format!(
        "({branch}; {})", if max_usd > 0.0 { format!("--max-usd ${max_usd:.2} from {from}") } else { "no --max-usd cap".into() })));
    let started = std::time::Instant::now();
    // where the run is, for the error: what is committed and where approve picks up
    let mut at = String::from("start");
    let mut ran: Vec<String> = vec![];
    let out = (|| {
        let (base, sha) = base_of(&r.root, st["base"].as_str().map(String::from))?;
        let wf = job.pin(&r, &base, &sha)?;
        fs::create_dir_all(d.join("out")).map_err(|e| e.to_string())?;
        set_phase(&r, &job, &mut st, "implementing")?;
        let wt = worktree(&r, &st)?;

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
            let mut args = framing(n, &d, &base);
            args.push(("task", text.clone()));
            let p = fmt(&prompt("task"), &args);
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
                fs::write(&p, format!("{was}\n\n**Task {}.** {msg}", i + 1)).map_err(|e| e.to_string())?;
                ran.push("unverified.md".into());
            }
        }
        if !converged {
            return Err("the task loop did not converge".into());
        }
        publish(&r, &mut job, &mut st, &wt, &wf, &mut at, &mut ran).map(|p| (p, wt))
    })();
    drop(lock);
    let _ = fs::remove_dir_all(&job.scratch);
    let cost = format!("${:.2} · {:.0} s", job.spent, started.elapsed().as_secs_f64());
    match out {
        Ok((p, wt)) => Ok(report(&d, &st, p, &wt, &ran, &cost)),
        Err(e) => Err(failed(&r, &job, &mut st, e, &at, &cost)),
    }
}

/// `rookery ask`: agent.py's follow_up() once the item is implemented. One call answers or changes
/// the branch; the worktree holds it to which: an answer is reverted, a change is committed and goes
/// through the `after` steps and the review again. No base merge: base is the user's to merge in.
pub fn ask(n: u64, note: &str, max_usd: Option<f64>, mcp: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let mut st = load(&r, n)?;
    let d = item(&r, n);
    let (lock, running) = hold(&d)?;
    may_ask(&st, running, &again(&st, &d))?;
    let note = note.trim();
    if note.is_empty() {
        return Err(format!("say what to change, or ask something: rookery ask {n} \"rename the flag\""));
    }
    let (max_usd, from) = budget(max_usd)?;
    let mut job = job_for(&r, n, mcp, max_usd);
    job.begin(&r, "ask");
    let branch = st["branch"].as_str().unwrap().to_string();
    println!("{} {}  {}", paint("1", &format!("#{n}")), st["title"].as_str().unwrap_or(""), paint("2", &format!(
        "({branch}; {})", if max_usd > 0.0 { format!("--max-usd ${max_usd:.2} from {from}") } else { "no --max-usd cap".into() })));
    let started = std::time::Instant::now();
    let mut at = String::from("the follow-up");
    let mut ran: Vec<String> = vec![];
    let out = (|| {
        let (base, sha) = base_of(&r.root, st["base"].as_str().map(String::from))?;
        let wf = job.pin(&r, &base, &sha)?;
        fs::create_dir_all(d.join("out")).map_err(|e| e.to_string())?;
        let wt = worktree(&r, &st)?;
        let p = fmt(&prompt("follow-up"), &[
            ("source", format!("task #{n}")), ("handoff", format!("its branch {branch} holds the change")),
            ("who", "The developer asked".into()), ("note", note.into()), ("d", d.display().to_string()),
            ("since", base.clone()), ("base", base.clone()), ("merge", String::new()),
            ("posts", "shows that text to the developer".into()),
        ]);
        println!("{}", paint("2", &format!("Following up with {} …", wf["task"]["agent"].as_str().map_or("sonnet".into(), |a| format!("rk:{a}")))));
        // a follow-up adds no tasks, so every tick still reduces the open count
        let said = frozen(&d.join("tasks.md"), || claude::claude(&mut job, &p, "follow-up", false, &wf["task"], true, &wt));
        let checked = said.and_then(|(status, msg)| match status.as_str() {
            "answered" | "changed" => Ok((status, msg)),
            "failed" => Err(format!("the follow-up reported failed:\n\n{msg}")),
            s => Err(format!("the follow-up replied `STATUS: {s}`; it replies {}.", names("follow_verdicts").join(", "))),
        });
        let (mut status, mut said) = match checked {
            Ok(x) => x,
            Err(e) => {
                let _ = keep_plans_only(&wt); // nothing half-done stays for the next follow-up to commit
                return Err(e);
            }
        };
        if status == "changed" && dirty(&wt)?.is_empty() {
            status = "answered".into(); // no diff at all: it was an answer after all
        }
        if status == "changed" {
            let first = note.lines().next().unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
            commit(&job, &wt, &format!("#{n} follow-up: {}", first.chars().take(60).collect::<String>()), json!({"follow-up": true}))?;
        } else {
            let reverted = keep_plans_only(&wt)?;
            if !reverted.is_empty() {
                job.step("reverted", json!({"paths": reverted}));
                said += &format!("\n\n_Rookery reverted the edits to {}: an answer changes nothing._", reverted.join(", "));
            }
        }
        let section = format!("## 🤖 Follow-up\n\n> {}\n\n{said}", note.replace('\n', "\n> "));
        let f = d.join("out").join("follow-ups.md");
        let was = fs::read_to_string(&f).map(|t| t + "\n\n").unwrap_or_default();
        fs::write(&f, was + &section).map_err(|e| e.to_string())?;
        println!();
        print_md(&section);
        // not only this run's: a commit an earlier failed follow-up left goes through now
        if st["reviewed"].as_str() == Some(git_text(&wt, &["rev-parse", "HEAD"])?.as_str()) {
            return Ok(None);
        }
        publish(&r, &mut job, &mut st, &wt, &wf, &mut at, &mut ran).map(|p| Some((p, wt)))
    })();
    drop(lock);
    let _ = fs::remove_dir_all(&job.scratch);
    let cost = format!("${:.2} · {:.0} s", job.spent, started.elapsed().as_secs_f64());
    match out {
        Ok(Some((p, wt))) => Ok(report(&d, &st, p, &wt, &ran, &cost)),
        Ok(None) => {
            println!("\n{} {}  {}", paint("1", &format!("#{n}")), phase_paint(st["phase"].as_str().unwrap_or("")), paint("2", &cost));
            println!("{} rookery ask {n} \"…\"  {}", paint("1", "next:"), paint("2", &format!("(or rookery show {n})")));
            Ok(())
        }
        Err(e) => Err(failed(&r, &job, &mut st, e, &at, &cost)),
    }
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
    if let Ok(t) = fs::read_to_string(d.join("out").join("follow-ups.md")) {
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
        "changes-wanted" => format!("rookery ask {n} \"…\" to have it fixed   (or fix it on the branch, then rookery approve {n})"),
        "done" => format!("merge {branch} into {base} yourself   (or rookery ask {n} \"…\" for a change or an answer)"),
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
    fn ask_only_once_implemented() {
        let st = |phase: &str, approved: bool, implemented: bool| json!({"n": 6, "phase": phase, "approved": approved,
                                                                         "implemented": implemented, "title": "t", "base": "main"});
        for p in ["done", "changes-wanted"] {
            assert!(may_ask(&st(p, true, true), false, "").is_ok(), "{p}");
            assert!(may_ask(&st(p, true, true), true, "").unwrap_err().contains("another rookery"), "{p}: running");
        }
        for p in ["error", "implementing", "reviewing"] {
            assert!(may_ask(&st(p, true, true), false, "").is_ok(), "{p}: a failed or stopped follow-up is asked again");
            assert!(may_ask(&st(p, true, false), false, "").unwrap_err().ends_with("rookery approve 6"), "{p}: the run never finished");
        }
        assert!(may_ask(&st("awaiting-input", false, false), false, "").unwrap_err().contains("rookery reply 6"));
        assert!(may_ask(&st("awaiting-approval", false, false), false, "").unwrap_err().contains("rookery approve 6"));
        assert!(may_ask(&st("error", false, false), false, "rookery new \"t\" --base main").unwrap_err().ends_with("--base main"));
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
