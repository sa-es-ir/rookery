//! The prompt box: a one-line editor inside a rounded border, with a dim placeholder while it is
//! empty, the commands that match what is typed so far under it, then a hint line. Drawn in raw
//! mode; `prompt()` uses it only on a terminal.
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::{cursor, queue, terminal};
use std::io::{self, Write};

const HINT: &str = "tab completes · help <command> for its options · ↑↓ history · ctrl+d to exit";

/// One typed line, or None for Ctrl-D, or Ctrl-C on an empty line. On Enter the box is erased
/// and the line stays as `> line`, so what the command prints follows it. `commands` are
/// (name, what it does), listed while the first word is still being typed.
pub fn line(history: &[String], placeholder: &str, commands: &[(String, String)]) -> io::Result<Option<String>> {
    terminal::enable_raw_mode()?;
    let r = edit(history, placeholder, commands);
    let _ = terminal::disable_raw_mode();
    r
}

fn edit(history: &[String], placeholder: &str, commands: &[(String, String)]) -> io::Result<Option<String>> {
    let mut out = io::stdout();
    let paint = crate::colour();
    let (mut buf, mut at, mut back) = (Vec::<char>::new(), 0, 0);
    let mut drawn = false;
    loop {
        let w = terminal::size().map_or(80, |(w, _)| w as usize);
        let (text, col, below) = frame(&buf, at, placeholder, commands, w, paint);
        if drawn {
            queue!(out, cursor::MoveUp(1))?;
        }
        queue!(out, cursor::MoveToColumn(0), terminal::Clear(terminal::ClearType::FromCursorDown))?;
        write!(out, "{text}")?;
        queue!(out, cursor::MoveUp(below), cursor::MoveToColumn(col))?;
        out.flush()?;
        drawn = true;

        // ponytail: a resize redraws at the new width, but lines the old width wrapped are left behind
        let Event::Key(k) = event::read()? else { continue };
        if k.kind != KeyEventKind::Press {
            continue; // Windows reports the release too
        }
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL) && !k.modifiers.contains(KeyModifiers::ALT); // AltGr is Ctrl+Alt
        match k.code {
            KeyCode::Char('c') if ctrl && !buf.is_empty() => (buf.clear(), at = 0).1,
            KeyCode::Char('c' | 'd') if ctrl && buf.is_empty() => {
                queue!(out, cursor::MoveUp(1), cursor::MoveToColumn(0), terminal::Clear(terminal::ClearType::FromCursorDown))?;
                out.flush()?;
                return Ok(None);
            }
            KeyCode::Char(c) if !ctrl => (buf.insert(at, c), at += 1).1,
            KeyCode::Backspace if at > 0 => {
                at -= 1;
                buf.remove(at);
            }
            KeyCode::Delete if at < buf.len() => {
                buf.remove(at);
            }
            KeyCode::Left => at = at.saturating_sub(1),
            KeyCode::Right => at = (at + 1).min(buf.len()),
            KeyCode::Home => at = 0,
            KeyCode::End => at = buf.len(),
            KeyCode::Esc => (buf.clear(), at = 0).1,
            KeyCode::Tab => {
                if let Some((name, _)) = matching(&buf, commands).next() {
                    buf = format!("{name} ").chars().collect();
                    at = buf.len();
                }
            }
            KeyCode::Up | KeyCode::Down => {
                let to = if k.code == KeyCode::Up { (back + 1).min(history.len()) } else { back.saturating_sub(1) };
                if to != back {
                    back = to;
                    buf = if back == 0 { vec![] } else { history[history.len() - back].chars().collect() };
                    at = buf.len();
                }
            }
            KeyCode::Enter if !buf.is_empty() => {
                let s: String = buf.iter().collect();
                let (d, r) = if paint { ("\x1b[2m", "\x1b[0m") } else { ("", "") };
                queue!(out, cursor::MoveUp(1), cursor::MoveToColumn(0), terminal::Clear(terminal::ClearType::FromCursorDown))?;
                write!(out, "{d}>{r} {s}\r\n")?;
                out.flush()?;
                return Ok(Some(s));
            }
            _ => {}
        }
    }
}

/// The commands named by what is typed so far: all of them while the box is empty, none once
/// the first word is finished.
fn matching<'a>(buf: &[char], commands: &'a [(String, String)]) -> impl Iterator<Item = &'a (String, String)> + use<'a> {
    let typed: String = buf.iter().collect();
    let word = (!typed.contains(char::is_whitespace)).then_some(typed);
    commands.iter().filter(move |(name, _)| word.as_ref().is_some_and(|w| name.starts_with(w.as_str())))
}

/// The box `w` columns wide (one short of the edge, so no terminal wraps it), the cursor's
/// column on its middle line, and how many lines are printed below that line. A line longer
/// than the box scrolls so the cursor stays in view.
fn frame(buf: &[char], at: usize, placeholder: &str, commands: &[(String, String)], w: usize, paint: bool) -> (String, u16, u16) {
    let (b, d, r) = if paint { ("\x1b[90m", "\x1b[2m", "\x1b[0m") } else { ("", "", "") };
    let w = w.max(20) - 1;
    let room = w - 6; // "│ > " … " │"
    let start = at.saturating_sub(room - 1);
    let (body, len) = if buf.is_empty() {
        let p: String = placeholder.chars().take(room).collect();
        let n = p.chars().count();
        (format!("{d}{p}{r}"), n)
    } else {
        let s: String = buf[start..].iter().take(room).collect();
        let n = s.chars().count();
        (s, n)
    };
    let rule = "─".repeat(w - 2);
    let mut text = format!("{b}╭{rule}╮{r}\r\n{b}│{r} {d}>{r} {body}{} {b}│{r}\r\n{b}╰{rule}╯{r}\r\n", " ".repeat(room - len));
    let g = if paint { "\x1b[38;2;168;204;104m" } else { "" }; // the logo's light green, as in the guide
    let mut below = 2;
    for (name, about) in matching(buf, commands) {
        let about: String = about.chars().take((w - 2).saturating_sub(12)).collect();
        text += &format!("  {g}{name:<9}{r} {d}{about}{r}\r\n");
        below += 1;
    }
    let hint: String = HINT.chars().take(w - 2).collect();
    text += &format!("  {d}{hint}{r}");
    (text, (4 + at - start) as u16, below)
}

#[cfg(test)]
mod tests {
    #[test]
    fn frame_fits_and_keeps_the_cursor_in_view() {
        let long: Vec<char> = "x".repeat(200).chars().collect();
        let cmds = [("doctor".to_string(), "Check things".to_string()), ("help".to_string(), "Show options".to_string())];
        for (buf, at) in [(&[][..], 0), (&long[..5], 5), (&long[..], 200), (&long[..], 0)] {
            let (text, col, below) = super::frame(buf, at, "Try \"doctor\"", &cmds, 40, false);
            let lines: Vec<_> = text.split("\r\n").collect();
            for l in &lines[..3] {
                assert_eq!(l.chars().count(), 39, "{l:?}");
            }
            assert_eq!(below as usize, lines.len() - 2);
            assert!((4..39 - 2).contains(&(col as usize)), "{col}");
        }
        let menu = |typed: &str| super::matching(&typed.chars().collect::<Vec<_>>(), &cmds).map(|c| c.0.as_str()).collect::<Vec<_>>();
        assert_eq!(menu(""), ["doctor", "help"]);
        assert_eq!(menu("he"), ["help"]);
        assert!(menu("doctor ").is_empty() && menu("x").is_empty());
        assert!(super::frame(&[], 0, "Try", &cmds, 40, false).0.contains("│ > Try "));
    }
}
