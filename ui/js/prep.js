/* prep — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, S, el, ico, toast } from "./core.js";import { refreshInfo } from "./info.js";import { jobs } from "./jobs.js";import { go } from "./nav.js";
/* ============================ repo preparation ============================ */

/* ---------------- repo prep state (first clone + updates) ------------------ */
export const STAGE_NAMES = {
  download: "downloading the latest snapshot",
  extract: "unpacking the files",
  fingerprint: "fingerprinting the files",
  update: "updating the changed files",
  apply: "applying the changes",
};


export function repoPrepRow(key) {
  return (S.info.repos || []).find(r => r.key === key) || null;
}


// A repo gates the pages that need it while it is unusable: not deployed yet
// (first clone still going) or a clone/update currently in flight.
export function repoReady(key) {
  const row = repoPrepRow(key);
  if (!row) return true;
  if (row.progress) return false;
  if (row.deployed) return true;
  return key === "scm" ? !!S.info.scm.found : !!S.info.extras.found;
}


export function prepActive() {
  if (!S.info || !S.info.server.is_packaged) return false;
  if (S.info.server.active) return true;
  if ((S.jobs || []).some(j => (j.kind === "repo_init" || j.kind === "repo_update") && j.status === "running")) return true;
  return (S.info.repos || []).some(r => !r.deployed || r.progress);
}


// The dedicated welcome screen is reserved for the packaged first-launch
// bootstrap. Later checks/updates keep using the sidebar progress
// UI, and standalone-browser startup remains unchanged.
export function firstBootPageNeeded() {
  // Keep the welcome page available after a failed first pass too. It owns
  // the retry action; requiring bootstrap.active here used to skip the page
  // precisely when a download had already failed.
  return !S.firstBootDismissed && !!S.info?.server?.is_packaged &&
    (S.info.repos || []).some(r => !r.deployed);
}


// Structural identity: only the card's VISIBILITY is structural ("busy" =
// something on screen, "done" = card must go). Row appearance, stage flips,
// counters and speed are all patched in place — the bars never leave, so the
// page never re-renders (or animates) while a download is in flight.
export function prepSignature() {
  if (!S.info) return "boot";
  const repos = S.info.repos || [];
  const active = S.info.server.active ? 1 : 0;
  const cardShown = active
    ? repos.some(r => !r.deployed)
    : repos.some(r => r.progress);
  return cardShown ? "busy" : "done";
}


export function fmtRate(bps) {
  if (!bps) return "";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " MB/s";
  return Math.round(bps / 1e3) + " KB/s";
}


export function fmtEta(sec) {
  if (!sec) return "";
  if (sec >= 60) return "~" + Math.round(sec / 60) + " min left";
  return "~" + sec + "s left";
}


// "412/564 MB (73%)  ·  6.4 MB/s  ·  ~24s left" — the numbers line per repo
export function prepMeta(r) {
  const p = r.progress || {};
  const bits = [];
  if (p.unit === "files" && p.total > 0) {
    // fingerprint/apply heartbeats count files, not bytes
    bits.push((p.done || 0) + "/" + p.total + " files (" + Math.min(100, Math.round(100 * (p.done || 0) / p.total)) + "%)");
  } else if (p.total >= 1000) {
    const doneMB = (p.done || 0) / 1e6, totalMB = p.total / 1e6;
    const pct = Math.min(100, Math.round(100 * (p.done || 0) / p.total));
    bits.push((totalMB >= 10 ? Math.round(doneMB) : doneMB.toFixed(1)) + "/" +
               (totalMB >= 10 ? Math.round(totalMB) : totalMB.toFixed(1)) + " MB (" + pct + "%)");
    if ((p.stage === "download" || p.stage === "apply") && p.speed) bits.push(fmtRate(p.speed));
    if ((p.stage === "download" || p.stage === "apply") && p.eta) bits.push(fmtEta(p.eta));
  } else if (p.total > 0) {
    bits.push(Math.round(100 * (p.done || 0) / p.total) + "%");
  } else if (p.done) {
    // total unknown (chunked download): still show how much has arrived
    bits.push(Math.round((p.done / 1e6) * 10) / 10 + " MB received");
    if ((p.stage === "download" || p.stage === "apply") && p.speed) bits.push(fmtRate(p.speed));
  }
  return bits.join("  ·  ");
}


// S.prows: per-repo handles into the on-screen bar rows (in-place patching).
export function ensurePrepRows(rows, container) {
  S.prows = S.prows || {};
  for (const r of rows) {
    if (S.prows[r.key] && S.prows[r.key].isConnected) continue;  // a page re-render replaced the strip — rebuild the row
    const row = el("div", { class: "rp-row" });
    row.append(
      el("div", { class: "rp-label" }),
      el("div", { class: "rp-meta mono" }),
      el("div", { class: "rp-bar" }, el("div", { class: "rp-fill" })));
    container.append(row);
    S.prows[r.key] = row;
  }
  const keys = new Set(rows.map(r => r.key));
  for (const k of Object.keys(S.prows)) if (!keys.has(k)) { S.prows[k].remove(); delete S.prows[k]; }
}


