/* --------------------------------------------------------------------------
   Frontend transport boundary.

   Only the small, read-only bootstrap slice is native for now. Everything
   else stays on the worker's HTTP origin until it gets its own transport.
   Keeping route selection pure makes the boundary straightforward to test
   without requiring a Tauri or DOM runtime.
   -------------------------------------------------------------------------- */

const NATIVE_BOOTSTRAP_ROUTES = Object.freeze({
  "/api/info": "info",
  "/api/manifest": "manifest",
  "/api/settings": "settings.get",
});


/** Return the allowlisted native method for a GET request, or null. */
export function selectNativeBootstrapRoute(path, body) {
  // This mirrors api()'s existing truthy-body -> POST convention. A native
  // call must never carry a request body, even for one of the read routes.
  if (body) return null;
  return NATIVE_BOOTSTRAP_ROUTES[path] || null;
}


/**
 * Return the Tauri invoke capability only when it is callable.
 *
 * Accepting an explicit scope keeps this seam mockable in focused checks and
 * avoids treating a marker object named __TAURI_INTERNALS__ as a bridge.
 */
export function getTauriInvoke(scope = typeof window === "undefined" ? null : window) {
  const internals = scope && scope.__TAURI_INTERNALS__;
  if (!internals || (typeof internals !== "object" && typeof internals !== "function")) return null;
  if (typeof internals.invoke !== "function") return null;
  return internals.invoke.bind(internals);
}


export function hasTauriInvoke(scope = typeof window === "undefined" ? null : window) {
  return !!getTauriInvoke(scope);
}


export function invokeNativeBootstrap(method, invoke = getTauriInvoke()) {
  return invoke("wb_rpc", { method, params: {} });
}
