/* Preview transport facade.
 *
 * Packaged Tauri windows use the worker RPC directly when the callable bridge
 * is present.  A normal browser keeps the existing HTTP request verbatim.
 * Once native transport is selected, its rejection is the request result: it
 * must never be hidden by an HTTP fallback.
 */

import { getTauriInvoke } from "./transport.js";


export function preview(kind, args) {
  const params = { kind, args };
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method: "preview", params });

  const path = `/api/preview?kind=${encodeURIComponent(kind)}&args=${encodeURIComponent(JSON.stringify(args))}`;
  // Manual image selections can exceed browser/server URL-length limits.
  const request = kind === "postprocess_images" && args?.scope === "selected"
    ? fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) })
    : fetch(path);
  return request
    .then(r => r.json().then(d => ({ ok: r.ok, status: r.status, d })))
    .then(({ ok, status, d }) => {
      if (!ok) throw new Error(d?.error || `the server answered ${status}`);
      return d;
    });
}
