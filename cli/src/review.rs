//! agent.py's render_review() and located(): a reviewer's STATUS and <review> JSON -> the markdown
//! of its section. A reply off that shape fails, like a missing STATUS marker. fixtures/review.json
//! holds both sides to the same output. And `rookery review`: agent.py's review_pr() on a local
//! range, with pr_diff() and reviewed_at().
use crate::claude::{self, Job, tail};
use crate::plan::{dirty, print_md, wt_root};
use crate::steer::{git, git_text};
use crate::text::{DEFAULTS, cap, fmt, prompt};
use crate::{paint, repo};
use regex::Regex;
use serde_json::{Value, json};
use std::fs;
use std::path::Path;
use std::sync::LazyLock;

/// agent.py's LOCKFILES: in the stat, never in the diff
const LOCKFILES: [&str; 2] = [":(exclude)*package-lock.json", ":(exclude)*.lock"];
static FILE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^diff --git ").unwrap());

/// agent.py's pr_diff(): the stat whole, then the diff without lockfiles, cut at the last file
/// boundary before `cap` characters, naming the files cut. Run here, not by the agent, so the
/// user's RTK hook (which drops hunk lines) never sees it.
pub fn pr_diff(wt: &Path, since: &str, cap: usize) -> Result<String, String> {
    let rng = format!("{since}...HEAD");
    let only = [&["--", "."][..], &LOCKFILES].concat();
    let stat = git_text(wt, &["diff", "--stat", &rng])?;
    let all = git_text(wt, &[&["diff", &rng][..], &only].concat())?;
    let names = git_text(wt, &[&["diff", "--name-only", &rng][..], &only].concat())?; // in the same order
    let starts: Vec<usize> = FILE.find_iter(&all).map(|m| m.start()).collect();
    let (mut kept, mut size, mut keep) = (String::new(), 0, 0);
    for (i, &s) in starts.iter().enumerate() {
        let f = &all[s..starts.get(i + 1).copied().unwrap_or(all.len())];
        let k = f.chars().count(); // Python's len()
        if size + k > cap {
            break;
        }
        (kept, size, keep) = (kept + f, size + k, keep + 1);
    }
    let mut out = format!("`git diff --stat {rng}`:\n\n```\n{stat}\n```\n\n`git diff {rng}`, lockfiles left out:\n\n```diff\n{}\n```",
                          kept.trim_end());
    let left: Vec<String> = names.lines().skip(keep).map(|n| format!("- {n}")).collect();
    if !left.is_empty() {
        out += &fmt("\n\nThe diff above stops at {cap:,} characters. These files are changed too and are not \
                     in it; read them with Read:\n\n", &[("cap", cap.to_string())]);
        out += &left.join("\n");
    }
    Ok(out)
}

/// the branches a change is usually cut from, after origin/HEAD's
const BASES: [&str; 6] = ["main", "master", "develop", "development", "dev", "trunk"];

