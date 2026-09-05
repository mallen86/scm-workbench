//! The small JSON-lines bridge between the Tauri shell and its one worker.
//!
//! The worker remains the owner of application behavior.  This module only
//! frames requests, serializes calls, and keeps stdout drained so a worker
//! cannot deadlock while writing a response.

use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Write};
use std::process::{ChildStdin, ChildStdout};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

const MAX_REQUEST_BYTES: usize = 1 * 1024 * 1024;
/// Keep this equal to the Python worker's response limit.
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug)]
struct Inner {
    stdin: Mutex<Option<ChildStdin>>,
    stdout: Mutex<Option<ChildStdout>>,
    pending: Mutex<HashMap<String, mpsc::Sender<Result<Value, String>>>>,
    call_lock: Mutex<()>,
    next_id: AtomicU64,
    available: AtomicBool,
}

/// Managed state for the single supervised Python worker.
#[derive(Clone, Debug)]
pub struct WorkerRpc {
    inner: Arc<Inner>,
    timeout: Duration,
}

impl Default for WorkerRpc {
    fn default() -> Self {
        Self::new()
    }
}

impl WorkerRpc {
    pub fn new() -> Self {
        Self::with_timeout(DEFAULT_TIMEOUT)
    }

    fn with_timeout(timeout: Duration) -> Self {
        Self {
            inner: Arc::new(Inner {
                stdin: Mutex::new(None),
                stdout: Mutex::new(None),
                pending: Mutex::new(HashMap::new()),
                call_lock: Mutex::new(()),
                next_id: AtomicU64::new(1),
                available: AtomicBool::new(false),
            }),
            timeout,
        }
    }

    /// Install the pipes taken from the already-spawned worker.  The reader
    /// owns stdout after this point and continuously drains complete frames.
    pub fn install(&self, stdin: ChildStdin, stdout: ChildStdout) -> Result<(), String> {
        self.shutdown();
        {
            let mut pipe = self
                .inner
                .stdin
                .lock()
                .map_err(|_| "worker unavailable".to_string())?;
            *pipe = Some(stdin);
        }
        {
            let mut pipe = self
                .inner
                .stdout
                .lock()
                .map_err(|_| "worker unavailable".to_string())?;
            *pipe = Some(stdout);
        }
        self.inner.available.store(true, Ordering::Release);

        let reader = {
            let mut pipe = self
                .inner
                .stdout
                .lock()
                .map_err(|_| "worker unavailable".to_string())?;
            pipe.take()
                .ok_or_else(|| "worker unavailable".to_string())?
        };
        let inner = Arc::clone(&self.inner);
        if thread::Builder::new()
            .name("scm-worker-rpc-reader".into())
            .spawn(move || read_worker_output(inner, reader))
            .is_err()
        {
            self.mark_unavailable("worker unavailable");
            return Err("worker unavailable".to_string());
        }
        Ok(())
    }

    /// Make future calls fail and wake a call which is waiting for a frame.
    /// This is used both on stdout EOF and during application close.
    pub fn shutdown(&self) {
        self.mark_unavailable("worker unavailable");
        if let Ok(mut stdout) = self.inner.stdout.lock() {
            stdout.take();
        }
    }

    fn mark_unavailable(&self, message: &str) {
        self.inner.available.store(false, Ordering::Release);
        if let Ok(mut stdin) = self.inner.stdin.lock() {
            stdin.take();
        }
        let pending = self
            .inner
            .pending
            .lock()
            .ok()
            .map(|mut calls| std::mem::take(&mut *calls));
        if let Some(calls) = pending {
            for (_, sender) in calls {
                let _ = sender.send(Err(message.to_string()));
            }
        }
    }

    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        validate_method(method)?;
        if !params.is_object() {
            return Err("invalid params".to_string());
        }
        if !self.inner.available.load(Ordering::Acquire) {
            return Err("worker unavailable".to_string());
        }

        // The one lock is intentional: this first slice has one in-flight
        // request, while the reader remains independent and always drains.
        let _serialized = self
            .inner
            .call_lock
            .lock()
            .map_err(|_| "worker unavailable".to_string())?;
        if !self.inner.available.load(Ordering::Acquire) {
            return Err("worker unavailable".to_string());
        }

        let id = format!("rpc-{}", self.inner.next_id.fetch_add(1, Ordering::Relaxed));
        let request = encode_request(&id, method, &params)?;
        let (sender, receiver) = mpsc::channel();
        self.inner
            .pending
            .lock()
            .map_err(|_| "worker unavailable".to_string())?
            .insert(id.clone(), sender);

