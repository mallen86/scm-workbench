import { el, ico } from "./core.js";
import { COMMAND_PREVIEW_EVENT, updatePreview } from "./forms.js";
import {
  startPdfPreview,
  pollPdfPreview,
  cancelPdfPreview,
} from "./pdf-preview-transport.js";


const PREVIEW_DEBOUNCE_MS = 750;
const POLL_DELAY_MS = 175;
const POLL_MAX = 100;
const RETRY_DELAY_MS = 2000;
const RETRY_MAX = 3;


function firstError(result, fallback) {
  if (Array.isArray(result?.errors) && typeof result.errors[0] === "string") {
    return result.errors[0];
  }
  return fallback;
}


function validOperationId(value) {
  return typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
}


export function pdfFrontPreviewPanel() {
  return el("section", { class: "pdf-front-preview", "aria-label": "First page PDF preview" },
    el("div", { class: "pdf-preview-head" },
      el("span", { class: "pdf-preview-title" }, ico("eye"), "First Page Preview"),
      el("span", { class: "pdf-preview-tag" }, "Low quality")),
    el("p", { class: "pdf-preview-intro" },
      "Built from up to 16 front images. Card backs and final print quality are not shown."),
    el("div", { class: "pdf-preview-stage", "data-pdf-preview-stage": "", "aria-live": "polite" },
      el("div", { class: "pdf-preview-empty" },
        ico("image"),
        el("span", {}, "Complete the PDF settings to see the first front page."))));
}


export function pdfValidationSummaryPanel() {
  return el("section", { class: "pdf-validation-summary", "aria-live": "polite" },
    el("div", { class: "pdf-validation-line muted" },
      ico("refresh"), el("span", {}, "Checking the PDF settings.")));
}


