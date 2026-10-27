//! What agent.py shares with this binary: the prompts and defaults it reads at import, and how
//! a reply is parsed. fixtures/*.json hold both sides to the same behaviour.
use regex::Regex;
use serde_json::Value;
use std::sync::LazyLock;

// the ones a local run sends; the server-only ones (with-*, not-mounted, follow-merged/-conflicts) stay out
const PROMPTS: &[(&str, &str)] = &[
    ("plan", include_str!("../../prompts/plan.md")),
    ("reply", include_str!("../../prompts/reply.md")),
    ("task", include_str!("../../prompts/task.md")),
    ("review", include_str!("../../prompts/review.md")),
    ("check", include_str!("../../prompts/check.md")),
    ("write", include_str!("../../prompts/write.md")),
    ("pr-review", include_str!("../../prompts/pr-review.md")),
    ("follow-up", include_str!("../../prompts/follow-up.md")),
    ("use-skill", include_str!("../../prompts/use-skill.md")),
    ("offer-skills", include_str!("../../prompts/offer-skills.md")),
    ("memory-note", include_str!("../../prompts/memory-note.md")),
    ("system", include_str!("../../prompts/system.md")),
    ("remember", include_str!("../../prompts/remember.md")),
    ("rewrite", include_str!("../../prompts/rewrite.md")),
];

pub static DEFAULTS: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(include_str!("../../prompts/defaults.json")).expect("prompts/defaults.json"));

/// prompts/<name>.md as agent.py reads it: a CRLF checkout reads as LF there too.
pub fn prompt(name: &str) -> String {
    let (_, t) = PROMPTS.iter().find(|(n, _)| *n == name).unwrap_or_else(|| panic!("no prompts/{name}.md"));
    t.replace("\r\n", "\n")
}

/// A defaults.json number.
pub fn cap(key: &str) -> u64 {
    DEFAULTS[key].as_u64().unwrap_or_else(|| panic!("defaults.json has no number {key}"))
}

/// A defaults.json list of strings.
pub fn names(key: &str) -> Vec<String> {
    DEFAULTS[key].as_array().into_iter().flatten().filter_map(|v| v.as_str().map(String::from)).collect()
}

/// Python's str.format, as far as prompts/ use it: `{name}`, `{name:,}`, `{{`, `}}`. A field
/// with no value panics, as a KeyError would: the templates are compiled in, the tests run them.
pub fn fmt(t: &str, args: &[(&str, String)]) -> String {
    let mut out = String::with_capacity(t.len());
    let mut rest = t;
    while let Some(i) = rest.find(['{', '}']) {
        out.push_str(&rest[..i]);
        let r = &rest[i..];
        if r.starts_with("{{") || r.starts_with("}}") {
            out.push_str(&r[..1]);
            rest = &r[2..];
            continue;
        }
        assert!(r.starts_with('{'), "a single }} in a template");
        let end = r.find('}').expect("an unclosed { in a template");
        let (name, spec) = r[1..end].split_once(':').unwrap_or((&r[1..end], ""));
        let v = &args.iter().find(|(k, _)| *k == name).unwrap_or_else(|| panic!("no value for {{{name}}}")).1;
        match spec {
            "" => out.push_str(v),
            "," => out.push_str(&grouped(v)),
            _ => panic!("format spec {spec:?} is not supported"),
        }
        rest = &r[end + 1..];
    }
    out + rest
}

fn grouped(v: &str) -> String {
    let (sign, digits) = v.split_at(v.starts_with('-') as usize);
    let mut out = String::from(sign);
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

// alone on its line; markdown bold or code around it is tolerated (agent.py's MARKER, same pattern)
static MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?m)^(?:\*\*|`)?STATUS:(?:\*\*)?[ \t]*([\w-]+)[ \t]*(?:\*\*|`)?[ \t]*$").unwrap()
});

/// -> (STATUS value, the reply without its marker line and without a fence around the whole of
/// it), or None: parsed, never inferred.
pub fn marker(text: &str) -> Option<(String, String)> {
    let c = MARKER.captures(text)?;
    let m = c.get(0).unwrap();
    let reply = format!("{}{}", &text[..m.start()], &text[m.end()..]);
    let reply = reply.trim();
    Some((c[1].to_string(), unfence(reply).map_or(reply, str::trim).to_string()))
}

/// agent.py's FENCED, ```[\w-]*\n((?:(?!```).)*?)\n?```, fullmatched: the regex crate has no
/// lookahead, so by hand. The first ``` after the opening line must be the closing one.
fn unfence(s: &str) -> Option<&str> {
    let (info, body) = s.strip_prefix("```")?.split_once('\n')?;
    if !info.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '-') || body.find("```")? + 3 != body.len() {
        return None;
    }
    let body = &body[..body.len() - 3];
    Some(body.strip_suffix('\n').unwrap_or(body))
}

