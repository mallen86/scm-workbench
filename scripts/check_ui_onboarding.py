#!/usr/bin/env python3
"""Static contracts for simple PDF rendering and packaged first-boot setup."""
import html
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
JS = UI / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    forms = (JS / "forms.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    core = (JS / "core.js").read_text(encoding="utf-8")
    nav = (JS / "nav.js").read_text(encoding="utf-8")
    prep = (JS / "prep.js").read_text(encoding="utf-8")
    page = (JS / "pages" / "preparing.js").read_text(encoding="utf-8")
    css = (UI / "theme.css").read_text(encoding="utf-8")

    # Calling visible() without its option caused optVisible(undefined, spec)
    # to throw and left the simple Create PDF route entirely blank.
    if forms.count("if (o && visible(o))") != 2:
        return fail("flat form rows do not pass each option to the visibility predicate")
    if "if (o && visible())" in forms:
        return fail("the blank simple-PDF runtime exception is still reachable")
    for marker in (
        'if (kind === "create_pdf" && rows.length)',
        "flatMap(g => g.options || [])",
        "for (const keys of rows) placeGroup(simpleGroup, keys)",
    ):
        if marker not in forms:
            return fail(f"simple PDF does not follow its cross-group row plan: {marker}")

    for marker in (
        'import "./pages/preparing.js";',
        'if (firstBootPageNeeded()) go("preparing", null, { push: false });',
    ):
        if marker not in app:
            return fail(f"startup does not select the transient preparation page: {marker}")
    if "firstBootDismissed: false" not in core:
        return fail("first-boot dismissal is not session-local shared state")
    if 'if (p === "preparing") return null;' not in nav:
        return fail("the transient preparation page became bookmarkable")
    for marker in (
        "export function firstBootPageNeeded()",
        "!S.firstBootDismissed",
        "S.info?.server?.is_packaged",
        "some(r => !r.deployed)",
        'native?.dataset.prepAll === "true"',
        'ready ? "ready"',
        'ready ? "Downloaded and ready"',
        "const indeterminate = !det && !ready",
        'bar.classList.toggle("indet", indeterminate)',
        'fill.style.width = indeterminate ? "" : pct + "%"',
        "setTimeout(tick, 750)",
        "const allReady = repos.length > 0 && repos.every(r => r.deployed);",
        'toast(allReady ? "ok" : "warn"',
        '"Setup did not finish. Retry the missing repositories."',
        # A failed repo operation used to vanish with its progress row, leaving
        # simple mode with no console and no other trace of the failure.
        "export function repoFailures()",
        'job.kind !== "repo_init" && job.kind !== "repo_update"',
        "if (seen.has(key)) continue;",
        'job.status === "fail" && job.id !== S.repoFailureDismissed',
        "function renderRepoFailures(container, failures)",
        'S.repoFailureDismissed = failure.job.id;',
        "await import(\"./forms.js\")",
        "if (!repoFailures().length) removeGlobalStrip();",
    ):
        if marker not in prep:
            return fail(f"preparation state contract is missing: {marker}")
    # Progress rows and the failure notice are mutually exclusive, and the
    # notice is the sidebar's own: the first-boot page keeps its retry rows.
    if "const container = native || globalStrip();" not in prep:
        return fail("the prep box is no longer hosted by the shared sidebar strip")
    if "const failures = native ? [] : repoFailures();" not in prep:
        return fail("the sidebar prep box does not consult a settled failure")
    render_at = prep.find("if (failures.length) {")
    row_at = prep.find("ensurePrepRows(rows, container);", render_at)
    if render_at < 0 or row_at < 0 or row_at < render_at:
        return fail("the failure notice no longer precedes the progress rows")
    # The sidebar box is driven by the prep watcher. A repository operation can
    # begin while a page is already up (an update pressed in Settings, a retry
    # from the notice), so the job-list refresh is where that start is observed;
    # without it the box only appeared after some later info refresh or a
    # navigation happened to start the watcher.
    console_js = (JS / "console.js").read_text(encoding="utf-8")
    for marker in ("_prepTimer, prepActive, startPrepWatcher",
                   "if (prepActive() && !_prepTimer) startPrepWatcher();"):
        if marker not in console_js:
            return fail(f"a repository operation starting on an open page is not watched: {marker}")
    refresh_at = console_js.find("export async function refreshJobs")
    start_at = console_js.find("if (prepActive() && !_prepTimer) startPrepWatcher();", refresh_at)
    if refresh_at < 0 or start_at < 0:
        return fail("the prep watcher is not started from the job-list refresh")
    if start_at < 0 or (lambda nxt: nxt >= 0 and start_at > nxt)(
            console_js.find("\nexport ", start_at)):
        return fail("the prep watcher start left refreshJobs")
    # Starting from a bare job list every 4 s must not stack a second watcher,
    # and the first paint must not wait for a refresh round-trip.
    if "if (prepActive()) { updatePrepRows(); tick(); }" not in prep:
        return fail("the prep watcher does not paint from the state already in hand")

    # Retrying from the notice must reuse the real runner (forms.js imports
    # prep.js, so the import has to stay dynamic) and offer a way to dismiss.
    if 'class: "rp-actions"' not in prep or '"aria-label": "Dismiss this message"' not in prep:
        return fail("the repo failure notice has no retry and dismiss actions")
    if "repoReady" not in (JS / "forms.js").read_text(encoding="utf-8"):
        return fail("forms.js no longer owns the repo runner the retry reuses")
    for path in sorted(JS.rglob("*.js")):
        if path != JS / "prep.js" and "repoFailures" in path.read_text(encoding="utf-8"):
            return fail(f"{path.relative_to(ROOT)} renders the repo failure notice")
    for marker in (
        "PAGES.preparing",
        "S.firstBootDismissed = true",
        "bootPage();",
        'id: "repoprog"',
        '"data-prep-all": "true"',
        "wrap.__patch = updatePrepRows",
        "Explore while setup continues",
        "Setup needs your attention",
        "async function retrySetup(button)",
        'j.kind === "repo_init" || j.kind === "repo_update"',
        'doRun("repo_init", null',
        '"Retry setup"',
        'startPrepWatcher();',
        'go("preparing", null, { push: false, anim: false });',
    ):
        if marker not in page:
            return fail(f"first-boot page is missing: {marker}")
    if ".first-boot-page" not in css or ".first-boot-close" not in css:
        return fail("first-boot page presentation or close control is unstyled")
    if "/api/" in page or "wb_rpc" in page or "fetch(" in page:
        return fail("first-boot page bypasses the shared info/native transport path")

    # Everything that seats itself above the sidebar footer's divider shares
    # one box. The app-update strip used to sit flush against the sidebar with
    # no background, border or padding while the repo strip beside it was a
    # box, so the same seat looked like two different things.
    updater_ui = (JS / "updater-ui.js").read_text(encoding="utf-8")
    for name, source in (("prep.js", prep), ("updater-ui.js", updater_ui)):
        if 'class: "repoprog sidebar-note"' not in source:
            return fail(f"{name} does not use the shared sidebar notification box")
    if ".sidebar-note {" not in css:
        return fail("the shared sidebar notification box is unstyled")
    if "background: var(--surface-2); border: 1px solid var(--border-soft);" not in css:
        return fail("the shared sidebar notification box has no background or border")
    # Inner typography belongs to the shared class, or the two boxes' headings
    # render in different colours.
    for marker in (".sidebar-note .rp-head", ".sidebar-note .rp-label"):
        if marker not in css:
            return fail(f"the shared sidebar box does not own {marker}")
    if "#repoprog-global .rp-head" in css or "#repoprog-global .rp-label" in css:
        return fail("a sidebar box still styles its heading outside the shared class")
    if css.find(".sidebar-note {") < css.find(".repoprog {"):
        return fail("the shared box is declared before .repoprog, so its padding would lose")
    # The failed/done overrides must still beat the shared class.
    if not re.search(r"#updateprog\.failed \.rp-head", css):
        return fail("the update strip lost its failed-state colour")
    for path in sorted(JS.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if "foot.before(" in source and "sidebar-note" not in source:
            return fail(f"{path.relative_to(ROOT)} seats a sidebar notification outside the shared box")

    # The one-time welcome card belongs to this screen: it is appended once the
    # repos are ready and is the sole exit when it is shown (its own button
    # leaves the setup view, so the actions row would repeat that exit).
    onboarding = (JS / "onboarding.js").read_text(encoding="utf-8")
    for marker in ('export function onboardCard(onDone)',
                   'if (typeof onDone === "function") onDone();',
                   "else bootPage();",
                   'setSettings({ onboarded: true })'):
        if marker not in onboarding:
            return fail(f"the welcome card is missing its dismissal path: {marker}")
    for marker in ('import { onboardCard } from "../onboarding.js";',
                   'const showWelcome = allReady && !S.info?.settings?.onboarded;',
                   'wrap.append(onboardCard(leaveSetup));'):
        if marker not in page:
            return fail(f"the setup screen does not own the welcome card: {marker}")
    # The welcome card takes the progress card's place, so the finished status
    # line does not push it down the page. The two are exclusive branches.
    if "if (showWelcome) {" not in page or "wrap.append(progressCard);" not in page:
        return fail("the welcome card and the progress card are not exclusive")
    welcome_at = page.find("if (showWelcome) {")
    progress_at = page.find("wrap.append(progressCard);")
    if welcome_at < 0 or progress_at < 0 or progress_at < welcome_at:
        return fail("the progress card is not rendered in the welcome card's place")
    if page.find("wrap.append(progressCard);", welcome_at) < page.find("} else {", welcome_at):
        return fail("the progress card is still rendered alongside the welcome card")
    # The welcome card is the first thing a new user reads, so its wording is
    # part of the contract: the offset stage is named the way the sidebar names
    # it, it says that stage is optional (Create PDF's "Apply saved offset"
    # toggle ships off), and the fetch stage describes the outcome rather than
    # an internal folder.
    steps = re.findall(r'\["(\d)", "([^"]*)", "([^"]*)"\]', onboarding)
    if len(steps) != 4:
        return fail(f"the welcome card does not list four stages: {steps}")
    index = UI / "index.html"
    offset_label = re.search(r'data-page="offset"[^>]*>.*?</span>([^<]*)<', index.read_text(encoding="utf-8"))
    if not offset_label:
        return fail("the sidebar's offset label could not be read")
    sidebar_offset = html.unescape(offset_label.group(1)).strip()
    if steps[0][1] != sidebar_offset:
        return fail(f"the welcome card calls stage one {steps[0][1]!r}, but the sidebar says "
                    f"{sidebar_offset!r}")
    if "Optional" not in steps[0][2]:
        return fail("the welcome card does not say the offset stage is optional")
    if "game/front" in steps[1][2]:
        return fail("the welcome card still names an internal folder for fetched art")
    if steps[1][2] != "Choose a game and decklist and let SCM handle fetching the images.":
        return fail(f"the fetch stage wording changed unexpectedly: {steps[1][2]!r}")
    for _n, title, desc in steps:
        if any(dash in title or dash in desc for dash in ("\u2014", "\u2013")):
            return fail(f"a welcome stage uses an en or em dash: {title!r}")

    # On the welcome screen the app chrome stands back, and the class that does
    # it is cleared by the next navigation so no page has to clean up after it.
    if 'document.body.classList.add("setup-welcome");' not in page:
        return fail("the welcome screen does not ask the top bar to stand back")
    nav = (JS / "nav.js").read_text(encoding="utf-8")
    if 'document.body.classList.remove("setup-welcome");' not in nav:
        return fail("navigating away does not restore the top bar")
    if nav.find('document.body.classList.remove("setup-welcome");') > nav.find("S.page = page;"):
        return fail("the top bar is restored after the page is recorded, so a render could re-add it late")
    if "body.setup-welcome .topbar { display: none; }" not in css:
        return fail("the welcome screen's top bar is not actually hidden")
    # Hiding the chrome must not strand anyone: the card's own controls leave it.
    if "first-boot-close" not in page or 'class: "btn primary"' not in onboarding:
        return fail("the welcome screen hides the top bar without an in-card exit")

    # A returning user must never see it again, and no other page may render it.
    if 'S.info.settings.onboarded' not in onboarding:
        return fail("the welcome card does not read the dismissed flag")
    for other in sorted(JS.rglob("*.js")):
        if other in (JS / "onboarding.js", JS / "pages" / "preparing.js"):
            continue
        if "onboardCard" in other.read_text(encoding="utf-8"):
            return fail(f"{other.relative_to(ROOT)} renders the one-time welcome card")

    print("ok: simple PDF visibility and transient packaged first-boot setup contracts pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