/// `[<base>..<ref> | <base>...<ref> | <ref>]` -> (base, ref, detected). Either side may be left
/// out: the ref is HEAD; the base is the one of origin/HEAD's branch and `BASES` the ref left
/// last (fewest commits since the fork point), so a feature cut from develop compares with
/// develop and develop itself with main. Each is the local branch when there is one, so its own
/// unpushed commits are not reviewed, else origin's. A tie goes to origin/HEAD's, then `BASES` order.
pub fn range(root: &Path, spec: Option<&str>) -> Result<(String, String, bool), String> {
    let spec = spec.unwrap_or("");
    let (base, head) = spec.split_once("...").or_else(|| spec.split_once("..")).unwrap_or(("", spec));
    let head = if head.is_empty() { "HEAD" } else { head }.to_string();
    if !base.is_empty() {
        return Ok((base.to_string(), head, false));
    }
    let sha = |b: &str| git_text(root, &["rev-parse", "--verify", "--quiet", &format!("{b}^{{commit}}")]).ok();
    let head_sha = sha(&head).ok_or_else(|| format!("{head} is not a branch or commit here"))?;
    let head_name = git_text(root, &["rev-parse", "--abbrev-ref", &head]).unwrap_or_default();
    let origin = git_text(root, &["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
        .ok().map(|o| o.trim_start_matches("origin/").to_string());
    let names: Vec<String> = origin.iter().cloned()
        .chain(BASES.iter().map(|b| b.to_string()).filter(|b| origin.as_ref() != Some(b))).collect();
    let mut best: Option<(u64, String)> = None;
    for name in names.iter().filter(|n| **n != head_name && format!("origin/{n}") != head_name) {
        let Some(b) = [name.clone(), format!("origin/{name}")].into_iter().find(|b| sha(b).is_some()) else { continue };
        let Ok(fork) = git_text(root, &["merge-base", &b, &head_sha]) else { continue };
        if fork == head_sha {
            continue; // the ref is all in it: nothing to compare
        }
        let ahead = git_text(root, &["rev-list", "--count", &format!("{fork}..{head_sha}")])?.parse().unwrap_or(u64::MAX);
        if best.as_ref().is_none_or(|(a, _)| ahead < *a) {
            best = Some((ahead, b));
        }
    }
    let shown = if head == "HEAD" && !head_name.is_empty() { &head_name } else { &head };
    let (_, base) = best.ok_or_else(|| format!("found no base branch ({}) that {shown} forked from, so there is nothing \
                                                to compare it with. Name both: rookery review <base>..<branch>", names.join(", ")))?;
    Ok((base, head, true))
}

/// agent.py's drop_pr_worktree(): the review's worktree is never committed to, so removing it
/// can never lose work.
fn drop_wt(root: &Path, wt: &Path) -> Result<(), String> {
    if wt.exists() {
        let _ = git(root, &["worktree", "remove", "--force", &wt.to_string_lossy()]);
    }
    if wt.exists() {
        // a leftover git no longer knows about
        let _ = fs::remove_dir_all(wt);
    }
    git(root, &["worktree", "prune"]).map(|_| ())
}

/// `rookery review`: one advisory review of the commits on a ref since it left its base, in a
/// detached worktree at the ref (the user's checkout is never touched), steering from the base.
/// A change reviewed before (same merge base, same head) is shown again, not paid for again,
/// unless a note asks something new.
pub fn review(spec: Option<String>, note: Option<String>, mcp: bool) -> Result<(), String> {
    let r = repo(&std::env::current_dir().map_err(|e| e.to_string())?)?;
    let (base, head, detected) = range(&r.root, spec.as_deref())?;
    let sha = |x: &str| git_text(&r.root, &["rev-parse", "--verify", "--quiet", &format!("{x}^{{commit}}")])
        .map_err(|_| format!("{x} is not a branch or commit here"));
    let (base_sha, head_sha) = (sha(&base)?, sha(&head)?);
    // HEAD reads as the branch it is on; the worktree's own HEAD is another commit
    let head = if head == "HEAD" { git_text(&r.root, &["rev-parse", "--abbrev-ref", "HEAD"])? } else { head };
    let since = git_text(&r.root, &["merge-base", &base_sha, &head_sha]).map_err(|_| format!("{head} and {base} share no commit"))?;
    if since == head_sha {
        let why = if head == base { format!("{head} is the base") } else { format!("{head} has no commits that {base} lacks") };
        return Err(format!("{why}, so there is nothing to review. Name what to review: rookery review {base}..<branch>"));
    }
    let short = &head_sha[..7];
    if detected {
        // a guess the user may not share: said before anything runs or is shown
        println!("Comparing {} with {}  {}", paint("1", &head), paint("1", &base),
                 paint("2", &format!("(the branch it forked from; to compare with another: rookery review <base>..{head})")));
    }
    let note = note.map(|n| n.trim().to_string()).filter(|n| !n.is_empty());
    let run = (claude::now() * 1000.0) as u64;
    let mut job = Job {
        repo_key: r.key.clone(), run, mcp,   // n 0: a review belongs to no item
        scratch: std::env::temp_dir().join(format!("rookery-{}-review-{run}", r.key)),
        log: Some(r.state.join("runs.jsonl")),
        ..Default::default()
    };
    job.begin(&r, "review");
    job.step("review", json!({"base": base, "ref": head, "head": head_sha, "since": since, "note": note.is_some()}));
    // agent.py's reviewed_at(): kept only once rendered, so a file here is a review that ended ok.
    // Keyed by the merge base, not base's tip: base moving on does not change the diff.
    let saved = r.state.join("reviews").join(format!("{}-{}.md", &since[..12], &head_sha[..12]));
    if note.is_none() && let Ok(t) = fs::read_to_string(&saved) {
        job.step("skipped", json!({"head": head_sha}));
        println!("{} at {short} ({base}...{head}). Commit a change, or ask something with -m \"…\", to review again.\n",
                 paint("1", "Already reviewed"));
        print_md(&t);
        println!("{} {}", paint("1", "saved:"), saved.display());
        return Ok(());
    }
    let commits = git_text(&r.root, &["log", "--reverse", "--format=- %h %s", &format!("{since}..{head_sha}")])?;
    if sha("HEAD").is_ok_and(|h| h == head_sha) {
        let d = dirty(&r.root)?;
        if !d.is_empty() {
            eprintln!("{} {} uncommitted change(s) here are not reviewed: only commits are.", paint("33", "warning:"), d.len());
        }
    }
    println!("{} {base}...{head}  {}", paint("1", "Review"), paint("2", &format!("({short}, {} commit(s))", commits.lines().count())));
    let started = std::time::Instant::now();
    // a review that is killed outright (a second Ctrl-C, a crash) leaves its worktree; the same head's next review removes it
    let wt = wt_root(&r).join(format!("review-{}", &head_sha[..12]));
    let out = (|| {
        // what steers the reviewer: the base branch as it is now, never the ref under review
        let wf = job.pin(&r, &base, &base_sha)?;
        drop_wt(&r.root, &wt)?;
        fs::create_dir_all(wt.parent().unwrap()).map_err(|e| e.to_string())?;
        git(&r.root, &["worktree", "add", "--detach", &wt.to_string_lossy(), &head_sha])?;
        // a sha, never the name: `HEAD` or a ref the worktree lacks would diff the wrong thing
        let since_ = &since[..12];
        let p = fmt(&prompt("pr-review"), &[
            ("what", format!("the commits on {head} since it left {base}")), ("who", "The developer".into()),
            ("title", head.clone()), ("body", commits.clone()),
            ("note", note.as_ref().map_or(String::new(), |n| format!("\nThe person who asked for this review wrote:\n\n{n}\n"))),
            ("head", format!("{head} at {short}")), ("since", since_.into()), ("posts", "shows".into()),
            ("wt", wt.display().to_string()), ("diff", pr_diff(&wt, since_, cap("diff_cap") as usize)?),
        ]);
        let rs = &wf["pr-review"];
        let (verdict, text) = claude::claude(&mut job, &p, "pr-review", false, rs, true, &wt)?;
        let by = rs["agent"].as_str().into_iter().chain(rs["skills"].as_array().unwrap().iter().filter_map(Value::as_str))
            .map(|k| format!("rk:{k}")).collect::<Vec<_>>().join(" · ");
        // before the worktree goes: it checks each finding's `where`
        render_review(&verdict, &text, &by, "pr_verdicts", "##", Some(&wt)).map(|t| (verdict, t))
    })();
    let _ = drop_wt(&r.root, &wt);
    let _ = fs::remove_dir_all(&job.scratch);
    let cost = paint("2", &format!("${:.2} · {:.0} s", job.spent, started.elapsed().as_secs_f64()));
    let (verdict, t) = out.map_err(|e| {
        let (what, e) = crate::cancel::why(e);
        job.step(what, json!({"error": e}));
        println!("{cost}");
        e
    })?;
    fs::create_dir_all(saved.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::write(&saved, &t).map_err(|e| e.to_string())?;
    job.step("posted", json!({"verdict": verdict, "head": head_sha, "since": since}));
    println!("{cost}\n");
    print_md(&t);
    println!("{} {}", paint("1", "saved:"), saved.display());
    Ok(())
}

static REVIEW_JSON: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<review>\s*(?:```(?:json)?\s*)?(.*?)\s*(?:```\s*)?</review>").unwrap());
// path, optional :line or :line-line
static WHERE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"([^\s,;:()]+)(?::L?(\d+)(?:-L?\d+)?)?").unwrap());
const UNCONFIRMED: &str = "❔ Rookery did not find this file or line in the code. Check it by hand.";

/// false when `where` names a file, or a line past its end, that is not in wt: a guess, not
/// something the reviewer read. Only tokens with a / or . count as paths, so prose passes.
pub fn located(wt: &Path, where_: &str) -> bool {
    let Ok(root) = wt.canonicalize() else { return false };
    let w = where_.replace('`', "");
    for c in WHERE.captures_iter(&w) {
        let p = c[1].trim_end_matches('.');
        if !p.contains(['/', '.']) {
            continue;
        }
        let Ok(f) = root.join(p).canonicalize() else { return false };
        if !f.starts_with(&root) {
            return false;
        }
        if let Some(line) = c.get(2) {
            // bytes.splitlines(): \n, \r and \r\n end a line
            let Ok(b) = std::fs::read(&f) else { return false };
            let t = String::from_utf8_lossy(&b).replace("\r\n", "\n");
            let lines = t.split(['\n', '\r']).count() - (t.is_empty() || t.ends_with(['\n', '\r'])) as usize;
            if !(1..=lines).contains(&line.as_str().parse().unwrap_or(0)) {
                return false;
            }
        }
    }
    true
}

/// Python's `str(x or "")` for a JSON value.
/// ponytail: a non-string renders as JSON, not as Python's repr; reviewers send strings.
fn txt(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null | Value::Bool(false) => String::new(),
        v => v.to_string(),
    }
}

