/* console — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, S, api, el, fmtTs, ico, iconize, nativePick, toast } from "./core.js";import { revealPath } from "./native-actions.js";import { displayCmd, repoRowForKind } from "./forms.js";import { jobs } from "./jobs.js";import { refreshInfo, showBootFailure } from "./info.js";import { bindNav, bootPage, uiMode } from "./nav.js";import { startPrepWatcher } from "./prep.js";
let _streamSerial = 0;
function closeStream() {
  _streamSerial++;
  if (S.jobSub) { S.jobSub.close(); S.jobSub = null; }
}

// Keep the DOM transcript bounded while paging through the entire bounded
// native/HTTP log window.  A fifth, one-line probe tells us whether the fourth
// page ended because of a clipped line or because more transcript remains.
const CONSOLE_LOG_PAGE = 4096;
const CONSOLE_LOG_CAP = 16384;

export async function loadConsoleLog(jobId) {
  const lines = [];
  let after = 0;
  let firstSeq = 0;
  let status = null;
  let exitCode = null;
  let gap = false;
  let lineTruncated = false;
  let omitted = false;
  let probeFailed = false;
  for (let page = 0; page < CONSOLE_LOG_CAP / CONSOLE_LOG_PAGE; page++) {
    const before = after;
    const data = await jobs.log(jobId, before, CONSOLE_LOG_PAGE);
    if (page === 0) firstSeq = Number(data.first_seq) || 0;
    status = data.status || status;
    exitCode = data.exit_code ?? exitCode;
    gap = gap || !!data.gap;
    lineTruncated = lineTruncated || !!data.line_truncated;
    const pageLines = data.lines || [];
    lines.push(...pageLines.slice(0, CONSOLE_LOG_CAP - lines.length));
    const next = Number(data.next_seq);
    after = Number.isInteger(next) && next >= before ? next : before + pageLines.length;
    const more = !!data.truncated && after > before && pageLines.length > 0;
    if (!more) break;
    if (page === CONSOLE_LOG_CAP / CONSOLE_LOG_PAGE - 1) {
      // Do not put the probe's line in the DOM; the live handoff remains the
      // last displayed cursor, so the subscription will deliver it normally.
      try {
        const probe = await jobs.log(jobId, after, 1);
        omitted = (probe.lines || []).length > 0 ||
          (!!probe.truncated && Number(probe.next_seq) > after);
      } catch {
        omitted = true;
        probeFailed = true;
      }
    }
  }
  return { lines, nextSeq: after, firstSeq, status, exitCode, gap, lineTruncated, omitted, probeFailed };
}
export let _lastJobsSig;


export async function refreshJobs(forceRender = false) {
  const next = (await jobs.list()).jobs;
  // Redraw only when the job set actually changed (new job, status flip) —
  // the 4 s poll must not repaint an unchanged list (no blink). An explicit
  // forceRender (used when the dashboard is (re)entered) repaints once even
  // though nothing changed — a freshly rendered page needs its list filled.
  const sig = (next || []).map(j => j.id + ":" + j.status).join(",");
  const changed = sig !== _lastJobsSig;
  S.jobs = next;
  if (changed) {
    _lastJobsSig = sig;
    updateBadge();
    renderConsoleTabs();
  }
  setRevealButtons();
  if (S.page === "dashboard" && (changed || forceRender)) {
    const slot = $("#recent-jobs");
    if (slot) {
      slot.innerHTML = "";
      if (!S.jobs.length) slot.append(el("div", { class: "empty" }, ico("clock"), "No jobs yet — run something and it shows up here."));
      else for (const j of S.jobs.slice(0, 6)) slot.append(jobRow(j));
    }
  }
}


export function jobRow(j) {
  return el("div", {
    class: "jobrow",
    onclick: () => { openConsole(j.id); },
  },
    el("div", { class: "jr-ico" }, ico("terminal")),
    el("div", { class: "jr-body" },
      el("div", { class: "jr-t" }, j.title),
      el("div", { class: "jr-cmd" }, displayCmd(j.cmd, j.kind) || ""),
    ),
    el("div", { class: "jr-meta" },
      el("div", { class: `statusdot ${j.status}` }, j.status),
      el("div", { class: "t" }, fmtTs(j.ts)),
    ),
  );
}


export function updateBadge() {
  const n = S.jobs.filter(j => j.status === "running").length;
  const b = $("#badge-running");
  b.hidden = !n;
  b.textContent = n;
  b.classList.toggle("show", !!n);
}


/* ================================ console ================================= */

export function toggleConsole() {
  if (uiMode() === "simple") return;   // simple mode has no console — pages show their own status strip
  const c = $("#console");
  c.hidden = !c.hidden;
  if (!c.hidden) {
    c.classList.remove("closed");
    if (S.activeJobId) attachStream(S.activeJobId, true);
  } else closeStream();
}


