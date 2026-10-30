//! `rookery config`: the provider every `claude` call runs on, and its API key, kept in
//! ~/.rookery/config.json (`provider`, `keys`, next to `max_usd`). The table (URL, what the
//! aliases resolve to, prices) is agent.py's PROVIDERS, from prompts/defaults.json. `claude` is the
//! user's own /login: no key, and the call's env is left as it is.
use crate::text::DEFAULTS;
use crate::{input, paint};
use serde_json::{Map, Value};
use std::io::{IsTerminal, Write};
use std::path::PathBuf;

/// (name, label, what it is when it has no `models`, where a key is made), in the picker's order.
/// The names are defaults.json's; a test holds the two lists together.
const LABELS: [(&str, &str, &str, &str); 4] = [
    ("claude", "Claude login", "your claude /login: Pro, Max or Team", ""),
    ("anthropic", "Anthropic API", "Claude models, billed per token", "console.anthropic.com"),
    ("deepseek", "DeepSeek", "", "platform.deepseek.com"),
    ("glm", "GLM (Z.ai)", "", "z.ai"),
];

pub fn path() -> PathBuf {
    crate::steer::rookery_home().join("config.json")
}

/// config.json as an object; none yet is an empty one.
pub fn load() -> Result<Map<String, Value>, String> {
    let p = path();
    let Ok(t) = std::fs::read_to_string(&p) else { return Ok(Map::new()) };
    match serde_json::from_str(&t).map_err(|e| format!("{}: {e}", p.display()))? {
        Value::Object(m) => Ok(m),
        _ => Err(format!("{}: not a JSON object", p.display())),
    }
}

/// Written whole to a file beside it, then renamed over it: a crash never leaves half a file.
/// On Unix only its owner can read it; on Windows the profile folder's ACL does that.
fn save(c: &Map<String, Value>) -> Result<(), String> {
    let p = path();
    let dir = p.parent().unwrap();
    std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let tmp = dir.join(format!("config.json.{}", std::process::id()));
    let mut o = std::fs::OpenOptions::new();
    o.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut o, 0o600);
    let text = serde_json::to_string_pretty(c).unwrap() + "\n";
    o.open(&tmp).and_then(|mut f| f.write_all(text.as_bytes())).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, &p).map_err(|e| format!("{}: {e}", p.display()))
}

fn table() -> &'static Map<String, Value> {
    DEFAULTS["providers"].as_object().expect("defaults.json has no providers")
}

fn needs_key(name: &str) -> bool {
    table()[name].as_object().is_some_and(|p| !p.is_empty())
}

fn label(name: &str) -> &str {
    LABELS.iter().find(|l| l.0 == name).map_or(name, |l| l.1)
}

fn key_of<'a>(c: &'a Map<String, Value>, name: &str) -> Option<&'a str> {
    c.get("keys")?.get(name)?.as_str().filter(|k| !k.is_empty())
}

/// `…` and the last 4 characters: all of a key that is ever printed.
fn last4(key: &str) -> String {
    let c: Vec<char> = key.chars().collect();
    format!("…{}", c[c.len().saturating_sub(4)..].iter().collect::<String>())
}

/// The provider and its key from `c`: `provider`, else `claude`. One that is unknown or has no key
/// is an error, never a quiet fallback to the login.
fn resolve(c: &Map<String, Value>) -> Result<(String, Option<String>), String> {
    let name = c.get("provider").and_then(Value::as_str).unwrap_or("claude");
    if !table().contains_key(name) {
        return Err(format!("{}: provider {name:?} is not one of {}. Run `rookery config` to pick one.",
                           path().display(), table().keys().cloned().collect::<Vec<_>>().join(", ")));
    }
    if !needs_key(name) {
        return Ok((name.into(), None));
    }
    match key_of(c, name) {
        Some(k) => Ok((name.into(), Some(k.into()))),
        None => Err(format!("{} is selected but has no API key. Run `rookery config` to add one.", label(name))),
    }
}

/// What every call runs on now: read for each call, as the server reads provider_for().
pub fn active() -> Result<(String, Option<String>), String> {
    resolve(&load()?)
}

/// `active()` in a few words, for doctor.
pub fn describe(name: &str, key: Option<&str>) -> String {
    format!("{}{}", label(name), key.map(|k| format!(" (key {})", last4(k))).unwrap_or_default())
}

/// agent.py's provider_env(): the env overrides for one call; none for the login.
pub fn env(name: &str, key: Option<&str>) -> Vec<(String, String)> {
    let Some(key) = key else { return vec![] };
    let p = &table()[name];
    let mut e = vec![("ANTHROPIC_BASE_URL".to_string(), p["url"].as_str().unwrap().to_string()), ("ANTHROPIC_API_KEY".into(), key.into())];
    e.extend(p["models"].as_object().into_iter().flatten()
        .map(|(a, m)| (format!("ANTHROPIC_DEFAULT_{}_MODEL", a.to_uppercase()), m.as_str().unwrap().to_string())));
    e
}

