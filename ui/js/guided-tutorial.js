/* guided-tutorial: a session-only, replayable walkthrough of the core
   Workbench workflow. It points at existing controls and never changes form
   values, imports files, or starts jobs on the user's behalf. */

import { $, S, el, ico, toast } from "./core.js";
import { go } from "./nav.js";


export const GUIDED_TUTORIAL_STEPS = Object.freeze([
  Object.freeze({
    page: "fetch",
    target: ".plugin-sections",
    title: "Choose a game",
    text: "Pick the game whose decklist you want to prepare. Its formats and options appear below.",
  }),
  Object.freeze({
    page: "fetch",
    target: '.form-card[data-kind^="fetch:"] .field[data-key="deck_source"]',
    title: "Add your decklist",
    text: "Choose an existing file, import one, paste text, or use a supported URL. Complete the required fields for the selected game.",
  }),
  Object.freeze({
    page: "fetch",
    target: '.form-card[data-kind^="fetch:"] .runbar',
    title: "Fetch card art",
    text: "Review the fetch options, then use the action here. The job runs in the background and this page shows its progress.",
  }),
  Object.freeze({
    page: "postprocess",
    target: ".pp-simple-picker, .pp-library",
    title: "Optional image post-processing",
    text: "You can skip this step and create a PDF directly. To enlarge fetched images, the Simple Upscaler is ready to use. The Advanced AI Upscaler requires installing its model and libraries first. Run a processor only on original images; another run scales them again.",
  }),
  Object.freeze({
    page: "pdf",
    targets: Object.freeze([
      '.form-card[data-kind="create_pdf"] .field[data-key="card_size"]',
      '.form-card[data-kind="create_pdf"] .field[data-key="paper_size"]',
    ]),
    title: "Choose card and paper sizes",
    text: "Choose the card dimensions and paper loaded in your printer. This combination controls how cards fit on each sheet and which cutting template matches.",
  }),
  Object.freeze({
    page: "pdf",
    target: ".back-image-inline",
    title: "Add a card back",
    text: "Choose an image here and Workbench safely copies it into the card-back folder.",
  }),
  Object.freeze({
    page: "pdf",
    target: '.form-card[data-kind="create_pdf"] .runbar',
    title: "Generate the PDF",
    text: "Review the paper, card size, and first-page preview. Then create the PDF here. Progress and the completed file stay available in Workbench.",
  }),
]);

const TARGET_WAIT_MS = 50;
const TARGET_WAIT_MAX = 30;
const EDGE_GAP = 16;
const TARGET_PAD = 7;
const CARD_GAP = 14;
let active = null;


function clearPending(state) {
  clearTimeout(state.targetTimer);
  state.targetTimer = null;
  if (state.positionFrame !== null) cancelAnimationFrame(state.positionFrame);
  state.positionFrame = null;
}


function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}


function positionTutorial() {
  const state = active;
  if (!state) return;
  state.positionFrame = null;
  const { popover, spotlight } = state;
  const width = window.innerWidth;
  const height = window.innerHeight;
  const configuredTargets = state.targets || [];
  const targets = configuredTargets.length && configuredTargets.every(target => target?.isConnected)
    ? configuredTargets : [];

  popover.style.visibility = "hidden";
  popover.style.left = "0px";
  popover.style.top = "0px";
  if (!targets.length) {
    state.root.classList.add("no-target");
    spotlight.hidden = true;
    const card = popover.getBoundingClientRect();
    popover.dataset.side = "center";
    popover.style.left = `${Math.max(EDGE_GAP, (width - card.width) / 2)}px`;
    popover.style.top = `${Math.max(EDGE_GAP, (height - card.height) / 2)}px`;
    popover.style.visibility = "visible";
    return;
  }

  state.root.classList.remove("no-target");
  const rects = targets.map(target => target.getBoundingClientRect());
  const rect = {
    left: Math.min(...rects.map(item => item.left)),
    right: Math.max(...rects.map(item => item.right)),
    top: Math.min(...rects.map(item => item.top)),
    bottom: Math.max(...rects.map(item => item.bottom)),
  };
  const left = clamp(rect.left - TARGET_PAD, EDGE_GAP / 2, width - EDGE_GAP / 2);
  const right = clamp(rect.right + TARGET_PAD, EDGE_GAP / 2, width - EDGE_GAP / 2);
  const top = clamp(rect.top - TARGET_PAD, EDGE_GAP / 2, height - EDGE_GAP / 2);
  const bottom = clamp(rect.bottom + TARGET_PAD, EDGE_GAP / 2, height - EDGE_GAP / 2);
  spotlight.hidden = false;
  spotlight.style.left = `${left}px`;
  spotlight.style.top = `${top}px`;
  spotlight.style.width = `${Math.max(0, right - left)}px`;
  spotlight.style.height = `${Math.max(0, bottom - top)}px`;

  const card = popover.getBoundingClientRect();
  let cardLeft;
  let cardTop;
  let side;
  if (width - right >= card.width + CARD_GAP + EDGE_GAP) {
    side = "right";
    cardLeft = right + CARD_GAP;
    cardTop = top + (bottom - top - card.height) / 2;
  } else if (left >= card.width + CARD_GAP + EDGE_GAP) {
    side = "left";
    cardLeft = left - CARD_GAP - card.width;
    cardTop = top + (bottom - top - card.height) / 2;
  } else if (height - bottom >= card.height + CARD_GAP + EDGE_GAP) {
    side = "below";
    cardLeft = left + (right - left - card.width) / 2;
    cardTop = bottom + CARD_GAP;
  } else {
    side = "above";
    cardLeft = left + (right - left - card.width) / 2;
    cardTop = top - CARD_GAP - card.height;
  }
  popover.dataset.side = side;
  popover.style.left = `${clamp(cardLeft, EDGE_GAP, Math.max(EDGE_GAP, width - card.width - EDGE_GAP))}px`;
  popover.style.top = `${clamp(cardTop, EDGE_GAP, Math.max(EDGE_GAP, height - card.height - EDGE_GAP))}px`;
  popover.style.visibility = "visible";
}


