/* The Custom game's source area. The fetch page still owns its shared cleanup
 * section; imports never run a fetch plugin or automatically process images. */
import { S, confirmModal, el, ico, toast } from "./core.js";
import { go } from "./nav.js";
import { chooseCustomArt, hasNativeCustomArt, importCustomArtDrop, importCustomArtFiles, listenCustomArtDrops, openCustomArtFolder } from "./custom-art-transport.js";

export const CUSTOM_ART_PLUGIN = "__custom_art";
export const CUSTOM_ART_CHANGED = "workbench:custom-art-changed";
export const customArtState = () => S.customArt || (S.customArt = { busy: false, destination: null, progress: null, result: null, error: null });
const changed = () => document.dispatchEvent(new Event(CUSTOM_ART_CHANGED));
export function clearCustomArtStatus() {
  const state = customArtState();
  state.result = null; state.error = null; state.progress = null;
  changed();
}
const labelFor = destination => destination === "front" ? "Front images" : destination === "back" ? "Card back" : "Double-sided images";

async function runImport(destination, operation) {
  const state = customArtState();
  if (state.busy) return;
  state.busy = true; state.destination = destination; state.error = null; state.result = null; state.progress = null;
  changed();
  try {
    const existing = S.info?.scm?.back_images || [];
    if (destination === "back" && existing.length) {
      const replace = await confirmModal({ title: "Replace the card back image?",
        text: `This replaces ${existing.length === 1 ? `“${existing[0].name}”` : `the ${existing.length} recognized images in game/back`}. Placeholders and non-image files stay. Choose exactly one image (up to 32 MiB).`,
        okLabel: "Replace card back", icon: "image" });
      if (!replace) return;
    }
    const result = await operation(progress => { state.progress = progress; changed(); });
    if (result !== null) {
      state.result = result;
      const failed = result.failed.length;
      toast(failed ? "warn" : "ok", destination === "back" && result.imported ? "Card back installed; exactly one recognized back is in place." : `${result.imported} image${result.imported === 1 ? "" : "s"} imported${failed ? `; ${failed} need attention` : ""}.`);
      if (result.imported) {
        try {
          const { refreshInfo } = await import("./info.js");
          await refreshInfo({ keepForms: true });
        } catch { state.error = "Images were copied, but folder information could not refresh. Reopen this page to refresh it."; }
      }
    }
  } catch (error) {
    state.error = error.message || "Image import failed.";
    toast("err", state.error);
  } finally {
    state.busy = false; state.progress = null;
    changed();
  }
}

