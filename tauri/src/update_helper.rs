//! The native, deliberately small external update helper.
//!
//! This module is entered before Tauri is initialised.  It has no protocol
//! pipes and emits no output: the data directory is its only interface.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
#[cfg(test)]
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const JOURNAL: &str = ".update-journal.json";
const ACK: &str = ".update-ack";
const HEALTH: &str = ".update-health.json";
const RESULT: &str = ".update-result.json";
const JOURNAL_LIMIT: u64 = 64 * 1024;
const REQUEST: &str = ".update-launch-request.json";
const HELPER_DIR: &str = "update-helper";
const HELPER_MAX_BYTES: u64 = 256 * 1024 * 1024;
const REQUEST_LIMIT: u64 = 4 * 1024;
const TOKEN_LEN: usize = 64;
const SCHEMA_VERSION: u32 = 1;
const HANDOFF_WAIT: Duration = Duration::from_secs(30);
const HEALTH_WAIT: Duration = Duration::from_secs(45);
const POLL: Duration = Duration::from_millis(100);
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
#[cfg(windows)]
const DETACHED_PROCESS: u32 = 0x0000_0008;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Handoff,
    Recover,
}

#[derive(Clone, Debug)]
pub struct Invocation {
    pub data_dir: PathBuf,
    pub token: String,
    pub mode: Mode,
    pub recovery_wait_pid: Option<u32>,
}

/// Parse only the one exact command line accepted by the helper.  In
/// particular, do not accept an argv tail which could accidentally become
/// arguments to the new application.
pub fn parse_cli(args: &[String]) -> Option<Result<Invocation, ()>> {
    if args.get(1).map(String::as_str) != Some("--update-helper") {
        return None;
    }
    if args.len() < 8 || args[2] != "--data-dir" || args[4] != "--token" || args[6] != "--mode" {
        return Some(Err(()));
    }
    let data = PathBuf::from(&args[3]);
    if !data.is_absolute() || !is_lexically_canonical(&data) {
        return Some(Err(()));
    }
    let token = args[5].clone();
    if !valid_token(&token) {
        return Some(Err(()));
    }
    let mode = match args[7].as_str() {
        "handoff" if args.len() == 8 => Mode::Handoff,
        "recover" if args.len() == 10 && args[8] == "--wait-pid" => Mode::Recover,
        _ => return Some(Err(())),
    };
    let recovery_wait_pid = if mode == Mode::Recover {
        let pid = args[9].parse::<u32>().ok().filter(|pid| *pid != 0);
        if pid.is_none() {
            return Some(Err(()));
        }
        pid
    } else {
        None
    };
    Some(Ok(Invocation {
        data_dir: data,
        token,
        mode,
        recovery_wait_pid,
    }))
}

pub fn run_cli(args: &[String]) -> Result<(), ()> {
    let invocation = match parse_cli(args) {
        Some(Ok(value)) => value,
        _ => return Err(()),
    };
    run(invocation).map_err(|_| ())
}

pub fn run(invocation: Invocation) -> io::Result<()> {
    let data = validate_data_dir(&invocation.data_dir)?;
    let journal_path = data.join(JOURNAL);
    let mut journal = read_journal(&journal_path)?;
    if journal.token != invocation.token {
        return Err(invalid("journal token does not match command token"));
    }
    let layout = validate_layout(&journal)?;
    match invocation.mode {
        Mode::Handoff => handoff(&data, &mut journal, &layout)?,
        Mode::Recover => {
            recover_with_wait(&data, &mut journal, &layout, invocation.recovery_wait_pid)?
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub image: String,
    pub start_token: u128,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Journal {
    pub version: u32,
    pub token: String,
    pub phase: Phase,
    pub expected_version: String,
    pub target: String,
    pub candidate_name: String,
    pub backup_name: String,
    pub old_shell_pid: u32,
    pub old_worker_pid: u32,
    #[serde(default)]
    pub old_shell_identity: Option<ProcessIdentity>,
    #[serde(default)]
    pub old_worker_identity: Option<ProcessIdentity>,
    #[serde(default)]
    pub new_pid: Option<u32>,
    #[serde(default)]
    pub new_identity: Option<ProcessIdentity>,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Phase {
    Prepared,
    Identified,
    BackupRenamed,
    CandidatePublished,
    Launching,
    Launched,
    Healthy,
    Completed,
    Rollback,
    Failed,
}

#[derive(Clone, Debug)]
struct Layout {
    target: PathBuf,
    parent: PathBuf,
    candidate: PathBuf,
    backup: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Health {
    /// The application version, not the journal schema version.
    version: String,
    token: String,
    nonce: String,
    canonical_target: String,
    new_shell_pid: u32,
    timestamp: u64,
}

#[derive(Clone, Debug, Serialize)]
struct ResultRecord<'a> {
    version: u32,
    token: &'a str,
    success: bool,
    expected_version: &'a str,
    message: &'a str,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn bounded_text(value: &str, max: usize) -> bool {
    !value.is_empty() && value.len() <= max && !value.chars().any(char::is_control)
}

pub(crate) fn valid_token(token: &str) -> bool {
    token.len() == TOKEN_LEN
        && token
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct LaunchRequest {
    pub version: u32,
    pub token: String,
}

/// Read the shell-side launch request.  A malformed, symlinked, oversized, or
/// otherwise non-regular request is simply not actionable by the watcher.
pub(crate) fn read_launch_request(data: &Path) -> io::Result<Option<LaunchRequest>> {
    let path = data.join(REQUEST);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if !meta.file_type().is_file() || meta.len() > REQUEST_LIMIT {
        return Err(invalid("launch request is not a regular bounded file"));
    }
    let bytes = fs::read(&path)?;
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| invalid("malformed launch request"))?;
    let object = value
        .as_object()
        .ok_or_else(|| invalid("launch request is not an object"))?;
    if object.len() != 2 || !object.contains_key("version") || !object.contains_key("token") {
        return Err(invalid("launch request has an unexpected shape"));
    }
    let request: LaunchRequest =
        serde_json::from_value(value).map_err(|_| invalid("invalid launch request schema"))?;
    if request.version != SCHEMA_VERSION || !valid_token(&request.token) {
        return Err(invalid("invalid launch request fields"));
    }
    Ok(Some(request))
}

pub(crate) fn pending_journal(data: &Path) -> io::Result<Option<Journal>> {
    let data = validate_data_dir(data)?;
    let path = data.join(JOURNAL);
    match fs::symlink_metadata(&path) {
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    }
    let journal = read_journal(&path)?;
    let _ = validate_layout(&journal)?;
    Ok(Some(journal))
}

fn is_lexically_canonical(path: &Path) -> bool {
    !path
        .components()
        .any(|part| matches!(part, Component::CurDir | Component::ParentDir))
}

pub(crate) fn canonical_data_dir(input: &Path) -> io::Result<PathBuf> {
    validate_data_dir(input)
}

fn validate_data_dir(input: &Path) -> io::Result<PathBuf> {
    if !input.is_absolute() || !is_lexically_canonical(input) {
        return Err(invalid("data directory must be an absolute canonical path"));
    }
    reject_symlink_components(input)?;
    let metadata = fs::symlink_metadata(input)?;
    if !metadata.is_dir() {
        return Err(invalid("data directory is not a directory"));
    }
    let canonical = fs::canonicalize(input)?;
    if !same_path_spelling(&canonical, input) {
        return Err(invalid("data directory is not canonical"));
    }
    Ok(canonical)
}

fn same_path_spelling(left: &Path, right: &Path) -> bool {
    #[cfg(windows)]
    {
        windows_without_verbatim_prefix(left) == windows_without_verbatim_prefix(right)
    }
    #[cfg(not(windows))]
    {
        left == right
    }
}

/// `std::fs::canonicalize` adds the Win32 verbatim prefix (`\\?\`) even when
/// its input is the ordinary absolute path supplied by LOCALAPPDATA,
/// `Path.resolve()`, or PowerShell's Resolve-Path. That representation change
/// is not a path change, so remove only that prefix for lexical equality
/// checks. Canonical verbatim paths are still returned and used for all I/O.
#[cfg(windows)]
fn windows_without_verbatim_prefix(path: &Path) -> PathBuf {
    use std::ffi::OsString;
    use std::os::windows::ffi::{OsStrExt, OsStringExt};

    const VERBATIM: &[u16] = &[b'\\' as u16, b'\\' as u16, b'?' as u16, b'\\' as u16];
    const VERBATIM_UNC: &[u16] = &[
        b'\\' as u16,
        b'\\' as u16,
        b'?' as u16,
        b'\\' as u16,
        b'U' as u16,
        b'N' as u16,
        b'C' as u16,
        b'\\' as u16,
    ];
    let wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    let ordinary = if wide.starts_with(VERBATIM_UNC) {
        let mut value = vec![b'\\' as u16, b'\\' as u16];
        value.extend_from_slice(&wide[VERBATIM_UNC.len()..]);
        value
    } else if wide.starts_with(VERBATIM) {
        wide[VERBATIM.len()..].to_vec()
    } else {
        wide
    };
    PathBuf::from(OsString::from_wide(&ordinary))
}

fn reject_symlink_components(path: &Path) -> io::Result<()> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        if let Component::RootDir = component {
            continue;
        }
        if let Ok(meta) = fs::symlink_metadata(&current) {
            if meta.file_type().is_symlink() {
                return Err(invalid("path contains a symbolic link"));
            }
        }
    }
    Ok(())
}

fn read_journal(path: &Path) -> io::Result<Journal> {
    let meta = fs::symlink_metadata(path).map_err(|_| invalid("update journal is missing"))?;
    if !meta.file_type().is_file() || meta.len() > JOURNAL_LIMIT {
        return Err(invalid("update journal is not a regular bounded file"));
    }
    let bytes = fs::read(path)?;
    let journal: Journal =
        serde_json::from_slice(&bytes).map_err(|_| invalid("malformed update journal"))?;
    if journal.version != SCHEMA_VERSION
        || !valid_token(&journal.token)
        || !bounded_text(&journal.expected_version, 128)
        || !bounded_text(&journal.target, 32 * 1024)
    {
        return Err(invalid("invalid update journal schema"));
    }
    if journal.old_shell_pid == 0 || journal.old_worker_pid == 0 {
        return Err(invalid("invalid update journal identity or version"));
    }
    Ok(journal)
}

fn validate_layout(journal: &Journal) -> io::Result<Layout> {
    let target = PathBuf::from(&journal.target);
    if !target.is_absolute() || !is_lexically_canonical(&target) {
        return Err(invalid("target must be an absolute canonical path"));
    }
    reject_symlink_components(&target)?;
    let supplied_parent = target
        .parent()
        .ok_or_else(|| invalid("target has no parent"))?;
    let parent = fs::canonicalize(supplied_parent)?;
    let name = target
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or_else(|| invalid("target has invalid basename"))?;
    if !same_path_spelling(supplied_parent, &parent) || !valid_target_basename(name) {
        return Err(invalid("target parent or application basename is invalid"));
    }
    let target = parent.join(name);
    if !same_path_spelling(&target, Path::new(&journal.target)) {
        return Err(invalid("target is not canonical"));
    }
    if fs::symlink_metadata(&target)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(invalid("target is a symbolic link"));
    }
    if target.exists() {
        validate_application_layout(&target)?;
    }
    let candidate_expected = format!(".SCM-Workbench-candidate-{}", journal.token);
    let backup_expected = format!(".SCM-Workbench-backup-{}", journal.token);
    if journal.candidate_name != candidate_expected || journal.backup_name != backup_expected {
        return Err(invalid("candidate or backup name is not token-bound"));
    }
    let candidate = parent.join(&journal.candidate_name);
    let backup = parent.join(&journal.backup_name);
    if candidate.parent() != Some(parent.as_path()) || backup.parent() != Some(parent.as_path()) {
        return Err(invalid("candidate and backup must be direct siblings"));
    }
    ensure_same_volume(&target, &candidate)?;
    Ok(Layout {
        target,
        parent,
        candidate,
        backup,
    })
}

fn valid_target_basename(name: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        name == "SCM Workbench.app"
    }
    #[cfg(windows)]
    {
        // The Windows artifact is a flat directory. Its install directory
        // name is user-controlled; the exact app basename is enforced by the
        // required SCM Workbench.exe entry below.
        bounded_text(name, 255)
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        name == "scm-workbench" || name == "SCM Workbench"
    }
}

