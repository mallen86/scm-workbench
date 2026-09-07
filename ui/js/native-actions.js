/* Native OS-action facade.
 *
 * Packaged windows use the worker RPC for actions that belong to the host OS.
 * Browser mode keeps the existing HTTP compatibility routes. Once a callable
 * native bridge is selected, its rejection is the action's result: never retry
 * the action through HTTP.
 */

import { getTauriInvoke } from "./transport.js";


function browserJson(path, options) {
  return fetch(path, options).then(response => response.json().catch(() => ({})));
}


function nativeOrBrowser(method, params, browserPath, options) {
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method, params });
  return browserJson(browserPath, options);
}


/** Open a local file with the operating system's default application. */
export function openFile(path) {
  return nativeOrBrowser(
    "file.open",
    { path },
    `/api/file?path=${encodeURIComponent(path)}&open=1`,
  );
}


/** Reveal a local path in the operating system's file manager. */
export function revealPath(path) {
  return nativeOrBrowser(
    "file.reveal",
    { path },
    "/api/reveal",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) },
  );
}


/** Open a URL in the operating system's default browser. */
export function openExternalUrl(url) {
  return nativeOrBrowser(
    "url.open",
    { url },
    `/api/file?url=${encodeURIComponent(url)}`,
  );
}

/** Save an artifact through the native parented dialog and worker grant. */
export function saveArtifact(grantId, suggestedName) {
  const invoke = getTauriInvoke();
  if (!invoke) return Promise.reject(new Error("artifact export requires the app window"));
  return invoke("wb_save_artifact", { grantId, suggestedName });
}
