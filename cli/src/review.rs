//! agent.py's render_review() and located(): a reviewer's STATUS and <review> JSON -> the markdown
//! of its section. A reply off that shape fails, like a missing STATUS marker. fixtures/review.json
//! holds both sides to the same output.
use crate::claude::tail;
use crate::text::DEFAULTS;
use regex::Regex;
use serde_json::Value;
use std::path::Path;
use std::sync::LazyLock;

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
}
