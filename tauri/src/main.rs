// SCM Workbench — native shell.
//
// One process family, no clients, no servers-as-citizens:
//
//   SCM Workbench.exe (this binary, GUI subsystem)
//     └── python  <data>/runtime/.../python.exe -m scm_workbench.server
//           └── jobs (spawned by the server, killed by the server)
//
// The window is a Tauri webview (WebView2 on Windows, WKWebView on macOS).
// Nothing here needs .NET, pythonnet, or any third-party runtime on the
// machine. If the worker cannot start, the window says so — loudly. There is
// no browser tab to fall back to.

// No console window on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::io::Write;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{
    AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Loopback port the worker binds. 8038 (not the legacy 8037) so the
/// prototype can never collide with a still-running legacy server child.
const WORKER_PORT: u16 = 8038;

/// Type shared with the watchdog thread: the live worker child, if any.
type WorkerSlot = Arc<Mutex<Option<std::process::Child>>>;

/// The page shown while the worker is starting: an app asset (served from
/// the bundle), navigated to the live server once the port answers.
fn loading_page() -> WebviewUrl {
    WebviewUrl::App("loading.html".into())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![wb_restart])
        .manage(WorkerSlot::default())
        .setup(|app| {
            let exe = std::env::current_exe().expect("current_exe");
            let exe_dir = exe.parent().map(|p| p.to_path_buf()).unwrap_or_default();
            let data = data_dir();
            // The per-user data area is created at launch, exactly as the old
            // bootstrap did: on a fresh machine the first thing the app ever
            // does is make its own home (settings, logs, managed repos live
            // here — never in the app folder, so updates can't touch it).
            if let Err(e) = fs::create_dir_all(&data) {
                let window = WebviewWindowBuilder::new(app, "main", loading_page())
                    .title("SCM Workbench")
                    .build()
                    .expect("failed to create the main window");
                fail_window(
                    &window,
                    &data,
                    "Its own files area couldn't be created — the app keeps its settings, work files and logs in a per-user folder, and it needs that folder to do anything.",
                    &format!("creating it failed with: {e}"),
                );
                return Ok(());
            }
            let root = app_root(&exe);
            let py = worker_python(&root, &data);

            let window = WebviewWindowBuilder::new(app, "main", loading_page())
                .title("SCM Workbench")
                .min_inner_size(940.0, 600.0)
                .inner_size(1280.0, 860.0)
                .center()
                .build()
                .expect("failed to create the main window");

            let log_path = data.join("server-tauri.log");
            let slot: WorkerSlot = app.state::<WorkerSlot>().inner().clone();
            let app_handle = app.handle().clone();

            // We own the port: if a previous (hard-killed) instance left its
            // worker listening, stop it; if a foreign program has the port, we
            // decline to start rather than evict it.
            if let Err(e) = claim_worker_port(WORKER_PORT, &log_path) {
                fail_window(&window, &data, "This app's port is held by another program.", &e);
                return Ok(());
            }

            match spawn_worker(&data, &root, &log_path, &py, &slot) {
                Ok(()) => {
                    let w = window.clone();
                    thread::spawn(move || watch_worker(app_handle, slot, w));
                }
                Err(e) => {
                    fail_window(
                        &window,
                        &data,
                        "The part of the app that does the work didn't start. That usually means the bundled runtime inside the app is missing or damaged.",
                        &format!("{e}"),
                    );
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // The window goes, the worker goes with it. (A plain kill() is
            // enough: the server's own job children are killed by the server
            // when it dies, and on Windows a process exit does not orphan a
            // console.)
            if let WindowEvent::CloseRequested { .. } = event {
                if let Some(mut child) = window
                    .app_handle()
                    .state::<WorkerSlot>()
                    .lock()
                    .ok()
                    .and_then(|mut g| g.take())
                {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("while running the app");
}

/// The data area: %LOCALAPPDATA%\scm-workbench (Windows),
/// ~/Library/Application Support/scm-workbench (macOS) — or SCM_WORKBENCH_DATA.
/// Same place the legacy launcher always used, so an existing install's
/// runtime, repos and settings are picked up by the new shell in place.
fn data_dir() -> PathBuf {
    if let Ok(d) = std::env::var("SCM_WORKBENCH_DATA") {
        return PathBuf::from(d);
    }
    #[cfg(target_os = "macos")]
    let home = std::env::var("HOME").unwrap_or_default();
    #[cfg(target_os = "macos")]
    {
        return PathBuf::from(home).join("Library/Application Support/scm-workbench");
    }
    let local = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default();
    PathBuf::from(local).join("scm-workbench")
}

/// The app root:
///   Windows bundle — the package sits at <exe dir>/app/scm_workbench, so the
///                     root is the exe's directory itself;
///   macOS bundle   — an app bundle may only carry Contents/ at its root
///                     (the code-signature seal covers exactly that), so the
///                     payload lives in <App>.app/Contents/{app,runtime} and
///                     the root is the Contents dir (one parent up);
///   dev checkout   — <root>/tauri/target/{debug,release}/scm-workbench, so the
///                    root is four parents up.
fn app_root(exe: &Path) -> PathBuf {
    let exe_dir = match exe.parent() {
        Some(p) => p.to_path_buf(),
        None => return exe.to_path_buf(),
    };
    if exe_dir.join("app/scm_workbench/__init__.py").is_file() {
        return exe_dir;
    }
    #[cfg(target_os = "macos")]
    if let Some(contents) = exe_dir.parent() {
        if contents.file_name().and_then(|n| n.to_str()) == Some("Contents")
            && contents.join("app/scm_workbench/__init__.py").is_file()
        {
            return contents.to_path_buf();
        }
    }
    let mut p = exe.to_path_buf();
    for _ in 0..4 {
        p = match p.parent() {
            Some(x) => x.to_path_buf(),
            None => break,
        };
    }
    p
}

/// The worker interpreter, in search order:
///  1. the SCM_WORKBENCH_PYTHON env var (dev override — any interpreter);
///  2. the app's own runtime inside the bundle (the release layout, where
///     the machine needs to provide nothing):
///       Windows — <root>/runtime/python/install/python.exe
///       macOS   — <root>/runtime/python/install/bin/python3.13
///     (the pbs pin that scripts/bake_runtime.py unpacks — 3.13 today);
///  3. the legacy data-area private runtime (pre-bundle architecture).
fn worker_python(root: &Path, data: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("SCM_WORKBENCH_PYTHON") {
        return PathBuf::from(p);
    }
    let mut candidates: Vec<PathBuf> = Vec::new();
    #[cfg(windows)]
    candidates.push(root.join("runtime/python/install/python.exe"));
    #[cfg(target_os = "macos")]
    candidates.push(root.join("runtime/python/install/bin/python3.13"));
    #[cfg(windows)]
    candidates.push(data.join("runtime/python/install/python.exe"));
    #[cfg(target_os = "macos")]
    candidates.push(data.join("runtime/python/install/bin/python3.13"));
    for c in &candidates {
        if c.is_file() {
            return c.clone();
        }
    }
    // None found: hand back the bundled-path candidate — the failure page
    // shows it, which is the thing to look at when the bundle is incomplete.
    candidates.into_iter().next().unwrap_or_default()
}

/// Spawn the UI server as a supervised child: no console, stdio to the
/// server log, the data area pinned via env (same contract as before).
fn spawn_worker(
    data: &Path,
    root: &Path,
    log_path: &Path,
    py: &Path,
    slot: &WorkerSlot,
) -> std::io::Result<()> {
    let mut cmd = Command::new(py);
    cmd.current_dir(root)
        .env("SCM_WORKBENCH_DATA", data)
        .env("SCM_WORKBENCH_PACKAGED", "1")
        // The worker's interpreter *is* the app's runtime (it ships inside the
        // bundle), which is exactly what this variable means to the server:
        // "a job-capable python exists, and it is this one".
        .env("SCM_WORKBENCH_PYTHON", py)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout({
            let f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_path)?;
            Stdio::from(f)
        })
        .stderr({
            let f = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_path)?;
            Stdio::from(f)
        })
        .arg("-X")
        .arg("utf8")
        .arg("-m")
        .arg("scm_workbench.server")
        .arg("--port")
        .arg(WORKER_PORT.to_string())
        .arg("--no-browser");
    // The package is importable from the app root (dev) or from <root>/app
    // (bundle layout, where the package lives in app/scm_workbench).
    let pkg = if root.join("app").is_dir() {
        root.join("app")
    } else {
        root.to_path_buf()
    };
    cmd.env("PYTHONPATH", pkg);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let child = cmd.spawn()?;
    {
        let mut log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_path)?;
        let _ = writeln!(
            log,
            "== tauri shell {} spawned worker: {} (cwd {}, data {})",
            env!("CARGO_PKG_VERSION"),
            py.display(),
            root.display(),
            data.display()
        );
    }
    let _ = slot.lock().ok().and_then(|mut g| g.replace(child)).is_some();
    Ok(())
}

/// Wait until the worker answers on the loopback port (or it dies), then
/// point the window at it. Keep watching: if the worker dies later, the
/// window says so instead of going quiet.
fn watch_worker(app: AppHandle, slot: WorkerSlot, window: WebviewWindow) {
    let url = format!("http://127.0.0.1:{WORKER_PORT}");

    let mut up = false;
    let deadline = Instant::now() + Duration::from_secs(120);
    while !up && Instant::now() < deadline {
        if port_open(WORKER_PORT) {
            up = true;
            break;
        }
        // Worker already gone?
        if let Some(code) = slot
            .lock()
            .ok()
            .and_then(|mut g| g.as_mut().and_then(|c| c.try_wait().ok()).flatten())
        {
            let _ = app.run_on_main_thread({
                let w = window.clone();
                let d = data_dir();
                move || {
                    fail_window(
                        &w,
                        &d,
                        "The part of the app that does the work ended before it was ready.",
                        &format!("(exit code {code})\n{}", log_tail(&d, 20)),
                    );
                }
            });
            return;
        }
        thread::sleep(Duration::from_millis(250));
    }
    if !up {
        let _ = app.run_on_main_thread({
            let w = window.clone();
            let d = data_dir();
            move || {
                fail_window(
                    &w,
                    &d,
                    "The part of the app that does the work took too long to become ready.",
                    &log_tail(&d, 20),
                );
            }
        });
        return;
    }

    let _ = app.run_on_main_thread({
        let w = window.clone();
        let url = url.clone();
        move || {
            // From a data: page a top-level http navigation is allowed.
            let _ = w.eval(&format!("window.location.replace('{url}');"));
        }
    });

    // Crash watcher: poll the child; if it exits, surface it.
    loop {
        thread::sleep(Duration::from_secs(2));
        match slot
            .lock()
            .ok()
            .and_then(|mut g| g.as_mut().and_then(|c| c.try_wait().ok()).flatten())
        {
            Some(code) => {
                let _ = app.run_on_main_thread({
                    let w = window.clone();
                    let d = data_dir();
                    move || {
                        fail_window(
                            &w,
                            &d,
                            "The part of the app that does the work stopped while the app was open.",
                            &format!("(exit code {code})\n{}", log_tail(&d, 20)),
                        );
                    }
                });
                return;
            }
            _ => continue,
        }
    }
}

/// Make this instance the sole owner of the worker port.
///
/// A previous instance whose window was killed hard (End Task, a crash) can
/// leave its python worker behind, still listening — the window is what
/// normally reaps it. If our port is held by one of our own process kinds
/// (a python worker or this app's exe) we stop it and take the port. If it
/// belongs to anything else we refuse to start: we do not displace an
/// unrelated program's port.
fn claim_worker_port(port: u16, log_file: &Path) -> Result<(), String> {
    if !port_open(port) {
        return Ok(());
    }
    #[cfg(windows)]
    {
        let out = Command::new("netstat")
            .args(["-ano"])
            .output()
            .map_err(|e| format!("could not inspect port {port}: {e}"))?;
        let text = String::from_utf8_lossy(&out.stdout);
        let want = format!(":{port}");
        let pids: Vec<u32> = text
            .lines()
            .filter(|l| l.contains(&want) && l.contains("LISTENING"))
            .filter_map(|l| l.split_whitespace().last())
            .filter_map(|p| p.parse::<u32>().ok())
            .collect();
        for pid in pids {
            let line = Command::new("tasklist")
                .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
                .output()
                .ok()
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_lowercase())
                .unwrap_or_default();
            let name = line.split(',').next().unwrap_or("").trim_matches('"').to_string();
            let ours = name == "python.exe" || name == "scm workbench.exe";
            record(
                log_file,
                &format!(
                    "[shell] port {port} is held by pid {pid} ({name}) — {}",
                    if ours { "a leftover from a previous instance; stopping it" } else { "not one of our process kinds" }
                ),
            );
            if !ours {
                return Err(format!(
                    "port {port} is in use by another program (pid {pid}, {name}) — close that program and try again"
                ));
            }
            let _ = Command::new("taskkill")
                .args(["/F", "/PID", &pid.to_string()])
                .status();
        }
        for _ in 0..20 {
            if !port_open(port) {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(250));
        }
        Err(format!(
            "port {port} is still in use after stopping the previous instance — try again in a moment"
        ))
    }
    #[cfg(target_os = "macos")]
    {
        // lsof names the listener; a leftover of this app's own kind is
        // stopped, a foreign holder declines the start (same contract as
        // Windows above).
        let out = Command::new("lsof")
            .args(["-i", &format!(":{port}"), "-sTCP:LISTEN", "-t", "-n", "-P"])
            .output()
            .map_err(|e| format!("could not inspect port {port}: {e}"))?;
        let text = String::from_utf8_lossy(&out.stdout);
        let pids: Vec<u32> = text
            .lines()
            .filter_map(|l| l.trim().parse::<u32>().ok())
            .collect();
        for pid in pids {
            let line = Command::new("ps")
                .args(["-p", &pid.to_string(), "-o", "comm="])
                .output()
                .ok()
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                .unwrap_or_default();
            let name = line.rsplit('/').next().unwrap_or(line.as_str()).to_string();
            let ours = name.starts_with("python") || name == "SCM Workbench";
            record(
                log_file,
                &format!(
                    "[shell] port {port} is held by pid {pid} ({name}) — {}",
                    if ours { "a leftover from a previous instance; stopping it" } else { "not one of our process kinds" }
                ),
            );
            if !ours {
                return Err(format!(
                    "port {port} is in use by another program (pid {pid}, {name}) — close that program and try again"
                ));
            }
            let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
        }
        for _ in 0..20 {
            if !port_open(port) {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(250));
        }
        Err(format!(
            "port {port} is still in use after stopping the previous instance — try again in a moment"
        ))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = log_file;
        Err(format!(
            "port {port} is in use — another instance (or program) is holding it; close it and try again"
        ))
    }
}

/// Append one line to the app log (best effort — logging must never panic the shell).
fn record(path: &Path, line: &str) {
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{line}");
    }
}

