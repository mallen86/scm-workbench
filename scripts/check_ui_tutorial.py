#!/usr/bin/env python3
"""Executable contracts for the optional guided Workbench tutorial."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
JS = UI / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    tutorial = (JS / "guided-tutorial.js").read_text(encoding="utf-8")
    onboarding = (JS / "onboarding.js").read_text(encoding="utf-8")
    settings = (JS / "pages" / "settings.js").read_text(encoding="utf-8")
    postprocess = (JS / "pages" / "postprocess.js").read_text(encoding="utf-8")
    css = (UI / "theme.css").read_text(encoding="utf-8")

    for marker in (
        "export const GUIDED_TUTORIAL_STEPS",
        'page: "fetch"',
        'target: ".plugin-sections"',
        "target: '.form-card[data-kind^=\"fetch:\"] .field[data-key=\"deck_source\"]'",
        "target: '.form-card[data-kind^=\"fetch:\"] .runbar'",
        'page: "postprocess"',
        'target: ".pp-simple-picker, .pp-library"',
        'page: "pdf"',
        'targets: Object.freeze([',
        "'.form-card[data-kind=\"create_pdf\"] .field[data-key=\"card_size\"]'",
        "'.form-card[data-kind=\"create_pdf\"] .field[data-key=\"paper_size\"]'",
        'target: ".back-image-inline"',
        "target: '.form-card[data-kind=\"create_pdf\"] .runbar'",
        'go("fetch");',
        "export function startGuidedTutorial()",
        "export function stopGuidedTutorial()",
        'event.key === "Escape"',
        'document.removeEventListener("keydown", onKeyDown)',
        'window.removeEventListener("resize", schedulePosition)',
        'window.removeEventListener("popstate", stopGuidedTutorial)',
        'clearInterval(state.positionTimer)',
        '"aria-modal": "true"',
        '"aria-label": "Stop guided tutorial"',
        '"Stop tutorial"',
    ):
        if marker not in tutorial:
            return fail(f"guided tutorial is missing {marker}")
    for forbidden in ("/api/", "wb_rpc", ".click(", "doRun(", "importBackImage(", "setSettings("):
        if forbidden in tutorial:
            return fail(f"guided tutorial performs work instead of explaining it: {forbidden}")

    step_copy = re.findall(r'^    (?:title|text): "([^"]*)"', tutorial, re.MULTILINE)
    if len(step_copy) != 14:
        return fail(f"guided tutorial does not have seven titled steps: {step_copy}")
    if any(dash in text for text in step_copy for dash in ("\u2014", "\u2013")):
        return fail("guided tutorial user-facing text contains an en or em dash")
    for phrase in ("Choose a game", "Add your decklist", "Fetch card art", "Optional image post-processing", "Choose card and paper sizes", "Add a card back", "Generate the PDF"):
        if phrase not in step_copy:
            return fail(f"guided tutorial does not cover {phrase}")
    if not all(phrase in step_copy[7] for phrase in ("skip this step", "Simple Upscaler", "Advanced AI Upscaler", "installing its model and libraries")):
        return fail("post-processing tutorial does not explain that both upscalers are optional and the AI assets require installation")
    if 'class: "card pp-simple-picker"' not in postprocess or 'class: "card pp-library"' not in postprocess:
        return fail("post-processing tutorial must have a target in both Simple and Advanced modes")
    if "Select Only fronts in the form when a back is not needed." in tutorial:
        return fail("the card-back step still gives incorrect Only fronts guidance")

    for marker in (
        'import { startGuidedTutorial } from "./guided-tutorial.js";',
        "finishOnboarding(onDone, true)",
        "finishOnboarding(onDone, false)",
        '"Got it. Show me around"',
        '"Skip tutorial"',
        "if (startTour) startGuidedTutorial();",
    ):
        if marker not in onboarding:
            return fail(f"first-run tutorial choice is missing {marker}")
    if onboarding.find("if (typeof onDone") > onboarding.find("if (startTour)"):
        return fail("the tutorial starts before the welcome screen leaves")
    for marker in (
        'import { startGuidedTutorial } from "../guided-tutorial.js";',
        'class: "card guided-tutorial-card"',
        'optional image post-processing, choosing card and paper sizes',
        '"Start guided tutorial"',
        'onclick: startGuidedTutorial',
        '"You can stop at any step."',
    ):
        if marker not in settings:
            return fail(f"Settings cannot replay the guided tutorial: {marker}")
    for selector in (
        ".guided-tour-root", ".guided-tour-shield", ".guided-tour-spotlight",
        ".guided-tour-popover", ".guided-tour-actions", ".guided-tour-close",
    ):
        if selector not in css:
            return fail(f"guided tutorial styling is missing {selector}")
    if "pointer-events: auto;" not in css or "prefers-reduced-motion: reduce" not in css:
        return fail("guided tutorial does not block accidental actions or respect reduced motion")

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(JS / "guided-tutorial.js"), str(JS / "onboarding.js")],
        input=r'''
import fs from "node:fs";
const tutorialSource = fs.readFileSync(process.argv[2], "utf8");
const onboardingSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value).toString("base64")}`;
const fail = message => { throw new Error(message); };

class FakeClassList {
  constructor(value = "") { this.values = new Set(String(value).split(/\s+/).filter(Boolean)); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toString() { return [...this.values].join(" "); }
}

function setConnected(node, value) {
  if (!(node instanceof FakeNode)) return;
  node.isConnected = value;
  for (const child of node.children) setConnected(child, value);
}

class FakeNode {
  constructor(tag = "div", attrs = {}) {
    this.tag = tag;
    this.children = [];
    this.parentNode = null;
    this.isConnected = false;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
    this.style = {};
    this.classList = new FakeClassList(attrs.class || "");
    this.className = attrs.class || "";
    this.rect = attrs.rect || null;
    this.scrolls = 0;
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class" || key === "rect" || value == null) continue;
      if (key.startsWith("on") && typeof value === "function") this[key] = value;
      else this.setAttribute(key, value);
    }
  }
  append(...items) {
    for (const item of items.flat(Infinity)) {
      if (item == null || item === false) continue;
      this.children.push(item);
      if (item instanceof FakeNode) {
        item.parentNode = this;
        if (this.isConnected) setConnected(item, true);
      }
    }
  }
  replaceChildren(...items) {
    for (const child of this.children) setConnected(child, false);
    this.children = [];
    this.append(...items);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
    setConnected(this, false);
  }
  setAttribute(name, value) {
    if (name === "class") {
      this.className = String(value);
      this.classList = new FakeClassList(value);
    } else if (name === "hidden") this.hidden = !!value;
    else this[name] = value;
  }
  matches(selector) {
    if (selector === "button:not([disabled])") return this.tag === "button" && !this.disabled;
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    return this.tag === selector;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = node => {
      if (!(node instanceof FakeNode)) return;
      if (node !== this && node.matches(selector)) found.push(node);
      for (const child of node.children) visit(child);
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() {
    if (this.rect) return {...this.rect};
    if (this.classList.contains("guided-tour-popover")) return {left:0, top:0, right:360, bottom:220, width:360, height:220};
    return {left:0, top:0, right:0, bottom:0, width:0, height:0};
  }
  scrollIntoView() { this.scrolls++; }
  focus() { globalThis.document.activeElement = this; }
  get textContent() {
    return this.children.map(child => child instanceof FakeNode ? child.textContent : String(child)).join("");
  }
}

globalThis.FakeNode = FakeNode;
globalThis.sharedState = {page: "settings", info: {settings: {onboarded: false}}};
globalThis.targets = new Map();
globalThis.navigations = [];
globalThis.toasts = [];
const body = new FakeNode("body");
setConnected(body, true);
const starter = new FakeNode("button");
body.append(starter);
const documentListeners = new Map();
globalThis.document = {
  body,
  activeElement: starter,
  addEventListener(name, fn) { documentListeners.set(name, fn); },
  removeEventListener(name, fn) { if (documentListeners.get(name) === fn) documentListeners.delete(name); },
  querySelector(selector) { return body.querySelector(selector); },
};
const windowListeners = new Map();
globalThis.window = {
  innerWidth: 1200,
  innerHeight: 800,
  addEventListener(name, fn) { windowListeners.set(name, fn); },
  removeEventListener(name, fn) { if (windowListeners.get(name) === fn) windowListeners.delete(name); },
};
let frameId = 0;
const frames = new Map();
globalThis.requestAnimationFrame = fn => { const id = ++frameId; frames.set(id, fn); return id; };
globalThis.cancelAnimationFrame = id => frames.delete(id);
const flushFrames = () => {
  for (let round = 0; round < 10 && frames.size; round++) {
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach(fn => fn());
  }
};
let intervalId = 0;
const intervals = new Map();
globalThis.setInterval = (fn, ms) => { const id = ++intervalId; intervals.set(id, {fn, ms}); return id; };
globalThis.clearInterval = id => intervals.delete(id);

globalThis.installTargets = page => {
  for (const target of globalThis.targets.values()) target.isConnected = target.page === page;
};
const coreUrl = dataUrl(`
  export const S = globalThis.sharedState;
  export const $ = (selector, root = globalThis.document) => {
    if (root === globalThis.document) {
      const target = globalThis.targets.get(selector);
      if (target?.isConnected) return target;
    }
    return root?.querySelector?.(selector) || null;
  };
  export function el(tag, attrs = {}, ...kids) {
    const node = new globalThis.FakeNode(tag, attrs);
    node.append(...kids);
    return node;
  }
  export function ico(name) { return new globalThis.FakeNode("icon", {name}); }
  export function toast(kind, text) { globalThis.toasts.push({kind, text}); }
`);
const navUrl = dataUrl(`
  export function go(page) {
    globalThis.sharedState.page = page;
    globalThis.navigations.push(page);
    globalThis.installTargets(page);
  }
`);
const tutorialUrl = dataUrl(tutorialSource
  .replace('from "./core.js"', `from "${coreUrl}"`)
  .replace('from "./nav.js"', `from "${navUrl}"`));
const tutorial = await import(tutorialUrl);
for (let index = 0; index < tutorial.GUIDED_TUTORIAL_STEPS.length; index++) {
  const step = tutorial.GUIDED_TUTORIAL_STEPS[index];
  const selectors = step.targets || [step.target];
  for (let targetIndex = 0; targetIndex < selectors.length; targetIndex++) {
    const selector = selectors[targetIndex];
    if (globalThis.targets.has(selector)) continue;
    const left = targetIndex ? 570 : 300;
    const right = targetIndex ? 850 : 550;
    const target = new FakeNode("target", {rect:{left, top:180 + index * 20, right, bottom:240 + index * 20, width:right - left, height:60}});
    target.page = step.page;
    globalThis.targets.set(selector, target);
  }
}

tutorial.startGuidedTutorial();
flushFrames();
if (!tutorial.guidedTutorialActive() || globalThis.sharedState.page !== "fetch" ||
    globalThis.navigations.join() !== "fetch" || intervals.size !== 1 ||
    !documentListeners.has("keydown") || !windowListeners.has("resize") || !windowListeners.has("popstate") ||
    !body.classList.contains("guided-tutorial-open")) fail("tutorial did not start with bounded lifecycle state");
let root = body.querySelector(".guided-tour-root");
let popover = body.querySelector(".guided-tour-popover");
if (!root || !popover || !popover.textContent.includes("Step 1 of 7") ||
    !popover.textContent.includes("Choose a game") ||
    globalThis.targets.get(tutorial.GUIDED_TUTORIAL_STEPS[0].target).scrolls !== 1)
  fail("first tutorial step did not point to the game chooser");

const next = () => {
  const button = body.querySelector(".guided-tour-next");
  if (!button?.onclick) fail("tutorial step has no forward action");
  button.onclick();
  flushFrames();
};
next();
if (globalThis.navigations.length !== 1 || !popover.textContent.includes("Add your decklist"))
  fail("same-page decklist step caused a route change or did not render");
next();
if (!popover.textContent.includes("Fetch card art")) fail("fetch action step did not render");
next();
const postprocessTarget = globalThis.targets.get(".pp-simple-picker, .pp-library");
if (globalThis.sharedState.page !== "postprocess" || globalThis.navigations.join() !== "fetch,postprocess" ||
    !popover.textContent.includes("Step 4 of 7") || !popover.textContent.includes("Optional image post-processing") ||
    !popover.textContent.includes("Simple Upscaler") || !popover.textContent.includes("Advanced AI Upscaler") ||
    !postprocessTarget?.isConnected || postprocessTarget.scrolls !== 1)
  fail("optional post-processing step did not navigate and point to the processor picker");
next();
if (globalThis.sharedState.page !== "pdf" || globalThis.navigations.join() !== "fetch,postprocess,pdf" ||
    !popover.textContent.includes("Choose card and paper sizes")) fail("size step did not navigate to Create PDF");
const spotlight = body.querySelector(".guided-tour-spotlight");
if (spotlight.style.left !== "293px" || spotlight.style.width !== "564px")
  fail("size step did not spotlight the combined card and paper controls");
const back = popover.querySelectorAll("button").find(button => button.textContent === "Back");
if (!back?.onclick) fail("PDF step has no Back action");
back.onclick();
flushFrames();
if (globalThis.sharedState.page !== "postprocess" || !popover.textContent.includes("Optional image post-processing") ||
    globalThis.navigations.at(-1) !== "postprocess") fail("Back did not return to optional post-processing");
next();
if (globalThis.sharedState.page !== "pdf" || globalThis.navigations.at(-1) !== "pdf")
  fail("Next did not return to Create PDF after visiting post-processing");
next();
if (!popover.textContent.includes("Add a card back") || popover.textContent.includes("Only fronts"))
  fail("card-back step is missing or still gives incorrect Only fronts guidance");
next();
if (!popover.textContent.includes("Step 7 of 7") || !popover.textContent.includes("Generate the PDF") ||
    !popover.textContent.includes("Finish")) fail("final PDF action step did not render");

let prevented = false;
documentListeners.get("keydown")({key:"Escape", preventDefault() { prevented = true; }});
flushFrames();
if (!prevented || tutorial.guidedTutorialActive() || body.querySelector(".guided-tour-root") ||
    body.classList.contains("guided-tutorial-open") || intervals.size ||
    documentListeners.has("keydown") || windowListeners.has("resize") || windowListeners.has("popstate") ||
    globalThis.sharedState.page !== "pdf" || globalThis.navigations.at(-1) !== "pdf")
  fail("Escape did not stop in place and clean every listener and timer");

tutorial.startGuidedTutorial();
flushFrames();
for (let index = 0; index < 7; index++) {
  documentListeners.get("keydown")({key:"ArrowRight", preventDefault() {}});
  flushFrames();
}
if (tutorial.guidedTutorialActive() || globalThis.sharedState.page !== "fetch" ||
    globalThis.navigations.at(-1) !== "fetch" || globalThis.toasts.at(-1)?.text !==
    "Tutorial complete. You can replay it from Settings.") fail("finishing did not return to Fetch card art and close the replayable tutorial");

// Exercise both first-run choices. They must persist the welcome dismissal,
// leave the setup screen, and only the affirmative choice may start the tour.
globalThis.saved = [];
globalThis.tourStarts = 0;
globalThis.boots = 0;
globalThis.welcomeToasts = [];
globalThis.settingsFailure = false;
const onboardCoreUrl = dataUrl(`
  export const S = globalThis.sharedState;
  export function el(tag, attrs = {}, ...kids) {
    const node = new globalThis.FakeNode(tag, attrs);
    node.append(...kids);
    return node;
  }
  export function toast(kind, text) { globalThis.welcomeToasts.push({kind, text}); }
`);
const onboardTourUrl = dataUrl("export function startGuidedTutorial() { globalThis.tourStarts++; }");
const onboardNavUrl = dataUrl("export function bootPage() { globalThis.boots++; }");
const settingsUrl = dataUrl(`
  export async function setSettings(changes) {
    if (globalThis.settingsFailure) throw new Error("save failed");
    globalThis.saved.push(changes);
    return {ok:true};
  }
`);
const onboardingUrl = dataUrl(onboardingSource
  .replace('from "./core.js"', `from "${onboardCoreUrl}"`)
  .replace('from "./guided-tutorial.js"', `from "${onboardTourUrl}"`)
  .replace('from "./nav.js"', `from "${onboardNavUrl}"`)
  .replace('from "./settings-transport.js"', `from "${settingsUrl}"`));
const onboarding = await import(onboardingUrl);
const buttonNamed = (card, name) => card.querySelectorAll("button").find(button => button.textContent === name);
let done = 0;
let card = onboarding.onboardCard(() => { done++; });
await buttonNamed(card, "Skip tutorial").onclick();
if (globalThis.saved.length !== 1 || !globalThis.sharedState.info.settings.onboarded || done !== 1 || globalThis.tourStarts)
  fail("skipping the optional tutorial did not dismiss onboarding without starting it");
globalThis.sharedState.info.settings.onboarded = false;
card = onboarding.onboardCard(() => { done++; });
await buttonNamed(card, "Got it. Show me around").onclick();
if (globalThis.saved.length !== 2 || !globalThis.sharedState.info.settings.onboarded || done !== 2 || globalThis.tourStarts !== 1)
  fail("the first-run affirmative choice did not start the guided tutorial after dismissal");
globalThis.settingsFailure = true;
globalThis.sharedState.info.settings.onboarded = false;
card = onboarding.onboardCard(() => { done++; });
await buttonNamed(card, "Got it. Show me around").onclick();
if (done !== 2 || globalThis.tourStarts !== 1 || globalThis.sharedState.info.settings.onboarded ||
    globalThis.welcomeToasts.at(-1)?.kind !== "err") fail("a failed onboarding save still left or started the tutorial");

console.log("ok: tutorial covers optional post-processing, PDF sizes, card backs, and cleanup on stop or finish");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        return fail(f"Node guided tutorial contract failed: {(node.stderr or node.stdout).strip()}")
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