/// agent.py's cost_of(): the CLI's own figure on Claude, the provider's price table elsewhere
/// (USD per million tokens: input, cache hit, output). A model the table lacks is an error.
pub fn cost(out: &Value, name: &str) -> Result<f64, String> {
    let Some(price) = table()[name]["price"].as_object() else { return Ok(out["total_cost_usd"].as_f64().unwrap_or(0.0)) };
    let mut usd = 0.0;
    for (m, u) in out["modelUsage"].as_object().into_iter().flatten() {
        let p = price.get(m).and_then(Value::as_array)
            .ok_or_else(|| format!("no {name} price for model {m:?}: add it to providers in prompts/defaults.json"))?;
        let (i, hit, o) = (p[0].as_f64().unwrap(), p[1].as_f64().unwrap(), p[2].as_f64().unwrap());
        let n = |k: &str| u[k].as_f64().unwrap_or(0.0);
        // a cache write is billed as a plain input token there
        usd += ((n("inputTokens") + n("cacheCreationInputTokens")) * i + n("cacheReadInputTokens") * hit + n("outputTokens") * o) / 1e6;
    }
    Ok(usd)
}

/// Select `name`, with `key` when one is given (else the saved one stays).
fn choose(c: &mut Map<String, Value>, name: &str, key: Option<String>) {
    c.insert("provider".into(), name.into());
    if let Some(k) = key {
        let keys = c.entry("keys").or_insert_with(|| Value::Object(Map::new()));
        if !keys.is_object() {
            *keys = Value::Object(Map::new());
        }
        keys.as_object_mut().unwrap().insert(name.into(), k.into());
    }
}

/// Remove `name`'s key -> whether it was in use, which puts the calls back on the login.
fn forget(c: &mut Map<String, Value>, name: &str) -> bool {
    if let Some(k) = c.get_mut("keys").and_then(Value::as_object_mut) {
        k.remove(name);
    }
    let in_use = c.get("provider").and_then(Value::as_str) == Some(name);
    if in_use {
        c.insert("provider".into(), "claude".into());
    }
    in_use
}

/// One picker row: label, what it runs, and whether it is in use or has a key.
fn row(c: &Map<String, Value>, name: &str) -> String {
    let what = match LABELS.iter().find(|l| l.0 == name).map(|l| l.2) {
        Some(w) if !w.is_empty() => w.to_string(),
        _ => {
            let mut m: Vec<&str> = table()[name]["models"].as_object().into_iter().flatten().filter_map(|(_, v)| v.as_str()).collect();
            m.dedup();
            m.join(", ")
        }
    };
    let used = c.get("provider").and_then(Value::as_str).unwrap_or("claude") == name;
    let state = match (used, needs_key(name), key_of(c, name)) {
        (true, true, None) => paint("31", "selected, no key"),
        (true, _, _) => paint("32", "● in use"),
        (false, true, Some(k)) => paint("2", &format!("key {}", last4(k))),
        (false, true, None) => paint("2", "no key"),
        (false, false, _) => String::new(),
    };
    format!("{:<14} {} {state}", label(name), paint("2", &format!("{what:<38}")))
}

/// A key typed or pasted in a box that shows bullets, or one line from stdin when piped.
/// Some("") keeps the saved key; None is Esc.
fn read_key(name: &str, saved: Option<&str>) -> Result<Option<String>, String> {
    let at = LABELS.iter().find(|l| l.0 == name).map_or("", |l| l.3);
    let k = if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() {
        println!("{} {}", paint("1", &format!("{} API key", label(name))),
                 paint("2", &if at.is_empty() { String::new() } else { format!("(make one at {at})") }));
        let placeholder = match saved {
            Some(k) => format!("Enter keeps the saved key {}", last4(k)),
            None => "Paste the key here".into(),
        };
        match input::secret(&placeholder, "the key shows as dots · enter saves · esc goes back").map_err(|e| e.to_string())? {
            Some(k) => k,
            None => return Ok(None),
        }
    } else {
        let mut l = String::new();
        std::io::stdin().read_line(&mut l).map_err(|e| e.to_string())?;
        l
    };
    let k = k.trim().to_string();
    if k.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err("an API key has no spaces in it: nothing was saved".into());
    }
    Ok(Some(k))
}