fn ensure_same_volume(a: &Path, b: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let av = fs::metadata(a.parent().unwrap_or(a))?.dev();
        let bv = fs::metadata(b.parent().unwrap_or(b))?.dev();
        if av != bv {
            return Err(invalid("target and candidate are on different volumes"));
        }
    }
    Ok(())
}

fn journal_write(data: &Path, journal: &Journal) -> io::Result<()> {
    let bytes = serde_json::to_vec(journal).map_err(|_| invalid("cannot encode update journal"))?;
    if bytes.len() as u64 > JOURNAL_LIMIT {
        return Err(invalid("update journal exceeds 64 KiB"));
    }
    atomic_write(data.join(JOURNAL).as_path(), &bytes)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("atomic file has no parent"))?;
    fs::create_dir_all(parent)?;
    let pid = std::process::id();
    let n = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp = parent.join(format!(
        ".{}.tmp-{}-{}",
        path.file_name().unwrap().to_string_lossy(),
        pid,
        n
    ));
    let result = (|| {
        let mut f = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        drop(f);
        replace_file(&tmp, path)?;
        sync_dir(parent)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

#[cfg(not(windows))]
fn replace_file(from: &Path, to: &Path) -> io::Result<()> {
    fs::rename(from, to)
}

#[cfg(windows)]
fn replace_file(from: &Path, to: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let mut source: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut destination: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    let ok = unsafe {
        MoveFileExW(
            source.as_mut_ptr(),
            destination.as_mut_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        return Err(io::Error::from_raw_os_error(
            unsafe { GetLastError() } as i32
        ));
    }
    Ok(())
}

fn sync_dir(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        File::open(path)?.sync_all()?;
    }
    Ok(())
}

/// Materialize the running shell outside the bundle.  The helper is copied
/// before it is launched so an updater can replace the bundle without
/// unlinking the image executing the helper.
pub(crate) fn materialize_helper(
    current_exe: &Path,
    data: &Path,
    token: &str,
) -> io::Result<PathBuf> {
    if !valid_token(token) {
        return Err(invalid("invalid helper token"));
    }
    let data = validate_data_dir(data)?;
    let source_meta = fs::symlink_metadata(current_exe)?;
    if !source_meta.file_type().is_file() || source_meta.len() > HELPER_MAX_BYTES {
        return Err(invalid("current executable is not a bounded regular file"));
    }
    let dir = data.join(HELPER_DIR);
    fs::create_dir_all(&dir)?;
    let dir_meta = fs::symlink_metadata(&dir)?;
    if !dir_meta.is_dir()
        || dir_meta.file_type().is_symlink()
        || !same_path_spelling(&fs::canonicalize(&dir)?, &dir)
    {
        return Err(invalid("helper directory is not a canonical directory"));
    }
    let destination = dir.join(token);
    if let Ok(existing) = fs::symlink_metadata(&destination) {
        if !existing.file_type().is_file()
            || existing.file_type().is_symlink()
            || existing.len() > HELPER_MAX_BYTES
        {
            return Err(invalid("existing helper is not a bounded regular file"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let source_mode = source_meta.permissions().mode() & 0o777;
            let existing_mode = existing.permissions().mode() & 0o777;
            if source_mode & 0o022 != 0
                || existing_mode != source_mode
                || existing_mode & 0o022 != 0
            {
                return Err(invalid("existing helper has unsafe permissions"));
            }
        }
        let mut source = File::open(current_exe)?;
        let mut copy = File::open(&destination)?;
        let mut source_hash = Sha256::new();
        let mut copy_hash = Sha256::new();
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            source_hash.update(&buffer[..count]);
        }
        loop {
            let count = copy.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            copy_hash.update(&buffer[..count]);
        }
        if source_hash.finalize() == copy_hash.finalize() {
            return Ok(destination);
        }
        return Err(invalid("existing helper does not match current executable"));
    }
    let temporary = dir.join(format!(".{token}.tmp-{}", std::process::id()));
    let result = (|| {
        let mut source = File::open(current_exe)?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        let mut hash = Sha256::new();
        let mut copied = 0u64;
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            output.write_all(&buffer[..count])?;
            hash.update(&buffer[..count]);
            copied = copied.saturating_add(count as u64);
        }
        if copied != source_meta.len() {
            return Err(invalid("helper copy byte count changed"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                &temporary,
                fs::Permissions::from_mode(source_meta.permissions().mode()),
            )?;
        }
        output.sync_all()?;
        drop(output);
        let mut verify_file = File::open(&temporary)?;
        let mut verify_hash = Sha256::new();
        let mut verified = 0u64;
        loop {
            let count = verify_file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            verify_hash.update(&buffer[..count]);
            verified = verified.saturating_add(count as u64);
        }
        if verified != copied || verify_hash.finalize() != hash.finalize() {
            return Err(invalid("helper copy verification failed"));
        }
        rename_noreplace(&temporary, &destination)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map(|_| destination)
}

pub(crate) fn spawn_helper(
    helper: &Path,
    data: &Path,
    token: &str,
    mode: Mode,
    recovery_wait_pid: Option<u32>,
) -> io::Result<Child> {
    if !valid_token(token) || (mode == Mode::Handoff && recovery_wait_pid.is_some()) {
        return Err(invalid("invalid helper launch arguments"));
    }
    if mode == Mode::Recover && recovery_wait_pid.unwrap_or(0) == 0 {
        return Err(invalid("recovery helper requires a wait PID"));
    }
    let helper_meta = fs::symlink_metadata(helper)?;
    if !helper_meta.file_type().is_file() || helper_meta.len() > HELPER_MAX_BYTES {
        return Err(invalid("helper executable is not a bounded regular file"));
    }
    let data = validate_data_dir(data)?;
    let mut command = Command::new(helper);
    command
        .arg("--update-helper")
        .arg("--data-dir")
        .arg(data.as_os_str())
        .arg("--token")
        .arg(token)
        .arg("--mode")
        .arg(match mode {
            Mode::Handoff => "handoff",
            Mode::Recover => "recover",
        });
    if let Some(pid) = recovery_wait_pid {
        command.arg("--wait-pid").arg(pid.to_string());
    }
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW);
    }
    command.spawn()
}

pub(crate) fn remove_launch_request(data: &Path) -> io::Result<()> {
    let path = data.join(REQUEST);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if meta.file_type().is_symlink() || !meta.file_type().is_file() {
        return Err(invalid("launch request is not a regular file"));
    }
    fs::remove_file(path)
}

pub(crate) fn remove_ack(data: &Path) -> io::Result<()> {
    let path = data.join(ACK);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if meta.file_type().is_symlink() || !meta.file_type().is_file() {
        return Err(invalid("update ACK is not a regular file"));
    }
    fs::remove_file(path)
}

#[allow(dead_code)]
pub(crate) fn wait_for_ack(data: &Path, token: &str, timeout: Duration) -> bool {
    wait_for_ack_since(data, token, timeout, SystemTime::now())
}

pub(crate) fn wait_for_ack_since(
    data: &Path,
    token: &str,
    timeout: Duration,
    attempt_started: SystemTime,
) -> bool {
    if !valid_token(token) {
        return false;
    }
    let end = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < end {
        let path = data.join(ACK);
        if let Ok(meta) = fs::symlink_metadata(&path) {
            let fresh = meta
                .modified()
                .ok()
                .map(|mtime| mtime >= attempt_started)
                .unwrap_or(false);
            if meta.file_type().is_file() && meta.len() == TOKEN_LEN as u64 && fresh {
                if fs::read(&path).ok().as_deref() == Some(token.as_bytes()) {
                    return true;
                }
            }
        }
        thread::sleep(POLL);
    }
    false
}

/// Only stale, token-shaped regular files are eligible.  In particular a
/// symlink is never followed or removed.  The age fence avoids unlinking a
/// helper which a rollback relaunch is still executing.
pub(crate) fn cleanup_old_helpers(data: &Path, older_than: Duration) -> io::Result<()> {
    let dir = data.join(HELPER_DIR);
    let meta = match fs::symlink_metadata(&dir) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || !same_path_spelling(&fs::canonicalize(&dir)?, &dir)
    {
        return Err(invalid("helper directory is unsafe"));
    }
    let now = SystemTime::now();
    for entry in fs::read_dir(&dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if !valid_token(name) {
            continue;
        }
        let meta = fs::symlink_metadata(&path)?;
        if !meta.file_type().is_file() || meta.file_type().is_symlink() {
            continue;
        }
        let old = meta
            .modified()
            .ok()
            .and_then(|time| now.duration_since(time).ok())
            .map(|age| age >= older_than)
            .unwrap_or(false);
        if old {
            let _ = fs::remove_file(path);
        }
    }
    Ok(())
}

fn ack(data: &Path, token: &str) -> io::Result<()> {
    atomic_write(&data.join(ACK), token.as_bytes())
}

fn handoff(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    if journal.phase != Phase::Prepared {
        return recover(data, journal, layout);
    }
    let shell = process_identity(journal.old_shell_pid)
        .ok_or_else(|| invalid("old shell is not running"))?;
    let worker = process_identity(journal.old_worker_pid)
        .ok_or_else(|| invalid("old worker is not running"))?;
    if !old_identities_ok(&shell, &worker, &layout.target) {
        return Err(invalid("old process identity does not match target"));
    }
    journal.old_shell_identity = Some(shell.clone());
    journal.old_worker_identity = Some(worker.clone());
    journal.phase = Phase::Identified;
    journal_write(data, journal)?;
    ack(data, &journal.token)?;
    wait_for_identities(
        &[
            (journal.old_shell_pid, &shell),
            (journal.old_worker_pid, &worker),
        ],
        HANDOFF_WAIT,
    )?;
    continue_transaction(data, journal, layout)
}

fn recover(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    recover_with_wait(data, journal, layout, None)
}

fn recover_with_wait(
    data: &Path,
    journal: &mut Journal,
    layout: &Layout,
    recovery_wait_pid: Option<u32>,
) -> io::Result<()> {
    // A recovery helper is launched by the shell which still has the old
    // target open. Capture that shell's identity before waiting; a PID alone
    // is never sufficient to authorize a rename.
    if let Some(pid) = recovery_wait_pid {
        let expected = fs::canonicalize(executable_for_target(&layout.target))
            .unwrap_or_else(|_| executable_for_target(&layout.target));
        let identity = process_identity(pid)
            .ok_or_else(|| invalid("recovery shell identity could not be captured"))?;
        if Path::new(&identity.image) != expected {
            return Err(invalid(
                "recovery shell does not match the target executable",
            ));
        }
        wait_for_identities(&[(pid, &identity)], HANDOFF_WAIT)?;
    }
    // Recovery observes the recorded old identities before touching either
    // copy. This preserves the handoff fence even if the helper itself died.
    if let (Some(shell), Some(worker)) = (&journal.old_shell_identity, &journal.old_worker_identity)
    {
        wait_for_identities(
            &[
                (journal.old_shell_pid, shell),
                (journal.old_worker_pid, worker),
            ],
            HANDOFF_WAIT,
        )?;
    }
    // Rename is atomic, so target-missing plus a journaled backup is the only
    // unambiguous interrupted state. Restore it before considering health;
    // never try to launch while the old application is absent.
    if !layout.target.exists()
        && layout.backup.exists()
        && matches!(
            journal.phase,
            Phase::BackupRenamed
                | Phase::CandidatePublished
                | Phase::Launched
                | Phase::Healthy
                | Phase::Completed
                | Phase::Rollback
                | Phase::Failed
        )
        && (journal.phase != Phase::BackupRenamed || !layout.candidate.exists())
    {
        validate_tree(&layout.backup)?;
        rename_noreplace(&layout.backup, &layout.target)?;
        if layout.candidate.exists() {
            remove_authorized_tree(&layout.candidate, &layout.parent, &journal.candidate_name)?;
        }
        journal.phase = Phase::Failed;
        journal_write(data, journal)?;
        return finish_failure(
            data,
            journal,
            layout,
            "interrupted publish restored the previous application",
        );
    }
    if journal.phase == Phase::BackupRenamed
        && layout.target.exists()
        && layout.backup.exists()
        && !layout.candidate.exists()
    {
        return rollback_after_failure(data, journal, layout);
    }
    match journal.phase {
        Phase::Prepared => rollback_prepared(data, journal, layout),
        Phase::Identified | Phase::BackupRenamed => continue_transaction(data, journal, layout),
        // A crash after publication but before launch must not silently leave
        // a new tree installed.  Only the launched phase has an identity that
        // can be health-checked.
        Phase::CandidatePublished => rollback_after_failure(data, journal, layout),
        Phase::Launching => recover_new_process(data, journal, layout),
        Phase::Launched => recover_new_process(data, journal, layout),
        Phase::Healthy | Phase::Completed => complete(data, journal, layout),
        Phase::Rollback => rollback_after_failure(data, journal, layout),
        Phase::Failed => {
            cleanup_failed(data, journal, layout)?;
            Ok(())
        }
    }
}

fn publish_candidate(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    if journal.phase == Phase::Identified {
        if layout.backup.exists() {
            // The rename may have reached disk just before its journal write.
            // Both copies present is not recoverable by guessing.
            if layout.target.exists() {
                return Err(invalid("target and backup both exist in identified phase"));
            }
            journal.phase = Phase::BackupRenamed;
            journal_write(data, journal)?;
        } else {
            if !layout.target.exists() {
                return Err(invalid("target disappeared before backup"));
            }
            rename_noreplace(&layout.target, &layout.backup)?;
            journal.phase = Phase::BackupRenamed;
            journal_write(data, journal)?;
        }
    }
    if journal.phase != Phase::BackupRenamed {
        return Err(invalid(
            "candidate publication requires identified or backup-renamed phase",
        ));
    }
    if !layout.backup.exists() {
        return Err(invalid("backup disappeared before candidate publish"));
    }
    validate_application_layout(&layout.candidate)?;
    if layout.target.exists() {
        return Err(invalid("target unexpectedly exists before publish"));
    }
    rename_noreplace(&layout.candidate, &layout.target)?;
    journal.phase = Phase::CandidatePublished;
    journal_write(data, journal)
}

fn continue_transaction(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    publish_candidate(data, journal, layout)?;
    launch_and_wait(data, journal, layout)
}

pub(crate) fn target_for_current_exe(exe: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        exe.parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .unwrap_or_else(|| exe.to_path_buf())
    }
    #[cfg(windows)]
    {
        exe.parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| exe.to_path_buf())
    }
    #[cfg(all(not(target_os = "macos"), not(windows)))]
    {
        exe.to_path_buf()
    }
}

pub(crate) fn journal_matches_current_target(journal: &Journal, exe: &Path) -> bool {
    let current = fs::canonicalize(target_for_current_exe(exe))
        .unwrap_or_else(|_| target_for_current_exe(exe));
    same_path_spelling(Path::new(&journal.target), &current)
}

/// Called only after the worker RPC is installed.  A normal startup can never
/// manufacture this marker: all values are bound to the pending transaction,
/// the current process, and the native package version.
pub(crate) fn publish_health(
    data: &Path,
    current_exe: &Path,
    token: &str,
    nonce: &str,
) -> io::Result<bool> {
    publish_health_for_target(
        data,
        current_exe,
        token,
        nonce,
        &target_for_current_exe(current_exe),
    )
}

#[cfg_attr(not(test), allow(dead_code))]
fn publish_health_for_target(
    data: &Path,
    current_exe: &Path,
    token: &str,
    nonce: &str,
    target: &Path,
) -> io::Result<bool> {
    if !valid_token(token) || !valid_token(nonce) {
        return Ok(false);
    }
    let current_image = fs::canonicalize(current_exe).unwrap_or_else(|_| current_exe.to_path_buf());
    let Some(identity) = process_identity(std::process::id()) else {
        return Ok(false);
    };
    if Path::new(&identity.image) != current_image {
        return Ok(false);
    }
    let Some(journal) = pending_journal(data)? else {
        return Ok(false);
    };
    let canonical_target = fs::canonicalize(target).unwrap_or_else(|_| target.to_path_buf());
    if journal.token != token
        || !same_path_spelling(Path::new(&journal.target), &canonical_target)
        || !matches!(journal.phase, Phase::Launching | Phase::Launched)
    {
        return Ok(false);
    }
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    let health = Health {
        version: env!("CARGO_PKG_VERSION").to_owned(),
        token: token.to_owned(),
        nonce: nonce.to_owned(),
        canonical_target: canonical_target.to_string_lossy().into_owned(),
        new_shell_pid: std::process::id(),
        timestamp,
    };
    let bytes = serde_json::to_vec(&health).map_err(|_| invalid("cannot encode health marker"))?;
    atomic_write(&data.join(HEALTH), &bytes)?;
    Ok(true)
}

fn executable_for_target(target: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        target.join("Contents/MacOS/SCM Workbench")
    }
    #[cfg(windows)]
    {
        target.join("SCM Workbench.exe")
    }
    #[cfg(all(not(target_os = "macos"), not(windows)))]
    {
        target.to_path_buf()
    }
}