export function openConsole(id) {
  if (uiMode() === "simple") return;   // simple mode has no console — pages show their own status strip
  const c = $("#console");
  c.hidden = false;
  requestAnimationFrame(() => c.classList.remove("closed"));
  S.activeJobId = id;
  renderConsoleTabs();
  attachStream(id, true);
}


export function renderConsoleTabs() {
  const bar = $("#console-tabs");
  bar.innerHTML = "";
  const shown = S.jobs.slice(0, 9);
  if (!shown.length) return;
  for (const j of shown) {
    bar.append(el("button", {
      class: `ctab ${S.activeJobId === j.id ? "active" : ""}`,
      onclick: () => { S.activeJobId = j.id; renderConsoleTabs(); attachStream(j.id, true); },
    },
      el("span", { class: `dot ${j.status === "running" ? "running" : j.status === "ok" ? "ok" : j.status === "fail" ? "" : ""}`, style: `background:var(--${j.status === "running" ? "accent" : j.status === "ok" ? "ok" : j.status === "fail" ? "err" : "warn"})` }),
      j.title.length > 26 ? j.title.slice(0, 26) + "…" : j.title,
      j.ts ? el("span", { class: "ctab-t" }, fmtTs(j.ts)) : null,
    ));
  }
  $("#console-kill").disabled = S.activeJobId?.[0] && !(S.jobs.find(j => j.id === S.activeJobId)?.status === "running");
  setRevealButtons();
  updateFooter();
}


export async function attachStream(id, resume) {
  const serial = ++_streamSerial;
  if (S.jobSub) { S.jobSub.close(); S.jobSub = null; }
  const log = $("#console-log");
  log.innerHTML = "";
  const job = S.jobs.find(j => j.id === id);
  if (!job) return;

  // Load the existing transcript first.  The returned next_seq is the exact
  // hand-off cursor: output produced during this request is replayed by the
  // native aggregate poller or by the browser EventSource without duplication.
  let initial;
  try {
    initial = await loadConsoleLog(id);
  } catch (error) {
    if (serial === _streamSerial) appendLogLine(`(log unavailable — ${error.message || "job output could not be loaded"})`, "dim");
    updateFooter();
    return;
  }
  if (serial !== _streamSerial) return;
  for (const line of initial.lines) appendLogLine(line);
  if (initial.gap) appendLogLine("⚠ earlier output is unavailable (the log was truncated)", "warn");
  if (initial.lineTruncated) appendLogLine("⚠ one or more output lines were clipped to the log line limit", "warn");
  if (initial.omitted) appendLogLine(
    initial.probeFailed
      ? "⚠ the display cap was reached; further output is available in the job log (availability could not be checked)"
      : `⚠ display capped at ${CONSOLE_LOG_CAP.toLocaleString()} lines; further output is available in the job log`,
    "warn",
  );
  log.scrollTop = log.scrollHeight;
  const current = S.jobs.find(j => j.id === id);
  const status = initial.status || current.status;
  if (current && status !== "running") {
    current.status = status;
    current.exit_code = initial.exitCode;
    updateBadge();
    renderConsoleTabs();
    updateFooter();
    if (status === "ok") appendLogLine("✓ done", "ok");
    else if (status === "fail") appendLogLine(`✕ exited with code ${initial.exitCode ?? "?"}`, "err");
    else if (status === "killed") appendLogLine("✕ stopped", "warn");
    return;
  }

  appendLogLine("▸ running — started " + new Date((job.ts || Date.now() / 1000) * 1000).toLocaleTimeString() + " (output streams in below as it happens)", "dim");
  const subscription = jobs.subscribe(id, {
    after: initial.nextSeq,
    onLine: line => {
      if (serial !== _streamSerial) return;
      appendLogLine(line.s);
      const logEl = $("#console-log");
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
    },
    onGap: () => {
      if (serial === _streamSerial) appendLogLine("⚠ earlier output is unavailable (the log was truncated)", "warn");
    },
    onError: error => {
      if (serial === _streamSerial) appendLogLine(`⚠ output stream: ${error.message || error}`, "warn");
    },
    onDone: done => {
      if (serial !== _streamSerial) return;
      if (S.jobSub === subscription) S.jobSub = null;
      const finished = S.jobs.find(j => j.id === id);
      if (finished) { finished.status = done.status; finished.exit_code = done.exit_code; }
      updateBadge();
      renderConsoleTabs();
      updateFooter();
      if (done.status === "ok") { appendLogLine("", null); appendLogLine("✓ done", "ok"); }
      else if (done.status === "fail") { appendLogLine("", null); appendLogLine(`✕ exited with code ${done.exit_code ?? "?"}`, "err"); }
      else if (done.status === "killed") { appendLogLine("", null); appendLogLine("✕ stopped", "warn"); }
    },
  });
  if (serial === _streamSerial) S.jobSub = subscription;
  else subscription.close();
  updateFooter();
}


