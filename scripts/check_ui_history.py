#!/usr/bin/env python3
"""Static and Node contract for the dedicated Job history page.

Covers the parts that are easy to break silently:

* the page is registered and routed in both interface modes;
* the jobs poll repaints the list in place instead of the page re-rendering;
* a job's recorded args are filtered through the manifest before they reach a
  form slot (unknown keys dropped, values coerced, defaults preserved);
* every manifest kind maps back to the page that owns its settings;
* the new user-visible copy keeps the repository's no-dash rule.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
JS = UI / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    page_path = JS / "pages" / "history.js"
    history_path = JS / "job-history.js"
    nav_path = JS / "nav.js"
    forms_path = JS / "forms.js"
    console_path = JS / "console.js"
    app_path = JS / "app.js"
    index_path = UI / "index.html"
    css_path = UI / "theme.css"
    for path in (page_path, history_path, nav_path, forms_path, console_path, app_path, index_path, css_path):
        if not path.is_file():
            return fail(f"{path.relative_to(ROOT)} is missing")

    page = page_path.read_text(encoding="utf-8")
    history = history_path.read_text(encoding="utf-8")
    nav = nav_path.read_text(encoding="utf-8")
    forms = forms_path.read_text(encoding="utf-8")
    console = console_path.read_text(encoding="utf-8")
    app = app_path.read_text(encoding="utf-8")
    index = index_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    # --- static wiring -----------------------------------------------------
    for required in ('export function jobPageFor(', 'export function jobPrefill(',
                     'export function jobRestorable(', 'export function jobIcon(',
                     'export function fmtJobTs(', 'export function openJobSettings(',
                     'export function jobHistoryRow(', 'export function renderJobHistory('):
        if required not in history:
            return fail(f"job-history.js is missing {required}")
    if "PAGES.history = " not in page:
        return fail("pages/history.js does not register PAGES.history")
    if 'import { renderJobHistory } from "../job-history.js";' not in page:
        return fail("pages/history.js does not render through job-history.js")
    if 'id: "job-history"' not in page:
        return fail("pages/history.js does not create the #job-history slot")
    if 'import "./pages/history.js";' not in app:
        return fail("app.js does not import the history page module")

    if 'history: "Job history"' not in nav:
        return fail("nav.js does not title the history route")
    # The dashboard is gone: its job list, docs card, status grid and quick
    # actions are not worth a page of their own now that history is dedicated
    # and the docs link lives in the sidebar. / is the history page.
    if (JS / "pages" / "dashboard.js").exists():
        return fail("the dashboard page module still exists")
    if "PAGES.dashboard" in nav or "PAGES.dashboard" in app:
        return fail("the dashboard page is still registered")
    if re.search(r'if \(p === ""\) return "dashboard";', nav) or 'page === "dashboard"' in nav:
        return fail("the root route still resolves to the dashboard")
    if 'data-page="dashboard"' in index:
        return fail("index.html still has a dashboard nav item")
    if "recent-jobs" in console or "recent-jobs" in nav:
        return fail("the dashboard's recent-jobs list is still wired up")
    for marker in ('export function rootPage()',
                   'return uiMode() === "simple" ? "fetch" : "history";',
                   'if (p === "") return rootPage();',
                   'const path = page === "history" ? "/" : "/" + page;'):
        if marker not in nav:
            return fail(f"the root route does not open job history: {marker} is missing")
    # The two cards a first run needs moved to the landing page with the
    # dashboard's shared helpers split into their own modules.
    for path, exports in ((JS / "repo-setup.js", ("export function connectCardNeeded(", "export function repoSetupCard(")),
                          (JS / "onboarding.js", ("export function onboardCard(",))):
        if not path.is_file():
            return fail(f"{path.relative_to(ROOT)} is missing")
        source = path.read_text(encoding="utf-8")
        for export in exports:
            if export not in source:
                return fail(f"{path.relative_to(ROOT)} is missing {export}")
    for marker in ('import { connectCardNeeded, repoSetupCard } from "../repo-setup.js";',
                   'if (connectCardNeeded()) wrap.append(repoSetupCard());'):
        if marker not in page:
            return fail(f"the landing page does not carry the first-run repo card: {marker}")
    # The welcome card belongs to the first-run setup screen, so the landing
    # page must not render it (it used to, which is how it leaked into a normal
    # page for a returning user).
    if "onboardCard" in page:
        return fail("the landing page renders the one-time welcome card again")
    # matrixCard used to live in the dashboard module; sizes is its only caller
    sizes = (JS / "pages" / "sizes.js").read_text(encoding="utf-8")
    if 'export function matrixCard(' not in sizes:
        return fail("matrixCard did not move to the sizes page")
    # matrixCard's variant switch calls $$() and iconize() — a missing import
    # breaks the switch with a console error instead of a visible failure
    if 'import { $, $$, PAGES, S, el, ico, iconize, pageHead } from "../core.js";' not in sizes:
        return fail("the sizes page does not import the helpers matrixCard uses")
    for name in ("offset", "pdf", "templates", "utilities"):
        source = (JS / "pages" / f"{name}.js").read_text(encoding="utf-8")
        if 'from "../repo-setup.js";' not in source:
            return fail(f"{name}.js does not import the shared repo setup card")
    if 'export const SIMPLE_PAGES = ["history",' not in nav:
        return fail("simple mode does not offer the job history page")
    if 'if (page === "history") refreshJobs(true);' not in nav:
        return fail("go() does not refresh the job list when history opens")
    if 'export function restoreArgs(' not in forms:
        return fail("forms.js does not export restoreArgs")
    for required in ('if (prefill.kind && S.manifest?.[prefill.kind]?.page === page)',
                     'S.forms[prefill.kind] = restoreArgs(prefill.kind, prefill.args || {});'):
        if required not in nav:
            return fail(f"applyPrefill does not restore job args: {required} is missing")
    if 'renderJobHistory($("#job-history"))' not in console:
        return fail("refreshJobs does not repaint the history list in place")
    if 'class="nav-item" data-page="history"' not in index:
        return fail("index.html has no job history nav item")
    if re.search(r'<a class="nav-item" data-page="history"[^>]*data-simple-hide', index):
        return fail("the job history nav item is hidden in simple mode")
    for required in (".jobrow .jr-act", ".jobrow .jr-go", ".jobrow.restorable:hover .jr-go",
                     "body.mode-simple .jobrow .jr-act button"):
        if required not in css:
            return fail(f"theme.css is missing {required}")

    # --- the repository's no-dash rule for new user-visible copy -----------
    for path, source in ((page_path, page), (history_path, history)):
        for match in re.finditer(r"[—–]", source):
            line = source[:match.start()].count("\n") + 1
            return fail(f"{path.relative_to(ROOT)}:{line} uses an en/em dash in user-visible copy")

    # --- Node contract for the pure mapping/restore logic ------------------
    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the job history contract")
    node_script = r'''
import fs from "node:fs";
const dataUrl = source => `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;

const manifest = {
  create_pdf: {
    title: "Create PDF", page: "pdf",
    groups: [
      { options: [
        { key: "card_size", type: "select", default: "standard" },
        { key: "paper_size", type: "select", default: "letter" },
        { key: "borderless", type: "toggle", default: false },
        { key: "quality", type: "number", default: 100 },
        { key: "front_dir", type: "path", default: "game/front" },
      ] },
    ],
  },
  "fetch:mtg": {
    title: "Fetch Card Art", page: "fetch",
    groups: [
      { options: [
        { key: "deck_source", type: "segment", default: "file" },
        { key: "prefer_set", type: "chips", default: [] },
        { key: "tokens", type: "toggle", default: false },
      ] },
    ],
  },
  calibration: {
    title: "Calibration sheets", page: "offset",
    groups: [{ options: [{ key: "paper_size", type: "select", default: "letter" }] }],
  },
  dxf_single: {
    title: "Generate a cutting template (DXF)", page: "templates",
    groups: [{ options: [{ key: "card_size", type: "select", default: "standard" }] }],
  },
};

const coreUrl = dataUrl(`
  export const S = { manifest: ${JSON.stringify(manifest)}, forms: {}, jobs: [] };
  export const $ = () => null;
  export const $$ = () => [];
  export const el = (tag, attrs = {}, ...kids) => ({ tag, attrs, kids });
  export const ico = name => ({ ico: name });
  export const confirmModal = () => Promise.resolve(false);
  export const toast = (...args) => { globalThis.histToasts.push(args); };
`);
const navUrl = dataUrl(`
  export const SIMPLE_PAGES = ["history", "fetch", "pdf", "offset", "settings"];
  export const uiMode = () => globalThis.histMode || "advanced";
  export const go = (page, prefill) => { globalThis.histGo = { page, prefill }; };
`);
const jobsUrl = dataUrl(`export const jobs = {};`);
const previewUrl = dataUrl(`export const preview = () => Promise.resolve({});`);
const prepUrl = dataUrl(`export const repoReady = () => true;`);
const formsSource = fs.readFileSync(process.argv[2], "utf8")
  .replace('from "./core.js"', `from "${coreUrl}"`)
  .replace('from "./jobs.js"', `from "${jobsUrl}"`)
  .replace('from "./preview.js"', `from "${previewUrl}"`)
  .replace('from "./prep.js"', `from "${prepUrl}"`)
  .replace('from "./nav.js"', `from "${navUrl}"`);
const formsUrl = dataUrl(formsSource);
const historySource = fs.readFileSync(process.argv[3], "utf8")
  .replace('from "./core.js"', `from "${coreUrl}"`)
  .replace('from "./forms.js"', `from "${formsUrl}"`)
  .replace('from "./nav.js"', `from "${navUrl}"`);
const history = await import(dataUrl(historySource));
const forms = await import(formsUrl);

const fail = message => { throw new Error(message); };
const show = value => JSON.stringify(value);

// --- job -> page mapping -----------------------------------------------
if (history.jobPageFor("create_pdf") !== "pdf" ||
    history.jobPageFor("fetch:mtg") !== "fetch" ||
    history.jobPageFor("calibration") !== "offset") fail("a manifest kind did not map to its page");
if (history.jobPageFor("update") !== null || history.jobPageFor(undefined) !== null ||
    history.jobPageFor("nonsense") !== null) fail("an unknown kind was mapped to a page");
if (history.jobRestorable({ kind: "update" })) fail("a page-less job was reported as restorable");

const fetchPrefill = history.jobPrefill({ kind: "fetch:mtg", args: { deck_source: "paste" } });
if (show(fetchPrefill) !== show({ page: "fetch", prefill: { kind: "fetch:mtg", args: { deck_source: "paste" }, plugin: "mtg" } }))
  fail("fetch job prefill lost its plugin or args: " + show(fetchPrefill));
const pdfPrefill = history.jobPrefill({ kind: "create_pdf", args: { card_size: "poker" } });
if (pdfPrefill.page !== "pdf" || pdfPrefill.prefill.plugin !== undefined)
  fail("a non-fetch job prefill grew a plugin");
if (history.jobPrefill({ kind: "update", args: {} }) !== null) fail("a page-less job produced a prefill");

// --- icon + timestamp helpers ------------------------------------------
if (history.jobIcon("create_pdf") !== "pdf" || history.jobIcon("fetch:mtg") !== "download" ||
    history.jobIcon("dxf_single") !== "scissors" || history.jobIcon("calibration") !== "target" ||
    history.jobIcon("clean_up") !== "trash" || history.jobIcon("extras_generate") !== "sparkle" ||
    history.jobIcon("repo_init") !== "refresh" || history.jobIcon("mystery") !== "terminal")
  fail("job icons do not follow their kind");
if (history.fmtJobTs(undefined) !== "" || history.fmtJobTs("nope") !== "" ||
    history.fmtJobTs(0) !== "" || history.fmtJobTs(null) !== "")
  fail("a missing or zero timestamp did not render as an empty label");
if (history.fmtJobTs(Date.now() / 1000) === "") fail("a current timestamp did not render");
// an older job must carry its date, not just a clock time
const old = history.fmtJobTs(Date.UTC(2020, 0, 2, 3, 4) / 1000);
if (!old || old === history.fmtJobTs(Date.now() / 1000)) fail("an old timestamp was not dated");

// --- arg restoration ---------------------------------------------------
const defaults = forms.restoreArgs("create_pdf", {});
if (show(defaults) !== show({ card_size: "standard", paper_size: "letter", borderless: false, quality: 100, front_dir: "game/front" }))
  fail("restoring nothing did not produce the manifest defaults: " + show(defaults));

const restored = forms.restoreArgs("create_pdf", {
  card_size: "poker", paper_size: "a4", borderless: true, quality: "80",
  injection: "rm -rf /", qualityX: 1, __proto__: { polluted: true },
});
if (show(Object.keys(restored)) !== show(["card_size", "paper_size", "borderless", "quality", "front_dir"]))
  fail("restore kept an unknown key: " + show(Object.keys(restored)));
if (restored.card_size !== "poker" || restored.paper_size !== "a4" || restored.borderless !== true ||
    restored.quality !== "80" || restored.front_dir !== "game/front")
  fail("restore did not keep the recorded values: " + show(restored));
if (restored.injection !== undefined || restored.qualityX !== undefined)
  fail("an unknown key survived restoration");

const coerced = forms.restoreArgs("create_pdf", {
  card_size: 7, borderless: "yes", quality: "abc", front_dir: { a: 1 }, paper_size: null,
});
if (show(coerced) !== show({ card_size: "standard", paper_size: "letter", borderless: false, quality: 100, front_dir: "game/front" }))
  fail("a wrongly typed value was not rejected in favour of the default: " + show(coerced));

const chips = forms.restoreArgs("fetch:mtg", { prefer_set: ["ONE", 7, null, "M25"], tokens: true, deck_source: "paste" });
if (show(chips) !== show({ deck_source: "paste", prefer_set: ["ONE", "M25"], tokens: true }))
  fail("chips/toggle restoration is wrong: " + show(chips));

for (const bad of [null, "x", 42, [], true]) {
  if (show(forms.restoreArgs("create_pdf", bad)) !== show(defaults))
    fail(`restore accepted a non-object arg (${show(bad)})`);
}
if (show(forms.restoreArgs("update", { anything: 1 })) !== "{}")
  fail("restore invented a form for a kind the manifest does not define");
if (show(forms.restoreArgs("create_pdf", { __proto__: { card_size: "hacked" } }).card_size) !== '"standard"')
  fail("a prototype value leaked into the restored settings");

// --- opening a job's settings ------------------------------------------
for (const [kind, page, plugin] of [["create_pdf", "pdf", undefined], ["fetch:mtg", "fetch", "mtg"], ["calibration", "offset", undefined]]) {
  globalThis.histGo = null; globalThis.histToasts = []; globalThis.histMode = "advanced";
  history.openJobSettings({ kind, title: "Fixture", args: {} });
  if (!globalThis.histGo || globalThis.histGo.page !== page)
    fail(`openJobSettings did not navigate to ${page} for ${kind}`);
  if (globalThis.histGo.prefill.plugin !== plugin)
    fail(`openJobSettings lost the plugin for ${kind}`);
  if (globalThis.histGo.prefill.kind !== kind)
    fail(`openJobSettings did not carry the kind for ${kind}`);
}
globalThis.histGo = null; globalThis.histToasts = []; globalThis.histMode = "simple";
history.openJobSettings({ kind: "dxf_single", title: "Fixture", args: {} });
if (globalThis.histGo) fail("simple mode navigated to a page it hides");
if (!globalThis.histToasts.length || globalThis.histToasts[0][0] !== "warn")
  fail("simple mode did not explain why the settings could not be opened");
globalThis.histGo = null; globalThis.histToasts = []; globalThis.histMode = "simple";
history.openJobSettings({ kind: "create_pdf", title: "Fixture", args: {} });
if (!globalThis.histGo || globalThis.histGo.page !== "pdf")
  fail("simple mode refused a page it does offer");
globalThis.histGo = null; globalThis.histToasts = []; globalThis.histMode = "simple";
history.openJobSettings({ kind: "calibration", title: "Fixture", args: {} });
if (!globalThis.histGo || globalThis.histGo.page !== "offset")
  fail("simple mode refused the offset page it offers");
globalThis.histGo = null; globalThis.histToasts = []; globalThis.histMode = "advanced";
history.openJobSettings({ kind: "update", title: "Fixture", args: {} });
if (globalThis.histGo || globalThis.histToasts.length)
  fail("a page-less job navigated or announced something");

console.log("ok: job history page routing, manifest filtering, and settings restoration pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(forms_path), str(history_path)],
        input=node_script,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node job history contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
