/* nav — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { toggleConsole, refreshJobs } from "./console.js";import { refreshInfo } from "./info.js";import { $, $$, PAGES, S, iconize, openUrl, toast } from "./core.js";import { defaultArgs, restoreArgs } from "./forms.js";import { setSettings } from "./settings-transport.js";
export function setNav(page) {
  $$("#nav .nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  $("#topbar-title").textContent = {
    preparing: "Getting ready", history: "Job history", fetch: "Fetch card art", pdf: "Create PDF", offset: "Offset & calibration",
    templates: "Cutting templates", extras: "Extras: MTG & Sorcery", sizes: "Sizes & layouts",
    utilities: "Utilities", settings: "Settings",
  }[page] || page;
  // The welcome card asks for the app chrome to stand back so the screen it
  // belongs to is the whole view. This runs on every navigation and before the
  // page renders, so leaving that screen restores the top bar without the page
  // having to remember to. See pages/preparing.js.
  document.body.classList.remove("setup-welcome");
  S.page = page;
}


// Each page has a real URL route (/pdf, /settings, …; job history is /) so
// refreshing stays on the page and the browser back/forward buttons work.
// The site root is the landing page, which is mode-aware: advanced opens on
// the job history, simple keeps to the workflow and opens on the first step.
export function rootPage() {
  return uiMode() === "simple" ? "fetch" : "history";
}


export function pageFromPath() {
  const p = location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
  if (p === "") return rootPage();
  // The preparation screen is session-transient, never a bookmarkable route.
  if (p === "preparing") return null;
  return p in PAGES ? p : null;
}


export function go(page, prefill, { push = true, anim = true } = {}) {
  if (prefill) applyPrefill(page, prefill);
  setNav(page);
  const pageEl = $("#page");
  pageEl.innerHTML = "";
  const content = PAGES[page](pageEl);
  if (content) {
    pageEl.append(content);
    if (typeof content.__patch === "function") content.__patch();
  }
  iconize(pageEl);
  // anim: false for internal re-renders (e.g. the prep watcher) — a silent
  // state update must not pulse the whole page like a navigation would
  if (anim && pageEl.firstElementChild) pageEl.firstElementChild.classList.add("page-anim");
  $(".page-scroll").scrollTop = 0;
  if (push) {
    const path = page === "history" ? "/" : "/" + page;
    if (location.pathname !== path) history.pushState({ page }, "", path);
  }
  // the job history list is rebuilt by its render — fill it now so arriving
  // there always shows the current jobs (the poll only repaints on change)
  if (page === "history") refreshJobs(true);
}

window.addEventListener("popstate", () => {
  const page = pageFromPath();
  // simple mode: a history entry for a page that is hidden there lands on fetch
  if (uiMode() === "simple" && page && !SIMPLE_PAGES.includes(page)) return go("fetch", null, { push: false });
  go(page || rootPage(), null, { push: false });
});


// Initial load: honor the URL we were given (refreshing /pdf must show PDF).
// Normalizes the path (trailing slash, unknown page) without adding history
// entries, so a hard refresh doesn't pollute the back button.
export function bootPage() {
  let page = pageFromPath();
  if (uiMode() === "simple" && !SIMPLE_PAGES.includes(page)) {
    page = "fetch"; // in simple mode the app opens on the first workflow step
  }
  const path = page ? (page === "history" ? "/" : "/" + page) : "/";
  if (location.pathname !== path) history.replaceState({ page: page || rootPage() }, "", path);
  go(page || rootPage(), null, { push: false });
}


export function applyPrefill(page, prefill) {
  if (!prefill) return;
  // Job history: a job's own args are filtered through the manifest and land
  // in that kind's form slot, so the page opens on exactly what the job ran.
  if (prefill.kind && S.manifest?.[prefill.kind]?.page === page) {
    S.forms[prefill.kind] = restoreArgs(prefill.kind, prefill.args || {});
  } else if (page === "pdf" && prefill.card_size) {
    S.forms.create_pdf = defaultArgs("create_pdf");
    S.forms.create_pdf.card_size = prefill.card_size;
  }
  if (page === "fetch" && prefill.plugin) S.plugin = prefill.plugin;
}


export function bindNav() {
  // Every nav item is a route except the documentation link, which is a real
  // outbound URL: it goes through the same openUrl action as every other
  // "open in the browser" affordance (native bridge in the app window, the
  // compatibility route in a browser).
  $$("#nav .nav-item").forEach(a => {
    if (a.dataset.docs) a.onclick = () => openUrl(a.dataset.docs, "the silhouette-card-maker docs");
    else a.onclick = () => go(a.dataset.page);
  });
  $$(".mode-switch .ms-btn").forEach(b => b.onclick = () => setUiMode(b.dataset.mode));
  $("#btn-console").onclick = toggleConsole;
  $$("#theme-switch .ts-btn").forEach(b => b.onclick = () => setTheme(b.dataset.theme));
}


let themeSelection = 0;
let confirmedTheme = null;
let themeWriteQueue = Promise.resolve();


function applyTheme(s, theme) {
  s.theme = theme;
  document.documentElement.dataset.theme = theme;
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === theme));
}


export function setTheme(theme) {
  const s = S.info?.settings || {};
  const previous = s.theme || document.documentElement.dataset.theme || "dark";
  if (confirmedTheme === null) confirmedTheme = previous;
  const selection = ++themeSelection;
  applyTheme(s, theme);
  // Paint immediately, but serialize persistence so browser POSTs cannot land
  // out of order. Each completed write becomes the confirmed rollback point;
  // an old failure is intentionally silent when a newer click owns the UI.
  themeWriteQueue = themeWriteQueue
    .then(() => setSettings({ theme }))
    .then(() => { confirmedTheme = theme; }, error => {
      if (selection !== themeSelection) return;
      applyTheme(s, confirmedTheme || previous);
      toast("err", error?.message || "Couldn't save the theme. The previous theme was restored.");
    });
  // The rejection handler above consumes write failures so the next queued
  // selection still runs and no click creates an unhandled promise.
  void themeWriteQueue;
}

/* show commands the way a user would run them: a bare "python" interpreter
   (never the app's private one by absolute path) and repo-relative script
   paths. Real paths stay in job.cmd for the engine. */


