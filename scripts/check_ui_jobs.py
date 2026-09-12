#!/usr/bin/env python3
"""Execute the frontend jobs facade's native and browser transport contract."""
from pathlib import Path
import shutil
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
    notices = (ROOT / "ui" / "js" / "job-notices.js").read_text(encoding="utf-8")
    notice_state_path = ROOT / "ui" / "js" / "job-notice-state.js"
    notice_state = notice_state_path.read_text(encoding="utf-8")
    pdf = (ROOT / "ui" / "js" / "pages" / "pdf.js").read_text(encoding="utf-8")
    fetch_page = (ROOT / "ui" / "js" / "pages" / "fetch.js").read_text(encoding="utf-8")
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
    # Terminal stream events carry status only. Advanced-mode PDF actions need
    # the canonical list row, which is where artifact paths and save grants are
    # exposed after the backend snapshots the output.
    for marker in ('export async function openJobPdf(job)',
                   'job.kind === "create_pdf"', 'onclick: () => openJobPdf(job)',
                   'const result = await openFile(output);',
                   'refreshJobs().then(() => {',
                   'if (serial === _streamSerial && S.activeJobId === id) updateFooter();'):
        if marker not in console:
            print(f"FAIL: advanced PDF completion actions are missing {marker}")
            return 1
    terminal_branch = console[console.find('onDone: done => {'):console.find('onDone: done => {') + 1800]
    if 'refreshJobs()' not in terminal_branch:
        print("FAIL: console completion does not refresh artifact paths and save grants")
        return 1
    for marker in ("S.startedJobIds[kind]", "S.jobArgs[j0.id]"):
        if marker not in forms:
            print(f"FAIL: form runs do not preserve navigation state: {marker}")
            return 1
    for marker in ('import { syncJobNotices } from "./job-notices.js";',
                   "syncJobNotices(next);"):
        if marker not in console:
            print(f"FAIL: canonical job refresh does not drive sidebar notices: {marker}")
            return 1
    for marker in ('data-job-notice', 'class: "rp-head rp-head-row"',
                   '"aria-label": "Dismiss this job message"',
                   'setTimeout(() => syncJobNotices(S.jobs)',
                   'class: `repoprog sidebar-note job-notice ${record.status}`'):
        if marker not in notices:
            print(f"FAIL: sidebar job notices are missing {marker}")
            return 1
    for marker in ("JOB_NOTICE_HOLD_MS = 5000", "JOB_NOTICE_MAX = 4",
                   'new Set(["repo_init", "repo_update", "update"])'):
        if marker not in notice_state:
            print(f"FAIL: sidebar job notice lifecycle is missing {marker}")
            return 1
    for marker in ('/^\\s*Image\\s+(\\d+)\\s*:/i', "opts.progressTotal(job)",
                   "S.startedJobIds?.[kind]", "jobs.list().then(result", "setInterval(tick, 500)",
                   "createFetchProgress", "stage ${view.stage} of 2", "fetchProgress.active",
                   "if (opts.slotTotal)", "fetchProgress.setDeckTotal"):
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
    # The fetch page is the only place that knows a prefetching fetch's second
    # stage should be measured against the decklist rather than the images.
    if "slotTotal: async job => job.deck_total || 0" not in fetch_page:
        print("FAIL: the fetch page does not give the strip the decklist slot count")
        return 1
    if "jobStrip(kind" not in fetch_page:
        print("FAIL: the fetch page no longer renders a job strip")
        return 1
    if 'document.body.classList.add("console-open")' not in console or \
            "body:not(.mode-simple).console-open .main" not in theme:
        print("FAIL: advanced console does not reserve page space")
        return 1
    # Both fetch and PDF use this shared completion structure. Successful jobs
    # hide the redundant Done header, leaving one desktop row with the useful
    # message at left and its actions at the far right.
    for marker in (".jobstrip.done .js-top { display: none; }",
                   ".jobstrip.done .js-body { grid-template-columns: minmax(0, 1fr) auto;",
                   ".jobstrip.done .js-actions { grid-column: 2; grid-row: 1; justify-self: end;"):
        if marker not in theme:
            print(f"FAIL: completed job actions are not on the message row: {marker}")
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
// Wait for the logical outcome, not a wall-clock constant: coarse OS timer
// resolution (e.g. Windows) can stretch the zero-delay retry cycles, so the
// bounded wait below tolerates slow ticks while still failing a hub that never
// exhausts (or that is noisy) within five seconds.
const exhaustionDeadline = Date.now() + 5000;
while (exhaustionErrors < 2 && Date.now() < exhaustionDeadline) {
  await new Promise(r => realSetTimeout(r, 100));
}
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
    # The two-stage fetch model is pure, so its stage machine is checked
    # directly against the plugin's real output sequence.
    stage_model = subprocess.run(
        [shutil.which("node") or "node", "--input-type=module", "-", str(UI / "fetch-progress.js")],        input=r'''import fs from "node:fs";
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const source = fs.readFileSync(process.argv[2], "utf8");
const mod = await import(dataUrl(source));
const fail = message => { throw new Error(message); };

// A run that never prefetches must not be described as a two-stage run.
let p = mod.createFetchProgress();
if (p.active) fail("a fresh run already claims a prefetch stage");
if (p.line("  Fetched 42/101 images") !== null) fail("a plain fetch line was treated as a prefetch stage");
if (p.line("Slot 7: Nami") !== null) fail("a slot line without a prefetch total invented a stage");
if (p.active) fail("a plain fetch line switched the run into two-stage mode");
if (p.view() !== null) fail("the two-stage view rendered without a prefetch total");

// The real MTG/MPCFill sequence: 88 prefetched images, then 100 deck slots.
p = mod.createFetchProgress();
const step = line => { const r = p.line(line); return r || {}; };
let r = step("  Prefetching 88 images with 8 workers...");
if (!p.active || r.stage !== 1) fail("the prefetch announcement did not start stage 1");
let v = p.view();
if (v.stage !== 1 || v.done !== 0 || v.total !== 88 || v.pct !== 0) fail("stage 1 did not start empty: " + JSON.stringify(v));
for (const n of [10, 44, 88]) step(`  Fetched ${n}/88 images`);
v = p.view();
if (v.stage !== 1 || v.done !== 88 || v.pct !== 100) fail("stage 1 did not reach 100%: " + JSON.stringify(v));

// "Prefetch complete." fills stage 1 and asks the caller to hold before stage 2.
r = step("Prefetch complete.");
if (!r.beginRename) fail("the prefetch completion did not ask for a stage-2 hold");
if (p.view().pct !== 100) fail("stage 1 did not read full at completion");
if (mod.RENAME_HOLD_MS <= 0) fail("a zero hold would make stage 1's 100% invisible");
p.beginRename();
v = p.view();
if (v.stage !== 2 || v.done !== 0 || v.pct !== 0) fail("stage 2 did not restart at 0%: " + JSON.stringify(v));

for (const n of [1, 44, 88]) {
  step(`Slot ${n}: Card ${n}`);
  const seen = p.view();
  if (seen.stage !== 2) fail("a slot line left stage 2");
  if (seen.pct !== Math.round(n / 88 * 100)) fail(`slot ${n} gave ${seen.pct}%`);
}
if (p.view().pct !== 100) fail("stage 2 did not reach 100%");
// A deck can carry more slots than unique images: past the announced total the
// count keeps moving but no ratio may be claimed.
step("Slot 95: Card 95");
v = p.view();
if (v.done !== 95 || v.pct !== 100 || v.text !== "95 slots renamed") fail("past the total: " + JSON.stringify(v));
if (/\//.test(v.text)) fail("past the total still printed a ratio: " + v.text);
// Counters never run backwards, whatever order the plugin prints them in.
step("Slot 44: Card 44");
if (p.view().done !== 95) fail("a lower slot number moved the counter backwards");

// A decklist that declares its own slot count is the honest denominator for
// stage 2: a card played six times is one image and six slots, so the
// prefetch total (88) is short of the slots that follow (100).
p = mod.createFetchProgress();
step("  Prefetching 88 images with 8 workers...");
step("  Fetched 88/88 images");
step("Prefetch complete.");
p.beginRename();
p.setDeckTotal(100);
v = p.view();
if (v.total !== 100 || v.done !== 0 || v.pct !== 0) fail("the decklist total did not become stage 2's scale: " + JSON.stringify(v));
for (const n of [25, 50, 99]) {
  step(`Slot ${n}: Card ${n}`);
  const seen = p.view();
  // Exactly the fraction of the decklist walked, not of the prefetch.
  if (seen.pct !== Math.round(n / 100 * 100)) fail(`slot ${n} of a 100 card decklist gave ${seen.pct}%`);
}
step("Slot 100: Last Card");
if (p.view().pct !== 100) fail("a complete 100 card decklist did not reach 100%");
// A count past the declared total still reports no ratio.
step("Slot 104: Beyond");
v = p.view();
if (v.done !== 104 || v.pct !== 100 || v.text !== "104 slots renamed") fail("beyond the decklist total: " + JSON.stringify(v));
// An unknown total leaves stage 1's number in place.
p = mod.createFetchProgress();
step("  Prefetching 12 images with 8 workers...");
step("Prefetch complete.");
p.beginRename();
p.setDeckTotal(0);
if (p.view().total !== 12) fail("an unknown decklist total did not fall back to the prefetch count");
console.log("ok: the two-stage fetch progress model passed");
''',
        text=True,
        capture_output=True,
    )
    if stage_model.returncode:
        print("FAIL: Node fetch-stage contract failed: " + (stage_model.stderr or stage_model.stdout).strip())
        return 1
    notice_model = subprocess.run(
        ["node", "--input-type=module", "-", str(notice_state_path)],
        input=r'''import fs from "node:fs";
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const mod = await import(dataUrl(fs.readFileSync(process.argv[2], "utf8")));
const fail = message => { throw new Error(message); };
const job = (id, status, ts = 1, kind = "create_pdf") => ({ id, status, ts, kind, title: `Job ${id}` });
let state = mod.createJobNoticeState();
let view = state.sync([job("old", "ok")], 1000);
if (view.length) fail("historical terminal jobs created notices");
view = state.sync([job("a", "running")], 1000);
if (view.length !== 1 || view[0].status !== "running" || view[0].expiresAt) fail("running job did not create a notice");
view = state.sync([job("a", "ok")], 2000);
if (view[0]?.status !== "ok" || view[0].expiresAt !== 2000 + mod.JOB_NOTICE_HOLD_MS) fail("running notice did not transition to a five-second terminal notice");
view = state.sync([job("a", "ok")], 6999);
if (view.length !== 1) fail("terminal notice disappeared before five seconds");
view = state.sync([job("a", "ok")], 7000);
if (view.length) fail("terminal notice did not expire at five seconds");
state = mod.createJobNoticeState();
state.sync([job("b", "running")], 1000);
state.dismiss("b");
if (state.sync([job("b", "running")], 1100).length) fail("dismissed running notice came back");
if (state.sync([job("b", "ok")], 1200).length) fail("dismissed job created a terminal notice");
state = mod.createJobNoticeState();
view = state.sync(Array.from({ length: 8 }, (_, i) => job(String(i), "running", i)), 1000);
if (view.length !== mod.JOB_NOTICE_MAX || view[0].job.id !== "7") fail("notice list is not bounded to the newest jobs");
view = state.sync([job("repo", "running", 10, "repo_update"), job("app", "running", 11, "update")], 1000);
if (view.some(record => record.job.id === "repo" || record.job.id === "app")) fail("specialized sidebar jobs were duplicated");
state = mod.createJobNoticeState();
state.sync([job("same", "running")], 1000);
view = state.sync([job("same", "ok")], 2000);
const expiry = view[0].expiresAt;
view = state.sync([job("same", "ok")], 3000);
if (view[0].expiresAt !== expiry) fail("terminal refresh reset the five-second timer");
console.log("ok: sidebar job notice lifecycle passed");
''',
        text=True,
        capture_output=True,
    )
    if notice_model.returncode:
        print("FAIL: Node job-notice contract failed: " + (notice_model.stderr or notice_model.stdout).strip())
        return 1
    print(stage_model.stdout.strip())
    print(notice_model.stdout.strip())
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
