//! A run's checklist at the bottom of the screen: its tasks, `after` steps and review, what runs
//! now, what it is doing, how long and how much. Inline, never a full-screen TUI: the output above
//! scrolls, only these rows are redrawn, and the run's end leaves them as its record. Piped or under
//! NO_COLOR there is no region: one line per event, so a CI log reads.
use crate::paint;
use crate::text::fit;
use serde_json::Value;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering::SeqCst};
use std::time::{Duration, Instant};

/// the one region on the screen; the stream loop, the run and the tick thread all reach it here
static LIVE: Mutex<Option<Live>> = Mutex::new(None);
/// which start() a tick thread belongs to: a thread whose run ended stops, even if a new one began
static GEN: AtomicU64 = AtomicU64::new(0);
pub const SPIN: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
pub const GREEN: &str = "38;2;168;204;104"; // the logo's light green

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Mark {
    Pending,
    Running,
    Done,
    Warn,
    Skipped,
    Failed,
}

/// One row: a task (`task-<i>`), an `after` step (`step-<agent>`), `review` or `follow-up`, keyed
/// as its claude call is.
pub struct Row {
    key: String,
    num: String,
    label: String,
    mark: Mark,
    note: String,
    secs: Option<f64>,
    usd: Option<f64>,
    since: Option<(Instant, f64)>, // when it started, and what the run had spent by then
}

impl Row {
    pub fn new(key: &str, num: &str, label: &str) -> Row {
        Row { key: key.into(), num: num.into(), label: label.into(), mark: Mark::Pending, note: String::new(), secs: None, usd: None, since: None }
    }

    /// Done before this run: a task ticked earlier, with its commit.
    pub fn done(mut self, note: &str) -> Row {
        (self.mark, self.note) = (Mark::Done, note.into());
        self
    }

    fn task(&self) -> bool {
        self.key.starts_with("task-")
    }
}

struct Live {
    rows: Vec<Row>,
    thinking: String,
    tool: String,
    started: Instant,
    spent: f64,
    drawn: u16,
    tty: bool,
    root: PathBuf,
    frame: usize,
    generation: u64,
}

/// m:ss
fn clock(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    format!("{}:{:02}", s / 60, s % 60)
}

/// `s` cut (`…`) to `w` characters, never padded.
fn cut(s: &str, w: usize) -> String {
    if s.chars().count() <= w { s.to_string() } else { fit(s, w) }
}

/// What a tool call works on, from its `input`: a path, a command's first line, a pattern, a
/// description. Never a file's content or a command's output.
pub fn target(name: &str, input: &Value, root: &Path) -> String {
    let s = |k: &str| input[k].as_str().unwrap_or("").to_string();
    match name {
        "Edit" | "Write" | "MultiEdit" | "Read" | "NotebookEdit" => {
            let p = input["file_path"].as_str().or(input["notebook_path"].as_str()).unwrap_or("");
            Path::new(p).strip_prefix(root).map_or(p.to_string(), |r| r.display().to_string())
        }
        "Bash" => s("command").lines().next().unwrap_or("").to_string(),
        "Grep" | "Glob" => s("pattern"),
        "Task" | "Agent" => s("description"),
        _ => String::new(),
    }
}

impl Live {
    fn total(&self) -> (usize, usize) {
        let tasks = self.rows.iter().filter(|r| r.task());
        let done = tasks.clone().filter(|r| matches!(r.mark, Mark::Done | Mark::Warn)).count();
        (done, tasks.count())
    }

    fn mark(&self, m: Mark) -> String {
        match m {
            Mark::Pending => paint("2", "·"),
            Mark::Running => paint(GREEN, &SPIN[self.frame % SPIN.len()].to_string()),
            Mark::Done => paint(GREEN, "✓"),
            Mark::Warn => paint("33", "⚠"),
            Mark::Skipped => paint("2", "–"),
            Mark::Failed => paint("31", "✗"),
        }
    }

