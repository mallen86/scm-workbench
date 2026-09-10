/* The app-update progress strip: the same visual language as the
   repo-prep bar (prep.js) — a fixed seat above the sidebar footer's
   divider, a stage label, a numbers line, and a bar that is either
   determinate (done/total from the job's throttled progress) or sliding
   indeterminate while a stage has no cheap counter (fetch / extract /
   install). It patches in place and never re-renders the page, so it
   works identically in simple and advanced mode (the console has no
   place to live in simple mode — which is exactly where this is
   needed), and it follows the user if they navigate away mid-update. */
import { $, S, el } from "./core.js";import { jobs } from "./jobs.js";const UPDATE_STAGES = {
  fetch: "fetching the release…",
  download: "downloading the new version",
  extract: "unpacking the new build",
  install: "swapping it into place",
  relaunch: "reopening the new version…",
};
let _updStrip = null;
let _updTimer = null;
let _updLastDone = null;
let _updLastAt = null;

function updStrip() {
  if (_updStrip && _updStrip.isConnected) return _updStrip;
  const foot = $(".sidebar-foot");
  if (!foot) return null;
  _updStrip = el("div", { class: "repoprog", id: "updateprog", "aria-live": "polite" },
    el("div", { class: "rp-head" }, "Updating SCM Workbench…"),
    el("div", { class: "rp-row" },
      el("div", { class: "rp-label" }),
      el("div", { class: "rp-meta mono" }),
      el("div", { class: "rp-bar indet" }, el("div", { class: "rp-fill" }))));
  foot.before(_updStrip);
  return _updStrip;
}

export function stopUpdateStrip() {
  if (_updTimer) { clearInterval(_updTimer); _updTimer = null; }
  if (_updStrip) { _updStrip.remove(); _updStrip = null; }
  _updLastDone = null; _updLastAt = null;
}

function updateStrip(job) {
  const strip = updStrip();
  if (!strip) return;
  const row = strip.children[1];
  const [label, meta, bar] = row.children;
  const p = (job && job.progress) || {};
  const stage = p.stage || (job && job.status === "running" ? "fetch" : null);
  label.textContent = (job?.title || "Updating SCM Workbench") + "  —  " + (UPDATE_STAGES[stage] || "working");
  let text = "";
  if (stage === "download" && p.total >= 1000) {
    const doneMB = (p.done || 0) / 1e6, totalMB = p.total / 1e6;
    const pct = Math.min(100, Math.round(100 * (p.done || 0) / p.total));
    text = (totalMB >= 10 ? Math.round(doneMB) : doneMB.toFixed(1)) + "/" +
           (totalMB >= 10 ? Math.round(totalMB) : totalMB.toFixed(1)) + " MB (" + pct + "%)";
    if (_updLastDone !== null && p.done > _updLastDone && _updLastAt) {
      const dt = Math.max(0.5, (Date.now() - _updLastAt) / 1000);
      const bps = (p.done - _updLastDone) / dt;
      if (bps > 1000) text += "  ·  " + (bps >= 1e6 ? (bps / 1e6).toFixed(1) + " MB/s" : Math.round(bps / 1e3) + " KB/s");
    }
    _updLastDone = p.done; _updLastAt = Date.now();
    bar.classList.remove("indet");
    bar.firstElementChild.style.width = Math.min(100, Math.round(100 * (p.done || 0) / p.total)) + "%";
  } else {
    _updLastDone = null; _updLastAt = null;
    bar.classList.add("indet");
  }
  meta.textContent = text;
}

async function finishUpdateStrip(job) {
  const strip = updStrip();
  if (!strip) return;
  const head = strip.children[0];
  const row = strip.children[1];
  const [label, meta, bar] = row.children;
  const failed = job.status === "fail" || job.status === "killed";
  strip.classList.toggle("failed", failed);
  strip.classList.toggle("done", job.status === "ok");
  if (job.status === "handoff") {
    head.textContent = "SCM Workbench update ready";
    meta.textContent = "Download complete. The app will close, install, and reopen automatically.";
    bar.classList.remove("indet");
    bar.firstElementChild.style.width = "100%";
    const restartAt = Number(job.progress?.restart_at);
    const paintCountdown = () => {
      if (_updStrip !== strip || !strip.isConnected) return;
      const seconds = Number.isFinite(restartAt)
        ? Math.max(0, Math.ceil(restartAt - Date.now() / 1000)) : 0;
      label.textContent = seconds > 0
        ? `Restarting in ${seconds} second${seconds === 1 ? "" : "s"}…`
        : "Restarting now…";
    };
    paintCountdown();
    if (Number.isFinite(restartAt)) _updTimer = setInterval(paintCountdown, 250);
    return;
  }
  bar.classList.remove("indet");
  bar.firstElementChild.style.width = "100%";
  if (job.status === "ok") {
    head.textContent = "SCM Workbench update complete";
    label.textContent = "Update finished";
    meta.textContent = "The new version is ready.";
    return;
  }
  head.textContent = failed ? "SCM Workbench update failed" : "SCM Workbench update stopped";
  label.textContent = job.status === "killed" ? "Update stopped" : "Update failed";
  let detail = job.status === "killed" ? "The update was stopped." : "The update did not finish.";
  try {
    const result = await jobs.log(job.id);
    const lines = (result.lines || []).map(line => String(line).trim()).filter(Boolean);
    const specific = [...lines].reverse().find(line => /^!\s+/.test(line)) ||
      [...lines].reverse().find(line => !/^✕\s+exited\b/.test(line) && !/^\$\s+/.test(line));
    if (specific) detail = specific.replace(/^!\s+/, "");
  } catch { /* the stable terminal status is still useful without its log */ }
  if (_updStrip === strip && strip.isConnected) meta.textContent = detail;
}

export function startUpdateStrip(jobId) {
  stopUpdateStrip();
  const tick = async () => {
    let rows;
    try { rows = (await jobs.list()).jobs || []; } catch { return; }
    const j = rows.find(x => x.id === jobId) || rows.find(x => x.kind === "update");
    if (!j) return;
    updateStrip(j);
    if (j.status !== "running") {
      if (_updTimer) { clearInterval(_updTimer); _updTimer = null; }
      await finishUpdateStrip(j);
    }
  };
  tick();
  _updTimer = setInterval(tick, 2500);
}
