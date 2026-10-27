//! What steers the agent, read from the base commit once per run, never the working tree:
//! agent.py's workflow(), frontmatter(), parse_agent() and steer(). The owner layer
//! (<owner>/.rookery on the server) is ~/.rookery/defaults here.
use crate::text::{DEFAULTS, cap, names, splitlines};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;

mod embedded {
    include!(concat!(env!("OUT_DIR"), "/embedded.rs"));
}

pub static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]*$").unwrap());
static REPO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[\w.-]+/[\w.-]+$").unwrap());
static FRONT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)^\s*---[ \t]*\r?\n(.*?)\r?\n---").unwrap());
const SLOTS: [&str; 4] = ["plan", "task", "review", "pr-review"];

pub type Meta = BTreeMap<String, String>;

/// Top-level frontmatter keys -> raw value, continuation lines folded in. A key outside `keys`
/// fails the run: `hooks:` would run shell here, `mcpServers:` reach out from here.
pub fn frontmatter(text: &str, keys: &[String], at: &str) -> Result<Meta, String> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut meta = Meta::new();
    let Some(m) = FRONT.captures(text) else { return Ok(meta) };
    let mut key: Option<String> = None;
    for line in dedent(splitlines(&m[1])) {
        if line.trim().is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with(char::is_whitespace) || line.starts_with("- ") || line == "-" {
            if let Some(k) = &key {
                let v = format!("{} {}", meta[k], line.trim());
                meta.insert(k.clone(), v.trim().to_string());
            }
            continue;
        }
        let (k, v) = line.split_once(':').unwrap_or((line, ""));
        let (k, v) = (k.trim(), v.trim());
        if !keys.iter().any(|x| x == k) {
            return Err(format!("{at}: frontmatter `{k}` is not allowed — only {}.", keys.join(", ")));
        }
        let v = if matches!(v, ">" | "|" | ">-" | "|-") { "" } else { v.trim_matches(['\'', '"']) };
        meta.insert(k.to_string(), v.to_string());
        key = Some(k.to_string());
    }
    Ok(meta)
}

/// textwrap.dedent: the margin all non-blank lines share comes off; blank lines become empty.
fn dedent(lines: Vec<&str>) -> Vec<&str> {
    fn indent(l: &str) -> &str {
        &l[..l.len() - l.trim_start_matches([' ', '\t']).len()]
    }
    let blank = |l: &str| l.chars().all(|c| c == ' ' || c == '\t');
    let mut margin: Option<&str> = None;
    for l in lines.iter().filter(|l| !blank(l)) {
        let i = indent(l);
        margin = Some(match margin {
            None => i,
            Some(m) => &m[..m.bytes().zip(i.bytes()).take_while(|(a, b)| a == b).count()],
        });
    }
    let margin = margin.unwrap_or("");
    lines.into_iter().map(|l| if blank(l) { "" } else { l.strip_prefix(margin).unwrap_or(l) }).collect()
}

/// An agent file held to the frontmatter allowlist; its name must be its filename's.
pub fn parse_agent(stem: &str, text: &str, at: &str) -> Result<Meta, String> {
    let meta = frontmatter(text, &names("agent_keys"), at)?;
    if !NAME.is_match(stem) || meta.get("name").is_some_and(|n| n != stem) {
        return Err(format!("{at}: an agent's name must match `{}` and its filename.", NAME.as_str().trim_matches(['^', '$'])));
    }
    let models = names("models");
    if !models.contains(meta.get("model").unwrap_or(&models[0])) {
        return Err(format!("{at}: `model` must be one of {}.", models.join(", ")));
    }
    Ok(meta)
}