        let write_result = (|| {
            let mut stdin = self
                .inner
                .stdin
                .lock()
                .map_err(|_| "worker unavailable".to_string())?;
            let pipe = stdin
                .as_mut()
                .ok_or_else(|| "worker unavailable".to_string())?;
            pipe.write_all(&request)
                .map_err(|_| "worker unavailable".to_string())?;
            pipe.flush().map_err(|_| "worker unavailable".to_string())?;
            Ok::<(), String>(())
        })();
        if let Err(error) = write_result {
            self.remove_pending(&id);
            self.mark_unavailable(&error);
            return Err(error);
        }

        match receiver.recv_timeout(self.timeout) {
            Ok(Ok(response)) => validate_response(&id, response),
            Ok(Err(error)) => {
                self.remove_pending(&id);
                Err(error)
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.remove_pending(&id);
                self.mark_unavailable("worker timeout");
                Err("worker timeout".to_string())
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.remove_pending(&id);
                Err("worker unavailable".to_string())
            }
        }
    }

    fn remove_pending(&self, id: &str) {
        if let Ok(mut pending) = self.inner.pending.lock() {
            pending.remove(id);
        }
    }
}

/// Narrow Tauri surface: no arbitrary Python method or path dispatch.
#[tauri::command]
pub fn wb_rpc(state: State<'_, WorkerRpc>, method: String, params: Value) -> Result<Value, String> {
    state.call(&method, params)
}

fn validate_method(method: &str) -> Result<(), String> {
    match method {
        "info" | "manifest" | "settings.get" => Ok(()),
        _ => Err("unknown method".to_string()),
    }
}

fn encode_request(id: &str, method: &str, params: &Value) -> Result<Vec<u8>, String> {
    let request = json!({"id": id, "method": method, "params": params});
    let mut bytes = serde_json::to_vec(&request).map_err(|_| "invalid request".to_string())?;
    bytes.push(b'\n');
    if bytes.len() > MAX_REQUEST_BYTES {
        return Err("request too large".to_string());
    }
    Ok(bytes)
}

#[derive(Debug)]
enum Frame {
    Data(Vec<u8>),
    TooLarge,
}

/// Read one complete line without allowing an unterminated worker frame to
/// grow without bound. Oversized frames are drained through their newline so
/// the reader remains synchronized for the next request.
fn read_frame<R: BufRead>(reader: &mut R) -> io::Result<Option<Frame>> {
    let mut frame = Vec::new();
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            if frame.is_empty() {
                return Ok(None);
            }
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "unterminated worker frame",
            ));
        }
        if let Some(newline) = chunk.iter().position(|byte| *byte == b'\n') {
            let end = newline + 1;
            if frame.len() + end > MAX_RESPONSE_BYTES {
                reader.consume(end);
                drain_frame(reader)?;
                return Ok(Some(Frame::TooLarge));
            }
            frame.extend_from_slice(&chunk[..end]);
            reader.consume(end);
            return Ok(Some(Frame::Data(frame)));
        }
        if frame.len() + chunk.len() > MAX_RESPONSE_BYTES {
            let amount = chunk.len();
            reader.consume(amount);
            drain_frame(reader)?;
            return Ok(Some(Frame::TooLarge));
        }
        frame.extend_from_slice(chunk);
        let amount = chunk.len();
        reader.consume(amount);
    }
}

fn drain_frame<R: BufRead>(reader: &mut R) -> io::Result<()> {
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            return Ok(());
        }
        let amount = chunk
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|position| position + 1)
            .unwrap_or(chunk.len());
        let done = chunk[..amount].contains(&b'\n');
        reader.consume(amount);
        if done {
            return Ok(());
        }
    }
}