// The first-boot page hosts the prep rows natively (#repoprog in its card).
// On every other page — most importantly the simple-mode fetch landing, where
// a first boot happens with no such page in sight — the same rows live in a
// small fixed strip, so first-launch progress is never invisible.
function globalStrip() {
  let s = $("#repoprog-global");
  if (s) return s;
  const foot = $(".sidebar-foot");
  if (!foot) return null; // no chrome to host it (never, in practice)
  s = el("div", { class: "repoprog sidebar-note", id: "repoprog-global" },
    el("div", { class: "rp-head" }, "Preparing your managed copies"));
  // Directly above the footer's divider: the divider (and the toggles under
  // it) never moves — the nav above is the flexible, scrolling part.
  foot.before(s);
  return s;
}

export function removeGlobalStrip() {
  const s = $("#repoprog-global");
  if (s) s.remove();
}


// ---- a failed repo operation stays on screen ------------------------------
// Progress disappears the moment a repo operation ends, so a failure used to
// leave the sidebar empty and only a toast that fades. In simple mode the
// console is hidden, which made a failed update completely silent: no progress
// box, no error, nothing to act on. The box now stays and says what failed.
//
// S.jobs is newest first, so the first repo job seen per repo is the latest
// word on that repo: a later success clears the notice by itself, and a stale
// failure from an earlier session cannot outlive a successful retry.
export function repoFailures() {
  const seen = new Set();
  const failures = [];
  for (const job of (S.jobs || [])) {
    if (job.kind !== "repo_init" && job.kind !== "repo_update") continue;
    const key = (job.args || {}).repo || "";
    if (seen.has(key)) continue;
    seen.add(key);
    if (job.status === "fail" && job.id !== S.repoFailureDismissed) failures.push(job);
  }
  return failures.map(job => {
    const key = (job.args || {}).repo || "";
    const row = (S.info.repos || []).find(r => r.key === key);
    return { job, key, name: row ? row.name : (key || "managed repo") };
  });
}


// The job's own log already carries the reason (repo_sync prints one "error:"
// line and exits). Read it once per job and keep the most specific line: the
// console is not available in simple mode, so the box is the only place a
// person can see why it failed.
const _failureDetail = new Map();

async function loadFailureDetail(job) {
  if (_failureDetail.has(job.id)) return _failureDetail.get(job.id);
  let detail = "The last attempt did not finish.";
  try {
    const result = await jobs.log(job.id, { maxLines: 200 });
    const lines = (result?.lines || []).map(line => String(line).trim()).filter(Boolean);
    const picked = [...lines].reverse().find(line => /^!\s+/.test(line)) ||
                   [...lines].reverse().find(line => /^error/i.test(line)) ||
                   [...lines].reverse().find(line => !/^\$\s/.test(line));
    if (picked) detail = picked.replace(/^!\s+/, "").slice(0, 180);
  } catch { /* the notice still names the repo without the reason */ }
  _failureDetail.set(job.id, detail);
  return detail;
}


// Retry re-runs the exact operation that failed, through the same runner the
// Settings row uses (so the confirm text and args match). forms.js imports
// prep.js, so the import is dynamic to keep that edge one-way.
async function retryRepoJob(failure, button) {
  button.disabled = true;
  try {
    const { doRun } = await import("./forms.js");
    await doRun(failure.job.kind, null, { args: { repo: failure.key } });
  } catch (error) {
    toast("err", error?.message || "could not start the repository operation");
  } finally {
    button.disabled = false;
  }
}


// Render the failure notice into the shared sidebar box, replacing any stale
// progress rows so the box never shows a row beside an error.
function renderRepoFailures(container, failures) {
  for (const key of Object.keys(S.prows || {})) {
    if (!S.prows[key].isConnected) continue;
    S.prows[key].remove();
  }
  S.prows = {};
  const head = container.firstElementChild;
  if (head) head.textContent = failures.length > 1
    ? "Some managed repos need attention"
    : "A managed repo needs attention";
  for (const failure of failures) {
    let row = container.querySelector(`[data-repo-failure="${CSS.escape(failure.job.id)}"]`);
    if (!row) {
      row = el("div", { class: "rp-row rp-fail", "data-repo-failure": failure.job.id });
      row.append(
        el("div", { class: "rp-label" }, failure.name + "  |  " + (failure.job.kind === "repo_init" ? "download failed" : "update failed")),
        el("div", { class: "rp-meta" }, "Checking the log for the reason"));
      const actions = el("div", { class: "rp-actions" });
      const retry = el("button", { type: "button", class: "btn sm" }, ico("refresh"), "Retry");
      retry.onclick = () => retryRepoJob(failure, retry);
      const close = el("button", { type: "button", class: "btn sm ghost", title: "Dismiss this message", "aria-label": "Dismiss this message" }, ico("x"));
      close.onclick = () => { S.repoFailureDismissed = failure.job.id; updatePrepRows(); };
      actions.append(retry, close);
      row.append(actions);
      container.append(row);
      loadFailureDetail(failure.job).then(detail => {
        if (!row.isConnected) return;
        row.children[1].textContent = detail;
      });
    }
  }
}

