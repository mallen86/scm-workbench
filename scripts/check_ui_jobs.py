#!/usr/bin/env python3
"""Execute the frontend jobs facade's native and browser transport contract."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def main() -> int:
    jobs = (UI / "jobs.js").read_text(encoding="utf-8")
    if "jobs.poll" not in jobs or "method: \"POST\"" not in jobs:
        print("FAIL: jobs facade is missing native polling or browser POST transport")
        return 1
    forms = (ROOT / "ui" / "js" / "forms.js").read_text(encoding="utf-8")
    console = (ROOT / "ui" / "js" / "console.js").read_text(encoding="utf-8")
    jobstrip = (ROOT / "ui" / "js" / "jobstrip.js").read_text(encoding="utf-8")
    pdf = (ROOT / "ui" / "js" / "pages" / "pdf.js").read_text(encoding="utf-8")
    theme = (ROOT / "ui" / "theme.css").read_text(encoding="utf-8")
    if "j.job?.warnings" not in forms or "catch (error)" not in forms or "startFailed" not in forms:
        print("FAIL: doRun does not preserve nested warnings and start errors")
        return 1
    if ("CONSOLE_LOG_CAP = 16384" not in console or "loadConsoleLog" not in console or
        "lineTruncated" not in console or "initial.omitted" not in console or "initial.nextSeq" not in console):
        print("FAIL: console does not page and visibly cap completed logs")
        return 1
    if "typeof handlers === \"number\"" in jobs:
        print("FAIL: subscribe retains undocumented positional argument mangling")
        return 1
    for marker in ("S.startedJobIds[kind]", "S.jobArgs[j0.id]"):
        if marker not in forms:
            print(f"FAIL: form runs do not preserve navigation state: {marker}")
            return 1
    for marker in ('/^\\s*Image\\s+(\\d+)\\s*:/i', "opts.progressTotal(job)",
                   "S.startedJobIds?.[kind]", "jobs.list().then(result", "setInterval(tick, 500)"):
        if marker not in jobstrip:
            print(f"FAIL: PDF job progress/completion persistence is missing {marker}")
            return 1
    if jobstrip.count("jobs.list().then(result") < 2:
        print("FAIL: rebuilt job strips do not immediately refresh canonical completion state")
        return 1
    for marker in ("progressTotal: async job", "S.jobArgs?.[done.id]", "resolveTemplate(f.paper_size, f.card_size, !!f.borderless)"):
        if marker not in pdf:
            print(f"FAIL: PDF progress or completion actions are missing {marker}")
            return 1
    if 'document.body.classList.add("console-open")' not in console or \
            "body:not(.mode-simple).console-open .main" not in theme:
        print("FAIL: advanced console does not reserve page space")
        return 1
    for path in sorted(UI.rglob("*.js")):
        if path.name == "jobs.js":
            continue
        source = path.read_text(encoding="utf-8")
        if "/api/jobs" in source or "new EventSource" in source:
            print(f"FAIL: {path.relative_to(ROOT)} bypasses the shared jobs facade")
            return 1
    node = subprocess.run(
        ["node", "--input-type=module", "-", str(UI / "jobs.js"), str(UI / "transport.js")],
        input=r'''import fs from "node:fs";
const jobsSource = fs.readFileSync(process.argv[2], "utf8");
const transportSource = fs.readFileSync(process.argv[3], "utf8");
const transportUrl = `data:text/javascript;base64,${Buffer.from(transportSource).toString("base64")}`;
const jobsUrl = `data:text/javascript;base64,${Buffer.from(jobsSource.replace('from "./transport.js"', `from "${transportUrl}"`)).toString("base64")}`;
const { jobs } = await import(jobsUrl);
const fail = message => { throw new Error(message); };
let fetchCalls = 0;
const nativeCalls = [];
let pollRelease;
let pollCount = 0;
globalThis.fetch = (...args) => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
globalThis.window = { __TAURI_INTERNALS__: { invoke(method, args) {
  nativeCalls.push({ method, args });
  const rpcMethod = args.method;
  if (rpcMethod === "jobs.list") return Promise.resolve({ jobs: [] });
  if (rpcMethod === "jobs.start") return Promise.resolve({ ok: true, job: { id: "n", warnings: ["native warning"] } });
  if (rpcMethod === "jobs.log") return Promise.resolve({ lines: ["old"], first_seq: 0, next_seq: 1, status: "running" });
  if (rpcMethod === "jobs.kill") return Promise.resolve({ ok: true });
  if (rpcMethod === "jobs.poll") { pollCount++; return new Promise(resolve => { pollRelease = resolve; }); }
  return Promise.reject(new Error("unexpected method"));
} } };
await jobs.list();
const started = await jobs.start("fixture", { value: 1 });
if (JSON.stringify(started.job.warnings) !== JSON.stringify(["native warning"])) fail("nested job warnings were lost");
await jobs.log("n", 2, 3);
await jobs.kill("n");
if (fetchCalls || JSON.stringify(nativeCalls.slice(0, 4).map(x => x.args)) !== JSON.stringify([
  { method: "jobs.list", params: {} },
  { method: "jobs.start", params: { kind: "fixture", args: { value: 1 } } },
  { method: "jobs.log", params: { job_id: "n", after: 2, max_lines: 3 } },
  { method: "jobs.kill", params: { job_id: "n" } },
])) fail("native operation payloads or HTTP isolation are wrong");
let linesA = [], linesB = [], doneA = 0, doneB = 0;
const closeA = jobs.subscribe("a", { after: 0, onLine: x => linesA.push(x), onDone: () => doneA++ });
const closeB = jobs.subscribe("b", { after: 4, onLine: x => linesB.push(x), onDone: () => doneB++ });
await new Promise(r => setTimeout(r, 10));
if (pollCount !== 1) fail("native subscribers did not share one outstanding aggregate poll");
const pollRequest = nativeCalls[nativeCalls.length - 1];
if (pollRequest.method !== "wb_rpc" || pollRequest.args.method !== "jobs.poll" ||
    JSON.stringify(pollRequest.args.params) !== JSON.stringify({ cursors: [
      { job_id: "a", after: 0 }, { job_id: "b", after: 4 },
    ], max_events: 256 })) fail("native aggregate poll payload is wrong");
pollRelease({ jobs: [
  { job_id: "a", lines: [{ i: 0, s: "A" }, { i: 0, s: "A" }], next_seq: 1, status: "running", complete: false, gap: false, truncated: false },
  { job_id: "b", lines: [{ i: 4, s: "B" }], next_seq: 5, status: "running", complete: false, gap: false, truncated: false },
] });
await new Promise(r => setTimeout(r, 10));
closeA(); closeB();
if (linesA.length !== 1 || linesA[0].s !== "A" || linesB.length !== 1 || linesB[0].s !== "B") fail("native cursor/deduplication or subscriber isolation failed");
let positionalRejected = false;
try { jobs.subscribe("bad", 4, {}); } catch { positionalRejected = true; }
if (!positionalRejected) fail("undocumented positional subscribe form was accepted");
// Row correlation is checked before dispatch; malformed data is a retryable
// poll error and must not leak into another subscription.
let malformedErrors = 0, malformedLines = 0;
const closeM = jobs.subscribe("m", { onError: () => malformedErrors++, onLine: () => malformedLines++ });
await new Promise(r => setTimeout(r, 10));
pollRelease({ jobs: [{ job_id: "not-m", lines: [{ i: 0, s: "bad" }], next_seq: 1, status: "running", complete: false }] });
await new Promise(r => setTimeout(r, 10));
closeM();
if (!malformedErrors || malformedLines) fail("malformed native poll rows were dispatched");
// truncation is pagination, not loss: continue from next_seq without a gap
// warning and drain the next page in order.
let pages = [], gaps = 0, pageDone = 0;
const closeP = jobs.subscribe("page", { onLine: x => pages.push(x.i), onGap: () => gaps++, onDone: () => pageDone++ });
await new Promise(r => setTimeout(r, 10));
pollRelease({ jobs: [{ job_id: "page", lines: [{ i: 0, s: "one" }], next_seq: 1, status: "running", complete: false, truncated: true, gap: false }] });
await new Promise(r => setTimeout(r, 270));
pollRelease({ jobs: [{ job_id: "page", lines: [{ i: 1, s: "two" }], next_seq: 2, status: "ok", complete: true, truncated: false, gap: false }] });
await new Promise(r => setTimeout(r, 10));
closeP();
if (pages.join(",") !== "0,1" || gaps || pageDone !== 1) fail("native truncated pagination skipped data or raised a false gap");
// A terminal response is delivered once, with final lines preceding done.
let terminal = [];
const closeC = jobs.subscribe("c", { onLine: x => terminal.push("line"), onDone: x => terminal.push("done") });
await new Promise(r => setTimeout(r, 10));
pollRelease({ jobs: [{ job_id: "c", lines: [{ i: 0, s: "x" }], next_seq: 1, status: "ok", exit_code: 0, complete: true, gap: false, truncated: false }] });
await new Promise(r => setTimeout(r, 10));
closeC();
if (terminal.join(",") !== "line,done") fail("native terminal ordering/done-once failed");
// Rejected native operations (including start) never reach HTTP.
globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.reject(new Error("native down"));
let rejected = false, startRejected = false;
try { await jobs.list(); } catch { rejected = true; }
try { await jobs.start("fixture", {}); } catch { startRejected = true; }
if (!rejected || !startRejected || fetchCalls) fail("native failure silently fell back to HTTP");
let retryErrors = 0;
const closeE = jobs.subscribe("retry", { onError: () => retryErrors++ });
await new Promise(r => setTimeout(r, 10));
if (!retryErrors) fail("native polling failure was not surfaced to subscribers");
closeE();
// Replace timers only for this deterministic exhaustion check: all six
// bounded retries run immediately, then the subscription is closed.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, delay) => realSetTimeout(fn, 0);
let exhaustionErrors = 0;
const closeF = jobs.subscribe("exhaust", { onError: () => exhaustionErrors++ });
await new Promise(r => realSetTimeout(r, 40));
closeF();
globalThis.setTimeout = realSetTimeout;
if (exhaustionErrors !== 2) fail("native retry exhaustion was not finite or was noisy");
// Browser requests use the old routes and kill is POST.
delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => options.method === "POST" && url === "/api/jobs"
    ? { ok: true, job: { id: "browser", warnings: ["browser warning"] } } : { jobs: [] } };
};
await jobs.list();
const browserStarted = await jobs.start("x", { a: 1 });
if (JSON.stringify(browserStarted.job.warnings) !== JSON.stringify(["browser warning"])) fail("browser nested job warnings were lost");
await jobs.log("x", 3, 7); await jobs.kill("x");
if (requests[0].url !== "/api/jobs" || requests[1].url !== "/api/jobs" || requests[1].options.method !== "POST" ||
    requests[2].url !== "/api/jobs/x/log?after=3&max_lines=7" || requests[3].url !== "/api/jobs/x/kill" || requests[3].options.method !== "POST") fail("browser routes or payload methods changed");
let sources = [], browserLines = [], browserDone = 0, browserErrors = 0;
globalThis.EventSource = class { constructor(url) { this.url = url; this.listeners = {}; sources.push(this); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  emit(name, data) { for (const fn of this.listeners[name] || []) fn({ data: JSON.stringify(data) }); }
  close() { this.closed = true; }
};
const closeD = jobs.subscribe("x", { after: 0, onLine: x => browserLines.push(x), onDone: () => browserDone++, onError: () => browserErrors++ });
sources[0].emit("line", { i: 0, s: "zero" });
sources[0].onerror();
await new Promise(r => setTimeout(r, 300));
if (!sources[1] || !sources[1].url.endsWith("/stream?after=1")) fail("browser reconnect did not use latest cursor");
if (browserErrors !== 1) fail("browser outage emitted repeated error notifications");
sources[1].emit("line", { i: 0, s: "zero" }); sources[1].emit("line", { i: 1, s: "one" }); sources[1].emit("done", { status: "ok" });
closeD();
if (browserLines.length !== 2 || browserLines[0].i !== 0 || browserLines[1].i !== 1 || browserDone !== 1) fail("browser replay dedupe or terminal delivery failed");
console.log("ok: native/browser jobs facade contract passed");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        print("FAIL: Node jobs contract failed: " + (node.stderr or node.stdout).strip())
        return 1
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
