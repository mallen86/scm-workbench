#!/bin/sh
# Packaged Linux lifecycle smoke. Run under a graphical session or xvfb-run.
set -eu

app="${1:-/usr/lib/scm-workbench/scm-workbench}"
case "$app" in /*) ;; *) echo "linux smoke: executable path must be absolute" >&2; exit 2;; esac
[ -x "$app" ] || { echo "linux smoke: executable is missing: $app" >&2; exit 2; }
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || {
    echo "linux smoke: DISPLAY or WAYLAND_DISPLAY is required (use xvfb-run -a)" >&2
    exit 2
}

runtime="$(dirname "$app")/runtime/python/install/bin/python3.13"
[ -x "$runtime" ] || { echo "linux smoke: bundled runtime is missing: $runtime" >&2; exit 2; }

work="$(mktemp -d "${TMPDIR:-/tmp}/scm-workbench-linux-smoke.XXXXXX")"
app_pid=""
worker_pid=""
cleanup() {
    [ -z "$app_pid" ] || kill -9 "$app_pid" 2>/dev/null || true
    [ -z "$worker_pid" ] || kill -9 "$worker_pid" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT INT TERM

port_closed() {
    python3 - "$1" <<'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.2)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(0)
finally:
    sock.close()
raise SystemExit(1)
PY
}

start_app() {
    data="$1"
    log="$2"
    mkdir -p "$data"
    SCM_WORKBENCH_DATA="$data" \
    SCM_WORKBENCH_NO_BOOTSTRAP=1 \
    SCM_WORKBENCH_NO_UPDATE_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    "$app" >"$log" 2>&1 &
    app_pid=$!
}

await_ipc() {
    data="$1"
    log="$2"
    worker_pid=""
    for _ in $(seq 1 90); do
        sleep 1
        if ! port_closed 8038; then
            echo "linux smoke FAILED: packaged worker opened port 8038" >&2
            return 1
        fi
        worker_pid="$(pgrep -P "$app_pid" -f 'python3\.13.*scm_workbench\.server' | head -n 1 || true)"
        if [ -n "$worker_pid" ] \
           && grep -q '\[ipc\] served ready' "$data/server-tauri.log" 2>/dev/null \
           && grep -q '\[ipc\] served info' "$data/server-tauri.log" 2>/dev/null \
           && grep -q '\[ipc\] served manifest' "$data/server-tauri.log" 2>/dev/null \
           && grep -q '\[ipc\] served settings.get' "$data/server-tauri.log" 2>/dev/null \
           && grep -q '\[ipc\] served jobs.list' "$data/server-tauri.log" 2>/dev/null \
           && grep -q '\[ipc\] served updates.get' "$data/server-tauri.log" 2>/dev/null; then
            return 0
        fi
        if ! kill -0 "$app_pid" 2>/dev/null; then
            break
        fi
    done
    echo "---- shell log ----" >&2
    tail -n 100 "$log" >&2 2>/dev/null || true
    echo "---- worker log ----" >&2
    tail -n 100 "$data/server-tauri.log" >&2 2>/dev/null || true
    echo "linux smoke FAILED: embedded UI did not produce ready plus five public IPC markers" >&2
    return 1
}

assert_worker_stopped() {
    for _ in $(seq 1 60); do
        if ! kill -0 "$worker_pid" 2>/dev/null; then
            worker_pid=""
            return 0
        fi
        sleep 0.25
    done
    echo "linux smoke FAILED: supervised worker $worker_pid outlived the shell" >&2
    return 1
}

# Bundled dependency and helper proof before opening a webview.
"$runtime" -B -c "import PIL, ezdxf, pypdfium2, bs4, numpy, matplotlib; print('linux smoke: bundled runtime imports pass')"
"$runtime" -B "$(dirname "$0")/check_pdf_preview_helper.py" \
    "$(dirname "$app")/app/scm_workbench/pdf_preview_helper.py"

# Native close: WM_DELETE_WINDOW must exercise Tauri's CloseRequested path.
data="$work/soft-data"
log="$work/soft-shell.log"
start_app "$data" "$log"
await_ipc "$data" "$log"
if ! timeout 15s xdotool search --onlyvisible --name 'SCM Workbench' windowclose; then
    echo "linux smoke FAILED: could not request a native window close" >&2
    exit 1
fi
for _ in $(seq 1 60); do
    if ! kill -0 "$app_pid" 2>/dev/null; then break; fi
    sleep 0.25
done
if kill -0 "$app_pid" 2>/dev/null; then
    echo "linux smoke FAILED: native window close did not exit" >&2
    exit 1
fi
wait "$app_pid" 2>/dev/null || true
app_pid=""
assert_worker_stopped
if ! port_closed 8038; then
    echo "linux smoke FAILED: port 8038 remained open after native close" >&2
    exit 1
fi
if grep -E '^\[workbench\] "(GET|POST) /' "$data/server-tauri.log" >/dev/null 2>&1; then
    echo "linux smoke FAILED: embedded WebView made an HTTP request" >&2
    exit 1
fi

# Hard shell loss: protocol EOF must still reap the worker without a destructor.
data="$work/hard-data"
log="$work/hard-shell.log"
start_app "$data" "$log"
await_ipc "$data" "$log"
kill -9 "$app_pid"
wait "$app_pid" 2>/dev/null || true
app_pid=""
assert_worker_stopped
if ! port_closed 8038; then
    echo "linux smoke FAILED: port 8038 remained open after hard shell termination" >&2
    exit 1
fi

echo "linux smoke: native IPC rendered, port stayed closed, and soft/hard shell exits reaped the worker"