/// Trimmed and cut to `cap` characters with "…"; a table cell is one line with its | escaped.
fn cut(s: &str, cap: u64, cell: bool) -> String {
    let mut s = s.trim().to_string();
    if cell {
        s = s.split_whitespace().collect::<Vec<_>>().join(" ").replace('|', "\\|");
    }
    if s.chars().count() <= cap as usize {
        return s;
    }
    s.chars().take(cap as usize - 1).collect::<String>().trim_end().to_string() + "…"
}

/// -> the markdown for the run's report under `level`. `verdicts` names the defaults.json table
/// (`review_verdicts`, `check_verdicts`, `pr_verdicts`), best first. With `wt`, a finding whose
/// `where` is not in it is marked ❔: flagged, never dropped.
pub fn render_review(verdict: &str, text: &str, by: &str, verdicts: &str, level: &str, wt: Option<&Path>) -> Result<String, String> {
    let bad = |why: String| format!("{by}'s reply {why}. Its reply ended:\n\n{}", tail(text, 1500));
    let vs = DEFAULTS[verdicts].as_object().unwrap_or_else(|| panic!("defaults.json has no {verdicts}"));
    let best = vs.keys().next().unwrap();
    if !vs.contains_key(verdict) {
        return Err(bad(format!("has `STATUS: {verdict}`; it replies {}", vs.keys().cloned().collect::<Vec<_>>().join(", "))));
    }
    let m = REVIEW_JSON.captures(text).ok_or_else(|| bad("has no <review> JSON".into()))?;
    // a model sometimes drops the last `}` after a whole findings list: a closer added, never content
    let r: Value = match serde_json::from_str(&m[1]) {
        Ok(r) => r,
        Err(e) => serde_json::from_str(&format!("{}}}", &m[1])).map_err(|_| bad(format!("has <review> JSON that does not parse ({e})")))?,
    };
    let sev = DEFAULTS["severity"].as_object().unwrap();
    let fs = r.get("findings").and_then(Value::as_array).filter(|fs| fs.iter().all(|f| {
        f.is_object() && f["severity"].as_str().is_some_and(|s| sev.contains_key(s)) && f["title"].is_string()
    }));
    let Some(fs) = fs.filter(|_| r["summary"].is_string()) else {
        return Err(bad("has <review> JSON without a summary, or with a finding that has no title or severity".into()));
    };
    if verdict == best && fs.iter().any(|f| f["severity"] == "high") {
        return Err(bad(format!("says {best} but has a high finding")));
    }
    let caps = |k: &str| DEFAULTS["review_caps"][k].as_u64().unwrap();
    // the count first, so "nothing to fix" is plain before anyone reads on
    let tally: String = sev.iter().filter_map(|(s, icon)| {
        let k = fs.iter().filter(|f| f["severity"] == s.as_str()).count();
        (k > 0).then(|| format!(" · {} {k} {s}", icon.as_str().unwrap()))
    }).collect();
    let lead = if fs.is_empty() { "✅ **No findings**".into() }
               else { format!("🔎 **{} finding{}**{tally}", fs.len(), if fs.len() == 1 { "" } else { "s" }) };
    let mut out = vec![format!("{level} {}", vs[verdict].as_str().unwrap()), lead, cut(&txt(&r["summary"]), caps("summary"), false)];
    let answer = cut(&txt(&r["answer"]), caps("answer"), false);
    if !answer.is_empty() {
        out.push(format!("> **Your note:** {}", answer.replace('\n', "\n> ")));
    }
    let shown = &fs[..fs.len().min(caps("findings") as usize)];
    if !shown.is_empty() {
        let mut rows = vec!["| # | Severity | Where | Finding |".to_string(), "|---|---|---|---|".into()];
        let mut unsure = false;
        for (i, f) in shown.iter().enumerate() {
            let where_ = cut(&txt(&f["where"]), caps("where"), true).replace('`', "");
            // the raw where: a cut one could miss
            let mark = if wt.is_some_and(|wt| !located(wt, &txt(&f["where"]))) { " ❔" } else { "" };
            unsure |= !mark.is_empty();
            let s = f["severity"].as_str().unwrap();
            rows.push(format!("| {} | {} {s} | {}{mark} | {} |", i + 1, sev[s].as_str().unwrap(),
                              if where_.is_empty() { String::new() } else { format!("`{where_}`") }, cut(&txt(&f["title"]), caps("title"), true)));
        }
        if fs.len() > shown.len() {
            rows.push(format!("\n{} more findings not shown.", fs.len() - shown.len()));
        }
        if unsure {
            rows.push(format!("\n{UNCONFIRMED}"));
        }
        out.push(rows.join("\n"));
        let details: Vec<String> = shown.iter().enumerate().filter(|(_, f)| !txt(&f["detail"]).is_empty())
            .map(|(i, f)| format!("**{}. {}**\n\n{}", i + 1, cut(&txt(&f["title"]), caps("title"), true), cut(&txt(&f["detail"]), caps("detail"), false)))
            .collect();
        if !details.is_empty() {
            out.push(format!("<details><summary>Details</summary>\n\n{}\n\n</details>", details.join("\n\n")));
        }
    }
    out.push(format!("<sub>{by}</sub>"));
    Ok(out.join("\n\n") + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn review_fixtures() {
        let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../fixtures/review.json");
        let rf: Value = serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap();
        let wt = std::env::temp_dir().join(format!("rookery-review-{}", std::process::id()));
        let _ = fs::remove_dir_all(&wt);
        for (p, t) in rf["files"].as_object().unwrap() {
            fs::create_dir_all(wt.join(p).parent().unwrap()).unwrap();
            fs::write(wt.join(p), t.as_str().unwrap()).unwrap();
        }
        for l in rf["located"].as_array().unwrap() {
            assert_eq!(located(&wt, l[0].as_str().unwrap()), l[1].as_bool().unwrap(), "{}", l[0]);
        }
        for f in rf["render"].as_array().unwrap() {
            let s = |k: &str| f[k].as_str().unwrap();
            let got = render_review(s("verdict"), s("text"), rf["by"].as_str().unwrap(), &format!("{}_verdicts", s("verdicts")),
                                    s("level"), f["wt"].as_bool().unwrap().then_some(wt.as_path()));
            match (got, f["expect"].as_str()) {
                (Ok(out), Some(want)) => assert_eq!(out, want, "{}", s("why")),
                (Err(e), None) => assert!(e.contains(s("error")), "{}: {e}", s("why")),
                (got, _) => panic!("{}: {got:?}", s("why")),
            }
        }
        let _ = fs::remove_dir_all(&wt);
    }

    #[test]
    fn diff_and_range_on_a_real_repo() {
        let d = std::env::temp_dir().join(format!("rookery-prdiff-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        let g = |a: &[&str]| { git(&d, &[&["-c", "user.name=t", "-c", "user.email=t@t"][..], a].concat()).unwrap(); };
        g(&["init", "-q", "-b", "main"]);
        g(&["commit", "-q", "--allow-empty", "-m", "base"]);
        assert!(range(&d, None).unwrap_err().contains("found no base branch"), "on main, with nothing it forked from");
        g(&["checkout", "-q", "-b", "feat"]);
        // test_agent.py's pr_diff case, byte for byte
        for (name, text) in [("a.txt", "a\n".to_string()), ("b.txt", "b".repeat(300) + "\n"), ("c.txt", "c\n".into()),
                             ("package-lock.json", "{}\n".into()), ("web/yarn.lock", "x\n".into())] {
            fs::create_dir_all(d.join(name).parent().unwrap()).unwrap();
            fs::write(d.join(name), text).unwrap();
        }
        g(&["add", "-A"]);
        g(&["commit", "-q", "-m", "feat"]);
        let all = pr_diff(&d, "main", 100_000).unwrap();
        assert!(all.contains("package-lock.json |") && !all.contains("+{}") && !all.contains("+x"), "the stat names lockfiles, the diff skips them");
        assert!(all.contains("+a\n") && all.contains("+c\n") && !all.contains("read them with Read"), "{all}");
        let cut = pr_diff(&d, "main", 400).unwrap();
        assert!(cut.contains("+a\n") && !cut.contains("+bbb") && !cut.contains("+c\n"), "cut whole files, at the first that does not fit");
        assert!(cut.ends_with("read them with Read:\n\n- b.txt\n- c.txt"), "{cut}");

        g(&["branch", "dev", "main"]);
        g(&["remote", "add", "origin", "https://example.invalid/r.git"]);
        g(&["update-ref", "refs/remotes/origin/dev", "main"]);
        g(&["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/dev"]);
        assert_eq!(range(&d, None).unwrap(), ("dev".into(), "HEAD".into(), true), "a tie: origin's default, the local branch first");
        g(&["branch", "-D", "dev"]);
        assert_eq!(range(&d, Some("feat")).unwrap(), ("origin/dev".into(), "feat".into(), true), "else origin's");
        assert_eq!(range(&d, Some("main..feat")).unwrap(), ("main".into(), "feat".into(), false));
        assert_eq!(range(&d, Some("main...")).unwrap(), ("main".into(), "HEAD".into(), false));
        // gitflow: a feature cut from develop compares with develop, develop itself with main
        g(&["checkout", "-q", "-b", "develop", "main"]);
        g(&["commit", "-q", "--allow-empty", "-m", "on develop"]);
        g(&["checkout", "-q", "-b", "feature/x"]);
        g(&["commit", "-q", "--allow-empty", "-m", "x"]);
        assert_eq!(range(&d, None).unwrap(), ("develop".into(), "HEAD".into(), true), "the closest fork point wins over origin/HEAD");
        g(&["symbolic-ref", "-d", "refs/remotes/origin/HEAD"]);
        assert_eq!(range(&d, Some("develop")).unwrap(), ("main".into(), "develop".into(), true), "never itself");
        let _ = fs::remove_dir_all(&d);
    }
}