export function renderCustomArt() {
  const wrap = el("section", { class: "card custom-art-source" },
    el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("image")),
      el("div", { class: "grow" }, el("h2", {}, "Your card images"),
        el("p", {}, "Drop or choose your card images. Front and double-sided originals are kept without overwriting; a card back replaces the existing recognized back image."))));
  const zones = el("div", { class: "custom-art-zones" });
  const status = el("div", { class: "custom-art-status", "aria-live": "polite", hidden: true });
  const actions = el("div", { class: "actions custom-art-next", hidden: true },
    el("button", { type: "button", class: "btn btn-ghost", onclick: () => go("postprocess", { scope: "both" }) }, ico("sparkle"), "Post-process images"),
    el("button", { type: "button", class: "btn primary", onclick: () => go("pdf") }, ico("arrow"), "Go to Create PDF"));
  const chooseButtons = [];
  for (const destination of ["front", "double_sided", "back"]) {
    const title = labelFor(destination);
    const choose = el("button", {
      type: "button", class: "custom-art-drop", "aria-label": `Choose ${title.toLowerCase()}`,
      onclick: () => runImport(destination, progress => chooseCustomArt(destination, progress)),
    }, ico("image"), el("strong", {}, destination === "back" ? "Drop one image here" : "Drop images here"), el("span", { class: "small" }, destination === "back" ? "or choose one image… (32 MiB max)" : "or choose images…"));
    chooseButtons.push(choose);
    const open = el("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": `Open ${title.toLowerCase()} folder`, onclick: async () => {
      open.disabled = true;
      try { await openCustomArtFolder(destination); }
      catch (error) { toast("err", error.message || "Could not open the image folder."); }
      finally { open.disabled = false; }
    } }, ico("folder"), "Open folder");
    const zone = el("section", { class: "custom-art-zone", "data-destination": destination, "aria-label": title },
      el("h3", {}, title), choose,
      el("div", { class: "custom-art-folder" }, el("span", { class: "small faint mono" }, `game/${destination}/`), open));
    zone.addEventListener("dragover", event => {
      event.preventDefault();
      if (hasNativeCustomArt() || customArtState().busy) return;
      event.dataTransfer.dropEffect = "copy";
      zone.classList.add("drag-over");
    });
    zone.addEventListener("dragleave", event => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove("drag-over");
    });
    zone.addEventListener("drop", event => {
      event.preventDefault(); event.stopPropagation(); zone.classList.remove("drag-over");
      // Native drop authorization comes exclusively from the shell token.
      if (hasNativeCustomArt()) return;
      const files = Array.from(event.dataTransfer?.files || []);
      if (files.length) runImport(destination, progress => importCustomArtFiles(destination, files, progress));
    });
    zones.append(zone);
  }
  wrap.append(zones, el("p", { class: "small faint custom-art-limits" }, "Front and double-sided: up to 256 images per import, 32 MiB each, 512 MiB total. Card back: exactly one image, up to 32 MiB; replaces the existing back."), status, actions);
  const repaint = () => {
    const state = customArtState();
    wrap.setAttribute("aria-busy", state.busy ? "true" : "false");
    chooseButtons.forEach(button => { button.disabled = state.busy; });
    status.replaceChildren();
    status.hidden = !state.busy && !state.error && !state.result;
    actions.hidden = state.busy || !state.result?.imported;
    if (state.busy) {
      const progress = state.progress;
      status.append(el("p", { class: "small" }, `Importing into ${labelFor(state.destination).toLowerCase()}${progress ? `: ${progress.completed} of ${progress.total}` : "…"}`),
        el("progress", { class: "custom-art-progress", "aria-label": "Image import progress", ...(progress ? { max: progress.total, value: progress.completed } : {}) }));
    }
    if (state.error) status.append(el("p", { class: "small warn" }, state.error));
    if (state.result) {
      const { imported, failed } = state.result;
      status.append(el("p", { class: `small ${failed.length ? "warn" : "ok"}` }, state.destination === "back"
        ? imported ? "Card back installed. Exactly one recognized image remains in game/back/." : "Could not confirm the card-back import. Check the destination folder and error details before trying again."
        : `${imported} image${imported === 1 ? "" : "s"} copied to ${labelFor(state.destination).toLowerCase()}.${failed.length ? ` ${failed.length} file${failed.length === 1 ? "" : "s"} need${failed.length === 1 ? "s" : ""} attention. Successfully copied images were kept.` : ""}`));
      if (failed.length) status.append(el("details", { class: "custom-art-failures" }, el("summary", {}, "Files needing attention"),
        failed.map(file => el("div", { class: "small" }, `${file.name}: ${file.error}`))));
    }
  };
  let disposed = false;
  let unlisten = null;
  wrap.__patch = () => {
    document.addEventListener(CUSTOM_ART_CHANGED, repaint);
    repaint();
    listenCustomArtDrops(payload => {
      if (disposed || !wrap.isConnected || customArtState().busy || !Number.isFinite(payload?.x) || !Number.isFinite(payload?.y)) return;
      const target = [...zones.children].find(zone => {
        const rect = zone.getBoundingClientRect();
        return payload.x >= rect.left && payload.x <= rect.right && payload.y >= rect.top && payload.y <= rect.bottom;
      });
      if (!target) return;
      if (payload.error) { customArtState().error = payload.error; changed(); return; }
      if (typeof payload.token === "string") runImport(target.dataset.destination, () => importCustomArtDrop(target.dataset.destination, payload.token));
    }).then(off => {
      if (disposed) Promise.resolve(off()).catch(() => {});
      else unlisten = off;
    }).catch(error => {
      if (!disposed) { customArtState().error = error.message || "Drop notifications are unavailable. Use Choose images instead."; repaint(); }
    });
  };
  wrap.__dispose = () => {
    disposed = true;
    document.removeEventListener(CUSTOM_ART_CHANGED, repaint);
    if (unlisten) Promise.resolve(unlisten()).catch(() => {});
  };
  return wrap;
}