    /// One row, cut to `w`; colour only from paint(), so the tests read plain text.
    fn row(&self, r: &Row, w: usize, lw: usize, nw: usize) -> String {
        let mark = self.mark(r.mark);
        let time = match (r.secs, r.since) {
            (Some(s), _) => clock(s),
            (None, Some((t, _))) if r.mark == Mark::Running => clock(t.elapsed().as_secs_f64()),
            _ => String::new(),
        };
        let usd = r.usd.map(|u| format!("${u:.2}")).unwrap_or_default();
        let rest = format!("{:>2}  {}  {}  {time:>5}  {usd:>6}", r.num, fit(&r.label, lw), fit(&r.note, nw));
        let rest = cut(rest.trim_end(), w.saturating_sub(5));
        format!("  {mark}  {}", if matches!(r.mark, Mark::Pending | Mark::Skipped) { paint("2", &rest) } else { rest })
    }

    /// The region for a `w`×`h` terminal. Every row is cut to `w - 1`, so the rows printed are the
    /// rows to erase; under the height it needs, only the running row, its activity and the footer.
    /// The last frame has no spinner, no activity and no hint: it stays as the run's record.
    fn frame(&self, w: usize, h: usize, last: bool) -> Vec<String> {
        let w = w.saturating_sub(1).max(20);
        let nw = self.rows.iter().map(|r| r.note.chars().count()).max().unwrap_or(0);
        let lw = self.rows.iter().map(|r| r.label.chars().count()).max().unwrap_or(0).min((w.saturating_sub(5)).saturating_sub(nw + 21)).max(8);
        let (done, total) = self.total();
        let mut foot = vec![];
        if total > 0 {
            foot.push(format!("{done}/{total}"));
        }
        foot.extend([clock(self.started.elapsed().as_secs_f64()), format!("${:.2}", self.spent)]);
        if !last {
            foot.push("Ctrl-C stops, committed tasks stay".into());
        }
        let foot = paint("2", &cut(&format!("  {}", foot.join(" · ")), w));
        let act: Vec<String> = if last { vec![] } else {
            [("∴", &self.thinking), ("→", &self.tool)].into_iter().filter(|(_, t)| !t.is_empty())
                .map(|(m, t)| paint("2", &cut(&format!("        {m} {t}"), w))).collect()
        };
        let running = self.rows.iter().find(|r| r.mark == Mark::Running);
        if !last && h < self.rows.len() + act.len() + 3 {
            return running.map(|r| self.row(r, w, lw, nw)).into_iter().chain(running.map(|_| act.clone()).into_iter().flatten())
                .chain([foot]).collect();
        }
        let mut out = vec![];
        for r in &self.rows {
            out.push(self.row(r, w, lw, nw));
            if r.mark == Mark::Running {
                out.extend(act.iter().cloned());
            }
        }
        out.extend([String::new(), foot]);
        out
    }

    /// Erase what was drawn and draw again, row over row (less flicker than a clear first).
    fn draw(&mut self, last: bool) {
        use crossterm::{cursor, queue, terminal::{Clear, ClearType}};
        if !self.tty {
            return;
        }
        let (w, h) = crossterm::terminal::size().map_or((80, 24), |(w, h)| (w as usize, h as usize));
        let rows = if last { self.frame(w, usize::MAX, true) } else { self.frame(w, h, false) };
        let mut o = std::io::stdout().lock();
        if self.drawn > 0 {
            let _ = queue!(o, cursor::MoveUp(self.drawn));
        }
        let _ = queue!(o, cursor::MoveToColumn(0));
        for r in &rows {
            let _ = write!(o, "{r}");
            let _ = queue!(o, Clear(ClearType::UntilNewLine));
            let _ = writeln!(o);
        }
        let _ = queue!(o, Clear(ClearType::FromCursorDown));
        let _ = o.flush();
        // ponytail: a resize narrower than the last frame leaves its wrapped rows above; erase by the old width if that shows
        self.drawn = if last { 0 } else { rows.len() as u16 };
    }

    fn erase(&mut self) {
        use crossterm::{cursor, terminal::{Clear, ClearType}};
        if self.drawn > 0 {
            let _ = crossterm::execute!(std::io::stdout(), cursor::MoveUp(self.drawn), cursor::MoveToColumn(0), Clear(ClearType::FromCursorDown));
            self.drawn = 0;
        }
    }

    /// The row's name in one line, for the piped path.
    fn named(&self, r: &Row) -> String {
        match &r.key {
            k if k.starts_with("task-") => format!("task {}/{}  {}", r.num, self.total().1, r.label),
            k if k.starts_with("step-") => format!("step {}", r.label),
            _ => r.label.clone(),
        }
    }
}

/// Ends the region when it drops, on any path out of the run, a panic too.
pub struct Guard;

