/* User-selected card-art imports. Native file paths remain owned by the shell;
 * browser compatibility uploads File bytes. A native failure is final. */
import { getTauriInvoke } from "./transport.js";

export const CUSTOM_ART_MAX_FILES = 256;
export const CUSTOM_ART_MAX_FILE_BYTES = 32 * 1024 * 1024;
export const CUSTOM_BACK_MAX_FILE_BYTES = CUSTOM_ART_MAX_FILE_BYTES;
export const CUSTOM_ART_MAX_BATCH_BYTES = 512 * 1024 * 1024;
export const CUSTOM_ART_ACCEPT = ".jpg,.jpeg,.jpe,.jfif,.png,.apng,.gif,.webp,.tif,.tiff,.bmp,.dib,.avif,.heif,.heic,.qoi,.dds,.jp2,.j2k";
const destinations = new Set(["front", "double_sided", "back"]);
const destinationValue = value => {
  if (!destinations.has(value)) throw new Error("Choose the front, double-sided, or back image folder.");
  return value;
};
const checked = result => {
  if (!result || result.ok !== true) throw new Error(result?.errors?.[0] || "Image import failed.");
  return result;
};

function checkedImport(value) {
  const result = checked(value);
  if (!destinations.has(result.destination) || !Number.isInteger(result.imported) || result.imported < 0 || result.imported > (result.destination === "back" ? 1 : CUSTOM_ART_MAX_FILES) ||
      !Array.isArray(result.names) || result.names.length !== result.imported ||
      !Array.isArray(result.failed) || result.names.length + result.failed.length > (result.destination === "back" ? 1 : CUSTOM_ART_MAX_FILES) ||
      result.names.some(name => typeof name !== "string") ||
      result.failed.some(file => typeof file?.name !== "string" || typeof file?.error !== "string"))
    throw new Error("The image import returned an invalid result. Check the destination folder before trying again.");
  return result;
}

export const hasNativeCustomArt = () => !!getTauriInvoke();

async function http(path, options, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1, Math.min(60000, timeoutMs)));
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const result = await response.json();
    if (!response.ok) throw new Error(result?.errors?.[0] || result?.error || "Image import failed.");
    return checked(result);
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("The request timed out. Check the destination folder before trying again.");
    throw error;
  } finally { clearTimeout(timer); }
}

export async function openCustomArtFolder(destination) {
  destinationValue(destination);
  const invoke = getTauriInvoke();
  return checked(invoke ? await invoke("wb_rpc", { method: "custom_art.open_folder", params: { destination } }) :
    await http("/api/custom-art/open-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ destination }) }));
}

export async function importCustomArtDrop(destination, token) {
  destinationValue(destination);
  const invoke = getTauriInvoke();
  if (!invoke) throw new Error("Native file drop is unavailable.");
  return checkedImport(await invoke("wb_custom_art_import", { destination, token }));
}

export async function importCustomArtFiles(destination, suppliedFiles, onProgress = () => {}) {
  destinationValue(destination);
  if (getTauriInvoke()) throw new Error("Use the native drop zones or Choose images in the app.");
  const files = Array.from(suppliedFiles || []);
  if (destination === "back" && files.length !== 1) throw new Error("Choose exactly one card back image.");
  if (!files.length || files.length > CUSTOM_ART_MAX_FILES) throw new Error(`Choose between 1 and ${CUSTOM_ART_MAX_FILES} image files.`);
  const limit = destination === "back" ? CUSTOM_BACK_MAX_FILE_BYTES : CUSTOM_ART_MAX_FILE_BYTES;
  if (files.some(file => !Number.isSafeInteger(file.size) || file.size <= 0 || file.size > limit))
    throw new Error(destination === "back" ? "Card back must be non-empty and no larger than 32 MiB." : "Each image must be non-empty and no larger than 32 MiB.");
  if (files.reduce((total, file) => total + file.size, 0) > CUSTOM_ART_MAX_BATCH_BYTES)
    throw new Error("One import can contain at most 512 MiB of images.");
  const result = { ok: true, destination, imported: 0, names: [], failed: [] };
  const deadline = Date.now() + 600000;
  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    onProgress({ completed: index, total: files.length });
    if (Date.now() >= deadline) {
      result.failed.push(...files.slice(index).map(item => ({ name: item.name, error: "Import time limit reached; this file was not copied." })));
      break;
    }
    try {
      const query = new URLSearchParams({ destination, name: file.name });
      const imported = checkedImport(await http(`/api/custom-art/import?${query}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }, deadline - Date.now()));
      result.imported += imported.imported;
      result.names.push(...imported.names);
      result.failed.push(...imported.failed);
    } catch (error) {
      result.failed.push({ name: file.name, error: error.message || "Import failed." });
    }
  }
  onProgress({ completed: files.length, total: files.length });
  return result;
}

export async function chooseCustomArt(destination, onProgress) {
  destinationValue(destination);
  const invoke = getTauriInvoke();
  if (invoke) {
    const result = await invoke("wb_custom_art_pick", { destination });
    return result === null ? null : checkedImport(result);
  }
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = destination !== "back";
  input.accept = CUSTOM_ART_ACCEPT;
  input.hidden = true;
  const files = await new Promise((resolve, reject) => {
    const done = value => { input.remove(); resolve(value); };
    input.onchange = () => done(Array.from(input.files || []));
    input.oncancel = () => done([]);
    input.onerror = () => { input.remove(); reject(new Error("Could not open the image picker.")); };
    document.body.append(input);
    input.click();
  });
  return files.length ? importCustomArtFiles(destination, files, onProgress) : null;
}

export async function listenCustomArtDrops(handler, scope = typeof window === "undefined" ? null : window) {
  const invoke = getTauriInvoke(scope);
  if (!invoke) return () => {};
  const internal = scope?.__TAURI_INTERNALS__;
  if (typeof internal?.transformCallback !== "function") throw new Error("Native drop notifications are unavailable. Use Choose images instead.");
  const callback = internal.transformCallback(event => handler(event.payload));
  let eventId;
  try {
    eventId = await invoke("plugin:event|listen", { event: "custom-art-drop", target: { kind: "WebviewWindow", label: "main" }, handler: callback });
  } catch (error) {
    internal.unregisterCallback?.(callback);
    throw error;
  }
  let active = true;
  return async () => {
    if (!active) return;
    active = false;
    try { await invoke("plugin:event|unlisten", { event: "custom-art-drop", eventId }); }
    finally { internal.unregisterCallback?.(callback); }
  };
}