/// .rookery/workflow.json -> every slot with every key, `under` (~/.rookery/defaults' workflow,
/// else the built-in) filling what the file leaves out, plus its `after` steps. `with` is checked
/// as the server checks it, so a file valid there is valid here, and dropped: no companions locally.
pub fn workflow(text: &str, at: &str, under: Option<&Value>) -> Result<Value, String> {
    let builtin;
    let under = match under {
        Some(u) => u,
        None => {
            builtin = {
                let mut w = DEFAULTS["workflow"].clone();
                w["after"] = json!([]);
                w
            };
            &builtin
        }
    };
    let given: Value = if text.is_empty() {
        json!({})
    } else {
        serde_json::from_str(text).map_err(|e| format!("{at} is not valid JSON: {e}"))?
    };
    let Some(g) = given.as_object().filter(|g| g.keys().all(|k| SLOTS.contains(&k.as_str()) || k == "after" || k == "with"))
    else {
        return Err(format!("{at}: the top level is an object with only {}, after, with.", SLOTS.join(", ")));
    };
    let max_steps = cap("max_steps") as usize;
    let after = g.get("after");
    if after.is_some_and(|a| !a.as_array().is_some_and(|a| a.len() <= max_steps)) {
        return Err(format!("{at}: `after` is a list of at most {max_steps} steps."));
    }
    let with = g.get("with").cloned().unwrap_or(json!([]));
    let ok = with.as_array().is_some_and(|w| {
        w.len() <= 3
            && w.iter().all(|r| r.as_str().is_some_and(|r| REPO.is_match(r)))
            && w.iter().collect::<std::collections::HashSet<_>>().len() == w.len()
    });
    if !ok {
        return Err(format!("{at}: `with` is a list of at most 3 different repos, each `owner/name`."));
    }
    let mut wf = Map::new();
    for slot in SLOTS {
        wf.insert(slot.into(), one(at, slot, g.get(slot).unwrap_or(&json!({})), &under[slot], false)?);
    }
    let steps = match after {
        Some(a) => Value::Array(a.as_array().unwrap().iter().enumerate()
            .map(|(i, s)| one(at, &format!("after[{i}]"), s, &DEFAULTS["step"], true))
            .collect::<Result<_, _>>()?),
        None => under["after"].clone(),
    };
    let agents: Vec<_> = steps.as_array().unwrap().iter().map(|s| s["agent"].clone()).collect();
    if (1..agents.len()).any(|i| agents[..i].contains(&agents[i])) {
        return Err(format!("{at}: `after` names an agent twice; each step needs its own."));
    }
    wf.insert("after".into(), steps);
    Ok(Value::Object(wf))
}

fn one(at: &str, slot: &str, s: &Value, default: &Value, step: bool) -> Result<Value, String> {
    let d = default.as_object().unwrap();
    let Some(s) = s.as_object().filter(|s| s.keys().all(|k| d.contains_key(k))) else {
        return Err(format!("{at}: `{slot}` is an object with only {}.", d.keys().cloned().collect::<Vec<_>>().join(", ")));
    };
    let mut m = d.clone();
    m.extend(s.clone());
    let skills = m["skills"].as_array().filter(|k| k.iter().all(Value::is_string));
    if !(m["agent"].is_null() || m["agent"].is_string()) || !m["instructions"].is_string() || skills.is_none() {
        return Err(format!("{at}: in `{slot}`, `agent` is a name or null, `skills` a list of names, `instructions` a string."));
    }
    let modes = DEFAULTS["modes"].as_object().unwrap();
    if step && (m["agent"].as_str().is_none_or(str::is_empty) || !m["mode"].as_str().is_some_and(|x| modes.contains_key(x))) {
        return Err(format!("{at}: `{slot}` needs an `agent` and a `mode`, {}.", modes.keys().cloned().collect::<Vec<_>>().join(" or ")));
    }
    for name in skills.unwrap().iter().chain(m["agent"].is_string().then_some(&m["agent"])) {
        let name = name.as_str().unwrap();
        if !NAME.is_match(name) {
            return Err(format!("{at}: `{name}` in `{slot}` — a name must match `[a-z0-9][a-z0-9-]*`."));
        }
    }
    Ok(Value::Object(m))
}

