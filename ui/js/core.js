/* ==========================================================================
   SCM Workbench UI — core module
   DOM helpers, icon set, shared state (S), API plumbing, toasts,
   the confirm modal and the PAGES registry. Everything else imports
   from here. The entry point is ui/js/app.js.
   ========================================================================== */

import { getTauriInvoke, invokeNativeBootstrap, selectNativeBootstrapRoute } from "./transport.js";
import { openExternalUrl } from "./native-actions.js";

/* ----------------------------- tiny DOM helpers -------------------------- */

export const $ = (sel, root = document) => root.querySelector(sel);


export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));


export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "value") n.value = v;
    else if (k === "checked" || k === "selected" || k === "hidden" || k === "disabled" || k === "for") {
      if (v) n[k] = v; else if (k === "hidden" || k === "disabled") n.removeAttribute(k);
    }
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}


/* ---------------------------------- icons -------------------------------- */

export const P = {
  home: '<path d="M3 9.8 12 3l9 6.8"/><path d="M5.5 10.8V20h13V10.8"/>',
  download: '<path d="M12 3.5v11"/><path d="m7 10 5 4.5 5-4.5"/><path d="M4.5 19.5h15"/>',
  pdf: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.6"/><path d="M12 2.5v3.5M12 18v3.5M2.5 12H6M18 12h3.5"/>',
  scissors: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><path d="M8.2 7.6 20 18.5"/><path d="M8.2 16.4 20 5.5"/>',
  sparkle: '<path d="M12 2.5c.8 4.6 3 6.8 7.5 7.5-4.5.8-6.7 3-7.5 7.5-.8-4.5-3-6.7-7.5-7.5 4.5-.7 6.7-2.9 7.5-7.5z"/>',
  ruler: '<path d="m3.2 16.8 13.6-13.6 4.2 4.2L7.4 21z"/><path d="m8.7 11.3 2.1 2.1"/><path d="m11.9 8.1 2.1 2.1"/><path d="m15.1 4.9 2.1 2.1"/>',
  wrench: '<path d="M14.5 5.5a4.8 4.8 0 0 0-6.4 6.4L3 17l4 4 5.1-5.1a4.8 4.8 0 0 0 6.4-6.4L14 13l-3-3z"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5 5l2.2 2.2M16.8 16.8 19 19M5 19l2.2-2.2M16.8 7.2 19 5"/>',
  terminal: '<path d="m5.5 7.5 4 4.5-4 4.5"/><path d="M12.5 16.5H17.5"/><rect x="2.5" y="3.5" width="19" height="17" rx="3"/>',
  window: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M3 9.5h18"/><path d="M6.2 7h.1M8.8 7h.1"/>',
  copy: '<rect x="9" y="9" width="11.5" height="11.5" rx="2"/><path d="M5.5 14.5H4.6A1.6 1.6 0 0 1 3 12.9V4.6A1.6 1.6 0 0 1 4.6 3h8.3a1.6 1.6 0 0 1 1.6 1.6v.9"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4.2l2 2.5H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 3.5 2.7 19.5h18.6z"/><path d="M12 9.5v4.5M12 17v.1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.1"/>',
  play: '<path d="M8 5.2v13.6L19 12z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  image: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="8.7" cy="9.7" r="1.6"/><path d="m4.5 17.5 4.5-4.5 3.5 3.5 3-2.5 4 3.5"/>',
  layers: '<path d="m12 2.7 9 5.3-9 5.3L3 8z"/><path d="m3.5 12.7 8.5 5 8.5-5"/><path d="m3.5 17 8.5 5 8.5-5"/>',
  refresh: '<path d="M20 8.6A8.5 8.5 0 0 0 5.3 6.2L3 9"/><path d="M4 15.4A8.5 8.5 0 0 0 18.7 17.8L21 15"/><path d="M3 4.5V9h4.5"/><path d="M21 19.5V15h-4.5"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9.5 7V4.8a1.3 1.3 0 0 1 1.3-1.3h2.4a1.3 1.3 0 0 1 1.3 1.3V7"/><path d="M6.7 7l.9 13.2h8.8L17.3 7"/><path d="M10.2 10.8v6M13.8 10.8v6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6.7V12l3.4 2"/>',
  zap: '<path d="M13 2.5 4.8 13.5h6L9.5 21.5l8.7-11h-6z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.3"/><path d="m15.2 15.2 5 5"/>',
  card: '<rect x="4" y="3" width="16" height="18.5" rx="3.2"/><path d="M8.2 8.2h7.6M8.2 12.2h7.6M8.2 16.2h4.8"/>',
  book: '<path d="M4.5 5a2 2 0 0 1 2-2h13v18h-13a2 2 0 0 1-2-2z"/><path d="M4.5 19a2 2 0 0 1 2-2h13"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4 11.5 12.5"/><path d="M19 14.5v4a2 2 0 0 1-2 2H6.5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4.5"/>',
  eye: '<path d="M2.8 12S6.2 5.7 12 5.7 21.2 12 21.2 12 17.8 18.3 12 18.3 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="3"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>',
  arrow: '<path d="M4 12h16M14 6l6 6-6 6"/>',
};


