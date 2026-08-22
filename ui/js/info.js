/* info — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { refreshJobs, startJobsPoll } from "./console.js";
import { $, $$, S, api, el, ico } from "./core.js";
import { bootPage, syncUiMode } from "./nav.js";
import { _prepTimer, prepActive, startPrepWatcher } from "./prep.js";

/* ================================= bootstrap =============================== */

export async function refreshInfo({ keepForms = false, jobs = true } = {}) {
  S.info = await api("/api/info");
  S.manifest = await api("/api/manifest");
  document.documentElement.dataset.theme = S.info.settings.theme || "dark";
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (S.info.settings.theme || "dark")));
  syncUiMode();
  // pills
  const dS = $("#dot-scm"); dS.classList.toggle("ok", S.info.scm.found);
  const dE = $("#dot-extras"); dE.classList.toggle("ok", S.info.extras.found); dE.classList.toggle("warn", !S.info.extras.found);
  $("#chip-python").textContent = "python " + S.info.server.python;
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
  const msg = /fetch|network|failed/i.test(raw) ? "no network response from the API" : raw;
  const bar = el("div", { class: "banner err" },
    el("span", { class: "b-ico" }, ico("alert")),
    el("span", { class: "grow" },
      `Can't reach the Workbench API at ${location.origin} (${msg}). Is the server still running? ` +
      "If you opened this tab from a Windows browser (WSL2), use the \u201CWindows host\u201D URL the server printed in its console."),
    el("button", { class: "btn sm", onclick: async () => {
      try {
        bar.remove();
        await refreshInfo();
        bootPage();
        startJobsPoll();
      } catch (e2) {
        showBootFailure(e2);
      }
    } }, "Retry"));
  $("#page").replaceChildren(bar);
}
