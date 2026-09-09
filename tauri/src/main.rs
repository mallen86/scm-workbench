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
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::{
    AppHandle, Manager, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;

mod ipc;
mod update_helper;
use ipc::WorkerRpc;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Stop the whole worker tree, not just the direct child.
///
/// The worker is the app's only long-lived child and it in turn spawns the
/// job processes the user runs; a plain `kill` leaves those running. So we
/// terminate the group the child was placed in: its verified worker process
/// group on macOS. Upstream jobs intentionally have their own groups and Python
/// reaps them on protocol EOF before this fallback. On Windows the complete tree
/// is contained by the retained kill-on-close job object.
///
/// Callers must still own an unreaped Child; signalling a stale numeric PID or
/// process-group identifier is forbidden.
fn kill_worker_tree(child: &Child) {
    #[cfg(target_os = "macos")]
    {
        // The ready handshake proves pgid == pid. `kill(-pgid, SIGKILL)`
        // terminates the worker group after cooperative upstream-job cleanup.
        unsafe {
            let pid = child.id() as libc::pid_t;
            libc::kill(-pid, libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    {
        let _ = child;
    }
}

/// Type shared with the watchdog thread: the live worker child, if any.
type WorkerSlot = Arc<Mutex<Option<Child>>>;

/// Reap the worker whenever application state is dropped during normal
/// unwinding. A hard shell kill cannot run Rust destructors; kernel closure of
/// protocol stdin is the independent worker-side backstop for that path.
///
/// On macOS the worker is also put in its own process group (see
/// `spawn_worker`) so a signal reaches every grandchild too; on Windows it is
/// assigned to a job object whose handle is retained here until the shell
/// exits. Both are belt to the `child.kill()` braces below.
struct Worker {
    slot: WorkerSlot,
    #[cfg(windows)]
    job: Mutex<Option<WorkerJob>>,
}

#[cfg(windows)]
impl Worker {
    fn install_job(&self, job: WorkerJob) -> std::io::Result<()> {
        let mut current = self.job.lock().map_err(|_| {
            std::io::Error::new(std::io::ErrorKind::Other, "worker job state was poisoned")
        })?;
        *current = Some(job);
        Ok(())
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        if let Some(mut child) = self.slot.lock().ok().and_then(|mut g| g.take()) {
            // A reaped Child must never be signalled after its PID is reusable.
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            // Kill the process group / job first, then the direct child and
            // reap it. Python handles separately-grouped upstream jobs on EOF.
            kill_worker_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// The pages shown by the packaged shell are both embedded assets.  The
/// The frontend distribution is the bounded `ui/` tree; no repository source,
/// configuration, or VCS metadata is embedded into the application assets.
fn loading_page() -> WebviewUrl {
    WebviewUrl::App("loading.html".into())
}

/// Only the Tauri asset origin may navigate this window.  In particular, a
/// stale or future UI call cannot turn the shell into a loopback browser tab.
fn is_embedded_url(url: &Url) -> bool {
    #[cfg(windows)]
    let (scheme, host) = ("http", "tauri.localhost");
    #[cfg(not(windows))]
    let (scheme, host) = ("tauri", "localhost");
    url.scheme() == scheme
        && url.host_str() == Some(host)
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
}

fn embedded_index_url() -> Url {
    #[cfg(windows)]
    let origin = "http://tauri.localhost";
    #[cfg(not(windows))]
    let origin = "tauri://localhost";
    Url::parse(&format!("{origin}/index.html")).expect("embedded Tauri URL is valid")
}

/// Native repository selection owns the directory picker. Browsers cannot
/// provide an absolute filesystem path, so this command is intentionally
/// separate from worker RPC and available only to the embedded app window.
#[tauri::command]
async fn wb_pick_repo_directory(window: WebviewWindow) -> Result<Value, String> {
    let (sender, receiver) = std::sync::mpsc::channel();
    window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("Select repository folder")
        .pick_folder(move |path| {
            let _ = sender.send(path);
        });
    let selected = tauri::async_runtime::spawn_blocking(move || receiver.recv())
        .await
        .map_err(|_| "repository folder picker failed".to_string())
        .and_then(|result| result.map_err(|_| "repository folder picker failed".to_string()))?;
    let Some(path) = selected else {
        return Ok(Value::Null);
    };
    let path = path
        .into_path()
        .map_err(|_| "selected path is unavailable".to_string())?;
    let selected_path = path
        .to_str()
        .ok_or_else(|| "selected path is not valid UTF-8".to_string())?;
    if selected_path.as_bytes().len() > 4096 {
        return Err("selected path exceeds 4096 UTF-8 bytes".to_string());
    }
    if selected_path
        .chars()
        .any(|character| character.is_control() || character == '\u{7f}')
    {
        return Err("selected path contains control characters".to_string());
    }
    Ok(Value::String(selected_path.to_string()))
}

/// Native decklist import owns the picker. The callback-based dialog is
/// intentionally bridged to an async command: no blocking picker runs on the
/// Tauri main thread, and the worker receives only the selected path.
#[tauri::command]
async fn wb_decklist_import(
    window: WebviewWindow,
    state: State<'_, WorkerRpc>,
) -> Result<Value, String> {
    let worker = state.inner().clone();
    let (sender, receiver) = std::sync::mpsc::channel();
    window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("Import decklist")
        .add_filter(
            "Decklists",
            &[
                "txt", "text", "csv", "tsv", "json", "xml", "md", "deck", "dek", "ydk", "ydke",
                "list",
            ],
        )
        .pick_file(move |path| {
            let _ = sender.send(path);
        });
    let selected = tauri::async_runtime::spawn_blocking(move || receiver.recv())
        .await
        .map_err(|_| "decklist picker failed".to_string())
        .and_then(|result| result.map_err(|_| "decklist picker failed".to_string()))?;
    let Some(path) = selected else {
        return Ok(Value::Null);
    };
    let path = path
        .into_path()
        .map_err(|_| "selected path is unavailable".to_string())?;
    let source_path = path
        .to_str()
        .ok_or_else(|| "selected path is not valid UTF-8".to_string())?;
    if source_path.as_bytes().len() > 4096 {
        return Err("selected path exceeds 4096 UTF-8 bytes".to_string());
    }
    worker.import_selected_decklist(source_path)
}

/// Native artifact save. The selected destination is consumed here and is
/// never returned to JavaScript before the worker has copied it. Rust only
/// accepts an opaque grant and a bounded basename hint from the WebView.
#[tauri::command]
async fn wb_save_artifact(
    window: WebviewWindow,
    state: State<'_, WorkerRpc>,
    grant_id: String,
    suggested_name: String,
) -> Result<Value, String> {
    if grant_id.len() != 64
        || !grant_id
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
    {
        return Err("invalid save grant".to_string());
    }
    let upper_stem = suggested_name
        .trim_end_matches([' ', '.'])
        .split('.')
        .next()
        .unwrap_or("")
        .to_ascii_uppercase();
    let reserved = matches!(upper_stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || upper_stem
            .strip_prefix("COM")
            .or_else(|| upper_stem.strip_prefix("LPT"))
            .is_some_and(|suffix| {
                matches!(suffix, "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9")
            });
    if suggested_name.is_empty()
        || suggested_name.len() > 255
        || suggested_name.ends_with([' ', '.'])
        || reserved
        || suggested_name
            .chars()
            .any(|c| c.is_control() || c == '/' || c == '\\' || c == ':')
    {
        return Err("invalid suggested file name".to_string());
    }
    let (sender, receiver) = std::sync::mpsc::channel();
    window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("Export PDF")
        .add_filter("PDF", &["pdf"])
        .set_file_name(suggested_name.clone())
        .save_file(move |path| {
            let _ = sender.send(path);
        });
    let selected = tauri::async_runtime::spawn_blocking(move || receiver.recv())
        .await
        .map_err(|_| "save dialog failed".to_string())?
        .map_err(|_| "save dialog failed".to_string())?;
    let Some(path) = selected else {
        return Ok(Value::Null);
    };
    let path = path
        .into_path()
        .map_err(|_| "selected destination is unavailable".to_string())?;
    let destination = path
        .to_str()
        .ok_or_else(|| "selected destination is not valid UTF-8".to_string())?
        .to_string();
    if destination.len() > 4096 || destination.chars().any(|c| c.is_control()) {
        return Err("selected destination is invalid".to_string());
    }
    let worker = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let started = worker.export_selected_artifact(&grant_id, &destination)?;
        if started.get("ok").and_then(Value::as_bool) == Some(false) {
            return Ok(started);
        }
        let operation_id = started
            .get("operation_id")
            .and_then(Value::as_str)
            .ok_or_else(|| "malformed artifact export response".to_string())?
            .to_string();
        let deadline = Instant::now() + Duration::from_secs(305);
        loop {
            if Instant::now() >= deadline {
                let _ = worker.cancel_artifact_export(&operation_id);
                return Err("artifact export timed out".to_string());
            }
            let value = worker.poll_artifact_export(&operation_id)?;
            if value.get("done").and_then(Value::as_bool) == Some(true) {
                return value
                    .get("result")
                    .cloned()
                    .ok_or_else(|| "malformed artifact export result".to_string());
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    })
    .await
    .map_err(|_| "artifact export task failed".to_string())?
}

fn main() {
    // The updater is an external, stdio-free mode.  It must be selected before
    // Tauri setup creates windows, starts IPC, or touches the worker.
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--update-helper") {
        if update_helper::run_cli(&args).is_err() {
            std::process::exit(2);
        }
        return;
    }
    let worker_slot = WorkerSlot::default();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            wb_restart,
            ipc::wb_rpc,
            wb_pick_repo_directory,
            wb_decklist_import,
            wb_save_artifact
        ])
        .manage(worker_slot.clone())
        .manage(Worker {
            slot: worker_slot,
            #[cfg(windows)]
            job: Mutex::new(None),
        })
        .manage(WorkerRpc::default())
        .setup(|app| {
            let exe = std::env::current_exe().expect("current_exe");
            let requested_data = data_dir();
            // The per-user data area is created at launch, exactly as the old
            // bootstrap did: on a fresh machine the first thing the app ever
            // does is make its own home (settings, logs, managed repos live
            // here — never in the app folder, so updates can't touch it).
            if let Err(e) = fs::create_dir_all(&requested_data) {
                let window = WebviewWindowBuilder::new(app, "main", loading_page())
                    .title("SCM Workbench")
                    .on_navigation(is_embedded_url)
                    .on_web_resource_request(|request, response| {
                        // Embedded assets are versioned by the binary, but a
                        // WebView may retain a cache across app updates. Ask
                        // it to revalidate the embedded UI conservatively;
                        // this does not touch the worker's HTTP cache.
                        if request.uri().path().starts_with('/') {
                            response.headers_mut().insert(
                                "Cache-Control",
                                "no-cache".parse().expect("valid cache header"),
                            );
                        }
                    })
                    .build()
                    .expect("failed to create the main window");
                fail_window(
                    &window,
                    &requested_data,
                    "Its own files area couldn't be created — the app keeps its settings, work files and logs in a per-user folder, and it needs that folder to do anything.",
                    &format!("creating it failed with: {e}"),
                );
                return Ok(());
            }
            let data = match update_helper::canonical_data_dir(&requested_data) {
                Ok(data) => data,
                Err(error) => {
                    record(&requested_data.join("server-tauri.log"), &format!("[shell] validating data directory failed: {error}"));
                    app.handle().exit(1);
                    return Ok(());
                }
            };
            let root = app_root(&exe);

            // A shell which was interrupted during an update must not start a
            // worker or create a window on top of the half-published tree.
            // Malformed/unsafe journals fail closed.  Only the exact token +
            // nonce pair written by launch_and_wait may bypass recovery.
            let token = std::env::var("SCM_WORKBENCH_UPDATE_TOKEN").ok();
            let nonce = std::env::var("SCM_WORKBENCH_UPDATE_NONCE").ok();
            let pending = update_helper::pending_journal(&data);
            let recovery = match (&token, &nonce, pending) {
                (None, None, Ok(None)) => None,
                (Some(token), Some(nonce), Ok(Some(journal)))
                    if update_helper::valid_token(token)
                        && update_helper::valid_token(nonce)
                        && update_helper::journal_matches_current_target(&journal, &exe)
                        && journal.token == *token
                        && matches!(journal.phase, update_helper::Phase::Launching | update_helper::Phase::Launched) => None,
                (Some(token), Some(nonce), Ok(Some(journal)))
                    if update_helper::valid_token(token)
                        && update_helper::valid_token(nonce)
                        && update_helper::journal_matches_current_target(&journal, &exe) => Some(Ok(journal)),
                (Some(_), Some(_), Ok(Some(_))) => Some(Err("new-shell update identity did not match the pending transaction".to_owned())),
                (None, None, Ok(Some(journal)))
                    if startup_recovery_phase(journal.phase)
                        && update_helper::journal_matches_current_target(&journal, &exe) => Some(Ok(journal)),
                (None, None, Ok(Some(_))) => Some(Err("pending update journal target does not match this shell".to_owned())),
                (_, _, Ok(Some(_))) => Some(Err("update identity is incomplete or invalid".to_owned())),
                (_, _, Ok(None)) => Some(Err("update identity is present but the journal is missing".to_owned())),
                (_, _, Err(error)) => Some(Err(format!("update journal validation failed: {error}"))),
            };
            if let Some(decision) = recovery {
                match decision {
                    Ok(journal) => {
                        let result = update_helper::materialize_helper(&exe, &data, &journal.token)
                            .and_then(|helper| update_helper::spawn_helper(
                                &helper, &data, &journal.token,
                                update_helper::Mode::Recover, Some(std::process::id()),
                            ).map(|_| ()));
                        if let Err(error) = result {
                            record(&data.join("server-tauri.log"), &format!("[shell] update recovery could not start: {error}"));
                            app.handle().exit(1);
                        } else {
                            app.handle().exit(0);
                        }
                    }
                    Err(error) => {
                        record(&data.join("server-tauri.log"), &format!("[shell] update startup failed closed: {error}"));
                        app.handle().exit(1);
                    }
                }
                return Ok(());
            }

            let py = worker_python(&root, &data);
            let window = WebviewWindowBuilder::new(app, "main", loading_page())
                .title("SCM Workbench")
                .min_inner_size(940.0, 600.0)
                .inner_size(1280.0, 860.0)
                .center()
                .on_navigation(is_embedded_url)
                .on_web_resource_request(|request, response| {
                    // Embedded assets are versioned by the binary, but a
                    // WebView may retain a cache across app updates. Ask it
                    // to revalidate the embedded UI conservatively; this
                    // does not touch the worker's HTTP cache.
                    if request.uri().path().starts_with('/') {
                        response.headers_mut().insert(
                            "Cache-Control",
                            "no-cache".parse().expect("valid cache header"),
                        );
                    }
                })
                .build()
                .expect("failed to create the main window");

            let log_path = data.join("server-tauri.log");
            let slot: WorkerSlot = app.state::<WorkerSlot>().inner().clone();
            let worker = app.state::<Worker>().inner();
            let rpc: WorkerRpc = app.state::<WorkerRpc>().inner().clone();
            let app_handle = app.handle().clone();
            // Worker is managed by the application builder and therefore
            // survives this setup closure until the app state is dropped.
            // CloseRequested still takes the slot first, making shutdown
            // idempotent while retaining the Drop backstop for hard exits.

            // The managed Worker keeps the slot reaped if no close event
            // fires, while the explicit close path remains authoritative.
            match spawn_worker(&data, &root, &log_path, &py, worker) {
                Ok((mut child, stdin, stdout)) => match rpc.install(stdin, stdout) {
                    Ok(()) => {
                        let _ = slot.lock().ok().and_then(|mut g| g.replace(child));
                        if let (Some(token), Some(nonce)) = (token.as_deref(), nonce.as_deref()) {
                            match update_helper::publish_health(&data, &exe, token, nonce) {
                                Ok(true) => record(&data.join("server-tauri.log"), "[shell] published update health"),
                                Ok(false) => record(&data.join("server-tauri.log"), "[shell] update health was not authorized by the pending journal"),
                                Err(error) => record(&data.join("server-tauri.log"), &format!("[shell] update health failed: {error}")),
                            }
                        }
                        // This is deliberately after worker setup: a valid
                        // request is the only thing which may hand off this
                        // shell, and an invalid request must never stop it.
                        let _ = update_helper::cleanup_old_helpers(&data, Duration::from_secs(60));
                        let request_app = app_handle.clone();
                        let request_data = data.clone();
                        let request_exe = exe.clone();
                        thread::spawn(move || {
                            watch_update_requests(request_app, request_data, request_exe)
                        });
                        let w = window.clone();
                        let watch_rpc = rpc.clone();
                        thread::spawn(move || watch_worker(app_handle, slot, w, watch_rpc));
                    }
                    Err(e) => {
                        kill_worker_tree(&child);
                        let _ = child.kill();
                        let _ = child.wait();
                        fail_window(
                            &window,
                            &data,
                            "The part of the app that does the work didn't start. That usually means the bundled runtime inside the app is missing or damaged.",
                            &e,
                        );
                    }
                },
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
            // The window goes, protocol EOF first gives Python a bounded
            // chance to reap its jobs. Native process-group/job-object
            // termination remains the fallback and hard-exit backstop.
            if let WindowEvent::CloseRequested { .. } = event {
                window.app_handle().state::<WorkerRpc>().shutdown();
                if let Some(mut child) = window
                    .app_handle()
                    .state::<WorkerSlot>()
                    .lock()
                    .ok()
                    .and_then(|mut g| g.take())
                {
                    // Closing protocol stdin lets Python stop and reap its
                    // separately-grouped upstream jobs before the worker exits.
                    // Fall back to the native process-tree backstop only if
                    // bounded cooperative shutdown does not complete.
                    let deadline = Instant::now() + Duration::from_secs(6);
                    let mut exited = false;
                    while Instant::now() < deadline {
                        match child.try_wait() {
                            Ok(Some(_)) => {
                                exited = true;
                                break;
                            }
                            Ok(None) => thread::sleep(Duration::from_millis(50)),
                            Err(_) => break,
                        }
                    }
                    if !exited {
                        kill_worker_tree(&child);
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("while running the app");
}

fn startup_recovery_phase(_phase: update_helper::Phase) -> bool {
    // Every valid journal phase is actionable by the helper.  Healthy,
    // Completed, and Failed are cleanup/finalization states, not a license to
    // boot a second worker over the transaction.
    true
}

/// Poll the bounded, exact launch request only after worker setup succeeded.
/// A request remains pending if helper launch or its ten-second handoff ack
/// fails; no malformed request can make this process exit.
fn watch_update_requests(app: AppHandle, data: PathBuf, current_exe: PathBuf) {
    let mut attempted = None::<(String, Instant)>;
    const RETRY_COOLDOWN: Duration = Duration::from_secs(2);
    loop {
        if let Ok(Some(request)) = update_helper::read_launch_request(&data) {
            let can_attempt = attempted
                .as_ref()
                .map(|(token, retry)| token != &request.token || Instant::now() >= *retry)
                .unwrap_or(true);
            if can_attempt {
                let prepared = update_helper::pending_journal(&data)
                    .ok()
                    .flatten()
                    .map(|journal| {
                        journal.phase == update_helper::Phase::Prepared
                            && journal.token == request.token
                    })
                    .unwrap_or(false);
                if prepared {
                    let attempt_started = std::time::SystemTime::now();
                    attempted = Some((request.token.clone(), Instant::now() + RETRY_COOLDOWN));
                    if let Err(error) = update_helper::remove_ack(&data) {
                        record(
                            &data.join("server-tauri.log"),
                            &format!("[shell] update ACK cleanup failed: {error}"),
                        );
                        return;
                    }
                    let handoff =
                        update_helper::materialize_helper(&current_exe, &data, &request.token)
                            .and_then(|helper| {
                                update_helper::spawn_helper(
                                    &helper,
                                    &data,
                                    &request.token,
                                    update_helper::Mode::Handoff,
                                    None,
                                )
                                .map(|_| ())
                            });
                    if handoff.is_ok()
                        && update_helper::wait_for_ack_since(
                            &data,
                            &request.token,
                            Duration::from_secs(10),
                            attempt_started,
                        )
                    {
                        // The ACK is durable evidence that the external helper
                        // owns the transaction. Remove the one-shot request
                        // before allowing this shell to close; a cleanup
                        // failure is terminal for the watcher, never a reason
                        // to exit the still-running shell.
                        if let Err(error) = update_helper::remove_launch_request(&data) {
                            record(
                                &data.join("server-tauri.log"),
                                &format!("[shell] launch request cleanup failed: {error}"),
                            );
                            return;
                        }
                        app.exit(0);
                        return;
                    }
                }
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
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
    {
        let home = std::env::var("HOME").unwrap_or_default();
        PathBuf::from(home).join("Library/Application Support/scm-workbench")
    }
    #[cfg(not(target_os = "macos"))]
    {
        let local = std::env::var("LOCALAPPDATA")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_default();
        PathBuf::from(local).join("scm-workbench")
    }
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

/// Spawn the Python worker as a supervised child: protocol stdio is piped to
/// WorkerRpc, stderr remains in the worker log, and the data area is pinned
/// via env. The worker runs the stdio-only `--ipc` mode.
fn spawn_worker(
    data: &Path,
    root: &Path,
    log_path: &Path,
    py: &Path,
    worker: &Worker,
) -> std::io::Result<(Child, ChildStdin, ChildStdout)> {
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
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
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
        .arg("--no-browser")
        .arg("--ipc");
    // The package is importable from the app root (dev) or from <root>/app
    // (bundle layout, where the package lives in app/scm_workbench).
    let pkg = if root.join("app").is_dir() {
        root.join("app")
    } else {
        root.to_path_buf()
    };
    cmd.env("PYTHONPATH", pkg);
    // On macOS the payload (app/, runtime/) lives under Contents/ - inside
    // the code-signature seal. Python compiling __pycache__ pyc's back into
    // that tree on the user's own first launch would break the seal of the
    // copy on disk: harmless while the quarantine is off, but the bundle
    // would read "damaged" again the moment it was re-quarantined. Keeping
    // the worker bytecode-less makes launches write-free (the few modules
    // involved re-parse in a few milliseconds - invisible next to a first
    // boot).
    #[cfg(target_os = "macos")]
    cmd.env("PYTHONDONTWRITEBYTECODE", "1");
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    // macOS: retain the post-spawn parent setpgid attempt. Exec can win that
    // race, so the Python worker also establishes and verifies pgid == pid
    // before its private ready handshake. Upstream jobs use their own groups
    // and are reaped cooperatively on protocol EOF.
    let child = cmd.spawn()?;

    // Assign the child before doing any further startup work. The job handle
    // is installed in the application-managed Worker immediately, so a hard
    // shell termination during startup still closes it and kills the worker.
    #[cfg(windows)]
    {
        let job = match attach_worker_to_job(&child) {
            Ok(job) => job,
            Err(error) => {
                let mut child = child;
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        if let Err(error) = worker.install_job(job) {
            let mut child = child;
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    }
    #[cfg(not(windows))]
    let _ = worker;

    // macOS: move the (already running) worker into its own process group so
    // `kill(-pgid, SIGKILL)` reaches the worker. Done from the
    // *parent* after spawn, via setpgid(child_pid, child_pid) — a parent may
    // regroup its direct child, and this is the form that does not touch the
    // child's exec. (The pre-spawn `posix_spawnattr_setpgid` route failed on
    // the runner: an exception in the pre-exec hook aborts the whole spawn.)
    // If exec wins, record the miss; the worker's self-grouping and exact
    // readiness response are the mandatory fallback before UI admission.
    #[cfg(target_os = "macos")]
    {
        let pid = child.id() as libc::pid_t;
        // setpgid(pid,pid) is valid only before the child execs or after the
        // worker has made itself group leader.
        let grouped = unsafe { libc::setpgid(pid, pid) };
        if grouped != 0 {
            record(
                log_path,
                "[shell] parent setpgid raced worker exec; awaiting worker self-grouping",
            );
        }
    }

    let mut child = child;
    // Take the protocol pipes before publishing the child in WorkerSlot. The
    // slot remains the sole owner used by the watchdog and close path, while
    // WorkerRpc owns the pipes after installation.
    let stdin = match child.stdin.take() {
        Some(pipe) => pipe,
        None => {
            kill_worker_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "worker stdin was not piped",
            ));
        }
    };
    let stdout = match child.stdout.take() {
        Some(pipe) => pipe,
        None => {
            kill_worker_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "worker stdout was not piped",
            ));
        }
    };

    let mut log = match fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
    {
        Ok(log) => log,
        Err(error) => {
            kill_worker_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let _ = writeln!(
        log,
        "== tauri shell {} spawned worker: {} (cwd {}, data {})",
        env!("CARGO_PKG_VERSION"),
        py.display(),
        root.display(),
        data.display()
    );
    Ok((child, stdin, stdout))
}

/// A Windows job handle retained for the shell's lifetime. Closing it kills
/// the worker and every process it spawned when the shell is hard-terminated.
#[cfg(windows)]
struct WorkerJob {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
unsafe impl Send for WorkerJob {}

#[cfg(windows)]
impl Drop for WorkerJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.handle);
        }
    }
}

#[cfg(windows)]
fn windows_api_error(operation: &str, code: u32) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::Other,
        format!(
            "{operation} failed with Windows error {code}: {}",
            std::io::Error::from_raw_os_error(code as i32)
        ),
    )
}

#[cfg(windows)]
fn last_windows_api_error(operation: &str) -> std::io::Error {
    let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
    windows_api_error(operation, code)
}

#[cfg(windows)]
fn kill_on_close_limits(
) -> windows_sys::Win32::System::JobObjects::JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    let mut limits =
        windows_sys::Win32::System::JobObjects::JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags =
        windows_sys::Win32::System::JobObjects::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    limits
}

/// Create and configure the kill-on-close job, then assign the child before
/// returning its handle to the application state. Every Win32 failure is
/// returned so startup diagnostics can explain why supervision was unavailable.
#[cfg(windows)]
fn attach_worker_to_job(child: &Child) -> std::io::Result<WorkerJob> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject,
    };

    let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if handle.is_null() {
        return Err(last_windows_api_error("CreateJobObjectW"));
    }
    let job = WorkerJob { handle };
    let mut limits = kill_on_close_limits();
    let configured = unsafe {
        SetInformationJobObject(
            job.handle,
            JobObjectExtendedLimitInformation,
            &mut limits as *mut _ as *const _,
            std::mem::size_of_val(&limits) as u32,
        )
    };
    if configured == 0 {
        return Err(last_windows_api_error("SetInformationJobObject"));
    }
    let assigned = unsafe { AssignProcessToJobObject(job.handle, child.as_raw_handle()) };
    if assigned == 0 {
        return Err(last_windows_api_error("AssignProcessToJobObject"));
    }
    Ok(job)
}

#[cfg(test)]
mod embedded_tests {
    use super::*;

    #[test]
    fn entry_page_is_an_embedded_asset() {
        assert!(matches!(
            loading_page(),
            WebviewUrl::App(path) if path.to_string_lossy() == "loading.html"
        ));
        assert!(embedded_index_url().path().ends_with("/index.html"));
    }

    #[test]
    fn navigation_policy_rejects_loopback_and_external_urls() {
        assert!(is_embedded_url(&embedded_index_url()));
        let mut spa_route = embedded_index_url();
        spa_route.set_path("/pdf");
        assert!(is_embedded_url(&spa_route));
        assert!(!is_embedded_url(
            &Url::parse("http://127.0.0.1:9999/").unwrap()
        ));
        assert!(!is_embedded_url(
            &Url::parse("https://example.com/").unwrap()
        ));
        #[cfg(windows)]
        for unsafe_url in [
            "http://tauri.localhost:9999/",
            "http://user@tauri.localhost/",
            "http://sub.tauri.localhost/",
        ] {
            assert!(!is_embedded_url(&Url::parse(unsafe_url).unwrap()));
        }
        #[cfg(not(windows))]
        for unsafe_url in [
            "tauri://localhost:9999/",
            "tauri://user@localhost/",
            "tauri://sub.localhost/",
        ] {
            assert!(!is_embedded_url(&Url::parse(unsafe_url).unwrap()));
        }
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;

    #[test]
    fn kill_on_close_limit_uses_the_windows_flag() {
        let limits = kill_on_close_limits();
        assert_eq!(
            limits.BasicLimitInformation.LimitFlags,
            windows_sys::Win32::System::JobObjects::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        );
    }

    #[test]
    fn windows_api_errors_include_operation_and_code() {
        let error = windows_api_error("AssignProcessToJobObject", 5);
        let text = error.to_string();
        assert!(text.contains("AssignProcessToJobObject"));
        assert!(text.contains("Windows error 5"));
    }
}

fn take_exited_worker(slot: &WorkerSlot) -> Option<std::process::ExitStatus> {
    let mut guard = slot.lock().ok()?;
    let status = guard.as_mut()?.try_wait().ok()??;
    // try_wait has reaped the process. Remove the stale Child immediately so
    // Drop can never signal a recycled PID/process-group identifier.
    guard.take();
    Some(status)
}

/// Wait for the worker's bounded native handshake (or a child failure), then
/// show the already-embedded UI. The RPC timeout bounds startup failure UX.
fn watch_worker(app: AppHandle, slot: WorkerSlot, window: WebviewWindow, rpc: WorkerRpc) {
    let readiness = rpc.ready();
    if let Err(error) = readiness {
        let code = take_exited_worker(&slot)
            .map(|status| format!(" (exit code {status})"))
            .unwrap_or_default();
        let _ = app.run_on_main_thread({
            let w = window.clone();
            let d = data_dir();
            move || {
                fail_window(
                    &w,
                    &d,
                    "The part of the app that does the work could not become ready.",
                    &format!("{error}{code}\n{}", log_tail(&d, 20)),
                );
            }
        });
        rpc.shutdown();
        return;
    }

    let _ = app.run_on_main_thread({
        let w = window.clone();
        move || {
            // Readiness was established over the supervised native protocol;
            // the WebView remains on the embedded Tauri asset origin.
            let _ = w.navigate(embedded_index_url());
        }
    });

    // Crash watcher: poll the child; if it exits, surface it.
    loop {
        thread::sleep(Duration::from_secs(2));
        match take_exited_worker(&slot) {
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
                rpc.shutdown();
                return;
            }
            _ => continue,
        }
    }
}

/// Append one line to the app log (best effort — logging must never panic the shell).
fn record(path: &Path, line: &str) {
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{line}");
    }
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
    js.push_str("var text='SCM Workbench — startup report\\n\\n'");
    js.push_str("+document.getElementById('wb-reason').textContent");
    js.push_str("+'\\n\\n'+document.getElementById('wb-log').textContent;");
    js.push_str("navigator.clipboard.writeText(text)");
    js.push_str(".then(function(){b.textContent='Copied';})");
    js.push_str(".catch(function(){b.textContent='Couldn\\'t copy';});");
    js.push_str("};})();");

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
    // Render the failure in the embedded loading document instead of
    // navigating to a data:, file:, or worker-HTTP URL. This keeps the retry
    // command available while the navigation policy remains asset-only.
    let script = format!(
        "document.open();document.write({});document.close();",
        json_string(&html)
    );
    let _ = window.eval(&script);
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