/// Validate the two identities captured during handoff without consulting
/// process state. Inputs are already canonical image paths from the native
/// identity query, so component-aware `starts_with` rejects sibling prefixes
/// and symlink escapes.
fn old_identities_ok(shell: &ProcessIdentity, worker: &ProcessIdentity, target: &Path) -> bool {
    let executable = executable_for_target(target);
    let allowed = [
        target.join("Contents/runtime"),
        target.join("Contents/app"),
        target.join("runtime"),
        target.join("app"),
    ];
    shell.image == executable.to_string_lossy()
        && allowed
            .iter()
            .any(|root| Path::new(&worker.image).starts_with(root))
}

fn validate_application_layout(root: &Path) -> io::Result<()> {
    validate_tree(root)?;
    #[cfg(target_os = "macos")]
    {
        let executable = root.join("Contents/MacOS/SCM Workbench");
        let meta = fs::symlink_metadata(&executable)
            .map_err(|_| invalid("application bundle executable is missing"))?;
        if !meta.is_file() || !path_is_safe_link(&executable, root)? {
            return Err(invalid("application bundle executable is unsafe"));
        }
        for required in [
            root.join("Contents/app/scm_workbench"),
            root.join("Contents/app/ui"),
            root.join("Contents/runtime"),
        ] {
            if !required.exists() {
                return Err(invalid("application bundle layout is incomplete"));
            }
        }
    }
    #[cfg(windows)]
    {
        let meta = fs::symlink_metadata(root)?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(invalid("Windows application target is not a directory"));
        }
        for required in [
            root.join("SCM Workbench.exe"),
            root.join("app/scm_workbench"),
            root.join("app/ui"),
            root.join("runtime"),
        ] {
            if !required.exists() {
                return Err(invalid("Windows application layout is incomplete"));
            }
        }
    }
    Ok(())
}

