/* prep — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, S, el, toast } from "./core.js";import { refreshInfo } from "./info.js";import { go } from "./nav.js";
/* ================================ dashboard ================================ */

/* ---------------- repo prep state (first clone + updates) ------------------ */
export const STAGE_NAMES = {
  download: "downloading the newest snapshot",
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
// bootstrap. Later checks/updates keep using the dashboard/sidebar progress
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


// The dashboard hosts the prep rows natively (#repoprog in its card). On
// other pages — most importantly the simple-mode fetch landing, where a
// first boot happens with no dashboard in sight — the same rows live in a
// small fixed strip, so first-launch progress is never invisible.
function globalStrip() {
  let s = $("#repoprog-global");
  if (s) return s;
  const foot = $(".sidebar-foot");
  if (!foot) return null; // no chrome to host it (never, in practice)
  s = el("div", { class: "repoprog", id: "repoprog-global" },
    el("div", { class: "rp-head" }, "Preparing your managed copies …"));
  // Directly above the footer's divider: the divider (and the toggles under
  // it) never moves — the nav above is the flexible, scrolling part.
  foot.before(s);
  return s;
}

export function removeGlobalStrip() {
  const s = $("#repoprog-global");
  if (s) s.remove();
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
  // dashboard/sidebar progress remains compact and shows only active work.
  const showAll = native?.dataset.prepAll === "true";
  const rows = showAll
    ? (S.info.repos || [])
    : (S.info.repos || []).filter(r => r.progress || (S.info.server.active && !r.deployed));
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
    row.children[0].textContent = r.name + "  —  " + stage;
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
    if (!prepActive()) { removeGlobalStrip(); _prepTimer = null; return; }
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
            ? "Your repos are ready — every page is live."
            : "Setup did not finish — retry the missing repositories.");
        }
        go(S.page || "dashboard", null, { push: false, anim: false });
      } else {
        updatePrepRows();
      }
    } catch (e) { /* server briefly busy — the next tick retries */ }
  };
  if (prepActive()) tick();
}