function schedulePosition() {
  if (!active || active.positionFrame !== null) return;
  active.positionFrame = requestAnimationFrame(positionTutorial);
}


function focusableButtons() {
  if (!active) return [];
  return Array.from(active.popover.querySelectorAll("button:not([disabled])"));
}


function onKeyDown(event) {
  if (!active) return;
  if (event.key === "Escape") {
    event.preventDefault();
    stopGuidedTutorial();
    return;
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    moveTo(active.index + 1);
    return;
  }
  if (event.key === "ArrowLeft" && active.index > 0) {
    event.preventDefault();
    moveTo(active.index - 1);
    return;
  }
  if (event.key !== "Tab") return;
  const buttons = focusableButtons();
  if (!buttons.length) return;
  const first = buttons[0];
  const last = buttons[buttons.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}


function findTarget(generation, attempt = 0) {
  const state = active;
  if (!state || generation !== state.generation) return;
  const step = GUIDED_TUTORIAL_STEPS[state.index];
  const selectors = step.targets || [step.target];
  const targets = selectors.map(selector => $(selector));
  const ready = targets.length === selectors.length && targets.every(target => target?.isConnected);
  if (!ready && attempt < TARGET_WAIT_MAX) {
    state.targetTimer = setTimeout(() => findTarget(generation, attempt + 1), TARGET_WAIT_MS);
    return;
  }
  state.targetTimer = null;
  state.targets = ready ? targets : [];
  state.targets[0]?.scrollIntoView?.({ block: "center", inline: "nearest", behavior: "auto" });
  schedulePosition();
}


function renderStep() {
  const state = active;
  if (!state) return;
  clearPending(state);
  state.targets = [];
  const step = GUIDED_TUTORIAL_STEPS[state.index];
  const last = state.index === GUIDED_TUTORIAL_STEPS.length - 1;
  state.popover.replaceChildren(
    el("div", { class: "guided-tour-top" },
      el("span", { class: "guided-tour-progress" }, `Step ${state.index + 1} of ${GUIDED_TUTORIAL_STEPS.length}`),
      el("button", {
        class: "guided-tour-close",
        type: "button",
        title: "Stop tutorial",
        "aria-label": "Stop guided tutorial",
        onclick: stopGuidedTutorial,
      }, ico("x")),
    ),
    el("h2", { id: "guided-tour-title" }, step.title),
    el("p", {}, step.text),
    el("div", { class: "guided-tour-actions" },
      el("button", { class: "btn", type: "button", onclick: stopGuidedTutorial }, "Stop tutorial"),
      state.index > 0 ? el("button", { class: "btn", type: "button", onclick: () => moveTo(state.index - 1) }, "Back") : null,
      el("button", {
        class: "btn primary guided-tour-next",
        type: "button",
        onclick: () => moveTo(state.index + 1),
      }, last ? "Finish" : "Next", last ? null : ico("arrow")),
    ),
  );
  state.popover.setAttribute("aria-label", `${step.title}. Step ${state.index + 1} of ${GUIDED_TUTORIAL_STEPS.length}`);
  state.popover.style.visibility = "hidden";
  if (S.page !== step.page) go(step.page);
  const generation = ++state.generation;
  schedulePosition();
  findTarget(generation);
  requestAnimationFrame(() => {
    if (active === state) $(".guided-tour-next", state.popover)?.focus();
  });
}


function moveTo(index) {
  if (!active) return;
  if (index >= GUIDED_TUTORIAL_STEPS.length) {
    stopGuidedTutorial();
    go("fetch");
    toast("ok", "Tutorial complete. You can replay it from Settings.");
    return;
  }
  if (index < 0) return;
  active.index = index;
  renderStep();
}


export function guidedTutorialActive() {
  return active !== null;
}


export function stopGuidedTutorial() {
  const state = active;
  if (!state) return;
  active = null;
  clearPending(state);
  clearInterval(state.positionTimer);
  window.removeEventListener("resize", schedulePosition);
  window.removeEventListener("popstate", stopGuidedTutorial);
  document.removeEventListener("scroll", schedulePosition, true);
  document.removeEventListener("keydown", onKeyDown);
  document.body.classList.remove("guided-tutorial-open");
  state.root.remove();
  if (state.returnFocus?.isConnected) state.returnFocus.focus();
}


export function startGuidedTutorial() {
  stopGuidedTutorial();
  const returnFocus = document.activeElement;
  const root = el("div", { class: "guided-tour-root" });
  const shield = el("div", { class: "guided-tour-shield", "aria-hidden": "true" });
  const spotlight = el("div", { class: "guided-tour-spotlight", "aria-hidden": "true" });
  const popover = el("section", {
    class: "guided-tour-popover",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "guided-tour-title",
  });
  root.append(shield, spotlight, popover);
  document.body.append(root);
  active = {
    root,
    spotlight,
    popover,
    returnFocus,
    index: 0,
    targets: [],
    targetTimer: null,
    positionFrame: null,
    positionTimer: setInterval(schedulePosition, 250),
    generation: 0,
  };
  document.body.classList.add("guided-tutorial-open");
  window.addEventListener("resize", schedulePosition);
  window.addEventListener("popstate", stopGuidedTutorial);
  document.addEventListener("scroll", schedulePosition, true);
  document.addEventListener("keydown", onKeyDown);
  renderStep();
}