function clearRepoFailures(container) {
  container.querySelectorAll("[data-repo-failure]").forEach(node => node.remove());
  const head = container.firstElementChild;
  if (head) head.textContent = "Preparing your managed copies";
}

function retargetProws(container) {
  for (const k of Object.keys(S.prows || {})) {
    const r = S.prows[k];
    if (r && r.isConnected && r.parentElement !== container) container.append(r);
  }
}

export function updatePrepRows() {
  const native = $("#repoprog");
  const container = native || globalStrip();
  if (!container) return;
  retargetProws(container);
  // The first-boot page keeps both repositories visible so a finished row
  // becomes a reassuring 100% “ready” row instead of disappearing. Existing
  // the sidebar strip remains compact and shows only active work.
  const showAll = native?.dataset.prepAll === "true";
  const rows = showAll
    ? (S.info.repos || [])
    : (S.info.repos || []).filter(r => r.progress || (S.info.server.active && !r.deployed));
  // A settled failure owns the sidebar box until the user dismisses it or a
  // later attempt succeeds. The first-boot page keeps its own rows instead:
  // it already shows the retry action for a first pass that did not finish.
  const failures = native ? [] : repoFailures();
  if (failures.length) {
    ensurePrepRows([], container);
    renderRepoFailures(container, failures);
    return;
  }
  if (!native) clearRepoFailures(container);
  ensurePrepRows(rows, container);
  for (const r of rows) {
    const row = S.prows[r.key];
    if (!row) continue;
    const p = r.progress || {};
    const ready = !r.progress && !!r.deployed;
    const waiting = !r.progress && !r.deployed && !!S.info.server.active;
    const det = ready || p.total > 0;
    const pct = ready ? 100 : (p.total > 0
      ? Math.min(100, Math.round(100 * (p.done || 0) / p.total)) : 0);
    const stage = ready ? "ready" : waiting ? "waiting to start" :
      (STAGE_NAMES[p.stage] || p.stage || "setup did not finish");
    row.children[0].textContent = r.name + "  |  " + stage;
    row.children[1].textContent = ready ? "Downloaded and ready" : prepMeta(r);
    const bar = row.children[2], fill = bar.firstElementChild;
    // Unknown-length active downloads have progress but no total. They need
    // the same cycling treatment as a waiting row, not an empty static bar.
    const indeterminate = !det && !ready;
    bar.classList.toggle("indet", indeterminate);
    // Do not leave an inline 0% width on the fill: inline styles beat the
    // stylesheet's .indet width and make the cycling animation invisible.
    fill.style.width = indeterminate ? "" : pct + "%";
  }
  if (!native && !rows.length) removeGlobalStrip();
}


export let _prepTimer = null;


export function stopPrepWatcher() {
  if (_prepTimer) clearTimeout(_prepTimer);
  _prepTimer = null;
}


// While any repo is still being prepared, poll /api/info: counters patch the
// bars in place; only a structural change (stage flip, repo ready) re-renders
// the page — which is what unlocks the waiting run buttons.
export function startPrepWatcher() {
  stopPrepWatcher();
  const tick = async () => {
    if (!prepActive()) {
      // Progress is over, but a failed operation must not vanish with it: the
      // last row render is what turns the box into the failure notice.
      updatePrepRows();
      if (!repoFailures().length) removeGlobalStrip();
      _prepTimer = null;
      return;
    }
    const before = prepSignature();
    _prepTimer = setTimeout(tick, 750);
    try {
      await refreshInfo({ keepForms: true, jobs: false });
      const now = prepSignature();
      if (now !== before) {
        if (now === "done") {
          const repos = S.info.repos || [];
          const allReady = repos.length > 0 && repos.every(r => r.deployed);
          toast(allReady ? "ok" : "warn", allReady
            ? "Your repos are ready. Every page is live."
            : "Setup did not finish. Retry the missing repositories.");
        }
        go(S.page || "history", null, { push: false, anim: false });
      } else {
        updatePrepRows();
      }
    } catch (e) { /* server briefly busy — the next tick retries */ }
  };
  // Paint from the state already in hand before the first refresh round-trip,
  // so a start that arrives with progress (the usual case) shows the box at
  // once instead of after one fetch.
  if (prepActive()) { updatePrepRows(); tick(); }
}