/// (label, slot) for every slot and `after` step.
pub fn slots(wf: &Value) -> Vec<(String, &Value)> {
    let mut out: Vec<_> = SLOTS.iter().map(|s| (s.to_string(), &wf[*s])).collect();
    out.extend(wf["after"].as_array().unwrap().iter().enumerate().map(|(i, s)| (format!("after[{i}]"), s)));
    out
}

pub fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let out = Command::new("git").args(args).current_dir(root).output().map_err(|e| format!("git: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr });
        return Err(format!("git {} failed: {}", args.join(" "), err.trim()));
    }
    Ok(out.stdout)
}

/// git's output as trimmed text, as agent.py's sh() returns it.
pub fn git_text(root: &Path, args: &[&str]) -> Result<String, String> {
    Ok(String::from_utf8_lossy(&git(root, args)?).trim().to_string())
}

/// The base commit's file at `p`, trimmed like agent.py's blob(); "" when there is none.
fn blob(root: &Path, sha: &str, p: &str) -> String {
    git_text(root, &["cat-file", "blob", &format!("{sha}:{p}")]).unwrap_or_default()
}

/// (path relative to its source dir, bytes, executable?)
type File = (String, Vec<u8>, bool);

/// Every file under `src` at sha. Symlinks and submodules stay out, as tarfile's "data" filter
/// keeps them out on the server; the exec bit comes along, as it does there.
/// ponytail: one `git cat-file` per file, fine for a skills dir; `--batch` if one grows large.
fn tree(root: &Path, sha: &str, src: &str) -> Result<Vec<File>, String> {
    let Ok(ls) = git(root, &["ls-tree", "-r", "-z", &format!("{sha}:{src}")]) else { return Ok(vec![]) };
    let mut out = vec![];
    for e in String::from_utf8_lossy(&ls).split('\0').filter(|e| !e.is_empty()) {
        let (head, path) = e.split_once('\t').unwrap();
        let [mode, kind, oid] = head.split(' ').collect::<Vec<_>>()[..] else { continue };
        if kind == "blob" && mode != "120000" {
            out.push((path.to_string(), git(root, &["cat-file", "blob", oid])?, mode == "100755"));
        }
    }
    Ok(out)
}

/// Every file under dir on disk; symlinks stay out.
fn disk(dir: &Path) -> Vec<File> {
    let mut out = vec![];
    let mut todo = vec![dir.to_path_buf()];
    while let Some(d) = todo.pop() {
        for e in fs::read_dir(&d).into_iter().flatten().flatten() {
            let (p, t) = (e.path(), e.file_type().unwrap());
            if t.is_dir() {
                todo.push(p);
            } else if t.is_file() {
                let rel = p.strip_prefix(dir).unwrap().to_string_lossy().replace('\\', "/");
                #[cfg(unix)]
                let exec = std::os::unix::fs::PermissionsExt::mode(&e.metadata().unwrap().permissions()) & 0o111 != 0;
                #[cfg(not(unix))]
                let exec = false;
                out.push((rel, fs::read(&p).unwrap_or_default(), exec));
            }
        }
    }
    out.sort();
    out
}

/// the catalog's files are all 100644; its scripts are run with `bash`
fn embedded(prefix: &str) -> Vec<File> {
    embedded::FILES.iter().filter_map(|(p, b)| p.strip_prefix(prefix).map(|r| (r.to_string(), b.to_vec(), false))).collect()
}

fn put(dest: &Path, (rel, bytes, exec): &File) -> Result<(), String> {
    let p = dest.join(rel);
    fs::create_dir_all(p.parent().unwrap()).and_then(|_| fs::write(&p, bytes)).map_err(|e| format!("{}: {e}", p.display()))?;
    #[cfg(unix)]
    if *exec {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    }
    let _ = exec;
    Ok(())
}