export function ico(name, cls = "") {
  const s = P[name] || P.file;
  return el("span", {
    class: `ic ${cls}`,
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${s}</svg>`,
  });
}


export function iconize(root) {
  $$("[data-ico]", root || document).forEach(n => {
    n.innerHTML = "";
    n.append(ico(n.getAttribute("data-ico")));
  });
}


/* ---------------------------------- state --------------------------------- */

export const S = {
  info: null,
  manifest: {},
  page: "history",
  forms: {},          // kind -> {key: value}
  plugin: "mtg",
  activeJobId: null,
  jobs: [],
  startedJobIds: {},       // latest job started in this UI session, keyed by kind
  jobCompletionCutoffs: {}, // old page completion hidden after its output is invalidated
  jobArgs: {},              // immutable form snapshot keyed by job id
  es: null,                 // retained for compatibility with older page code
  esIdx: 0,
  jobSub: null,
  timers: {},
  firstBootDismissed: false,
  repoFailureDismissed: null,   // repo job id whose failure notice was closed
  updateNoticeDismissed: null,  // release tag whose update notice was closed
};


export const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));


export const fmtBytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? (n / 1024).toFixed(0) + " KB" : n + " B";


export const fmtTs = t => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });


export async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined;
  const nativeMethod = selectNativeBootstrapRoute(path, body);
  const invoke = nativeMethod && getTauriInvoke();
  if (invoke) {
    try {
      return await invokeNativeBootstrap(nativeMethod, invoke);
    } catch (e) {
      // Tauri command errors may arrive as a string or a plain object rather
      // than an Error, while callers of api() consistently expect Error-like
      // failures just as they get from fetch().
      if (e instanceof Error) throw e;
      throw new Error(typeof e === "string" ? e : e?.message || "Native request failed");
    }
  }
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok && !j.error && j.errors) throw new Error(j.errors.join("; "));
  if (!r.ok && j.error) throw new Error(j.error);
  return j;
}


/* --------------------------------- toasts -------------------------------- */

export function toast(kind, msg, ms = 3800) {
  const icons = { ok: "check", err: "alert", warn: "alert", info: "info" };
  const t = el("div", { class: `toast ${kind}` }, ico(icons[kind] || "info"), el("span", {}, msg));
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 280); }, ms);
}

/* Links (and "open this file" actions) can't window.open in the app's
   embedded webview — WebKit won't spawn a new window there. The native action
   facade selects the OS bridge in packaged windows and the compatibility HTTP
   route in a browser, while this wrapper keeps the shared result toast. */
export async function openUrl(url, what) {
  try {
    const r = await openExternalUrl(url);
    if (r.ok) toast("ok", `Opening ${what}…`);
    else toast("warn", r.errors?.[0] || r.error || `Couldn't open ${what}.`);
    return r;
  } catch (error) {
    toast("warn", error?.message || `Couldn't open ${what}.`);
    return null;
  }
}


/* --------------------------------- modal ---------------------------------- */

export function confirmModal({ title, text, paras = [], list = [], icon = "alert", iconCls = "warn", okLabel = "OK", okClass = "btn", danger = false }) {
  return new Promise(resolve => {
    const root = $("#modal-root");
    const m = $(".modal", root);
    m.innerHTML = "";
    m.append(el("div", { class: `m-ico ${iconCls}` }, ico(icon)));
    m.append(el("h3", {}, title));
    if (text) m.append(el("p", {}, text));
    for (const p of paras) m.append(el("p", {}, p));
    if (list.length) m.append(el("div", { class: "m-list" }, ...list.map(x => el("div", {}, x))));
    const actions = el("div", { class: "m-actions" },
      el("button", { class: "btn", onclick: () => { close(); resolve(false); } }, "Cancel"),
      el("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => { close(); resolve(true); } }, okLabel),
    );
    m.append(actions);
    root.hidden = false;
    function close() { root.hidden = true; document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey);
    $(".modal-backdrop", root).onclick = () => { close(); resolve(false); };
  });
}


/* ================================ navigation =============================== */

export const PAGES = {};   // filled by the page modules on import


/* --------------------------------- shared --------------------------------- */

export function pageHead(title, sub) {
  return el("div", { class: "page-head" }, el("h1", {}, title), el("div", { class: "sub" }, sub));
}