fn validate_tree(root: &Path) -> io::Result<()> {
    if fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err(invalid("application tree root is a symbolic link"));
    }
    let canonical_root = fs::canonicalize(root)?;
    let mut visited = HashSet::new();
    let mut stack = HashSet::new();
    validate_tree_inner(root, &canonical_root, &mut visited, &mut stack)
}

fn validate_tree_inner(
    path: &Path,
    root: &Path,
    visited: &mut HashSet<PathBuf>,
    stack: &mut HashSet<PathBuf>,
) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink() {
        let link = fs::read_link(path)?;
        if link.is_absolute() {
            return Err(invalid("application tree has an absolute symbolic link"));
        }
        let resolved = fs::canonicalize(path)?;
        if !resolved.starts_with(root) {
            return Err(invalid("application tree symbolic link escapes root"));
        }
        return validate_tree_inner(&resolved, root, visited, stack);
    }
    if !meta.is_dir() && !meta.is_file() {
        return Err(invalid("application tree contains a special file"));
    }
    let canonical = fs::canonicalize(path)?;
    if !canonical.starts_with(root) {
        return Err(invalid("application tree path escapes root"));
    }
    if !stack.insert(canonical.clone()) {
        return Err(invalid("application tree contains a symbolic-link cycle"));
    }
    if visited.insert(canonical.clone()) && meta.is_dir() {
        for entry in fs::read_dir(path)? {
            validate_tree_inner(&entry?.path(), root, visited, stack)?;
        }
    }
    stack.remove(&canonical);
    Ok(())
}

fn path_is_safe_link(path: &Path, root: &Path) -> io::Result<bool> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_symlink() {
        return Ok(meta.is_file());
    }
    let link = fs::read_link(path)?;
    if link.is_absolute() {
        return Ok(false);
    }
    Ok(fs::canonicalize(path)?.starts_with(fs::canonicalize(root)?))
}

fn rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    if from.parent() != to.parent() {
        return Err(invalid("rename is not a sibling rename"));
    }
    if fs::symlink_metadata(to).is_ok() {
        return Err(invalid("rename destination already exists"));
    }
    platform_rename_noreplace(from, to)?;
    sync_dir(from.parent().unwrap())
}

#[cfg(target_os = "macos")]
fn platform_rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let from =
        CString::new(from.as_os_str().as_bytes()).map_err(|_| invalid("NUL in rename path"))?;
    let to = CString::new(to.as_os_str().as_bytes()).map_err(|_| invalid("NUL in rename path"))?;
    let rc = unsafe { renamex_np(from.as_ptr(), to.as_ptr(), 0x0000_0004) }; // RENAME_EXCL
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(all(unix, not(target_os = "macos")))]
fn platform_rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let from = std::ffi::CString::new(from.as_os_str().as_bytes())
        .map_err(|_| invalid("NUL in rename path"))?;
    let to = std::ffi::CString::new(to.as_os_str().as_bytes())
        .map_err(|_| invalid("NUL in rename path"))?;
    let rc = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            from.as_ptr(),
            libc::AT_FDCWD,
            to.as_ptr(),
            1,
        )
    }; // RENAME_NOREPLACE
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(windows)]
fn platform_rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
    let mut source: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut destination: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    let ok = unsafe {
        MoveFileExW(
            source.as_mut_ptr(),
            destination.as_mut_ptr(),
            MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(all(not(unix), not(windows)))]
fn platform_rename_noreplace(from: &Path, to: &Path) -> io::Result<()> {
    fs::rename(from, to)
}

fn recover_new_process(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    let pid = match journal.new_pid {
        Some(pid) => pid,
        None => return Err(invalid("launching phase has no adoptable process identity")),
    };
    let identity = match journal.new_identity.as_ref() {
        Some(identity) => identity,
        None => return Err(invalid("launching phase has no adoptable process identity")),
    };
    let expected = fs::canonicalize(executable_for_target(&layout.target))
        .unwrap_or_else(|_| executable_for_target(&layout.target));
    match process_identity(pid) {
        Some(actual) if actual == *identity && Path::new(&actual.image) == expected => {
            terminate_matching(pid, identity);
            wait_for_identities(&[(pid, identity)], Duration::from_secs(5))?;
            // The identity has now been positively observed to disappear;
            // clear it before rollback so an unreaped zombie PID cannot be
            // mistaken for an unrelated live process.
            journal.new_pid = None;
            journal.new_identity = None;
            rollback_after_failure(data, journal, layout)
        }
        None if !pid_alive(pid) => rollback_after_failure(data, journal, layout),
        None => Err(invalid("launching PID could not be safely identified")),
        Some(_) => Err(invalid("launching PID was reused by an unknown process")),
    }
}

fn launch_nonce() -> io::Result<String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| invalid("OS CSPRNG unavailable"))?;
    let mut encoded = String::with_capacity(64);
    for byte in bytes {
        encoded.push_str(&format!("{byte:02x}"));
    }
    Ok(encoded)
}

fn launch_and_wait(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    let executable = executable_for_target(&layout.target);
    validate_application_layout(&layout.target)?;
    journal.phase = Phase::Launching;
    journal_write(data, journal)?;
    let nonce = launch_nonce()?;
    let mut command = Command::new(&executable);
    command
        .env("SCM_WORKBENCH_UPDATE_TOKEN", &journal.token)
        .env("SCM_WORKBENCH_UPDATE_NONCE", &nonce);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW);
    }
    let mut child = command.spawn()?;
    let pid = child.id();
    let identity = wait_for_identity(&mut child, &executable, Duration::from_secs(5))?;
    journal.new_pid = Some(pid);
    journal.new_identity = Some(identity);
    journal.phase = Phase::Launched;
    journal_write(data, journal)?;
    let result = await_health(data, journal, layout, &nonce, Some(&mut child));
    drop(child);
    result
}

fn wait_for_identity(
    child: &mut std::process::Child,
    expected: &Path,
    timeout: Duration,
) -> io::Result<ProcessIdentity> {
    let pid = child.id();
    let end = std::time::Instant::now() + timeout;
    let expected = fs::canonicalize(expected).unwrap_or_else(|_| expected.to_path_buf());
    loop {
        if child.try_wait()?.is_some() {
            return Err(invalid("new shell exited before identity was recorded"));
        }
        if let Some(identity) = process_identity(pid) {
            if Path::new(&identity.image) == expected {
                return Ok(identity);
            }
        }
        if std::time::Instant::now() >= end {
            return Err(invalid("new shell identity was not observed"));
        }
        thread::sleep(POLL);
    }
}

fn await_health(
    data: &Path,
    journal: &mut Journal,
    layout: &Layout,
    nonce: &str,
    mut child: Option<&mut std::process::Child>,
) -> io::Result<()> {
    let pid = journal
        .new_pid
        .ok_or_else(|| invalid("launched phase has no new pid"))?;
    let identity = journal
        .new_identity
        .as_ref()
        .ok_or_else(|| invalid("launched phase has no new identity"))?;
    let end = std::time::Instant::now() + HEALTH_WAIT;
    while std::time::Instant::now() < end {
        if let Some(process) = child.as_deref_mut() {
            if process.try_wait()?.is_some() {
                return rollback_after_failure(data, journal, layout);
            }
        }
        if let Ok(health) = read_health(&data.join(HEALTH)) {
            let now_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            if health_fields_accept(&health, journal, layout, nonce, pid, now_ms)
                && identity_matches(pid, identity)
            {
                journal.phase = Phase::Healthy;
                journal_write(data, journal)?;
                return complete(data, journal, layout);
            }
        }
        if !identity_matches(pid, identity) {
            return rollback_after_failure(data, journal, layout);
        }
        thread::sleep(POLL);
    }
    terminate_matching(pid, identity);
    if let Some(process) = child.as_deref_mut() {
        let end = std::time::Instant::now() + Duration::from_secs(5);
        while std::time::Instant::now() < end && process.try_wait()?.is_none() {
            thread::sleep(POLL);
        }
    }
    journal.phase = Phase::Rollback;
    journal_write(data, journal)?;
    rollback_after_failure(data, journal, layout)
}

fn read_health(path: &Path) -> io::Result<Health> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_file() || meta.len() > JOURNAL_LIMIT {
        return Err(invalid("invalid health marker file"));
    }
    let health: Health =
        serde_json::from_slice(&fs::read(path)?).map_err(|_| invalid("malformed health marker"))?;
    if !bounded_text(&health.version, 128)
        || !valid_token(&health.token)
        || !valid_token(&health.nonce)
        || !bounded_text(&health.canonical_target, 32 * 1024)
        || health.new_shell_pid == 0
    {
        return Err(invalid("health marker fields are invalid"));
    }
    Ok(health)
}

fn fresh_timestamp(timestamp: u64, now_ms: u64) -> bool {
    timestamp <= now_ms.saturating_add(5_000)
        && now_ms.saturating_sub(timestamp) <= HEALTH_WAIT.as_millis() as u64
}

fn health_fields_accept(
    health: &Health,
    journal: &Journal,
    layout: &Layout,
    nonce: &str,
    pid: u32,
    now_ms: u64,
) -> bool {
    health.version == journal.expected_version
        && health.token == journal.token
        && health.nonce == nonce
        && health.canonical_target == layout.target.to_string_lossy()
        && health.new_shell_pid == pid
        && fresh_timestamp(health.timestamp, now_ms)
}

