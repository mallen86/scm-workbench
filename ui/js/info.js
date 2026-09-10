/* info — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { refreshJobs, startJobsPoll } from "./console.js";import { $, $$, S, api, el, ico } from "./core.js";import { bootPage, syncUiMode } from "./nav.js";import { _prepTimer, prepActive, startPrepWatcher } from "./prep.js";import { getTauriInvoke } from "./transport.js";
/* ================================= bootstrap =============================== */

export async function refreshInfo({ keepForms = false, jobs = true } = {}) {
  S.info = await api("/api/info");
  S.manifest = await api("/api/manifest");
  document.documentElement.dataset.theme = S.info.settings.theme || "dark";
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (S.info.settings.theme || "dark")));
  syncUiMode();
  if (!keepForms) S.forms = {};
  if (jobs) refreshJobs();
  // if preparation started *after* this page booted (an update job, a
  // re-clone), start watching for it — the bar must appear without reload
  if (prepActive() && !_prepTimer) startPrepWatcher();
}


// Shown when the API is still unreachable after several attempts (the server
// died, or a flaky WSL2 localhost hop) — and kept on screen, with a Retry
// button, until the connection works. Unlike a toast, it can't be missed.
export function showBootFailure(e) {
  const raw = (e && e.message) || "";
  const invoke = getTauriInvoke();
  const packaged = !!invoke;
  let message;
  if (packaged) {
    const reason = /timeout/i.test(raw) ? "timed out" :
      /unavailable|closed|stopped/i.test(raw) ? "is unavailable" :
      /invoke|native|failed/i.test(raw) ? "failed to answer a native request" :
      "failed to answer";
    message = `SCM Workbench's bundled worker ${reason}. This is an app startup problem, not a network problem.`;
  } else {
    const msg = /fetch|network|failed/i.test(raw) ? "no network response from the API" : raw;
    message = `Can't reach the Workbench API at ${location.origin} (${msg}). Is the server still running? ` +
      "If this tab is open in a Windows browser (WSL2), use the \u201CWindows host\u201D URL printed by the server.";
  }
  const bar = el("div", { class: "banner err" },
    el("span", { class: "b-ico" }, ico("alert")),
    el("span", { class: "grow" }, message),
    el("button", { class: "btn sm", onclick: async () => {
      try {
        bar.remove();
        if (packaged) {
          await invoke("wb_restart");
          return;
        }
        await refreshInfo();
        bootPage();
        startJobsPoll();
      } catch (e2) {
        showBootFailure(e2);
      }
    } }, "Retry"));
  $("#page").replaceChildren(bar);
}