static TASK_LINE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?mi)^[ \t]*[-*] \[( |x)(\] .+)$").unwrap());

/// agent.py's read_tasks() on tasks.md's text -> [(byte offset of the checkbox char, done?, text)].
pub fn read_tasks(text: &str) -> Vec<(usize, bool, String)> {
    TASK_LINE.captures_iter(text)
        .map(|c| (c.get(1).unwrap().start(), &c[1] != " ", c[2][2..].trim().to_string()))
        .collect()
}

/// agent.py's tick(): the checkbox at `at` (from read_tasks()) becomes `x`. One byte, so the
/// task text around it is never disturbed. The orchestrator ticks, never the agent.
pub fn tick(text: &str, at: usize) -> String {
    format!("{}x{}", &text[..at], &text[at + 1..])
}

/// agent.py's summary(): a task's first sentence, plain, cut to summary_cap: the line a reader scans.
pub fn summary(text: &str) -> String {
    let t = text.replace('`', "");
    let s = t.char_indices().find(|&(i, c)| c == '.' && t[i + 1..].starts_with(char::is_whitespace)).map_or(&t[..], |(i, _)| &t[..=i]);
    let max = cap("summary_cap") as usize;
    if s.chars().count() <= max {
        return s.to_string();
    }
    let cut: String = s.chars().take(max - 1).collect();
    cut.rsplit_once(' ').map_or(&cut[..], |(a, _)| a).to_string() + "…"
}

/// Python's str.splitlines(): every line break it knows, no trailing empty line.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            out.push(&s[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && it.peek().map(|p| p.1) == Some('\n') {
                it.next();
                start += 1;
            }
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn fixture(name: &str) -> Vec<Value> {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fixtures").join(name);
        serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap()
    }

    #[test]
    fn marker_fixtures() {
        for f in fixture("marker.json") {
            let got = marker(f["text"].as_str().unwrap());
            match f["status"].as_str() {
                None => assert!(got.is_none(), "{}", f["why"]),
                Some(s) => assert_eq!(got, Some((s.into(), f["reply"].as_str().unwrap().into())), "{}", f["why"]),
            }
        }
    }

    #[test]
    fn task_fixtures() {
        for f in fixture("tasks.json") {
            let text = f["text"].as_str().unwrap();
            let got: Vec<Value> = read_tasks(text).into_iter().map(|(_, d, t)| serde_json::json!([d, t])).collect();
            assert_eq!(Value::from(got), f["tasks"], "{}", f["why"]);
            let mut text = tick(text, read_tasks(text)[f["tick"].as_u64().unwrap() as usize].0);
            assert_eq!(text, f["ticked"].as_str().unwrap(), "ticking must not disturb the text");
            // every tick strictly reduces the open count, so the loop terminates
            while let Some((at, _, _)) = read_tasks(&text).into_iter().find(|t| !t.1) {
                let open = read_tasks(&text).iter().filter(|t| !t.1).count();
                text = tick(&text, at);
                assert_eq!(read_tasks(&text).iter().filter(|t| !t.1).count(), open - 1);
            }
        }
    }

    #[test]
    fn format_fixtures() {
        for f in fixture("format.json") {
            let args: Vec<(&str, String)> = f["args"].as_object().unwrap().iter()
                .map(|(k, v)| (k.as_str(), v.as_str().map_or_else(|| v.to_string(), String::from)))
                .collect();
            assert_eq!(fmt(f["template"].as_str().unwrap(), &args), f["out"].as_str().unwrap(), "{}", f["why"]);
        }
    }

    #[test]
    fn every_prompt_renders() {
        // a field this renderer cannot fill (a new spec, a stray brace) fails here, not mid-run
        let field = Regex::new(r"\{(\w+)(?::[^}]*)?\}").unwrap();
        for (name, _) in PROMPTS {
            let t = prompt(name);
            let args: Vec<(&str, String)> = field.captures_iter(&t).filter_map(|c| c.get(1))
                .map(|m| (&t[m.range()], "1".to_string())).collect();
            fmt(&t, &args);
        }
        assert!(cap("max_tasks") > 0 && names("models").contains(&"opus".to_string()));
    }

    #[test]
    fn summary_like_python() {
        // agent.summary() on the same input
        assert_eq!(summary("Add `x` to a.py. Then run it."), "Add x to a.py.");
        assert_eq!(summary("v1.2 stays. Next"), "v1.2 stays.");
        let long = "word ".repeat(30);
        assert_eq!(summary(&long), "word ".repeat(19).trim_end().to_string() + "…");
    }

    #[test]
    fn splitlines_like_python() {
        assert_eq!(splitlines("a\r\nb\rc\n\nd\n"), ["a", "b", "c", "", "d"]);
        assert!(splitlines("").is_empty());
    }
}