fn port_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// Replace the window contents with a plain, loud error page. Must be called
/// on the main thread.
/// The last `n` lines of the worker's log ("" if there is none yet).
fn log_tail(data: &Path, n: usize) -> String {
    let Ok(s) = fs::read_to_string(data.join("server-tauri.log")) else {
        return String::new();
    };
    let all: Vec<&str> = s.lines().collect();
    let start = all.len().saturating_sub(n);
    all[start..].join("\n")
}

/// Show the window's startup-failure page: the app's own voice about what
/// went wrong, the worker's last words, and a way out. It is deliberately
/// not a "couldn't reach the server" page — to the user this IS the app, and
/// a failure of its inside is a page of the app with an action on it
/// (Try again), never a dead end.
fn fail_window(window: &WebviewWindow, data: &Path, body: &str, detail: &str) {
    let mut css = String::new();
    css.push_str("html,body{height:100%}");
    css.push_str("body{margin:0;background:#14161c;color:#e8edf5;font:14px/1.6 system-ui,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center}");
    css.push_str(".box{max-width:680px;padding:0 28px;text-align:center}");
    css.push_str(".mark{width:44px;height:44px;margin:0 auto 14px;border-radius:10px;background:linear-gradient(135deg,#5b8cff,#9a6bff);opacity:.9}");
    css.push_str("h1{font-size:17px;margin:0 0 8px}");
    css.push_str("p{margin:0 0 14px;opacity:.9}");
    css.push_str("h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;opacity:.5;margin:18px 0 6px;text-align:left}");
    css.push_str("pre{background:#0c0e12;color:#ffd7d7;padding:12px 16px;border-radius:8px;white-space:pre-wrap;max-height:38vh;overflow:auto;font-size:12px;text-align:left}");
    css.push_str("pre:empty{display:none}");
    css.push_str(".row{display:flex;gap:10px;justify-content:center;margin-top:6px}");
    css.push_str("button{background:#2b3648;color:#e8edf5;border:0;border-radius:8px;padding:9px 20px;font-size:14px;cursor:pointer}");
    css.push_str("button:hover{background:#38465e}");
    css.push_str(".hint{opacity:.5;font-size:12px;margin-top:16px}");
    css.push_str("code{opacity:.85}");

    let mut js = String::new();
    js.push_str("(function(){");
    js.push_str("var t=window.__TAURI_INTERNALS__;");
    js.push_str("document.getElementById('wb-retry').onclick=function(){if(t&&t.invoke){t.invoke('wb_restart');}else{location.reload();}};");
    js.push_str("document.getElementById('wb-copy').onclick=function(e){");
    js.push_str("var b=e.currentTarget;");
    js.push_str("var text='SCM Workbench — startup report\n\n'");
    js.push_str("+document.getElementById('wb-reason').textContent");
    js.push_str("+'\n\n'+document.getElementById('wb-log').textContent;");
    js.push_str("navigator.clipboard.writeText(text)");
    js.push_str(".then(function(){b.textContent='Copied';})");
    js.push_str(".catch(function(){b.textContent='Couldn't copy';});");
    js.push_str("})();");

    let html = format!(
        r#"<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>
<body><div class="box">
<div class="mark"></div>
<h1>SCM Workbench couldn't get ready.</h1>
<p id="wb-reason">{body}</p>
<h2>What it told us</h2>
<pre id="wb-log">{tail}</pre>
<div class="row">
<button id="wb-retry" type="button">Try again</button>
<button id="wb-copy" type="button">Copy report</button>
</div>
<p class="hint">Its files live in <code>{data}</code> — that folder, and the text above, are what to look at if this keeps happening. Closing the window quits the app.</p>
</div><script>{js}</script></body></html>"#,
        css = css,
        body = escape(body),
        tail = escape(detail),
        data = escape(&data.display().to_string()),
        js = js,
    );
    let page = format!("data:text/html;charset=utf-8,{}", percent_encode(&html));
    let _ = window.eval(&format!(
        "window.location.replace({});",
        json_string(&page)
    ));
}

/// "Try again" on the startup-failure page: restart the whole app. The data
/// area is durable, so running setup() again re-spawns the worker and the
/// window comes back as the live app.
#[tauri::command]
fn wb_restart(app: AppHandle) {
    app.restart();
}

fn json_string(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_default()
}

fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
