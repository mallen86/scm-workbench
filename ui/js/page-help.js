/* Read-only help in a native HTML dialog. showModal keeps keyboard focus in
   the window and makes the underlying page inert without touching its form. */
import { S, el } from "./core.js";
import { pageHelpContent } from "./page-help-content.js";

let activeDialog = null;

export function showPageHelp(page, opener = document.activeElement) {
  const help = pageHelpContent(page, {
    mode: S.info?.settings?.ui_mode || "simple",
    packaged: !!S.info?.server?.is_packaged,
  });
  if (!help) return;
  if (activeDialog) activeDialog.close();

  const dialog = el("dialog", {
    class: "page-help-dialog",
    "aria-labelledby": "page-help-title",
    "aria-describedby": "page-help-intro",
  });
  const close = () => dialog.close();
  const closeButton = el("button", {
    class: "btn page-help-close", type: "button", autofocus: "",
    "aria-label": "Close page help", onclick: close,
  }, "Close");
  const body = el("div", { class: "page-help-body", tabindex: "0", "aria-label": "Help information" },
    el("p", { id: "page-help-intro", class: "page-help-intro" }, help.intro),
    ...help.sections.map(section => el("section", {},
      el("h3", {}, section.title), el("p", {}, section.text))));
  dialog.append(
    el("div", { class: "page-help-heading" },
      el("h2", { id: "page-help-title" }, help.title), closeButton), body,
  );
  dialog.addEventListener("keydown", event => {
    if (event.key === "Escape") event.stopPropagation();
    if (event.key !== "Tab") return;
    event.preventDefault();
    // The text pane is focusable so keyboard users can scroll long help.
    (document.activeElement === closeButton ? body : closeButton).focus();
  });
  // Escape uses the dialog's built-in cancel action. Close, Escape, and a
  // backdrop click all reach the same cleanup and focus restoration.
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (activeDialog !== dialog) return;
    activeDialog = null;
    const target = opener?.isConnected ? opener : document.querySelector(".page-help-button");
    target?.focus();
  }, { once: true });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close();
  });
  document.body.append(dialog);
  activeDialog = dialog;
  try {
    dialog.showModal();
    closeButton.focus();
  } catch (error) {
    activeDialog = null;
    dialog.remove();
    throw error;
  }
}