export function mountPdfFrontPreview(panel, validationPanel = null) {
  const stage = panel?.querySelector?.("[data-pdf-preview-stage]");
  if (!stage) return () => {};

  const state = {
    disposed: false,
    generation: 0,
    operationId: null,
    debounceTimer: null,
    focusTimer: null,
    pollTimer: null,
    retryTimer: null,
    resizeFrame: null,
    pollAbort: null,
    polls: 0,
    retries: 0,
    lastDetail: null,
    lastResult: null,
  };

  const clearTimers = () => {
    if (state.debounceTimer) clearTimeout(state.debounceTimer);
    if (state.focusTimer) clearTimeout(state.focusTimer);
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (state.retryTimer) clearTimeout(state.retryTimer);
    state.debounceTimer = null;
    state.focusTimer = null;
    state.pollTimer = null;
    state.retryTimer = null;
    if (state.resizeFrame !== null && typeof globalThis.cancelAnimationFrame === "function") {
      globalThis.cancelAnimationFrame(state.resizeFrame);
    }
    state.resizeFrame = null;
    state.pollAbort?.abort();
    state.pollAbort = null;
  };

  // WebKit can retain the stage's narrow-window grid height after the page
  // image grows during a window resize. Give the stage an explicit minimum
  // based on its current figure so overflow clipping can never hide the page's
  // bottom edge or caption. The next frame sees the post-resize image width.
  const scheduleStageFit = () => {
    if (state.disposed || !panel.isConnected || !stage.style ||
        typeof globalThis.requestAnimationFrame !== "function" ||
        typeof globalThis.getComputedStyle !== "function") return;
    if (state.resizeFrame !== null && typeof globalThis.cancelAnimationFrame === "function") {
      globalThis.cancelAnimationFrame(state.resizeFrame);
    }
    state.resizeFrame = globalThis.requestAnimationFrame(() => {
      state.resizeFrame = null;
      if (state.disposed || !panel.isConnected) return;
      const figure = stage.querySelector?.(".pdf-preview-figure");
      if (!figure?.getBoundingClientRect) {
        stage.style.removeProperty("--pdf-preview-fit-height");
        return;
      }
      const styles = globalThis.getComputedStyle(stage);
      const verticalSpace = ["paddingTop", "paddingBottom", "borderTopWidth", "borderBottomWidth"]
        .reduce((total, key) => total + (Number.parseFloat(styles[key]) || 0), 0);
      const figureHeight = figure.getBoundingClientRect().height;
      if (Number.isFinite(figureHeight) && figureHeight > 0) {
        stage.style.setProperty("--pdf-preview-fit-height",
          `${Math.max(330, Math.ceil(figureHeight + verticalSpace))}px`);
      }
    });
  };

  const replaceStage = (...children) => {
    if (state.disposed || !panel.isConnected) return;
    stage.innerHTML = "";
    stage.append(...children);
    scheduleStageFit();
  };

  const showWaiting = message => replaceStage(
    el("div", { class: "pdf-preview-empty" }, ico("image"), el("span", {}, message)));

  const paintValidation = result => {
    if (!validationPanel || state.disposed || !validationPanel.isConnected) return;
    validationPanel.innerHTML = "";
    const errors = Array.isArray(result?.errors) ? result.errors : [];
    const warnings = Array.isArray(result?.warnings) ? result.warnings : [];
    const seen = new Set();
    const addLine = (tone, icon, message) => {
      if (typeof message !== "string" || !message.trim() || seen.has(message)) return;
      seen.add(message);
      validationPanel.append(el("div", { class: `pdf-validation-line ${tone}` },
        ico(icon), el("span", {}, message)));
    };
    if (result?.pending) {
      addLine("muted", "refresh", "Waiting for the server to validate the PDF settings.");
      return;
    }
    for (const message of errors) addLine("err", "alert", message);
    for (const message of warnings) addLine("warn", "warncircle", message);
    if (errors.length) {
      addLine("err", "alert", "Fix the settings above before creating the PDF.");
    } else if (result?.no_front_images) {
      addLine("warn", "warncircle", "Add front images before creating the PDF.");
    } else if (result?.cmd) {
      addLine("ok", "check", "Ready to create. The settings and front folder passed validation.");
    } else {
      addLine("muted", "info", "Complete the settings above before creating the PDF.");
    }
  };

  const showError = (message, retryable = false) => {
    const actions = [];
    if (retryable && state.lastDetail) {
      actions.push(el("button", { class: "btn secondary pdf-preview-retry", type: "button",
        onclick: () => begin(state.lastDetail, true) }, ico("refresh"), "Try again"));
    }
    replaceStage(el("div", { class: "pdf-preview-error" },
      ico("warncircle"),
      el("div", {}, el("strong", {}, "Preview unavailable"), el("p", {}, message)),
      actions));
  };

  const cancelCurrent = () => {
    clearTimers();
    const operationId = state.operationId;
    state.operationId = null;
    if (operationId) cancelPdfPreview(operationId).catch(() => {});
  };

  const resultFigure = (result, updating = false) => {
    const image = el("img", {
      class: `pdf-preview-image${updating ? " updating" : ""}`,
      alt: "Low quality preview of the first PDF page",
      width: result.width,
      height: result.height,
    });
    image.onload = scheduleStageFit;
    image.src = `data:image/jpeg;base64,${result.data}`;
    const sampleText = result.available === result.sampled
      ? `Built from ${result.sampled} ${result.sampled === 1 ? "front" : "fronts"}.`
      : `Built from ${result.sampled} of ${result.available} discovered fronts.`;
    return el("figure", { class: "pdf-preview-figure" },
      image,
      el("figcaption", {}, sampleText,
        " This represents the first front page at reduced quality and may not reflect full deck ordering."));
  };

  const showLoading = (message = "Sampling up to 16 fronts and rendering the first page.") => {
    const loading = el("div", { class: "pdf-preview-loading" },
      el("span", { class: "spinner", "aria-hidden": "true" }),
      el("div", {}, el("strong", {}, "Building a low quality preview"),
        el("span", {}, message)),
      el("button", { class: "btn ghost pdf-preview-cancel", type: "button", onclick: () => {
        state.generation += 1;
        cancelCurrent();
        if (state.lastResult) {
          replaceStage(el("div", { class: "pdf-preview-refreshing stopped" },
            resultFigure(state.lastResult, true),
            el("div", { class: "pdf-preview-stopped" }, "Preview cancelled. Change a setting to rebuild it.")));
        } else {
          showWaiting("Preview cancelled. Change a setting to rebuild it.");
        }
      } }, "Cancel"));
    if (state.lastResult) {
      replaceStage(el("div", { class: "pdf-preview-refreshing" },
        resultFigure(state.lastResult, true), loading));
    } else {
      replaceStage(loading);
    }
  };

  const showResult = result => {
    if (result?.mime !== "image/jpeg" || typeof result.data !== "string" ||
        result.data.length > 700000 || !Number.isInteger(result.width) ||
        !Number.isInteger(result.height) || result.width < 1 || result.height < 1 ||
        result.width > 900 || result.height > 900 ||
        !Number.isInteger(result.sampled) || !Number.isInteger(result.available) ||
        result.sampled < 1 || result.sampled > 16 || result.available < result.sampled ||
        result.data.length % 4 !== 0 || !result.data.startsWith("/9j/") ||
        !/^[A-Za-z0-9+/]+={0,2}$/.test(result.data)) {
      showError("The preview image response was invalid.", true);
      return;
    }
    const previous = stage.querySelector?.("img");
    if (previous) previous.removeAttribute("src");
    state.lastResult = result;
    replaceStage(resultFigure(result));
  };

  const scheduleRetry = (generation, result) => {
    if (!result?.retryable || state.retries >= RETRY_MAX || state.disposed) {
      showError(firstError(result, "The representative preview could not be generated."),
        !!state.lastDetail);
      return;
    }
    state.retries += 1;
    showLoading(firstError(result, "Preview paused. Waiting to try again."));
    state.retryTimer = setTimeout(() => {
      if (!state.disposed && generation === state.generation && state.lastDetail) {
        begin(state.lastDetail, true);
      }
    }, RETRY_DELAY_MS);
  };

  const poll = async generation => {
    if (state.disposed || generation !== state.generation || !state.operationId) return;
    if (state.polls >= POLL_MAX) {
      const oldId = state.operationId;
      state.operationId = null;
      cancelPdfPreview(oldId).catch(() => {});
      showError("The representative preview did not finish in time.", true);
      return;
    }
    state.polls += 1;
    const controller = new AbortController();
    state.pollAbort = controller;
    try {
      const response = await pollPdfPreview(state.operationId, controller.signal);
      if (state.disposed || generation !== state.generation) return;
      state.pollAbort = null;
      if (response?.ok !== true) {
        throw new Error(response?.error?.message || "The PDF preview operation was not found.");
      }
      if (!["running", "done"].includes(response.status)) {
        throw new Error("invalid PDF preview poll response");
      }
      if (response.status === "running") {
        state.pollTimer = setTimeout(() => poll(generation), POLL_DELAY_MS);
        return;
      }
      state.operationId = null;
      const result = response.result;
      if (result?.ok === true) showResult(result);
      else if (result?.cancelled) showWaiting("Preview cancelled. Change a setting to rebuild it.");
      else scheduleRetry(generation, result);
    } catch (error) {
      if (state.disposed || generation !== state.generation || error?.name === "AbortError") return;
      state.pollAbort = null;
      const oldId = state.operationId;
      state.operationId = null;
      if (oldId) cancelPdfPreview(oldId).catch(() => {});
      showError(error?.message || "The representative preview could not be checked.", true);
    }
  };

  async function begin(detail, isRetry = false) {
    if (state.disposed || !panel.isConnected) return;
    state.generation += 1;
    const generation = state.generation;
    cancelCurrent();
    state.lastDetail = detail;
    if (!isRetry) state.retries = 0;
    state.polls = 0;

    const validated = detail?.result;
    if (!validated?.cmd || validated?.errors?.length || validated?.no_front_images) {
      if (validated?.no_front_images) showWaiting("Add front images to see the first front page.");
      else showWaiting("Complete the PDF settings to see the first front page.");
      return;
    }
    showLoading();
    try {
      // Do not abort a start request. If this generation becomes stale, its
      // returned operation id is immediately cancelled instead of being lost.
      const response = await startPdfPreview(detail.args);
      const returnedId = response?.operation?.id;
      if (state.disposed || generation !== state.generation) {
        if (validOperationId(returnedId)) cancelPdfPreview(returnedId).catch(() => {});
        return;
      }
      if (response?.ok !== true) {
        scheduleRetry(generation, response);
        return;
      }
      if (!validOperationId(returnedId) || response.operation?.status !== "running") {
        throw new Error("invalid PDF preview start response");
      }
      state.operationId = returnedId;
      state.pollTimer = setTimeout(() => poll(generation), POLL_DELAY_MS);
    } catch (error) {
      if (state.disposed || generation !== state.generation) return;
      showError(error?.message || "The representative preview could not be started.", true);
    }
  }

  const schedule = detail => {
    state.generation += 1;
    const generation = state.generation;
    cancelCurrent();
    state.lastDetail = detail;
    state.retries = 0;
    const validated = detail?.result;
    if (!validated?.cmd || validated?.errors?.length || validated?.no_front_images) {
      const previous = stage.querySelector?.("img");
      if (previous) previous.removeAttribute("src");
      state.lastResult = null;
      if (validated?.pending) showWaiting("Waiting for the server to validate the PDF settings.");
      else if (validated?.no_front_images) showWaiting("Add front images to see the first front page.");
      else showWaiting("Complete the PDF settings to see the first front page.");
      return;
    }
    showLoading("Waiting for the settings to settle before rendering.");
    state.debounceTimer = setTimeout(() => {
      if (!state.disposed && generation === state.generation) begin(detail);
    }, PREVIEW_DEBOUNCE_MS);
  };

  const onCommandPreview = event => {
    if (event?.detail?.kind !== "create_pdf") return;
    paintValidation(event.detail.result);
    schedule(event.detail);
  };
  const refreshIfMounted = () => {
    if (state.disposed || !panel.isConnected) return;
    if (state.focusTimer) clearTimeout(state.focusTimer);
    state.focusTimer = setTimeout(() => {
      if (!state.disposed && panel.isConnected) updatePreview("create_pdf");
    }, 100);
  };
  const onWindowFocus = () => refreshIfMounted();
  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") refreshIfMounted();
  };
  document.addEventListener(COMMAND_PREVIEW_EVENT, onCommandPreview);
  document.addEventListener("visibilitychange", onVisibilityChange);
  globalThis.window?.addEventListener?.("focus", onWindowFocus);
  globalThis.window?.addEventListener?.("resize", scheduleStageFit);

  return () => {
    if (state.disposed) return;
    state.disposed = true;
    state.generation += 1;
    document.removeEventListener(COMMAND_PREVIEW_EVENT, onCommandPreview);
    document.removeEventListener("visibilitychange", onVisibilityChange);
    globalThis.window?.removeEventListener?.("focus", onWindowFocus);
    globalThis.window?.removeEventListener?.("resize", scheduleStageFit);
    cancelCurrent();
    stage.style?.removeProperty?.("--pdf-preview-fit-height");
    const image = stage.querySelector?.("img");
    if (image) image.removeAttribute("src");
    state.lastResult = null;
    state.lastDetail = null;
    stage.innerHTML = "";
  };
}