export function appendLogLine(text, cls) {
  const log = $("#console-log");
  if (!log) return;
  let c = cls;
  if (!c) {
    if (/traceback|error:|exception|failed to start|not found/i.test(text)) c = "err";
    else if (/warn/i.test(text)) c = "warn";
    else if (/^(✓|\[ok\]|Done!|Generated PDF|Offset PDF|Calibration PDF:|  \[ok\]|Conversion complete)/i.test(text)) c = "ok";
    else if (/^(using|loaded|loaded |applying|converting|page \d)/i.test(text)) c = "info";
    else if (/^[\$\(]/.test(text)) c = "dim";
  }
  const ln = el("span", { class: `ln ${c ? "hl" : ""}` });
  if (text) ln.append(el("span", { class: `ln-${c || "plain"}` }, text));
  log.append(ln);
  if (log.children.length > 1500) log.children[0].remove();
}


export function updateFooter() {
  const f = $("#console-foot");
  f.innerHTML = "";
  const job = S.jobs.find(j => j.id === S.activeJobId);
  if (!job) return;
  f.append(
    el("span", { class: `statusdot ${job.status}` }, job.status),
    job.exit_code != null ? el("span", { class: "mono" }, `exit ${job.exit_code}`) : null,
    el("span", { class: "mono" }, fmtTs(job.ts)),
    el("span", { class: "grow" }),
    (() => { const dc = displayCmd(job.cmd, job.kind); return el("span", { class: "mono", title: dc }, truncate(dc, 90)); })(),
    (() => {
      const row = repoRowForKind(job.kind);
      const managed = row && row.mode === "managed";
      const left = (job.outputs || []).filter(Boolean).length;
      if (job.status === "ok" && managed && left)
        return el("button", { class: "btn btn-ghost btn-sm", title: "Copy the output to a folder of your choice (system save dialog).",
          onclick: () => moveJobToMyFiles(job) }, ico("folder"), left > 1 ? `Move to my files… (${left})` : "Move to my files…");
      if (managed)
        return el("button", { class: "btn btn-ghost btn-sm", disabled: "",
          title: "The output stays in the app's private working area; when the run is done you can move it out." },
          ico("folder"), "In the app area");
      return el("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
        try {
          const r = await revealPath(cwdOf(job));
          if (r.ok) toast("ok", "Opened folder in your file manager"); else toast("warn", r.errors?.[0] || "Could not reveal folder");
        } catch (error) {
          toast("warn", error?.message || "Could not reveal folder");
        }
      } }, ico("folder"), "Reveal folder");
    })(),
    el("button", { class: "btn btn-ghost btn-sm", onclick: () => { if (job.status === "running") jobs.kill(job.id).then(() => toast("warn", "Stopping…")).catch(e => toast("err", e.message || "Could not stop job")); } }, ico("stop"), "Stop"),
  );
}


export function cwdOf(job) {
  const m = (job.cmd || "").match(/^(\S+)\s+(\S+\.py)/);
  const scm = S.info?.scm?.path, ex = S.info?.extras?.path;
  if (job.kind.startsWith("fetch:")) return scm ? scm + "/game/front" : "";
  if (job.kind === "create_pdf" || job.kind === "offset_pdf" || job.kind === "clean_up") return scm || "";
  if (job.kind === "calibration") return scm ? scm + "/calibration" : "";
  if (job.kind === "dxf_single" || job.kind === "dxf_batch" || job.kind === "dxf_list") return scm ? scm + "/cutting_templates" : "";
  if (job.kind.startsWith("extras_")) return ex || "";
  return scm || "";
}


export function truncate(s, n) { return s && s.length > n ? "…" + s.slice(-n + 1) : s; }

/* “Move to my files…” — a managed run's artifacts (the create/offset PDFs,
   the calibration sheets) live in the app's private working area, so the
   console offers to carry them out through the native OS save panel.
   App window only: in a plain browser the save dialog doesn't exist. */