fn complete(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    if journal.phase != Phase::Healthy && journal.phase != Phase::Completed {
        return Err(invalid("cleanup before healthy state"));
    }
    if !layout.target.exists() {
        return Err(invalid("cannot complete an update without the target"));
    }
    validate_tree(&layout.target)?;
    journal.phase = Phase::Completed;
    journal_write(data, journal)?;
    if layout.backup.exists() {
        remove_authorized_tree(&layout.backup, &layout.parent, &journal.backup_name)?;
    }
    let result = ResultRecord {
        version: SCHEMA_VERSION,
        token: &journal.token,
        success: true,
        expected_version: &journal.expected_version,
        message: "update completed",
    };
    let bytes = serde_json::to_vec(&result).map_err(|_| invalid("cannot encode update result"))?;
    if bytes.len() as u64 > JOURNAL_LIMIT {
        return Err(invalid("update result exceeds 64 KiB"));
    }
    atomic_write(&data.join(RESULT), &bytes)?;
    let _ = fs::remove_file(data.join(HEALTH));
    fs::remove_file(data.join(JOURNAL))?;
    sync_dir(data)
}

fn rollback_prepared(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    if layout.backup.exists() {
        return rollback_after_failure(data, journal, layout);
    }
    if layout.target.exists() && layout.candidate.exists() {
        remove_authorized_tree(&layout.candidate, &layout.parent, &journal.candidate_name)?;
    }
    finish_failure(data, journal, layout, "prepared update was not handed off")
}

fn rollback_after_failure(data: &Path, journal: &mut Journal, layout: &Layout) -> io::Result<()> {
    if let (Some(pid), Some(identity)) = (journal.new_pid, journal.new_identity.as_ref()) {
        if identity_matches(pid, identity) {
            return Err(invalid(
                "matching new application is still live; rollback deferred",
            ));
        }
        if process_identity(pid).is_some() || pid_alive(pid) {
            return Err(invalid(
                "new PID was reused or could not be identified; rollback refused",
            ));
        }
    }
    journal.phase = Phase::Rollback;
    journal_write(data, journal)?;
    if layout.target.exists() && layout.backup.exists() {
        let failed = layout
            .parent
            .join(format!(".SCM-Workbench-failed-{}", journal.token));
        if failed.exists() {
            return Err(invalid("failed target copy already exists"));
        }
        validate_tree(&layout.target)?;
        rename_noreplace(&layout.target, &failed)?;
        remove_authorized_tree(
            &failed,
            &layout.parent,
            failed.file_name().unwrap().to_str().unwrap(),
        )?;
    }
    if !layout.target.exists() && layout.backup.exists() {
        validate_tree(&layout.backup)?;
        rename_noreplace(&layout.backup, &layout.target)?;
    }
    if layout.target.exists() && layout.candidate.exists() {
        remove_authorized_tree(&layout.candidate, &layout.parent, &journal.candidate_name)?;
    }
    finish_failure(
        data,
        journal,
        layout,
        "new application did not become healthy",
    )
}

fn cleanup_failed(data: &Path, journal: &Journal, layout: &Layout) -> io::Result<()> {
    if let (Some(pid), Some(identity)) = (journal.new_pid, journal.new_identity.as_ref()) {
        if identity_matches(pid, identity) {
            return Err(invalid(
                "matching new application is still live; cleanup deferred",
            ));
        }
        if process_identity(pid).is_some() || pid_alive(pid) {
            return Err(invalid(
                "failed-update PID was reused or could not be identified",
            ));
        }
    }
    if !layout.target.exists() && layout.backup.exists() {
        validate_tree(&layout.backup)?;
        rename_noreplace(&layout.backup, &layout.target)?;
    }
    if layout.target.exists() && layout.candidate.exists() {
        remove_authorized_tree(&layout.candidate, &layout.parent, &journal.candidate_name)?;
    }
    if layout.target.exists() && !layout.backup.exists() {
        fs::remove_file(data.join(JOURNAL))?;
    }
    Ok(())
}

fn write_failure(data: &Path, journal: &Journal, message: &str) -> io::Result<()> {
    let result = ResultRecord {
        version: SCHEMA_VERSION,
        token: &journal.token,
        success: false,
        expected_version: &journal.expected_version,
        message,
    };
    let bytes = serde_json::to_vec(&result).map_err(|_| invalid("cannot encode update result"))?;
    if bytes.len() as u64 > JOURNAL_LIMIT {
        return Err(invalid("update result exceeds 64 KiB"));
    }
    atomic_write(&data.join(RESULT), &bytes)
}

fn target_is_launchable(target: &Path) -> bool {
    let Ok(meta) = fs::symlink_metadata(target) else {
        return false;
    };
    if !meta.file_type().is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if meta.permissions().mode() & 0o111 == 0 {
            return false;
        }
    }
    true
}

#[cfg(test)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct RelaunchInvocation {
    pub executable: PathBuf,
    pub data: PathBuf,
    pub removed_env: Vec<&'static str>,
}

#[cfg(test)]
static RELAUNCH_INVOCATIONS: OnceLock<Mutex<Vec<RelaunchInvocation>>> = OnceLock::new();

#[cfg(test)]
#[allow(dead_code)]
pub(crate) fn take_relaunch_invocations() -> Vec<RelaunchInvocation> {
    RELAUNCH_INVOCATIONS
        .get_or_init(|| Mutex::new(Vec::new()))
        .lock()
        .unwrap()
        .drain(..)
        .collect()
}

#[cfg(test)]
fn spawn_old_target(executable: &Path, data: &Path) -> io::Result<()> {
    RELAUNCH_INVOCATIONS
        .get_or_init(|| Mutex::new(Vec::new()))
        .lock()
        .unwrap()
        .push(RelaunchInvocation {
            executable: executable.to_path_buf(),
            data: data.to_path_buf(),
            removed_env: vec!["SCM_WORKBENCH_UPDATE_TOKEN", "SCM_WORKBENCH_UPDATE_NONCE"],
        });
    Ok(())
}

#[cfg(not(test))]
fn spawn_old_target(executable: &Path, data: &Path) -> io::Result<()> {
    let mut command = Command::new(executable);
    command
        .env_remove("SCM_WORKBENCH_UPDATE_TOKEN")
        .env_remove("SCM_WORKBENCH_UPDATE_NONCE")
        .env("SCM_WORKBENCH_DATA", data)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW);
    }
    command.spawn().map(|_| ())
}

/// Record the failure before relaunching.  The journal is removed before the
/// child is spawned: if the old shell starts quickly it must not select this
/// recovery transaction again.  A fixture or damaged install with no
/// launchable executable still gets the durable failure result, but is never
/// reported as success.
fn finish_failure(
    data: &Path,
    journal: &mut Journal,
    layout: &Layout,
    message: &str,
) -> io::Result<()> {
    journal.phase = Phase::Failed;
    journal_write(data, journal)?;
    write_failure(data, journal, message)?;
    let executable = executable_for_target(&layout.target);
    let launchable = target_is_launchable(&executable);
    // Remove the durable marker before starting the old shell, so the
    // relaunch cannot immediately select the same recovery transaction. This
    // ordering is required even for a test seam or a damaged install where
    // the old executable is not launchable.
    let journal_path = data.join(JOURNAL);
    match fs::symlink_metadata(&journal_path) {
        Ok(meta) if meta.file_type().is_file() => {
            fs::remove_file(journal_path)?;
            sync_dir(data)?;
        }
        Ok(_) => return Err(invalid("update journal is not a regular file")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    // Unit tests use the seam even for the hermetic directory-shaped Linux
    // fixture; production only relaunches an actual executable.
    if launchable || cfg!(test) {
        // The transaction is already durably failed.  A spawn failure must
        // not turn that record into a false success or revive the journal.
        let _ = spawn_old_target(&executable, data);
    }
    Ok(())
}

fn remove_authorized_tree(path: &Path, parent: &Path, expected_name: &str) -> io::Result<()> {
    if path.parent() != Some(parent)
        || path.file_name().and_then(OsStr::to_str) != Some(expected_name)
    {
        return Err(invalid("unauthorized update cleanup path"));
    }
    validate_tree(path)?;
    remove_tree(path)?;
    sync_dir(parent)
}

fn remove_tree(path: &Path) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    // Unlink the link itself; never recurse through it. Internal relative
    // links are valid in signed macOS bundles and must be removed this way.
    if meta.file_type().is_symlink() {
        return fs::remove_file(path).or_else(|_| fs::remove_dir(path));
    }
    if meta.is_dir() {
        for entry in fs::read_dir(path)? {
            remove_tree(&entry?.path())?;
        }
        fs::remove_dir(path)
    } else {
        fs::remove_file(path)
    }
}

fn wait_for_identities(
    identities: &[(u32, &ProcessIdentity)],
    timeout: Duration,
) -> io::Result<()> {
    let end = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < end {
        if identities
            .iter()
            .all(|(pid, identity)| !identity_matches(*pid, identity))
        {
            return Ok(());
        }
        thread::sleep(POLL);
    }
    Err(invalid("old process did not exit before handoff deadline"))
}

fn pid_alive(pid: u32) -> bool {
    #[cfg(unix)]
    unsafe {
        return libc::kill(pid as libc::pid_t, 0) == 0
            || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return false;
        }
        unsafe {
            CloseHandle(handle);
        }
        true
    }
    #[cfg(not(any(unix, windows)))]
    {
        false
    }
}

fn terminate_matching(pid: u32, identity: &ProcessIdentity) {
    if !identity_matches(pid, identity) {
        return;
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGTERM);
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, PROCESS_TERMINATE,
        };
        let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if !handle.is_null() {
            let _ = TerminateProcess(handle, 1);
            CloseHandle(handle);
        }
    }
}

fn process_identity(pid: u32) -> Option<ProcessIdentity> {
    let image = process_image(pid)?;
    let start_token = process_start_token(pid)?;
    Some(ProcessIdentity { image, start_token })
}

fn identity_matches(pid: u32, expected: &ProcessIdentity) -> bool {
    process_identity(pid)
        .map(|actual| actual == *expected)
        .unwrap_or(false)
}