export const SIMPLE_PAGES = ["history", "fetch", "pdf", "offset", "settings"];   // what the nav keeps in simple mode


export function uiMode() {
  const s = S.info && S.info.settings;
  return (s && (s.ui_mode || "simple")) === "simple" ? "simple" : "advanced";
}


/* One place owns the simple/advanced sidebar shape: the body class, which
   section separators still have a visible item, and the mode-switch state.
   It is a pure DOM function (it never reads S.info), so the early boot path
   can call it before the settings read finishes. The separator answer comes
   from each section's own items rather than from document order — that is
   what lets Job history sit in the advanced Cutting/Reference gap and still
   read as its own row in simple mode, where both sections are hidden. */
export function applySimpleNav(simple) {
  document.body.classList.toggle("mode-simple", simple);
  $$("#nav .nav-sep").forEach(sep => {
    const section = sep.dataset.section;
    const any = !!section && $$(`#nav .nav-item[data-section="${section}"]`)
      .some(a => !(simple && a.hasAttribute("data-simple-hide")));
    sep.classList.toggle("hide", !any);
  });
  $$(".mode-switch .ms-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === (simple ? "simple" : "advanced")));
}


export function syncUiMode() {
  applySimpleNav(uiMode() === "simple");
}


export async function setUiMode(mode) {
  const cur = uiMode();
  if (!mode || mode === cur) return;
  // Save first, commit locally only once the server has confirmed — a
  // settings GET landing before the commit (or the one fired just below)
  // returning the previous value must not roll the switch back, or the
  // user would have to click twice.
  try {
    await setSettings({ ui_mode: mode });
  } catch {
    toast("err", "Couldn't save the interface setting. Still in " + cur + " mode.");
    return;
  }
  S.info.settings.ui_mode = mode;
  syncUiMode();
  const page = S.page || "history";
  if (mode === "simple" && !SIMPLE_PAGES.includes(page)) {
    go("fetch");
    toast("ok", "Simple: just the essentials. Fetch art, make the PDF, and calibrate your printer.");
  } else {
    if (page === "pdf" || page === "offset" || page === "settings") go(page, null, { push: false }); // re-render mode-specific forms/cards
    toast("ok", mode === "simple" ? "Simple: navigation shows only the essentials." : "Advanced: every page and control is available.");
  }
  // keepForms: a mode switch is a layout change, not a content change — the
  // values sitting in the visible form must survive, and if anything else did
  // clear S.forms[kind] in the meantime, the form card's afterFormChange
  // re-owns the slot on the next edit (so the preview can't wedge dead).
  refreshInfo({ keepForms: true }).catch(() => {});   // keep jobs/status/prep fresh for the re-render
}