fn read_worker_output(inner: Arc<Inner>, stdout: ChildStdout) {
    let mut reader = BufReader::new(stdout);
    loop {
        match read_frame(&mut reader) {
            Ok(None) => {
                mark_unavailable_inner(&inner, "worker unavailable");
                return;
            }
            Ok(Some(Frame::TooLarge)) => {
                fail_pending(&inner, "worker response too large");
            }
            Ok(Some(Frame::Data(mut frame))) => {
                // read_frame only returns newline-terminated frames.
                frame.pop();
                if frame.last() == Some(&b'\r') {
                    frame.pop();
                }
                let value = match String::from_utf8(frame)
                    .ok()
                    .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                {
                    Some(value) => value,
                    None => {
                        fail_pending(&inner, "malformed worker response");
                        continue;
                    }
                };
                let Some(id) = value.get("id").and_then(Value::as_str).map(str::to_owned) else {
                    fail_pending(&inner, "malformed worker response");
                    continue;
                };
                let mut mismatch = None;
                if let Ok(mut pending) = inner.pending.lock() {
                    if let Some(sender) = pending.remove(&id) {
                        let _ = sender.send(Ok(value));
                    } else if !pending.is_empty() {
                        mismatch = Some(std::mem::take(&mut *pending));
                    }
                }
                if let Some(calls) = mismatch {
                    for (_, sender) in calls {
                        let _ = sender.send(Err("mismatched worker response".to_string()));
                    }
                }
            }
            Err(_) => {
                mark_unavailable_inner(&inner, "worker unavailable");
                return;
            }
        }
    }
}

fn fail_pending(inner: &Inner, message: &str) {
    let pending = inner
        .pending
        .lock()
        .ok()
        .map(|mut calls| std::mem::take(&mut *calls));
    if let Some(calls) = pending {
        for (_, sender) in calls {
            let _ = sender.send(Err(message.to_string()));
        }
    }
}

fn mark_unavailable_inner(inner: &Inner, message: &str) {
    inner.available.store(false, Ordering::Release);
    if let Ok(mut stdin) = inner.stdin.lock() {
        stdin.take();
    }
    fail_pending(inner, message);
}