#[cfg(unix)]
fn process_image(pid: u32) -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        let mut buf = vec![0u8; 4096];
        let n = unsafe { proc_pidpath(pid as i32, buf.as_mut_ptr() as *mut _, buf.len() as u32) };
        if n <= 0 {
            return None;
        }
        buf.truncate(n as usize);
        fs::canonicalize(<OsStr as std::os::unix::ffi::OsStrExt>::from_bytes(&buf))
            .ok()?
            .to_str()
            .map(str::to_owned)
    }
    #[cfg(not(target_os = "macos"))]
    {
        fs::canonicalize(format!("/proc/{pid}/exe"))
            .ok()?
            .to_str()
            .map(str::to_owned)
    }
}

#[cfg(windows)]
fn process_image(pid: u32) -> Option<String> {
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return None;
    }
    let mut buf = vec![0u16; 32768];
    let mut len = buf.len() as u32;
    let ok = unsafe { QueryFullProcessImageNameW(handle, 0, buf.as_mut_ptr(), &mut len) } != 0;
    unsafe {
        CloseHandle(handle);
    }
    if !ok {
        return None;
    }
    buf.truncate(len as usize);
    fs::canonicalize(std::ffi::OsString::from_wide(&buf))
        .ok()?
        .to_str()
        .map(str::to_owned)
}

#[cfg(unix)]
fn process_start_token(pid: u32) -> Option<u128> {
    #[cfg(target_os = "macos")]
    {
        // proc_bsdinfo's start time is the stable PID-generation token.
        // Keep the ABI prefix typed: on 64-bit Darwin the timeval fields are
        // uint64_t and begin at SDK offsets 120 and 128.
        let mut storage = [0u64; 64];
        let n = unsafe {
            proc_pidinfo(
                pid as i32,
                3, // PROC_PIDTBSDINFO
                0,
                storage.as_mut_ptr() as *mut _,
                std::mem::size_of_val(&storage) as i32,
            )
        };
        if n < std::mem::size_of::<ProcBsdInfoPrefix>() as i32 {
            return None;
        }
        let info = unsafe { &*(storage.as_ptr() as *const ProcBsdInfoPrefix) };
        Some(((info.start_tvsec as u128) << 64) | info.start_tvusec as u128)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        let close = text.rfind(')')?;
        text[close + 2..].split_whitespace().nth(19)?.parse().ok()
    }
}

#[cfg(windows)]
fn process_start_token(pid: u32) -> Option<u128> {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return None;
    }
    let mut created = FILETIME::default();
    let mut exit = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    let ok =
        unsafe { GetProcessTimes(handle, &mut created, &mut exit, &mut kernel, &mut user) } != 0;
    unsafe {
        CloseHandle(handle);
    }
    if !ok {
        return None;
    }
    Some(((created.dwHighDateTime as u128) << 32) | created.dwLowDateTime as u128)
}

#[cfg(target_os = "macos")]
#[repr(C)]
struct ProcBsdInfoPrefix {
    pbi_flags: u32,
    pbi_status: u32,
    pbi_xstatus: u32,
    pbi_pid: u32,
    pbi_ppid: u32,
    pbi_uid: u32,
    pbi_gid: u32,
    pbi_ruid: u32,
    pbi_rgid: u32,
    pbi_svuid: u32,
    pbi_svgid: u32,
    rfu_1: u32,
    pbi_comm: [u8; 16],
    pbi_name: [u8; 32],
    pbi_nfiles: u32,
    pbi_pgid: u32,
    pbi_pjobc: u32,
    e_tdev: u32,
    e_tpgid: u32,
    pbi_nice: i32,
    start_tvsec: u64,
    start_tvusec: u64,
}

#[cfg(target_os = "macos")]
const _: () = assert!(std::mem::size_of::<ProcBsdInfoPrefix>() >= 136);

