//! Ctrl-C stops the command that runs, never the prompt around it. The claude call in flight is
//! killed with everything it started (a job object on Windows, its process group elsewhere: spike
//! S2), no further call starts, and the command ends through its own error path, so what is
//! committed stays committed. A second Ctrl-C, or one while no command runs, exits at once.
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};

static ACTIVE: AtomicBool = AtomicBool::new(false);
static CANCEL: AtomicBool = AtomicBool::new(false);

pub const CANCELLED: &str = "Cancelled with Ctrl-C.";

pub fn cancelled() -> bool {
    CANCEL.load(SeqCst)
}

/// A run's error as its log step and message: a cancel says so, not what the kill broke on the way.
pub fn why(e: String) -> (&'static str, String) {
    if cancelled() { ("cancelled", CANCELLED.into()) } else { ("error", e) }
}

/// Around one command: Ctrl-C cancels it instead of leaving rookery.
pub fn during<T>(f: impl FnOnce() -> T) -> T {
    CANCEL.store(false, SeqCst);
    ACTIVE.store(true, SeqCst);
    let out = f();
    ACTIVE.store(false, SeqCst);
    out
}

/// The handler, on either platform: false means exit now.
fn on_ctrl_c() -> bool {
    if !ACTIVE.load(SeqCst) || CANCEL.swap(true, SeqCst) {
        return false;
    }
    sys::kill();
    true
}

pub use sys::{guard, install};

/// Before spawn: a child Ctrl-C cannot reach on its own, so the kill takes its whole tree.
pub fn detach(c: &mut Command) {
    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(c, 0);
    #[cfg(not(unix))]
    let _ = c;
}

#[cfg(windows)]
mod sys {
    use std::sync::Mutex;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;
    use windows_sys::Win32::System::JobObjects::*;

    // the running call's job object, as an address; the handler runs on its own thread, so a lock
    static JOB: Mutex<usize> = Mutex::new(0);

    unsafe extern "system" fn handler(_: u32) -> i32 {
        super::on_ctrl_c() as i32 // FALSE passes it on to the default handler, which exits
    }

    pub fn install() {
        unsafe { SetConsoleCtrlHandler(Some(handler), 1) };
    }

    pub fn kill() {
        let j = *JOB.lock().unwrap();
        if j != 0 {
            unsafe { TerminateJobObject(j as HANDLE, 1) };
        }
    }

    /// Closing it kills what is left of the tree, an orphan of a call that ended included.
    pub struct Guard(HANDLE);

    /// Puts the child in a job object that dies with it. ponytail: assigned just after spawn, so a
    /// process claude.cmd starts in that instant escapes; CREATE_SUSPENDED if that ever shows.
    pub fn guard(child: &std::process::Child) -> Guard {
        use std::os::windows::io::AsRawHandle;
        let job = unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info as *const _ as *const _,
                                    size_of_val(&info) as u32);
            AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE);
            job
        };
        *JOB.lock().unwrap() = job as usize;
        if super::cancelled() {
            kill(); // Ctrl-C came between claude()'s check and the spawn
        }
        Guard(job)
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            *JOB.lock().unwrap() = 0;
            if !self.0.is_null() {
                unsafe { CloseHandle(self.0) };
            }
        }
    }
}

#[cfg(unix)]
mod sys {
    use std::sync::atomic::{AtomicI32, Ordering::SeqCst};

    // the running call's process group; atomics only, since a signal handler may not lock
    static GROUP: AtomicI32 = AtomicI32::new(0);

    extern "C" fn handler(_: libc::c_int) {
        if !super::on_ctrl_c() {
            unsafe { libc::_exit(130) };
        }
    }

    pub fn install() {
        unsafe { libc::signal(libc::SIGINT, handler as extern "C" fn(libc::c_int) as libc::sighandler_t) };
    }

    pub fn kill() {
        let g = GROUP.load(SeqCst);
        if g > 0 {
            unsafe { libc::kill(-g, libc::SIGKILL) };
        }
    }

    pub struct Guard;

    /// The child leads its own group (`detach()`), so the group is its pid.
    pub fn guard(child: &std::process::Child) -> Guard {
        GROUP.store(child.id() as i32, SeqCst);
        if super::cancelled() {
            kill(); // Ctrl-C came between claude()'s check and the spawn
        }
        Guard
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            GROUP.store(0, SeqCst);
        }
    }
}