fn validate_response(expected_id: &str, response: Value) -> Result<Value, String> {
    let object = response
        .as_object()
        .ok_or_else(|| "malformed worker response".to_string())?;
    let id = object
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| "malformed worker response".to_string())?;
    if id != expected_id {
        return Err("mismatched worker response".to_string());
    }
    let ok = object
        .get("ok")
        .and_then(Value::as_bool)
        .ok_or_else(|| "malformed worker response".to_string())?;
    if ok {
        if object.len() != 3 || object.contains_key("error") || !object.contains_key("result") {
            return Err("malformed worker response".to_string());
        }
        return Ok(object["result"].clone());
    }

    if object.len() != 3 || object.contains_key("result") {
        return Err("malformed worker response".to_string());
    }
    let error = object
        .get("error")
        .and_then(Value::as_object)
        .ok_or_else(|| "malformed worker response".to_string())?;
    let code = error
        .get("code")
        .and_then(Value::as_str)
        .ok_or_else(|| "malformed worker response".to_string())?;
    if error.len() != 2 || error.get("message").and_then(Value::as_str).is_none() {
        return Err("malformed worker response".to_string());
    }
    // Never pass worker-controlled detail through the command boundary.
    let safe_code = match code {
        "unknown_method" | "bad_request" | "internal" => code,
        _ => "internal",
    };
    Err(format!("worker error: {safe_code}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use std::sync::Barrier;

    #[test]
    fn allowlist_is_narrow() {
        assert!(validate_method("info").is_ok());
        assert!(validate_method("manifest").is_ok());
        assert!(validate_method("settings.get").is_ok());
        assert_eq!(validate_method("jobs.start"), Err("unknown method".into()));
    }

    #[test]
    fn request_has_id_and_newline_framing() {
        let frame = encode_request("rpc-7", "info", &json!({})).unwrap();
        assert_eq!(frame.last(), Some(&b'\n'));
        let value: Value = serde_json::from_slice(&frame[..frame.len() - 1]).unwrap();
        assert_eq!(value["id"], "rpc-7");
        assert_eq!(value["method"], "info");
    }

    #[test]
    fn response_validation_rejects_mismatch_and_malformed_shapes() {
        assert_eq!(
            validate_response("rpc-1", json!({"id":"rpc-2","ok":true,"result":{}})),
            Err("mismatched worker response".into())
        );
        assert_eq!(
            validate_response("rpc-1", json!({"id":"rpc-1","ok":true})),
            Err("malformed worker response".into())
        );
        assert_eq!(
            validate_response("rpc-1", json!({"id":"rpc-1","ok":"yes"})),
            Err("malformed worker response".into())
        );
        assert_eq!(
            validate_response(
                "rpc-1",
                json!({"id":"rpc-1","ok":false,"error":{"code":"internal"}})
            ),
            Err("malformed worker response".into())
        );
        assert_eq!(
            validate_response(
                "rpc-1",
                json!({"id":"rpc-1","ok":true,"result":{},"extra":true})
            ),
            Err("malformed worker response".into())
        );
    }

    #[test]
    fn structured_errors_are_safe() {
        assert_eq!(
            validate_response(
                "rpc-1",
                json!({"id":"rpc-1","ok":false,"error":{"code":"internal","message":"secret path"}})
            ),
            Err("worker error: internal".into())
        );
        assert_eq!(
            validate_response(
                "rpc-1",
                json!({"id":"rpc-1","ok":false,"error":{"code":"oops","message":"secret"}})
            ),
            Err("worker error: internal".into())
        );
    }

    #[test]
    fn eof_and_unavailable_fail_without_a_pipe() {
        let rpc = WorkerRpc::new();
        assert_eq!(
            rpc.call("info", json!({})),
            Err("worker unavailable".into())
        );
        rpc.mark_unavailable("worker unavailable");
        assert!(!rpc.inner.available.load(Ordering::Acquire));
    }

    #[test]
    fn timeout_is_bounded() {
        let rpc = WorkerRpc::with_timeout(Duration::from_millis(10));
        rpc.inner.available.store(true, Ordering::Release);
        let started = std::time::Instant::now();
        assert_eq!(
            rpc.call("info", json!({})),
            Err("worker unavailable".into())
        );
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn calls_have_a_serialization_gate() {
        let rpc = WorkerRpc::new();
        let first = Arc::clone(&rpc.inner);
        let barrier = Arc::new(Barrier::new(2));
        let other = Arc::clone(&barrier);
        let thread = thread::spawn(move || {
            let _guard = first.call_lock.lock().unwrap();
            other.wait();
            thread::sleep(Duration::from_millis(20));
        });
        barrier.wait();
        let started = std::time::Instant::now();
        let _guard = rpc.inner.call_lock.lock().unwrap();
        assert!(started.elapsed() >= Duration::from_millis(10));
        thread.join().unwrap();
    }

    #[test]
    fn request_size_limit_is_enforced() {
        let oversized = Value::String("x".repeat(MAX_REQUEST_BYTES));
        assert_eq!(
            encode_request("rpc-1", "info", &oversized),
            Err("request too large".into())
        );
    }

    #[test]
    fn response_size_limit_is_explicit() {
        assert!(MAX_RESPONSE_BYTES >= 8 * 1024 * 1024);
        assert!(MAX_REQUEST_BYTES < MAX_RESPONSE_BYTES);
    }

    #[test]
    fn frame_reader_rejects_unterminated_input() {
        let mut reader = std::io::Cursor::new(br#"{"id":"rpc-1"}"#.to_vec());
        assert_eq!(
            read_frame(&mut reader).unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn frame_reader_preserves_multiple_frames() {
        let mut reader = std::io::Cursor::new(b"one\ntwo\n".to_vec());
        assert!(matches!(read_frame(&mut reader), Ok(Some(Frame::Data(_)))));
        assert!(matches!(read_frame(&mut reader), Ok(Some(Frame::Data(_)))));
        assert!(matches!(read_frame(&mut reader), Ok(None)));
    }

    fn python_child(script: &str) -> std::process::Child {
        #[cfg(windows)]
        let mut command = Command::new("python");
        #[cfg(not(windows))]
        let mut command = Command::new("python3");
        command
            .arg("-c")
            .arg(script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("Python is required for the WorkerRpc pipe integration test")
    }

    #[test]
    fn pipe_round_trip_correlates_response_and_handles_eof() {
        let mut child = python_child(
            "import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({'id':r['id'],'ok':True,'result':{'method':r['method']}}), flush=True)\n",
        );
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let rpc = WorkerRpc::with_timeout(Duration::from_millis(500));
        rpc.install(stdin, stdout).unwrap();
        assert_eq!(
            rpc.call("manifest", json!({})).unwrap()["method"],
            "manifest"
        );

        child.kill().unwrap();
        child.wait().unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while rpc.inner.available.load(Ordering::Acquire) && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(!rpc.inner.available.load(Ordering::Acquire));
        assert_eq!(
            rpc.call("info", json!({})),
            Err("worker unavailable".into())
        );
    }

    #[test]
    fn pipe_call_times_out_without_hanging() {
        let mut child = python_child("import time; time.sleep(10)\n");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let rpc = WorkerRpc::with_timeout(Duration::from_millis(50));
        rpc.install(stdin, stdout).unwrap();
        let started = std::time::Instant::now();
        assert_eq!(rpc.call("info", json!({})), Err("worker timeout".into()));
        assert!(started.elapsed() < Duration::from_secs(1));
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