#[cfg(target_os = "macos")]
extern "C" {
    fn renamex_np(from: *const std::ffi::c_char, to: *const std::ffi::c_char, flags: u32) -> i32;
    fn proc_pidpath(pid: i32, buffer: *mut std::ffi::c_void, buffersize: u32) -> i32;
    fn proc_pidinfo(
        pid: i32,
        flavor: i32,
        arg: u64,
        buffer: *mut std::ffi::c_void,
        buffersize: i32,
    ) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::process::Child;
    use std::time::Duration;

    fn token() -> String {
        "a".repeat(64)
    }

    #[test]
    fn cli_is_strict_and_rejects_extra_paths() {
        let data_dir = std::env::temp_dir().join("scm-update-cli-data");
        let good = vec![
            "app".into(),
            "--update-helper".into(),
            "--data-dir".into(),
            data_dir.to_string_lossy().into_owned(),
            "--token".into(),
            token(),
            "--mode".into(),
            "handoff".into(),
        ];
        // The documented spelling has seven arguments after the executable.
        assert!(parse_cli(&good).is_some());
        let mut bad = good.clone();
        bad.push("extra".into());
        assert!(parse_cli(&bad).unwrap().is_err());
        let mut recover = good;
        recover[7] = "recover".into();
        recover.extend(["--wait-pid".into(), "42".into()]);
        assert_eq!(
            parse_cli(&recover).unwrap().unwrap().recovery_wait_pid,
            Some(42)
        );
        recover[9] = "0".into();
        assert!(parse_cli(&recover).unwrap().is_err());
    }

    #[test]
    fn token_is_exact_lower_hex() {
        assert!(valid_token(&token()));
        assert!(!valid_token(&"A".repeat(64)));
        assert!(!valid_token(&"a".repeat(63)));
        assert!(!valid_token(&format!("{}z", "a".repeat(63))));
    }

    #[test]
    fn launch_request_requires_exact_bounded_schema() {
        let dir = std::env::temp_dir().join(format!(
            "scm-request-{}",
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        let good = format!(r#"{{"version":1,"token":"{}"}}"#, token());
        fs::write(dir.join(REQUEST), good).unwrap();
        assert_eq!(read_launch_request(&dir).unwrap().unwrap().token, token());
        fs::write(
            dir.join(REQUEST),
            format!(r#"{{"version":1,"token":"{}","extra":true}}"#, token()),
        )
        .unwrap();
        assert!(read_launch_request(&dir).is_err());
        fs::write(dir.join(REQUEST), b"not-json").unwrap();
        assert!(read_launch_request(&dir).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn launch_request_symlink_is_not_actionable() {
        use std::os::unix::fs::symlink;
        let dir = std::env::temp_dir().join(format!(
            "scm-request-link-{}",
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let target = dir.join("request");
        let link = dir.join(REQUEST);
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        fs::write(&target, format!(r#"{{"version":1,"token":"{}"}}"#, token())).unwrap();
        symlink(&target, &link).unwrap();
        assert!(read_launch_request(&dir).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn helper_copy_is_atomic_verified_and_preserves_mode() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!(
            "scm-helper-copy-{}",
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        let dir = fs::canonicalize(&dir).unwrap();
        let source = dir.join("source");
        fs::write(&source, b"helper bytes").unwrap();
        fs::set_permissions(&source, fs::Permissions::from_mode(0o751)).unwrap();
        let copied = materialize_helper(&source, &dir, &token()).unwrap();
        assert_eq!(fs::read(&copied).unwrap(), b"helper bytes");
        assert_eq!(
            fs::metadata(&copied).unwrap().permissions().mode() & 0o777,
            0o751
        );
        assert_eq!(materialize_helper(&source, &dir, &token()).unwrap(), copied);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn journal_rejects_unknown_fields_and_wrong_version() {
        let raw = format!(
            r#"{{"version":1,"token":"{}","phase":"prepared","expected_version":"1","target":"/tmp/SCM Workbench","candidate_name":".SCM-Workbench-candidate-{}","backup_name":".SCM-Workbench-backup-{}","old_shell_pid":1,"old_worker_pid":2,"unknown":true}}"#,
            token(),
            token(),
            token()
        );
        assert!(serde_json::from_str::<Journal>(&raw).is_err());
    }

    #[test]
    fn atomic_write_is_bounded_and_replaces_only_after_fsync() {
        let dir = std::env::temp_dir().join(format!("scm-update-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        let path = dir.join("state");
        atomic_write(&path, b"one").unwrap();
        atomic_write(&path, b"two").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"two");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn health_timestamp_window_is_bounded() {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        assert!(fresh_timestamp(now, now));
        assert!(!fresh_timestamp(
            now.saturating_sub(HEALTH_WAIT.as_millis() as u64 + 1),
            now
        ));
    }

    #[test]
    fn sibling_rename_is_atomic_for_absent_and_present_destinations() {
        let dir = std::env::temp_dir().join(format!("scm-rename-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        let a = dir.join("a");
        let b = dir.join("b");
        fs::write(&a, b"a").unwrap();
        rename_noreplace(&a, &b).unwrap();
        assert_eq!(fs::read(&b).unwrap(), b"a");
        fs::write(&a, b"new").unwrap();
        assert!(rename_noreplace(&a, &b).is_err());
        assert_eq!(fs::read(&b).unwrap(), b"a");
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn identity_image_is_canonical_for_this_process() {
        let id = process_identity(std::process::id()).unwrap();
        assert_eq!(
            PathBuf::from(&id.image),
            fs::canonicalize(std::env::current_exe().unwrap()).unwrap()
        );
        assert!(id.start_token != 0);
        assert_eq!(Some(id.clone()), process_identity(std::process::id()));
    }

    #[cfg(unix)]
    #[test]
    fn internal_relative_links_are_valid_but_escape_and_cycles_are_not() {
        use std::os::unix::fs::symlink;
        let dir = std::env::temp_dir().join(format!("scm-links-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("real/sub")).unwrap();
        fs::write(dir.join("real/sub/file"), b"ok").unwrap();
        symlink("real", dir.join("inside")).unwrap();
        assert!(validate_tree(&dir).is_ok());
        symlink("/etc", dir.join("outside")).unwrap();
        assert!(validate_tree(&dir).is_err());
        fs::remove_file(dir.join("outside")).unwrap();
        symlink(".", dir.join("cycle")).unwrap();
        assert!(validate_tree(&dir).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn distinct_processes_have_distinct_start_tokens() {
        let mut child = Command::new("sleep").arg("2").spawn().unwrap();
        let child_id = process_identity(child.id()).unwrap();
        let self_id = process_identity(std::process::id()).unwrap();
        assert_ne!(child_id.start_token, self_id.start_token);
        let _ = child.kill();
        let _ = child.wait();
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_data_directory_is_rejected() {
        use std::os::unix::fs::symlink;
        let dir = std::env::temp_dir().join(format!("scm-data-{}", std::process::id()));
        let link = std::env::temp_dir().join(format!("scm-data-link-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::remove_file(&link);
        fs::create_dir(&dir).unwrap();
        symlink(&dir, &link).unwrap();
        assert!(validate_data_dir(&link).is_err());
        fs::remove_file(&link).unwrap();
        fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn old_identity_validation_is_component_safe_and_pure() {
        let target = PathBuf::from("/Applications/SCM Workbench.app");
        let worker_root = if cfg!(target_os = "macos") {
            target.join("Contents/runtime")
        } else {
            target.join("runtime")
        };
        let shell = ProcessIdentity {
            image: executable_for_target(&target)
                .to_string_lossy()
                .into_owned(),
            start_token: 1,
        };
        let worker = ProcessIdentity {
            image: worker_root.join("python").to_string_lossy().into_owned(),
            start_token: 2,
        };
        assert!(old_identities_ok(&shell, &worker, &target));
        let sibling = ProcessIdentity {
            image: if cfg!(target_os = "macos") {
                "/Applications/SCM Workbench.app-evil/Contents/runtime/python".into()
            } else {
                "/Applications/SCM Workbench-evil/runtime/python".into()
            },
            start_token: 2,
        };
        assert!(!old_identities_ok(&shell, &sibling, &target));
        let escape = ProcessIdentity {
            image: "/Applications/runtime/python".into(),
            start_token: 2,
        };
        assert!(!old_identities_ok(&shell, &escape, &target));
        let wrong_shell = ProcessIdentity {
            image: "/Applications/Other.app/Contents/MacOS/SCM Workbench".into(),
            start_token: 1,
        };
        assert!(!old_identities_ok(&wrong_shell, &worker, &target));
    }

    #[test]
    fn health_acceptance_predicate_rejects_each_independent_field_mismatch() {
        let fixture = Fixture::new();
        let journal = fixture.journal(Phase::Launched);
        let layout = fixture.layout(&journal);
        let nonce = "b".repeat(64);
        let now = 1_000_000u64;
        let valid = Health {
            version: journal.expected_version.clone(),
            token: journal.token.clone(),
            nonce: nonce.clone(),
            canonical_target: layout.target.to_string_lossy().into_owned(),
            new_shell_pid: 42,
            timestamp: now,
        };
        assert!(health_fields_accept(
            &valid, &journal, &layout, &nonce, 42, now
        ));
        let mut cases = Vec::new();
        let mut value = valid.clone();
        value.version = "wrong".into();
        cases.push(value);
        let mut value = valid.clone();
        value.token = "c".repeat(64);
        cases.push(value);
        let mut value = valid.clone();
        value.nonce = "d".repeat(64);
        cases.push(value);
        let mut value = valid.clone();
        value.canonical_target.push_str("-elsewhere");
        cases.push(value);
        let mut value = valid.clone();
        value.new_shell_pid = 43;
        cases.push(value);
        let mut value = valid.clone();
        value.timestamp = now - HEALTH_WAIT.as_millis() as u64 - 1;
        cases.push(value);
        let mut value = valid.clone();
        value.timestamp = now + 5_001;
        cases.push(value);
        for invalid_health in cases {
            assert!(!health_fields_accept(
                &invalid_health,
                &journal,
                &layout,
                &nonce,
                42,
                now
            ));
        }
    }

    #[test]
    fn publish_health_requires_exact_pending_identity_and_writes_schema() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        let mut journal = fixture.journal(Phase::Launched);
        fixture.write_journal(&journal);
        let current = std::env::current_exe().unwrap();
        let token = token();
        let nonce = "b".repeat(64);
        assert!(publish_health_for_target(
            &fixture.data,
            &current,
            &token,
            &nonce,
            &fixture.target
        )
        .unwrap());
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(fixture.data.join(HEALTH)).unwrap()).unwrap();
        assert_eq!(value.as_object().unwrap().len(), 6);
        assert_eq!(value["version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(value["token"], token);
        assert_eq!(value["nonce"], nonce);
        assert_eq!(
            value["canonical_target"],
            fixture.target.to_string_lossy().as_ref()
        );
        assert_eq!(value["new_shell_pid"], std::process::id());

        for (bad_token, bad_nonce, bad_phase, call_target) in [
            (
                "c".repeat(64),
                nonce.clone(),
                Phase::Launched,
                fixture.target.clone(),
            ),
            (
                token.clone(),
                "d".repeat(64),
                Phase::CandidatePublished,
                fixture.target.clone(),
            ),
            (
                token.clone(),
                nonce.clone(),
                Phase::Launched,
                fixture.backup.clone(),
            ),
        ] {
            let _ = fs::remove_file(fixture.data.join(HEALTH));
            journal.phase = bad_phase;
            journal.token = token.clone();
            journal.target = fixture.target.to_string_lossy().into_owned();
            fixture.write_journal(&journal);
            assert!(!publish_health_for_target(
                &fixture.data,
                &current,
                &bad_token,
                &bad_nonce,
                &call_target
            )
            .unwrap());
            assert!(!fixture.data.join(HEALTH).exists());
        }
    }

    #[test]
    fn launch_nonce_is_random_ephemeral_lower_hex() {
        let first = launch_nonce().unwrap();
        let second = launch_nonce().unwrap();
        assert_eq!(first.len(), 64);
        assert!(valid_token(&first));
        assert_ne!(first, second);
    }

    #[test]
    fn health_schema_is_exact_and_spoof_fields_are_rejected() {
        let raw = r#"{"version":"2.0","token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","canonical_target":"/x","new_shell_pid":7,"timestamp":1,"spoof":true}"#;
        assert!(serde_json::from_str::<Health>(raw).is_err());
        let valid = r#"{"version":"2.0","token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","canonical_target":"/x","new_shell_pid":7,"timestamp":1}"#;
        assert!(serde_json::from_str::<Health>(valid).is_ok());
    }

    #[test]
    fn phase_serialization_is_explicit() {
        assert_eq!(
            serde_json::to_string(&Phase::CandidatePublished).unwrap(),
            "\"candidate-published\""
        );
        assert!(serde_json::from_str::<Phase>("\"candidate_publish\"").is_err());
    }

    #[cfg(unix)]
    struct ChildGuard(Child);

    #[cfg(unix)]
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    struct Fixture {
        root: PathBuf,
        data: PathBuf,
        target: PathBuf,
        candidate: PathBuf,
        backup: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = fs::canonicalize(std::env::temp_dir())
                .unwrap()
                .join(format!(
                    "scm-update-state-machine-{}-{}",
                    std::process::id(),
                    TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
                ));
            let data = root.join("data");
            let parent = root.join("install");
            let target = parent.join(application_name());
            let candidate = parent.join(format!(".SCM-Workbench-candidate-{}", token()));
            let backup = parent.join(format!(".SCM-Workbench-backup-{}", token()));
            fs::create_dir_all(&data).unwrap();
            fs::create_dir_all(&parent).unwrap();
            Self {
                root,
                data,
                target,
                candidate,
                backup,
            }
        }

        fn journal(&self, phase: Phase) -> Journal {
            Journal {
                version: SCHEMA_VERSION,
                token: token(),
                phase,
                expected_version: "test-version".into(),
                target: self.target.to_string_lossy().into_owned(),
                candidate_name: self.candidate.file_name().unwrap().to_string_lossy().into(),
                backup_name: self.backup.file_name().unwrap().to_string_lossy().into(),
                old_shell_pid: 1,
                old_worker_pid: 2,
                old_shell_identity: None,
                old_worker_identity: None,
                new_pid: None,
                new_identity: None,
            }
        }

        fn layout(&self, journal: &Journal) -> Layout {
            validate_layout(journal).unwrap()
        }

        fn write_journal(&self, journal: &Journal) {
            journal_write(&self.data, journal).unwrap();
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[cfg(target_os = "macos")]
    fn application_name() -> &'static str {
        "SCM Workbench.app"
    }

    #[cfg(windows)]
    fn application_name() -> &'static str {
        "SCM Workbench"
    }

    #[cfg(all(not(target_os = "macos"), not(windows)))]
    fn application_name() -> &'static str {
        "scm-workbench"
    }

    fn make_application(path: &Path, marker: &str) {
        #[cfg(target_os = "macos")]
        {
            use std::os::unix::fs::symlink;
            fs::create_dir_all(path.join("Contents/MacOS")).unwrap();
            fs::create_dir_all(path.join("Contents/app/ui")).unwrap();
            fs::create_dir_all(path.join("Contents/runtime")).unwrap();
            fs::write(path.join("Contents/app/scm_workbench"), marker).unwrap();
            fs::write(path.join("Contents/runtime/launcher"), b"launcher").unwrap();
            // Keep a safe in-bundle link in the fixture while the executable
            // itself remains a regular file, matching validate_application_layout.
            symlink("launcher", path.join("Contents/runtime/link")).unwrap();
            fs::write(path.join("Contents/MacOS/SCM Workbench"), b"executable").unwrap();
        }
        #[cfg(windows)]
        {
            fs::create_dir_all(path.join("app/ui")).unwrap();
            fs::create_dir_all(path.join("runtime")).unwrap();
            fs::write(path.join("app/scm_workbench"), marker).unwrap();
            fs::write(path.join("SCM Workbench.exe"), b"executable").unwrap();
        }
        #[cfg(all(not(target_os = "macos"), not(windows)))]
        {
            fs::create_dir_all(path).unwrap();
            fs::write(path.join("marker"), marker).unwrap();
        }
    }

    fn marker(path: &Path) -> PathBuf {
        #[cfg(target_os = "macos")]
        {
            path.join("Contents/app/scm_workbench")
        }
        #[cfg(windows)]
        {
            path.join("app/scm_workbench")
        }
        #[cfg(all(not(target_os = "macos"), not(windows)))]
        {
            path.join("marker")
        }
    }

    fn assert_failure_result(fixture: &Fixture) {
        let result: serde_json::Value =
            serde_json::from_slice(&fs::read(fixture.data.join(RESULT)).unwrap()).unwrap();
        assert_eq!(result["success"], false);
    }

    fn assert_success_result(fixture: &Fixture) {
        let result: serde_json::Value =
            serde_json::from_slice(&fs::read(fixture.data.join(RESULT)).unwrap()).unwrap();
        assert_eq!(result["success"], true);
    }

    fn assert_restored_backup(fixture: &Fixture) {
        assert_eq!(fs::read(marker(&fixture.target)).unwrap(), b"old");
        assert!(!fixture.backup.exists());
        assert!(!fixture.candidate.exists());
    }

    #[test]
    fn validate_data_and_layout_accept_hermetic_application_trees() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let journal = fixture.journal(Phase::Prepared);
        assert_eq!(validate_data_dir(&fixture.data).unwrap(), fixture.data);
        let layout = fixture.layout(&journal);
        assert_eq!(layout.target, fixture.target);
        assert_eq!(layout.candidate, fixture.candidate);
        assert_eq!(layout.backup, fixture.backup);
    }

    #[cfg(windows)]
    #[test]
    fn ordinary_windows_paths_match_their_verbatim_canonical_forms() {
        let data = std::env::temp_dir().join(format!(
            "scm-update-ordinary-path-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&data).unwrap();
        let expected = fs::canonicalize(&data).unwrap();
        assert_ne!(
            data, expected,
            "the fixture must exercise the verbatim prefix"
        );
        assert_eq!(validate_data_dir(&data).unwrap(), expected);
        fs::remove_dir_all(data).unwrap();

        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let mut journal = fixture.journal(Phase::Prepared);
        journal.target = windows_without_verbatim_prefix(&fixture.target)
            .to_string_lossy()
            .into_owned();
        let layout = validate_layout(&journal).unwrap();
        assert_eq!(layout.target, fixture.target);
        assert!(same_path_spelling(
            Path::new(&journal.target),
            &fixture.target
        ));
    }

    #[test]
    fn publish_candidate_transitions_identified_through_both_renames() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let mut journal = fixture.journal(Phase::Identified);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        publish_candidate(&fixture.data, &mut journal, &layout).unwrap();

        assert_eq!(journal.phase, Phase::CandidatePublished);
        assert_eq!(fs::read(marker(&fixture.target)).unwrap(), b"new");
        assert_eq!(fs::read(marker(&fixture.backup)).unwrap(), b"old");
        assert!(!fixture.candidate.exists());
        assert_eq!(
            read_journal(&fixture.data.join(JOURNAL)).unwrap().phase,
            Phase::CandidatePublished
        );
    }

    #[test]
    fn publish_candidate_from_backup_renamed_only_publishes_candidate() {
        let fixture = Fixture::new();
        make_application(&fixture.candidate, "new");
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::BackupRenamed);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        publish_candidate(&fixture.data, &mut journal, &layout).unwrap();

        assert_eq!(journal.phase, Phase::CandidatePublished);
        assert_eq!(fs::read(marker(&fixture.target)).unwrap(), b"new");
        assert_eq!(fs::read(marker(&fixture.backup)).unwrap(), b"old");
        assert!(!fixture.candidate.exists());
    }

    #[test]
    fn rollback_prepared_removes_only_candidate_and_records_failure() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let mut journal = fixture.journal(Phase::Prepared);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        rollback_prepared(&fixture.data, &mut journal, &layout).unwrap();

        assert!(fixture.target.exists());
        assert!(!fixture.candidate.exists());
        assert!(!fixture.backup.exists());
        assert_eq!(journal.phase, Phase::Failed);
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_prepared_dispatches_to_prepared_rollback() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let mut journal = fixture.journal(Phase::Prepared);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert!(!fixture.candidate.exists());
        assert!(!fixture.data.join(JOURNAL).exists());
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_identified_refuses_ambiguous_target_and_backup() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.backup, "backup");
        let mut journal = fixture.journal(Phase::Identified);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        assert!(recover(&fixture.data, &mut journal, &layout).is_err());
        assert!(fixture.target.exists());
        assert!(fixture.backup.exists());
        assert!(!fixture.data.join(RESULT).exists());
    }

    #[test]
    fn recover_backup_renamed_restores_previous_application_without_launching() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "new");
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::BackupRenamed);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert_restored_backup(&fixture);
        assert_eq!(journal.phase, Phase::Failed);
        assert_failure_result(&fixture);
        let ours: Vec<_> = take_relaunch_invocations()
            .into_iter()
            .filter(|invocation| invocation.data == fixture.data)
            .collect();
        assert_eq!(ours.len(), 1);
        assert!(!fixture.data.join(JOURNAL).exists());
        assert_eq!(
            ours[0].removed_env,
            vec!["SCM_WORKBENCH_UPDATE_TOKEN", "SCM_WORKBENCH_UPDATE_NONCE"]
        );
    }

    #[test]
    fn recover_backup_renamed_with_missing_target_restores_backup() {
        let fixture = Fixture::new();
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::BackupRenamed);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert_restored_backup(&fixture);
        assert_eq!(journal.phase, Phase::Failed);
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_candidate_published_rolls_back_unhealthy_application() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "new");
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::CandidatePublished);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert_restored_backup(&fixture);
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_published_state_with_missing_target_restores_backup_and_cleans_candidate() {
        let fixture = Fixture::new();
        make_application(&fixture.candidate, "new");
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::CandidatePublished);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert_restored_backup(&fixture);
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_launching_and_launched_with_dead_process_restore_backup() {
        for phase in [Phase::Launching, Phase::Launched] {
            let fixture = Fixture::new();
            make_application(&fixture.target, "new");
            make_application(&fixture.backup, "old");
            let mut journal = fixture.journal(phase);
            journal.new_pid = Some(dead_pid());
            journal.new_identity = Some(ProcessIdentity {
                image: executable_for_target(&fixture.target)
                    .to_string_lossy()
                    .into_owned(),
                start_token: 1,
            });
            fixture.write_journal(&journal);
            let layout = fixture.layout(&journal);

            recover(&fixture.data, &mut journal, &layout).unwrap();

            assert_restored_backup(&fixture);
            assert_failure_result(&fixture);
        }
    }

    #[cfg(unix)]
    #[test]
    fn recover_new_process_terminates_only_matching_child_and_restores_backup() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "new");
        make_application(&fixture.backup, "old");
        let executable = executable_for_target(&fixture.target);
        let source = if Path::new("/bin/sleep").exists() {
            Path::new("/bin/sleep")
        } else {
            Path::new("/usr/bin/sleep")
        };
        fs::copy(source, &executable).unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(&executable).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&executable, permissions).unwrap();
        let child = Command::new(&executable).arg("10").spawn().unwrap();
        let mut child = ChildGuard(child);
        let identity = process_identity(child.0.id()).unwrap();
        let mut journal = fixture.journal(Phase::Launched);
        journal.new_pid = Some(child.0.id());
        journal.new_identity = Some(identity);
        let layout = fixture.layout(&journal);

        recover_new_process(&fixture.data, &mut journal, &layout).unwrap();

        assert!(child.0.try_wait().unwrap().is_some());
        assert_restored_backup(&fixture);
        assert_eq!(journal.phase, Phase::Failed);
        assert_failure_result(&fixture);
    }

    #[cfg(unix)]
    #[test]
    fn recover_new_process_refuses_identity_image_mismatch_without_terminating() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "new");
        make_application(&fixture.backup, "old");
        let child = Command::new("sleep").arg("10").spawn().unwrap();
        let mut child = ChildGuard(child);
        let actual = process_identity(child.0.id()).unwrap();
        let mut journal = fixture.journal(Phase::Launched);
        journal.new_pid = Some(child.0.id());
        journal.new_identity = Some(actual);
        let layout = fixture.layout(&journal);

        assert!(recover_new_process(&fixture.data, &mut journal, &layout).is_err());
        assert!(child.0.try_wait().unwrap().is_none());
        assert!(fixture.target.exists());
        assert!(fixture.backup.exists());
    }

    #[test]
    fn recover_healthy_and_completed_finalize_and_remove_backup() {
        for phase in [Phase::Healthy, Phase::Completed] {
            let fixture = Fixture::new();
            make_application(&fixture.target, "new");
            make_application(&fixture.backup, "old");
            let mut journal = fixture.journal(phase);
            fixture.write_journal(&journal);
            let layout = fixture.layout(&journal);

            recover(&fixture.data, &mut journal, &layout).unwrap();

            assert!(fixture.target.exists());
            assert!(!fixture.backup.exists());
            assert!(!fixture.data.join(JOURNAL).exists());
            assert_success_result(&fixture);
        }
    }

    #[test]
    fn recover_rollback_restores_backup() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "new");
        make_application(&fixture.backup, "old");
        let mut journal = fixture.journal(Phase::Rollback);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert_restored_backup(&fixture);
        assert_failure_result(&fixture);
    }

    #[test]
    fn recover_failed_cleans_candidate_and_journal() {
        let fixture = Fixture::new();
        make_application(&fixture.target, "old");
        make_application(&fixture.candidate, "new");
        let mut journal = fixture.journal(Phase::Failed);
        fixture.write_journal(&journal);
        let layout = fixture.layout(&journal);

        recover(&fixture.data, &mut journal, &layout).unwrap();

        assert!(fixture.target.exists());
        assert!(!fixture.candidate.exists());
        assert!(!fixture.data.join(JOURNAL).exists());
    }

    fn dead_pid() -> u32 {
        #[cfg(unix)]
        let mut child = Command::new("sleep").arg("1").spawn().unwrap();
        #[cfg(windows)]
        let mut child = Command::new("cmd")
            .args(["/C", "exit", "0"])
            .spawn()
            .unwrap();
        #[cfg(not(any(unix, windows)))]
        return 1;
        let pid = child.id();
        let _ = child.kill();
        let _ = child.wait();
        pid
    }

    #[allow(dead_code)]
    fn _duration_is_used(_: Duration) {}
}