pub struct Steer {
    pub claude_md: String,
    pub workflow: Value,
    pub agents: BTreeMap<String, Meta>,
    pub agent_sources: BTreeMap<String, String>,
    /// skill -> where it came from: .rookery/skills, .claude/skills, ~/.rookery/defaults, catalog
    pub sources: BTreeMap<String, String>,
}

/// Validate everything that steers the agent at base `sha` and assemble the run's plugin at
/// `plug` (skills/ and agents/ only: hooks/, .mcp.json or commands/ never get in). Raises on
/// the first bad input: a run never starts on a silent fallback.
///
/// Precedence, first wins: the repo's own (.rookery/, .claude/skills), ~/.rookery/defaults,
/// then the built-in agents, catalog and workflow.
pub fn steer(root: &Path, base: &str, sha: &str, defaults: &Path, plug: &Path) -> Result<Steer, String> {
    let home = "~/.rookery/defaults";
    let md = blob(root, sha, "CLAUDE.md");
    let owf = match fs::read_to_string(defaults.join("workflow.json")) {
        Ok(t) => Some(workflow(t.trim(), &format!("workflow.json in {home}"), None)?),
        Err(_) => None,
    };
    let wf = workflow(&blob(root, sha, ".rookery/workflow.json"), &format!(".rookery/workflow.json on {base}"), owf.as_ref())?;
    let max = cap("claude_md_max") as usize;
    for (slot, s) in slots(&wf) {
        let inst = s["instructions"].as_str().unwrap();
        if md.len() + inst.len() > max {
            let also = if inst.is_empty() { String::new() } else { format!(" plus the `{slot}` instructions in .rookery/workflow.json") };
            return Err(format!("CLAUDE.md{also} on {base} is {} KB, over the {} KB Rookery passes to the agent. \
                                Shorten it on {base}, then run again.", (md.len() + inst.len()) / 1024, max / 1024));
        }
    }
    fs::create_dir_all(plug.join(".claude-plugin")).map_err(|e| e.to_string())?;
    put(plug, &(".claude-plugin/plugin.json".into(), br#"{"name": "rk"}"#.to_vec(), false))?;
    fs::create_dir_all(plug.join("skills")).map_err(|e| e.to_string())?;

    // built-in agents, then the defaults', then .rookery/agents on base: a later one of the same
    // name replaces the role. Top-level *.md only: the CLI reads nothing else from agents/.
    let top = |p: &str| !p.contains('/') && p.ends_with(".md");
    let mut by = BTreeMap::new();
    for (label, files) in [("built-in", embedded("agents/")), (home, disk(&defaults.join("agents"))),
                           (".rookery/agents", tree(root, sha, ".rookery/agents")?)] {
        for f @ (p, _, _) in files.iter().filter(|(p, _, _)| top(p)) {
            put(&plug.join("agents"), f)?;
            by.insert(p.trim_end_matches(".md").to_string(), label.to_string());
        }
    }
    // skills: the first source to bring a name wins
    let mut won = BTreeMap::new();
    for (label, files) in [(".rookery/skills", tree(root, sha, ".rookery/skills")?),
                           (".claude/skills", tree(root, sha, ".claude/skills")?), (home, disk(&defaults.join("skills")))] {
        let mut got = BTreeMap::new();
        for f @ (p, _, _) in &files {
            if let Some((name, _)) = p.split_once('/').filter(|(n, _)| !won.contains_key(*n)) {
                put(&plug.join("skills"), f)?;
                got.insert(name.to_string(), label.to_string());
            }
        }
        won.extend(got);
    }
    // the catalog only as far as this repo enabled it; names are NAME-checked, so no `..`
    let enabled: std::collections::BTreeSet<_> = slots(&wf).iter()
        .flat_map(|(_, s)| s["skills"].as_array().unwrap().iter().map(|k| k.as_str().unwrap().to_string())).collect();
    for name in enabled.into_iter().filter(|n| !won.contains_key(n)).collect::<Vec<_>>() {
        let files = embedded(&format!("skills/{name}/"));
        if !files.is_empty() {
            for f in &files {
                put(&plug.join("skills").join(&name), f)?;
            }
            won.insert(name, "catalog".to_string());
        }
    }
    let at = |src: &str, sub: &str, f: &str| if src == home { format!("{sub}/{f} in {home}") } else { format!("{src}/{f} on {base}") };
    for (name, src) in &won {
        let at = if src == "catalog" { format!("catalog skill {name}") } else { at(src, "skills", name) };
        if !NAME.is_match(name) {
            return Err(format!("{at}: a skill name must match `[a-z0-9][a-z0-9-]*`."));
        }
        let md = fs::read_to_string(plug.join("skills").join(name).join("SKILL.md")).unwrap_or_default();
        frontmatter(&md, &names("skill_keys"), &at)?;
    }
    let mut agents = BTreeMap::new();
    for (name, src) in &by {
        let text = fs::read_to_string(plug.join("agents").join(format!("{name}.md"))).unwrap_or_default();
        let at = if src == "built-in" { format!("agent {name}") } else { at(src, "agents", &format!("{name}.md")) };
        agents.insert(name.clone(), parse_agent(name, &text, &at)?);
    }
    // --agent with a name that does not exist is silently ignored by the CLI: never pass one
    for (slot, s) in slots(&wf) {
        let agent: Vec<&str> = s["agent"].as_str().into_iter().collect();
        let skills: Vec<&str> = s["skills"].as_array().unwrap().iter().filter_map(Value::as_str).collect();
        for (kind, list, have) in [("agent", agent, agents.keys().collect::<Vec<_>>()), ("skill", skills, won.keys().collect())] {
            if let Some(missing) = list.iter().find(|x| !have.iter().any(|h| h == *x)) {
                let src = match &owf {
                    Some(o) if slots(o).iter().any(|(l, os)| *l == slot && *os == s) => format!("workflow.json in {home}"),
                    _ => format!(".rookery/workflow.json on {base}"),
                };
                return Err(format!("{src}: `{slot}` names the {kind} `{missing}`, and there is no {kind} by that name."));
            }
        }
    }
    Ok(Steer { claude_md: md, workflow: wf, agents, agent_sources: by, sources: won })
}

/// ~/.rookery (ROOKERY_HOME overrides it): the defaults layer and the worktrees.
pub fn rookery_home() -> PathBuf {
    if let Some(h) = std::env::var_os("ROOKERY_HOME") {
        return h.into();
    }
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap_or_default();
    PathBuf::from(home).join(".rookery")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> Vec<Value> {
        let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../fixtures").join(name);
        serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap()
    }

    #[test]
    fn workflow_fixtures() {
        for f in fixture("workflow.json") {
            match workflow(f["given"].as_str().unwrap(), "x", None) {
                Ok(got) => assert_eq!(Some(&got), f.get("expect"), "{}", f["why"]),
                Err(e) => assert!(f["error"].as_str().is_some_and(|w| e.contains(w)), "{}: {e}", f["why"]),
            }
        }
    }

    #[test]
    fn frontmatter_fixtures() {
        for f in fixture("frontmatter.json") {
            let text = f["text"].as_str().unwrap();
            let got = if f["kind"] == "agent" { parse_agent("x", text, "x") } else { frontmatter(text, &names("skill_keys"), "x") };
            match got {
                Ok(meta) => assert_eq!(Some(&json!(meta)), f.get("meta"), "{}", f["why"]),
                Err(e) => {
                    let w = f["error"].as_str().unwrap_or_else(|| panic!("{}: {e}", f["why"]));
                    assert!(e.contains(&format!("`{w}`")) || w == "filename" && e.contains("filename"), "{}: {e}", f["why"]);
                }
            }
        }
    }

    #[test]
    fn steer_reads_base_with_precedence() {
        let tmp = std::env::temp_dir().join(format!("rookery-steer-{}", std::process::id()));
        let (repo, defaults, plug) = (tmp.join("repo"), tmp.join("defaults"), tmp.join("plug"));
        let _ = fs::remove_dir_all(&tmp);
        let write = |p: PathBuf, t: &str| {
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, t).unwrap();
        };
        for (p, t) in [(".rookery/workflow.json", r#"{"task": {"skills": ["verify", "systematic-debugging"]},
                                                      "plan": {"skills": ["repo-recon", "house"]}}"#),
                       (".rookery/skills/verify/SKILL.md", "---\nname: verify\n---\nfrom .rookery\n"),
                       (".claude/skills/verify/SKILL.md", "---\nname: verify\n---\nloses\n"),
                       (".claude/skills/verify/run.sh", "the loser's file\n"),
                       (".claude/skills/repo-recon/SKILL.md", "---\nname: repo-recon\n---\nthe repo's own\n"),
                       (".rookery/agents/reviewer.md", "---\nname: reviewer\nmodel: sonnet\n---\nthe repo's reviewer\n"),
                       (".rookery/agents/sub/x.md", "---\nname: x\n---\n"), ("CLAUDE.md", "Use tabs.\n")] {
            write(repo.join(p), t);
        }
        write(defaults.join("skills/house/SKILL.md"), "---\nname: house\n---\nhouse style\n");
        write(defaults.join("skills/verify/SKILL.md"), "---\nname: verify\n---\nloses too\n");
        let g = |a: &[&str]| git(&repo, a).unwrap();
        g(&["init", "-q", "-b", "main"]);
        g(&["-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"]);
        g(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]);
        write(repo.join("CLAUDE.md"), "the working tree is not base\n");   // read from base, never here
        let sha = git_text(&repo, &["rev-parse", "HEAD"]).unwrap();

        let s = steer(&repo, "main", &sha, &defaults, &plug).unwrap();
        assert_eq!(s.claude_md, "Use tabs.");
        let src: Vec<_> = s.sources.iter().map(|(k, v)| format!("{k}={v}")).collect();
        assert_eq!(src, ["house=~/.rookery/defaults", "repo-recon=.claude/skills", "systematic-debugging=catalog",
                         "verify=.rookery/skills"]);
        assert!(fs::read_to_string(plug.join("skills/verify/SKILL.md")).unwrap().ends_with("from .rookery\n"));
        assert!(!plug.join("skills/verify/run.sh").exists(), "the winner's files only, never a merge of two");
        assert!(plug.join("skills/systematic-debugging/find-polluter.sh").exists(), "an enabled catalog skill, files and all");
        assert!(!plug.join("skills/changelog").exists(), "a catalog skill nobody enabled stays out");
        assert_eq!(s.agents["reviewer"]["model"], "sonnet", "a repo agent replaces the role by name");
        assert!(!plug.join("agents/x.md").exists() && plug.join("agents/planmaker.md").exists(), "top-level *.md only");

        write(repo.join(".rookery/workflow.json"), r#"{"review": {"agent": "ghost"}}"#);
        g(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "ghost"]);
        let sha = git_text(&repo, &["rev-parse", "HEAD"]).unwrap();
        let e = steer(&repo, "main", &sha, &defaults, &tmp.join("plug2")).err().unwrap();
        assert!(e.contains("`review` names the agent `ghost`"), "--agent would ignore it silently: {e}");
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn builtins_are_embedded_and_valid() {
        let agents = embedded("agents/");
        let mut got: Vec<_> = agents.iter().map(|(p, _, _)| p.as_str()).collect();
        got.sort();
        assert_eq!(got, ["planmaker.md", "pr-reviewer.md", "reviewer.md", "scope-checker.md"]);
        for (p, b, _) in &agents {
            let meta = parse_agent(p.trim_end_matches(".md"), &String::from_utf8_lossy(b), p).unwrap();
            assert_eq!(meta["model"], "opus", "roles think; tasks grind on sonnet");
        }
        assert!(!embedded("skills/systematic-debugging/").is_empty() && embedded("skills/README.md").is_empty());
    }
}
