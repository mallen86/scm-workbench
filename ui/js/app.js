/* ==========================================================================
   SCM Workbench UI — entry point
   The webview loads exactly one file: this one (see ui/index.html). It
   imports the rest of the modules — core, navigation, forms, jobs console,
   repo-prep progress, info refresh and every page — then wires up the
   DOMContentLoaded bootstrap.

   Modules in this directory:
     core.js      DOM helpers, icons, shared state S, api()/toast/modal, PAGES
     nav.js       routing (go/bootPage/setNav), theme + simple/advanced mode
     forms.js     manifest-driven forms: option renderer, cards, preview, run
     console.js   jobs list, the live console pane, footer actions
     prep.js      repo clone/update progress rows + watcher
     info.js      refreshInfo() and the boot-failure banner
     pages/*.js   one module per page (dashboard, fetch, pdf, offset, ...)
   ========================================================================== */
import { refreshInfo, showBootFailure } from "./info.js";import { bindNav, bootPage } from "./nav.js";import { bindConsole, startJobsPoll } from "./console.js";import { startPrepWatcher } from "./prep.js";import { iconize, $, $$ } from "./core.js";import "./pages/dashboard.js";
import "./pages/fetch.js";
import "./pages/pdf.js";
import "./pages/offset.js";
import "./pages/templates.js";
import "./pages/extras.js";
import "./pages/sizes.js";
import "./pages/utilities.js";
import "./pages/settings.js";

document.addEventListener("DOMContentLoaded", async () => {
  bindNav();
  bindConsole();
  iconize(document);
  // initial theme before info loads (avoid flash) — and the simple-mode nav
  // collapse, from the same early settings read, so the first paint is already
  // in the right shape
  try {
    const r = await fetch("/api/settings"); const s = await r.json();
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