impl Drop for Guard {
    fn drop(&mut self) {
        finish();
    }
}

fn with(f: impl FnOnce(&mut Live)) {
    let mut g = LIVE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(l) = g.as_mut() {
        f(l);
        l.draw(false);
    }
}

/// Whether a run's region is up: the stream sends its events here, not to claude.rs's Thinking.
pub fn on() -> bool {
    LIVE.lock().unwrap_or_else(|e| e.into_inner()).is_some()
}

/// The region for a run, with these rows; tool paths show relative to `root`.
pub fn start(rows: Vec<Row>, root: &Path) -> Guard {
    let tty = crate::colour();
    let generation = GEN.fetch_add(1, SeqCst) + 1;
    *LIVE.lock().unwrap_or_else(|e| e.into_inner()) = Some(Live {
        rows, thinking: String::new(), tool: String::new(), started: Instant::now(), spent: 0.0, drawn: 0, tty,
        root: root.into(), frame: 0, generation,
    });
    if tty {
        // the stream blocks on a long Bash with no event for minutes: the spinner and timers move anyway
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(100));
            let mut g = LIVE.lock().unwrap_or_else(|e| e.into_inner());
            match g.as_mut() {
                Some(l) if l.generation == generation => {
                    l.frame += 1;
                    l.draw(false);
                }
                _ => return,
            }
        });
    }
    with(|_| {});
    Guard
}

/// Rows that are not there yet, at the end: a follow-up's steps and review once it changed the branch.
pub fn add(rows: Vec<Row>) {
    with(|l| {
        for r in rows {
            if !l.rows.iter().any(|x| x.key == r.key) {
                l.rows.push(r);
            }
        }
    });
}

pub fn begin(key: &str) {
    with(|l| {
        let spent = l.spent;
        (l.thinking, l.tool) = (String::new(), String::new());
        let Some(i) = l.rows.iter().position(|r| r.key == key) else { return };
        let r = &mut l.rows[i];
        (r.mark, r.since) = (Mark::Running, Some((Instant::now(), spent)));
        if !l.tty {
            println!("\n{}", paint("1", &l.named(&l.rows[i])));
        }
    });
}

/// The row ends: its mark and note (a sha, `changes wanted`, `skipped`); time and cost are its own.
pub fn end(key: &str, mark: Mark, note: &str) {
    with(|l| {
        let spent = l.spent;
        let Some(i) = l.rows.iter().position(|r| r.key == key) else { return };
        let r = &mut l.rows[i];
        (r.mark, r.note) = (mark, note.into());
        if let Some((t, was)) = r.since {
            (r.secs, r.usd) = (Some(t.elapsed().as_secs_f64()), Some(spent - was));
        }
        if !l.tty {
            let r = &l.rows[i];
            let tail = [r.note.clone(), r.secs.map(clock).unwrap_or_default(), r.usd.map(|u| format!("${u:.2}")).unwrap_or_default()];
            println!("{} {}  {}", l.mark(mark), l.named(r), tail.iter().filter(|t| !t.is_empty()).cloned().collect::<Vec<_>>().join("  "));
        }
    });
}

/// What the run has spent, after each call: the footer's total.
pub fn spent(usd: f64) {
    with(|l| l.spent = usd);
}

/// An `assistant` event from the stream: its latest thinking summary and tool call.
pub fn event(e: &Value) {
    with(|l| {
        for b in e["message"]["content"].as_array().into_iter().flatten() {
            match b["type"].as_str() {
                Some("thinking") => {
                    let t = b["thinking"].as_str().unwrap_or("").replace("**", "");
                    if t.trim().is_empty() {
                        continue;
                    }
                    if l.tty {
                        l.thinking = t.split_whitespace().collect::<Vec<_>>().join(" ");
                    } else {
                        println!("{}", paint("2", &format!("  ∴ {}", t.trim().replace('\n', "\n    "))));
                    }
                }
                Some("tool_use") => {
                    let name = b["name"].as_str().unwrap_or("tool");
                    let t = target(name, &b["input"], &l.root);
                    let s = if t.is_empty() { name.to_string() } else { format!("{name}  {t}") };
                    if l.tty { l.tool = s } else { println!("{}", paint("2", &format!("  → {s}"))) }
                }
                _ => {}
            }
        }
    });
}