export function moveJobToMyFiles(job) {
  const outs = (job.outputs || []).filter(Boolean);
  if (!outs.length) return;
  if (!nativePick.canSave()) {
    toast("warn", "“Move to my files…” needs the app window — it opens the system save dialog.");
    return;
  }
  const next = outs[0];
  nativePick.pickSave(next.split(/[\\/]/).pop()).then(dest => {
    if (!dest) return; // cancelled
    api("/api/files/save", { src: next, dest })
      .then(r => {
        if (r.ok) {
          toast("ok", `Saved “${r.name}” to your chosen location.`);
          job.outputs = outs.slice(1);
          setRevealButtons();
        } else toast("warn", (r.errors && r.errors[0]) || "Could not save the file.");
      })
      .catch(() => toast("warn", "Could not save the file."));
  }).catch(e => toast("warn", `The save dialog failed to open (${e.message || "unknown error"}).`));
}


export function setRevealButtons() {
  const job = S.jobs.find(j => j.id === S.activeJobId);
  const btn = $("#console-reveal");
  if (!job || !btn) return;
  const row = repoRowForKind(job.kind);
  const managed = row && row.mode === "managed";
  const left = (job.outputs || []).filter(Boolean).length;
  const movable = job.status === "ok" && managed && left;
  btn.innerHTML = "";
  btn.append(ico("folder"));
  if (movable) {
    btn.append(left > 1 ? `Move to my files… (${left})` : "Move to my files…");
    btn.title = "Copy this run's output to a folder of your choice (system save dialog).";
    btn.disabled = false;
  } else if (managed) {
    btn.append("In the app area");
    btn.title = "This run's files stay in the app's private working area; once the run is done, “Move to my files…” brings them out.";
    btn.disabled = true;
  } else {
    btn.append("Reveal");
    btn.title = "Reveal output location";
    btn.disabled = false;
  }
}


/* bind console buttons (once) */
export function bindConsole() {
  $("#console-close").onclick = () => {
    const c = $("#console");
    c.classList.add("closed");
    closeStream();
    setTimeout(() => { c.hidden = true; }, 220);
  };
  $("#console-copy").onclick = async () => {
    const log = $("#console-log");
    const txt = $$(".ln", log).map(n => n.textContent).join("\n");
    await navigator.clipboard?.writeText(txt);
    toast("ok", "Log copied");
  };
  $("#console-reveal").onclick = async () => {
    const job = S.jobs.find(j => j.id === S.activeJobId);
    if (!job) return;
    const row = repoRowForKind(job.kind);
    if (job.status === "ok" && row && row.mode === "managed" && (job.outputs || []).length) {
      moveJobToMyFiles(job);
      return;
    }
    try {
      const r = await revealPath(cwdOf(job));
      if (r.ok) toast("ok", "Opened in your file manager"); else toast("warn", r.errors?.[0] || "Could not reveal");
    } catch (error) {
      toast("warn", error?.message || "Could not reveal");
    }
  };
  $("#console-kill").onclick = () => {
    if (!S.activeJobId) return;
    jobs.kill(S.activeJobId).then(r => r.ok ? toast("warn", "Stopping…") : null)
      .catch(e => toast("err", e.message || "Could not stop job"));
  };
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#console").hidden && !$("#console").classList.contains("closed")) $("#console-close").click();
    if (e.key === "c" && (e.metaKey || e.ctrlKey) && e.shiftKey) { e.preventDefault(); toggleConsole(); }
  });
}


export function startJobsPoll() {
  setInterval(() => { if (S.jobs.some(j => j.status === "running")) refreshJobs(); }, 4000);
}

document.addEventListener("DOMContentLoaded", async () => {
  bindNav();
  bindConsole();
  iconize(document);
  // initial theme before info loads (avoid flash) — and the simple-mode nav
  // collapse, from the same early settings read, so the first paint is already
  // in the right shape
  try {
    const s = await api("/api/settings");
    document.documentElement.dataset.theme = s.theme || "dark";
    const simple = (s.ui_mode || "advanced") === "simple";
    document.body.classList.toggle("mode-simple", simple);
    $$(".mode-switch .ms-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === (simple ? "simple" : "advanced")));
    if (simple) $$("#nav .nav-sep").forEach(sep => {
      let n = sep.nextElementSibling, any = false;
      while (n && !n.classList.contains("nav-sep")) {
        if (n.classList.contains("nav-item") && !n.hasAttribute("data-simple-hide")) { any = true; break; }
        n = n.nextElementSibling;
      }
      sep.classList.toggle("hide", !any);
    });
  } catch { }
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (document.documentElement.dataset.theme || "dark")));
  // the first API call can fail transiently (server still starting up, or
  // WSL2's per-connection localhost proxy hiccuping) — retry a few times
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await new Promise(r => setTimeout(r, 800 + 700 * attempt));
    try {
      await refreshInfo();
      bootPage();
      startJobsPoll();
      startPrepWatcher();
      return;
    } catch (e) {
      if (attempt === 3) showBootFailure(e);
    }
  }
});
