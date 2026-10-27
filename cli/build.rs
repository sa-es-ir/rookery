//! Embeds the built-in agents (agents/*.md) and the skill catalog (skills/<name>/**) from the repo
//! root, so the binary carries them as the server's checkout does. Prompts are include_str!'d.
use std::{env, fs, path::Path};

fn walk(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else {
            out.push(p);
        }
    }
}

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let mut files = vec![];
    walk(&root.join("agents"), &mut files);
    walk(&root.join("skills"), &mut files);
    files.sort();
    let mut src = String::from("pub static FILES: &[(&str, &[u8])] = &[\n");
    for f in files {
        let rel = f.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
        let parts = rel.split('/').count();
        // agents: top-level *.md only, as the CLI reads them; skills: inside a skill's dir only
        let keep = if rel.starts_with("agents/") { parts == 2 && rel.ends_with(".md") } else { parts >= 3 };
        if keep {
            src += &format!("    ({rel:?}, include_bytes!({:?})),\n", f.to_string_lossy());
        }
    }
    src += "];\n";
    fs::write(Path::new(&env::var("OUT_DIR").unwrap()).join("embedded.rs"), src).unwrap();
    println!("cargo:rerun-if-changed=../agents");
    println!("cargo:rerun-if-changed=../skills");
}