/// Print above the region: it is erased, `f` prints, and it is drawn again under that.
pub fn above(f: impl FnOnce()) {
    let mut g = LIVE.lock().unwrap_or_else(|e| e.into_inner());
    match g.as_mut() {
        Some(l) => {
            l.erase();
            f();
            l.draw(false);
        }
        None => f(),
    }
}

/// The last frame, which stays: a row still running was stopped or failed.
fn finish() {
    let mut g = LIVE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(mut l) = g.take() {
        let note = if crate::cancel::cancelled() { "stopped" } else { "" };
        for i in 0..l.rows.len() {
            let r = &mut l.rows[i];
            if r.mark != Mark::Running {
                continue;
            }
            (r.mark, r.note) = (Mark::Failed, note.into());
            r.secs = r.since.map(|(t, _)| t.elapsed().as_secs_f64());
            if !l.tty {
                let tail = [note.to_string(), r.secs.map(clock).unwrap_or_default()].join("  ");
                println!("{} {}  {}", paint("31", "✗"), l.named(&l.rows[i]), tail.trim());
            }
        }
        l.draw(true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn live(rows: Vec<Row>) -> Live {
        Live { rows, thinking: "Checking how the tests build a fixture".into(), tool: "Bash  cargo test".into(), started: Instant::now(),
               spent: 1.1, drawn: 0, tty: true, root: "/r".into(), frame: 0, generation: 0 }
    }

    fn rows() -> Vec<Row> {
        let mut r = vec![Row::new("task-0", "1", "Add the Json flag").done("a1b2c3d"), Row::new("task-1", "2", "Print items as JSON"),
                         Row::new("task-2", "3", "Document it"), Row::new("step-docs", "◇", "docs · check"), Row::new("review", "◇", "review")];
        (r[1].mark, r[1].since) = (Mark::Running, Some((Instant::now(), 0.0)));
        r
    }

    #[test]
    fn rows_never_wrap() {
        let l = live(rows());
        for w in [20, 40, 80, 200] {
            for row in l.frame(w, 50, false) {
                assert!(row.chars().count() < w.max(21), "{w}: {row:?}");
            }
        }
        let f = l.frame(80, 50, false);
        assert_eq!(f.len(), 5 + 2 + 2, "rows, the running row's thinking and tool, blank, footer");
        assert!(f[0].starts_with("  ✓   1  Add the Json flag") && f[0].contains("a1b2c3d"), "{:?}", f[0]);
        assert!(f[2].trim_start().starts_with("∴ Checking") && f[3].trim_start().starts_with("→ Bash  cargo test"));
        assert!(f[8].contains("1/3") && f[8].contains("$1.10") && f[8].contains("Ctrl-C"), "{:?}", f[8]);
    }

    #[test]
    fn a_short_terminal_gets_the_running_row_only() {
        let f = live(rows()).frame(80, 8, false);
        assert_eq!(f.len(), 4, "{f:?}");
        assert!(f[0].contains("Print items as JSON") && f[3].contains("1/3"));
    }

    #[test]
    fn the_last_frame_is_a_record() {
        let mut l = live(rows());
        l.rows[1].mark = Mark::Done;
        let f = l.frame(80, 3, true);
        assert_eq!(f.len(), 5 + 2, "every row, even under the height; no activity");
        assert!(f.iter().all(|r| !SPIN.iter().any(|c| r.contains(*c)) && !r.contains("Ctrl-C")), "{f:?}");
    }

    #[test]
    fn tool_targets_are_never_content() {
        let root = Path::new("/r");
        assert_eq!(target("Write", &json!({"file_path": "/r/src/a.rs", "content": "SECRET"}), root), "src/a.rs");
        assert_eq!(target("Read", &json!({"file_path": "/elsewhere/b.md"}), root), "/elsewhere/b.md");
        assert_eq!(target("NotebookEdit", &json!({"notebook_path": "/r/n.ipynb", "new_source": "x"}), root), "n.ipynb");
        assert_eq!(target("Bash", &json!({"command": "cargo test\necho SECRET"}), root), "cargo test");
        assert_eq!(target("Grep", &json!({"pattern": "fn main"}), root), "fn main");
        assert_eq!(target("Task", &json!({"description": "find callers", "prompt": "SECRET"}), root), "find callers");
        assert_eq!(target("WebFetch", &json!({"url": "https://x"}), root), "");
        assert_eq!(clock(102.4), "1:42");
    }
}