/// `rookery config [<provider>] [--forget]`: no provider opens the picker on a terminal (or
/// lists them, piped); a provider is selected, asking for its key when it has none.
pub fn config(name: Option<String>, drop_key: bool) -> Result<(), String> {
    let mut c = load()?;
    let names: Vec<String> = table().keys().cloned().collect();
    if let Some(n) = &name {
        if !table().contains_key(n) {
            return Err(format!("{n} is not a provider: one of {}", names.join(", ")));
        }
    }
    if drop_key {
        let Some(n) = name else { return Err("name the provider whose key to forget: rookery config <provider> --forget".into()) };
        let was = forget(&mut c, &n);
        save(&c)?;
        println!("{} key removed{}", label(&n), if was { "; calls run on your Claude login again" } else { "" });
        return Ok(());
    }
    let tty = std::io::stdin().is_terminal() && std::io::stdout().is_terminal();
    if name.is_none() && !tty {
        for n in &names {
            println!("  {:<10} {}", n, row(&c, n));
        }
        println!("\nset one: rookery config <provider>   (the key is read from the next line of input)");
        return Ok(());
    }
    let picked = name.is_none();
    let mut name = name;
    loop {
        let n = match name.take() {
            Some(n) => n,
            None => {
                let rows: Vec<String> = names.iter().map(|n| row(&c, n)).collect();
                let at = names.iter().position(|n| c.get("provider").and_then(Value::as_str).unwrap_or("claude") == n).unwrap_or(0);
                let title = format!("{} {}", paint("1", "Provider"), paint("2", "what every claude call runs on"));
                match input::pick(&title, "↑↓ choose · enter selects · d forgets a key · esc leaves", &rows, at, "d").map_err(|e| e.to_string())? {
                    None => { println!("{}", paint("2", "Nothing changed.")); return Ok(()) }
                    Some((i, 'd')) => {
                        if key_of(&c, &names[i]).is_some() {
                            forget(&mut c, &names[i]);
                            save(&c)?;
                        }
                        continue;
                    }
                    Some((i, _)) => names[i].clone(),
                }
            }
        };
        let key = if needs_key(&n) {
            match read_key(&n, key_of(&c, &n))? {
                None if picked => continue, // Esc: back to the list
                None => { println!("{}", paint("2", "Nothing changed.")); return Ok(()) }
                Some(k) if k.is_empty() && key_of(&c, &n).is_none() => return Err(format!("no key given: {} needs one", label(&n))),
                Some(k) if k.is_empty() => None,
                Some(k) => Some(k),
            }
        } else {
            None
        };
        choose(&mut c, &n, key);
        save(&c)?;
        let (n, k) = resolve(&c)?;
        println!("{} {} {}", paint("32", "✓"), paint("1", &describe(&n, k.as_deref())),
                 paint("2", &format!("runs every claude call now; saved in {}", path().display())));
        if k.is_some() {
            println!("  {}", paint("2", "check the key: rookery doctor (one small call)"));
        }
        return Ok(());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn every_provider_has_a_label() {
        assert_eq!(table().keys().collect::<Vec<_>>(), LABELS.iter().map(|l| l.0).collect::<Vec<_>>());
    }

    #[test]
    fn env_and_cost_are_the_servers() {
        // test_agent.py holds agent.py's provider_env() and cost_of() to these same numbers
        assert!(env("claude", None).is_empty());
        let e: Map<String, Value> = env("deepseek", Some("sk-test")).into_iter().map(|(k, v)| (k, v.into())).collect();
        assert_eq!(Value::Object(e), json!({
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_API_KEY": "sk-test",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro", "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash"}));
        assert_eq!(env("anthropic", Some("sk-ant")).len(), 2, "Claude by key: the CLI's own aliases");
        let u = json!({"inputTokens": 1e6, "cacheCreationInputTokens": 1e6, "cacheReadInputTokens": 1e6, "outputTokens": 1e6});
        let out = json!({"total_cost_usd": 99, "modelUsage": {"deepseek-flash": u, "deepseek-v4-pro": u}});
        assert!((cost(&out, "deepseek").unwrap() - (0.30 * 2.0 + 0.006 + 1.20 + 1.32 * 2.0 + 0.044 + 3.96)).abs() < 1e-9);
        assert!(cost(&json!({"modelUsage": {"deepseek-chat": u}}), "deepseek").unwrap_err().contains("deepseek-chat"));
        assert_eq!(cost(&json!({"total_cost_usd": 0.3, "modelUsage": {"claude-x": {}}}), "anthropic").unwrap(), 0.3);
    }

    #[test]
    fn choose_and_forget() {
        let mut c = Map::new();
        assert_eq!(resolve(&c).unwrap(), ("claude".into(), None), "nothing set: the login");
        c.insert("max_usd".into(), json!(5));
        c.insert("provider".into(), json!("glm"));
        assert!(resolve(&c).unwrap_err().contains("no API key"), "never a quiet fallback");
        choose(&mut c, "glm", Some("zk-1234abcd".into()));
        choose(&mut c, "deepseek", Some("sk-9".into()));
        choose(&mut c, "glm", None);
        assert_eq!(resolve(&c).unwrap(), ("glm".into(), Some("zk-1234abcd".into())), "the saved key stays");
        assert_eq!(c["max_usd"], 5, "the rest of config.json stays");
        assert!(!forget(&mut c, "deepseek"));
        assert!(forget(&mut c, "glm"), "the one in use: back to the login");
        assert_eq!(resolve(&c).unwrap(), ("claude".into(), None));
        assert_eq!(c["keys"], json!({}));
        c.insert("provider".into(), json!("nope"));
        assert!(resolve(&c).is_err());
        assert_eq!(last4("sk-abcdef"), "…cdef");
        assert_eq!(last4("ab"), "…ab");
    }
}
