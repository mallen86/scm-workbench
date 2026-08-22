/* ============================================================================
   SCM Workbench — SPA
   Vanilla JS, no build step. Forms are rendered from the server manifest,
   so every option the server knows about is exactly what you can set here.
   ========================================================================== */
"use strict";

/* ----------------------------- tiny DOM helpers -------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...kids) {
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

const P = {
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
function ico(name, cls = "") {
  const s = P[name] || P.file;
  return el("span", {
    class: `ic ${cls}`,
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${s}</svg>`,
  });
}
function iconize(root) {
  $$("[data-ico]", root || document).forEach(n => {
    n.innerHTML = "";
    n.append(ico(n.getAttribute("data-ico")));
  });
}

/* ---------------------------------- state --------------------------------- */

const S = {
  info: null,
  manifest: {},
  page: "dashboard",
  forms: {},          // kind -> {key: value}
  plugin: "mtg",
  activeJobId: null,
  jobs: [],
  es: null,
  esIdx: 0,
  timers: {},
};

const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtBytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? (n / 1024).toFixed(0) + " KB" : n + " B";
const fmtTs = t => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined;
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok && !j.error && j.errors) throw new Error(j.errors.join("; "));
  if (!r.ok && j.error) throw new Error(j.error);
  return j;
}

/* --------------------------------- toasts -------------------------------- */

function toast(kind, msg, ms = 3800) {
  const icons = { ok: "check", err: "alert", warn: "alert", info: "info" };
  const t = el("div", { class: `toast ${kind}` }, ico(icons[kind] || "info"), el("span", {}, msg));
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 280); }, ms);
}

/* --------------------------------- modal ---------------------------------- */

function confirmModal({ title, text, paras = [], list = [], icon = "alert", iconCls = "warn", okLabel = "OK", okClass = "btn", danger = false }) {
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

const PAGES = {};   // filled below

function setNav(page) {
  $$("#nav .nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  $("#topbar-title").textContent = {
    dashboard: "Dashboard", fetch: "Fetch card art", pdf: "Create PDF", offset: "Offset & calibration",
    templates: "Cutting templates", extras: "Extras: MTG & Sorcery", sizes: "Sizes & layouts",
    utilities: "Utilities", settings: "Settings",
  }[page] || page;
  S.page = page;
}

// Each page has a real URL route (/pdf, /settings, …; dashboard is /) so
// refreshing stays on the page and the browser back/forward buttons work.
function pageFromPath() {
  const p = location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
  if (p === "") return "dashboard";
  return p in PAGES ? p : null;
}

function go(page, prefill, { push = true, anim = true } = {}) {
  if (prefill) applyPrefill(page, prefill);
  setNav(page);
  const pageEl = $("#page");
  pageEl.innerHTML = "";
  const content = PAGES[page](pageEl);
  if (content) {
    pageEl.append(content);
    if (typeof content.__patch === "function") content.__patch();
  }
  iconize(pageEl);
  // anim: false for internal re-renders (e.g. the prep watcher) — a silent
  // state update must not pulse the whole page like a navigation would
  if (anim && pageEl.firstElementChild) pageEl.firstElementChild.classList.add("page-anim");
  $(".page-scroll").scrollTop = 0;
  if (push) {
    const path = page === "dashboard" ? "/" : "/" + page;
    if (location.pathname !== path) history.pushState({ page }, "", path);
  }
}

window.addEventListener("popstate", () => {
  const page = pageFromPath();
  go(page || "dashboard", null, { push: false });
});

// Initial load: honor the URL we were given (refreshing /pdf must show PDF).
// Normalizes the path (trailing slash, unknown page) without adding history
// entries, so a hard refresh doesn't pollute the back button.
function bootPage() {
  const page = pageFromPath();
  const path = page ? (page === "dashboard" ? "/" : "/" + page) : "/";
  if (location.pathname !== path) history.replaceState({ page: page || "dashboard" }, "", path);
  go(page || "dashboard", null, { push: false });
}

function applyPrefill(page, prefill) {
  if (page === "pdf" && prefill && prefill.card_size) {
    S.forms.create_pdf = defaultArgs("create_pdf");
    S.forms.create_pdf.card_size = prefill.card_size;
  }
  if (page === "fetch" && prefill && prefill.plugin) S.plugin = prefill.plugin;
}

function bindNav() {
  $$("#nav .nav-item").forEach(a => a.onclick = () => go(a.dataset.page));
  $("#btn-console").onclick = toggleConsole;
  $$("#theme-switch .ts-btn").forEach(b => b.onclick = () => setTheme(b.dataset.theme));
}

function setTheme(theme) {
  const s = S.info?.settings || {};
  s.theme = theme;
  fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme }) });
  document.documentElement.dataset.theme = theme;
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === theme));
}

function setUiMode(mode) {
  const cur = (S.info?.settings?.ui_mode || "advanced") === "simple" ? "simple" : "advanced";
  if (mode === cur) return;
  fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ui_mode: mode }) })
    .then(async () => {
      await refreshInfo();
      toast("ok", mode === "simple"
        ? "Simple interface — the Create PDF page now shows the basic settings only."
        : "Advanced interface — the full layout is back.");
      go("settings");
    })
    .catch(() => toast("err", "could not save the interface mode"));
}

/* show commands the way a user would run them: a bare "python" interpreter
   (never the app's private one by absolute path) and repo-relative script
   paths. Real paths stay in job.cmd for the engine. */
const escRe = x => String(x || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function repoRowForKind(kind) {
  const key = kind.startsWith("extras_") ? "extras" : "scm";
  return (S.info?.repos || []).find(r => r.key === key) || null;
}
function displayCmd(cmd, kind) {
  if (!cmd) return "";
  let out = cmd;
  const py = S.info?.server?.python_path;
  if (py) out = out.replace(new RegExp("^[\"']?" + escRe(py) + "[\"']?\\s+"), "python ");
  const row = repoRowForKind(kind);
  if (row && row.path) out = out.split(row.path + "/").join("");
  return out;
}

/* ============================== form system =============================== */
/* Forms are rendered from the server manifest; values live in S.forms[kind]. */

function defaultArgs(kind) {
  const spec = S.manifest[kind];
  if (!spec) return {};
  const args = {};
  for (const g of spec.groups || [])
    for (const o of g.options)
      args[o.key] = o.type === "chips" || o.type === "choice_chips" ? (Array.isArray(o.default) ? [...o.default] : [])
        : (o.default !== undefined ? o.default : (o.type === "number" || o.type === "range" ? (o.default ?? "") : ""));
  return args;
}

function formArgs(kind) {
  if (!S.forms[kind]) S.forms[kind] = defaultArgs(kind);
  return S.forms[kind];
}

function afterFormChange(kind) {
  clearTimer(kind);
  S.timers[kind] = setTimeout(() => updatePreview(kind), 250);
}
function clearTimer(kind) { if (S.timers[kind]) { clearTimeout(S.timers[kind]); S.timers[kind] = null; } }

function updatePreview(kind) {
  const box = document.querySelector(`.cmdbox[data-kind="${kind}"]`);
  if (!box) return;
  fetch(`/api/preview?kind=${encodeURIComponent(kind)}&args=${encodeURIComponent(JSON.stringify(S.forms[kind]))}`)
    .then(r => r.json())
    .then(d => renderPreview(box, d))
    .catch(() => {});
}

function renderPreview(box, d) {
  box.innerHTML = "";
  const head = el("div", { class: "cb-head" },
    el("span", { class: "t" }, ico("terminal"), "Command preview"),
  );
  const kind = box.dataset.kind;
  const btn = el("button", { class: "cb-copy", onclick: () => { if (d.cmd) navigator.clipboard?.writeText(displayCmd(d.cmd, kind)).then(() => toast("ok", "Copied to clipboard")); } }, ico("copy"), "Copy");
  head.append(btn);
  box.append(head);
  const pRow = repoRowForKind(kind);
  if (!d.cmd) {
    box.append(el("pre", { class: "dim" }, "— incomplete —"));
  } else {
    box.append(el("pre", {}, displayCmd(d.cmd, kind)));
    // a managed copy lives inside the app — no point printing where
    if (d.cwd && !(pRow && pRow.mode === "managed")) box.append(el("pre", { class: "cb-cmt" }, el("span", { class: "p-cmt" }, `# cwd: ${d.cwd}`)));
  }
  for (const n of d.warnings || []) box.append(el("div", { class: "note warn" }, "⚠ ", n));
  for (const e of d.errors || []) box.append(el("div", { class: "note err" }, "✕ ", e));
  if (!d.errors?.length && d.cmd) box.append(el("div", { class: "note ok" }, "✓ ready to run"));
}

/* option renderers — one per manifest type */

function renderOption(o, args, kind) {
  const wrap = el("div", { class: `field w-${o.width || "full"}`, "data-key": o.key });

  if (o.show && !o.show(args)) return null;

  const label = el("label", {}, o.label + (o.required ? ' <span class="req">*</span>' : ""));

  switch (o.type) {
    case "text":
    case "path": {
      const i = el("input", { class: `input ${o.type === "path" ? "mono" : ""}`, placeholder: o.placeholder || "", value: strVal(args[o.key]) });
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind); });
      wrap.append(label, i);
      break;
    }
    case "textarea": {
      const t = el("textarea", { class: "input", rows: 5 });
      t.value = strVal(args[o.key]);
      t.addEventListener("input", () => { args[o.key] = t.value; afterFormChange(kind); });
      wrap.append(label, t);
      break;
    }
    case "number": {
      const i = el("input", { class: "input mono", type: "number", step: o.step || 1, value: strVal(args[o.key]) });
      const steppers = el("span", { class: "steppers" },
        el("button", { type: "button", onclick: () => stepNum(i, -(o.step || 1)) }, "−"),
        el("button", { type: "button", onclick: () => stepNum(i, (o.step || 1)) }, "+"),
      );
      const w = el("span", { class: "numwrap" }, i, steppers);
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind); });
      wrap.append(label, w);
      break;
    }
    case "range": {
      const i = el("input", { class: "range", type: "range", min: o.min ?? 0, max: o.max ?? 100, step: o.step ?? 1 });
      i.value = args[o.key] ?? o.default ?? 0;
      const val = el("input", { class: "rangeval", type: "number", step: 1, min: 0 });
      const sync = v => {
        val.value = v;
        i.value = Math.min(i.max, Math.max(i.min, Math.round(Number(v) / i.step) * i.step));
        i.style.setProperty("--fill", `${((i.value - i.min) / (i.max - i.min)) * 100}%`);
        args[o.key] = Number(v);
      };
      i.addEventListener("input", () => { sync(i.value); afterFormChange(kind); });
      val.addEventListener("change", () => {
        if (val.value !== "") {
          const n = Number(val.value);
          if (Number.isFinite(n) && n >= 0) { sync(n); afterFormChange(kind); return; }
        }
        sync(args[o.key]); // blank / invalid → back to last good value
        afterFormChange(kind);
      });
      sync(i.value);
      wrap.append(label, el("span", { class: "rangewrap" }, i, val));
      break;
    }
    case "select": {
      const sel = el("select", { class: "input" });
      for (const [v, lab] of o.choices) {
        const opt = el("option", { value: v }, lab);
        if (String(args[o.key]) === String(v)) opt.selected = true;
        sel.append(opt);
      }
      sel.addEventListener("change", () => { args[o.key] = sel.value; afterFormChange(kind); });
      wrap.append(label, sel);
      break;
    }
    case "segment": {
      const seg = el("div", { class: "seg" });
      for (const [v, lab] of o.choices) {
        seg.append(el("button", {
          type: "button",
          class: String(args[o.key]) === String(v) ? "active" : "",
          onclick: e => {
            args[o.key] = v;
            $$("button", seg).forEach(b => b.classList.remove("active"));
            e.currentTarget.classList.add("active");
            afterFormChange(kind);
            o.onChange && o.onChange(v);
          },
        }, lab));
      }
      wrap.append(label, seg);
      break;
    }
    case "toggle": {
      // same field grammar as every other control: small label on top,
      // the switch below it. Clicking the label toggles too (via `for`).
      const sw = el("span", { class: "switch" },
        el("input", { type: "checkbox", id: `sw-${o.key}`, checked: !!args[o.key] }),
        el("span", { class: "track" }),
        el("span", { class: "knob" }),
      );
      $("input", sw).addEventListener("change", e => { args[o.key] = e.target.checked; afterFormChange(kind); o.onChange && o.onChange(e.target.checked); });
      label.setAttribute("for", `sw-${o.key}`);
      wrap.append(label, sw);
      break;
    }
    case "chips": {
      const box = el("div", { class: "chips" });
      const inp = el("input", { placeholder: o.placeholder || "add & press Enter" });
      const redraw = () => {
        $$(".chipx", box).forEach(c => c.remove());
        for (const c of args[o.key] || []) {
          box.prepend(el("span", { class: "chipx" }, c,
            el("button", { type: "button", onclick: e => { (args[o.key] = args[o.key].filter(x => x !== c)).length; e.stopPropagation(); redraw(); afterFormChange(kind); } }, "×")));
        }
        box.append(inp);
      };
      inp.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          const v = inp.value.trim().replace(/,$/, "");
          if (v && !(args[o.key] || []).includes(v)) (args[o.key] = args[o.key] || []).push(v);
          inp.value = "";
          redraw();
          afterFormChange(kind);
        } else if (e.key === "Backspace" && !inp.value && (args[o.key] || []).length) {
          args[o.key].pop(); redraw(); afterFormChange(kind);
        }
      });
      box.onclick = () => inp.focus();
      redraw();
      wrap.append(label, box);
      break;
    }
    case "choice_chips": {
      const box = el("div", { class: "choicechips" });
      for (const [v, lab] of o.choices) {
        box.append(el("button", {
          type: "button",
          class: (args[o.key] || []).includes(v) ? "active" : "",
          onclick: e => {
            const cur = args[o.key] = args[o.key] || [];
            const i = cur.indexOf(v);
            if (i >= 0) cur.splice(i, 1); else cur.push(v);
            e.currentTarget.classList.toggle("active");
            afterFormChange(kind);
          },
        }, lab));
      }
      wrap.append(label, box);
      break;
    }
    default: {
      const i = el("input", { class: "input", value: strVal(args[o.key]) });
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind); });
      wrap.append(label, i);
    }
  }
  if (o.type === "path" && (o.key === "output_path" || o.key === "output_pdf_path")) {
    const row = repoRowForKind(kind);
    if (row && row.mode === "managed") wrap.append(el("span", { class: "help" },
      "Kept in the app's working area — after the run, use the console's “Move to my files…” to bring the result out to your own files."));
  }
  if (o.help) wrap.append(el("span", { class: "help" }, o.help));
  return wrap;
}
function strVal(v) { return v === null || v === undefined ? "" : String(v); }
function stepNum(input, d) {
  const v = parseFloat(input.value);
  input.value = (isNaN(v) ? 0 : v) + d;
  input.dispatchEvent(new Event("input"));
}

/* generic form card for a manifest kind */

function formCard(kind, opts = {}) {
  const spec = S.manifest[kind];
  if (!spec) return el("div", { class: "empty" }, "unknown job kind");
  const args = formArgs(kind);

  const card = el("div", { class: "card form-card", "data-kind": kind });
  if (opts.head !== false) {
    card.append(el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico(opts.icon || "play")),
      el("div", { class: "grow" },
        el("h2", {}, spec.title),
        spec.description ? el("p", {}, spec.description) : null,
      ),
    ));
  }

  for (const g of spec.groups || []) {
    const opts = (g.options || []).filter(o => optVisible(o, kind));
    if (!groupVisible(g, kind) || !opts.length) continue;
    if (g.collapsible) {
      const any = opts.some(o => o.show ? o.show(args) : true);
      if (!any) continue;
      const adv = el("div", { class: "adv" });
      adv.append(
        el("button", { class: "adv-head", type: "button", onclick: () => adv.classList.toggle("open") },
          el("span", { class: "arr" }, ico("arrow")), g.title),
        el("div", { class: "adv-body" }, groupInner(opts, kind, args)),
      );
      card.append(adv);
    } else {
      card.append(el("div", { class: "section-label", "data-label": true }, g.title), groupInner(opts, kind, args));
    }
  }

  if (opts.preview !== false) {
    const box = el("div", { class: "cmdbox", "data-kind": kind });
    card.append(box);
    setTimeout(() => updatePreview(kind), 0); // must run once the card is in the document
  }

  if (opts.run !== false) {
    const runBtn = el("button", { class: "btn primary", id: `run-${kind}` }, ico("play"), spec.title);
    runBtn.dataset.label = spec.title; // restored by doRun() when the button is re-enabled
    runBtn.onclick = () => doRun(kind, runBtn);
    const note = el("span", { class: "rb-note" }, "Runs in the background — watch the job console below.");
    card.append(el("div", { class: "runbar" }, note, runBtn));
    const missing = (S.manifest[kind] ? S.manifest[kind].needs || [] : []).filter(k => !repoReady(k));
    if (missing.length) {
      runBtn.disabled = true;
      runBtn.classList.add("wait");
      runBtn.innerHTML = "";
      runBtn.append(ico("refresh"), "Waiting for " + missing.join(" + ") + "…");
      card.append(el("div", { class: "prep-note" },
        "This page needs “" + missing.join("”, “") + "” — the button unlocks as soon as the preparation above finishes."));
    }
  }
  return card;
}

function groupInner(opts, kind, args) {
  const row = el("div", { class: "frow" });
  for (const o of opts) {
    const node = renderOption(o, args, kind);
    if (node) row.append(node);
  }
  return row;
}

/* ---- Simple / Advanced interface mode ------------------------------------
   "advanced" (the default) renders every manifest option — the full layout.
   "simple" keeps only the basics of a page: for a kind that has options
   flagged `simple` in the manifest, only those render, and its collapsible
   power sections (Fit & edge finishing, Advanced, …) are hidden. Kinds
   without any `simple` flags (Fetch, Offset, Calibration, …) are already as
   simple as they get and render unchanged. Hidden options keep their defaults
   in S.forms, so the command preview is identical in both modes. */
function uiMode() {
  const s = S.info && S.info.settings;
  return (s && (s.ui_mode || "advanced")) === "simple" ? "simple" : "advanced";
}
function kindHasSimple(kind) {
  const spec = S.manifest[kind];
  return !!(spec && (spec.groups || []).some(g => (g.options || []).some(o => o.simple)));
}
function optVisible(o, kind) {
  if (uiMode() !== "simple" || !kindHasSimple(kind)) return true;
  return !!o.simple;
}
function groupVisible(g, kind) {
  if (uiMode() !== "simple" || !kindHasSimple(kind)) return true;
  return !g.collapsible;
}

/* ================================ job control ============================== */

async function doRun(kind, btn, opts = {}) {
  if (!S.info || S.manifest[kind]) {
    for (const need of S.manifest[kind].needs || []) {
      if (need === "scm" && !S.info.scm.found) {
        return toast("err", (S.info.server.is_packaged && !repoReady("scm"))
          ? "silhouette-card-maker is still being prepared — the button unlocks when it's done."
          : "SCM repo not found — open Settings and point it at your silhouette-card-maker folder.");
      }
      if (need === "extras" && !S.info.extras.found) {
        return toast("err", (S.info.server.is_packaged && !repoReady("extras"))
          ? "scm-extras is still being prepared — the button unlocks when it's done."
          : "scm-extras repo not found — open Settings and point it at your scm-extras folder.");
      }
    }
  }
  if (opts.confirm) {
    const ok = await confirmModal(opts.confirm);
    if (!ok) return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = ""; btn.append(el("span", { class: "spinner" }), " Starting…"); }
  try {
    const r = await fetch("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, args: opts.args !== undefined ? opts.args : S.forms[kind] }),
    });
    const j = await r.json();
    if (!j.ok) {
      toast("err", j.errors?.join("; ") || "Failed to start job");
    } else {
      for (const w of j.warnings || []) toast("warn", w, 5200);
      toast("ok", `${j.job.title} — job started`);
      refreshJobs();
      openConsole(j.job.id);
      if (kind === "calibration" || kind === "dxf_batch" || kind === "dxf_single" || kind === "extras_generate" || kind === "clean_up" || kind === "repo_update" || kind === "repo_init") {
        setTimeout(() => refreshInfo(), 2500);
      }
      if (kind.startsWith("fetch:")) {
        // keep the user's form state (pasted decklists etc.) alive
        setTimeout(() => refreshInfo({ keepForms: true }), 2500);
      }
      return j.job;
    }
    return null;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = ""; btn.append(ico("play"), btn.dataset.label || "Run"); }
  }
}

let _lastJobsSig;

async function refreshJobs() {
  const next = (await api("/api/jobs")).jobs;
  // Redraw only when the job set actually changed (new job, status flip) —
  // the 4 s poll must not repaint an unchanged list (no blink).
  const sig = (next || []).map(j => j.id + ":" + j.status).join(",");
  if (sig === _lastJobsSig) { S.jobs = next; setRevealButtons(); return; }
  _lastJobsSig = sig;
  S.jobs = next;
  updateBadge();
  renderConsoleTabs();
  if (S.page === "dashboard") {
    const slot = $("#recent-jobs");
    if (slot) {
      slot.innerHTML = "";
      if (!S.jobs.length) slot.append(el("div", { class: "empty" }, ico("clock"), "No jobs yet — run something and it shows up here."));
      else for (const j of S.jobs.slice(0, 6)) slot.append(jobRow(j));
    }
  }
}

function jobRow(j) {
  return el("div", {
    class: "jobrow",
    onclick: () => { openConsole(j.id); },
  },
    el("div", { class: "jr-ico" }, ico("terminal")),
    el("div", { class: "jr-body" },
      el("div", { class: "jr-t" }, j.title),
      el("div", { class: "jr-cmd" }, displayCmd(j.cmd, j.kind) || ""),
    ),
    el("div", { class: "jr-meta" },
      el("div", { class: `statusdot ${j.status}` }, j.status),
      el("div", { class: "t" }, fmtTs(j.ts)),
    ),
  );
}

function updateBadge() {
  const n = S.jobs.filter(j => j.status === "running").length;
  const b = $("#badge-running");
  b.hidden = !n;
  b.textContent = n;
  b.classList.toggle("show", !!n);
}

/* ================================ console ================================= */

function toggleConsole() {
  const c = $("#console");
  c.hidden = !c.hidden;
  if (!c.hidden) {
    c.classList.remove("closed");
    if (S.activeJobId) attachStream(S.activeJobId, true);
  } else if (S.es) { S.es.close(); S.es = null; }
}

function openConsole(id) {
  const c = $("#console");
  c.hidden = false;
  requestAnimationFrame(() => c.classList.remove("closed"));
  S.activeJobId = id;
  renderConsoleTabs();
  attachStream(id, true);
}

function renderConsoleTabs() {
  const bar = $("#console-tabs");
  bar.innerHTML = "";
  const shown = S.jobs.slice(0, 9);
  if (!shown.length) return;
  for (const j of shown) {
    bar.append(el("button", {
      class: `ctab ${S.activeJobId === j.id ? "active" : ""}`,
      onclick: () => { S.activeJobId = j.id; renderConsoleTabs(); attachStream(j.id, true); },
    },
      el("span", { class: `dot ${j.status === "running" ? "warn" : j.status === "ok" ? "ok" : j.status === "fail" ? "" : ""}`, style: `background:var(--${j.status === "running" ? "accent" : j.status === "ok" ? "ok" : j.status === "fail" ? "err" : "warn"})` }),
      j.title.length > 26 ? j.title.slice(0, 26) + "…" : j.title,
    ));
  }
  $("#console-kill").disabled = S.activeJobId?.[0] && !(S.jobs.find(j => j.id === S.activeJobId)?.status === "running");
  setRevealButtons();
  updateFooter();
}

function attachStream(id, resume) {
  if (S.es) S.es.close();
  const log = $("#console-log");
  log.innerHTML = "";
  const job = S.jobs.find(j => j.id === id);
  const isRunning = job && job.status === "running";
  if (!isRunning && job) {
    // finished: load the stored log directly (works even after a server restart)
    api(`/api/jobs/${id}/log`).then(d => {
      for (const l of d.lines) appendLogLine(l);
      log.scrollTop = log.scrollHeight;
      if (d.status === "ok") appendLogLine("✓ done", "ok");
      if (d.status === "fail") appendLogLine(`✕ exited with code ${d.exit_code ?? "?"}`, "err");
    }).catch(() => appendLogLine("(log unavailable — job may have been recorded before a restart)", "dim"));
    updateFooter();
    return;
  }
  S.esIdx = 0;
  const es = new EventSource(`/api/jobs/${id}/stream?after=${S.esIdx}`);
  S.es = es;
  es.addEventListener("line", e => {
    const d = JSON.parse(e.data);
    appendLogLine(d.s, d.i);
    S.esIdx = d.i + 1;
    const logEl = $("#console-log");
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  });
  es.addEventListener("done", e => {
    const d = JSON.parse(e.data);
    S.esIdx = 0;
    es.close();
    S.es = null;
    const job = S.jobs.find(j => j.id === id);
    if (job) job.status = d.status;
    updateBadge();
    renderConsoleTabs();
    updateFooter();
    if (d.status === "ok") {
      appendLogLine("", null);
      appendLogLine("✓ done", "ok");
    } else if (d.status === "fail") {
      appendLogLine("", null);
      appendLogLine(`✕ exited with code ${d.exit_code ?? "?"}`, "err");
    }
  });
  es.onerror = () => { if (S.es === es) { /* auto-retry once */ } };
}

function appendLogLine(text, cls) {
  const log = $("#console-log");
  if (!log) return;
  let c = cls;
  if (!c) {
    if (/traceback|error:|exception|failed to start|not found/i.test(text)) c = "err";
    else if (/warn/i.test(text)) c = "warn";
    else if (/^(✓|\[ok\]|Done!|Generated PDF|Offset PDF|Calibration PDF:|  \[ok\]|Conversion complete)/i.test(text)) c = "ok";
    else if (/^(using|loaded|loaded |applying|converting|page \d)/i.test(text)) c = "info";
    else if (/^[\$\(]/.test(text)) c = "dim";
  }
  const ln = el("span", { class: `ln ${c ? "hl" : ""}` });
  if (text) ln.append(el("span", { class: `ln-${c || "plain"}` }, text));
  log.append(ln);
  if (log.children.length > 1500) log.children[0].remove();
}

function updateFooter() {
  const f = $("#console-foot");
  f.innerHTML = "";
  const job = S.jobs.find(j => j.id === S.activeJobId);
  if (!job) return;
  f.append(
    el("span", { class: `statusdot ${job.status}` }, job.status),
    job.exit_code != null ? el("span", { class: "mono" }, `exit ${job.exit_code}`) : null,
    el("span", { class: "mono" }, fmtTs(job.ts)),
    el("span", { class: "grow" }),
    (() => { const dc = displayCmd(job.cmd, job.kind); return el("span", { class: "mono", title: dc }, truncate(dc, 90)); })(),
    (() => {
      const row = repoRowForKind(job.kind);
      const managed = row && row.mode === "managed";
      const left = (job.outputs || []).filter(Boolean).length;
      if (job.status === "ok" && managed && left)
        return el("button", { class: "btn btn-ghost btn-sm", title: "Copy the output to a folder of your choice (system save dialog).",
          onclick: () => moveJobToMyFiles(job) }, ico("folder"), left > 1 ? `Move to my files… (${left})` : "Move to my files…");
      if (managed)
        return el("button", { class: "btn btn-ghost btn-sm", disabled: "",
          title: "The output stays in the app's private working area; when the run is done you can move it out." },
          ico("folder"), "In the app area");
      return el("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
        const r = await api("/api/reveal", { path: cwdOf(job) });
        if (r.ok) toast("ok", "Opened folder in your file manager"); else toast("warn", r.errors?.[0] || "Could not reveal folder");
      } }, ico("folder"), "Reveal folder");
    })(),
    el("button", { class: "btn btn-ghost btn-sm", onclick: () => { if (job.status === "running") api(`/api/jobs/${job.id}/kill`).then(() => toast("warn", "Stopping…")); } }, ico("stop"), "Stop"),
  );
}
function cwdOf(job) {
  const m = (job.cmd || "").match(/^(\S+)\s+(\S+\.py)/);
  const scm = S.info?.scm?.path, ex = S.info?.extras?.path;
  if (job.kind.startsWith("fetch:")) return scm ? scm + "/game/front" : "";
  if (job.kind === "create_pdf" || job.kind === "offset_pdf" || job.kind === "clean_up") return scm || "";
  if (job.kind === "calibration") return scm ? scm + "/calibration" : "";
  if (job.kind === "dxf_single" || job.kind === "dxf_batch" || job.kind === "dxf_list") return scm ? scm + "/cutting_templates" : "";
  if (job.kind.startsWith("extras_")) return ex || "";
  return scm || "";
}
function truncate(s, n) { return s && s.length > n ? "…" + s.slice(-n + 1) : s; }

/* “Move to my files…” — a managed run's artifacts (the create/offset PDFs,
   the calibration sheets) live in the app's private working area, so the
   console offers to carry them out through the native OS save panel.
   App window only: in a plain browser the save dialog doesn't exist. */
function moveJobToMyFiles(job) {
  const outs = (job.outputs || []).filter(Boolean);
  if (!outs.length) return;
  const bridge = window.pywebview && window.pywebview.api;
  if (!bridge || !bridge.pick_save) {
    toast("warn", "“Move to my files…” needs the app window — it opens the system save dialog.");
    return;
  }
  const next = outs[0];
  bridge.pick_save(next.split("/").pop()).then(dest => {
    if (!dest) return; // cancelled
    api("/api/files/save", { src: next, dest })
      .then(r => {
        if (r.ok) {
          toast("ok", `Saved “${r.name}” to your chosen location.`);
          job.outputs = outs.slice(1);
          setRevealButtons();
        } else toast("warn", (r.errors && r.errors[0]) || "Could not save the file.");
      })
      .catch(() => toast("warn", "Could not save the file."));
  }).catch(() => {});
}

function setRevealButtons() {
  const job = S.jobs.find(j => j.id === S.activeJobId);
  const btn = $("#console-reveal");
  if (!job || !btn) return;
  const row = repoRowForKind(job.kind);
  const managed = row && row.mode === "managed";
  const left = (job.outputs || []).filter(Boolean).length;
  const movable = job.status === "ok" && managed && left;
  btn.innerHTML = "";
  btn.append(ico("folder"));
  if (movable) {
    btn.append(left > 1 ? `Move to my files… (${left})` : "Move to my files…");
    btn.title = "Copy this run's output to a folder of your choice (system save dialog).";
    btn.disabled = false;
  } else if (managed) {
    btn.append("In the app area");
    btn.title = "This run's files stay in the app's private working area; once the run is done, “Move to my files…” brings them out.";
    btn.disabled = true;
  } else {
    btn.append("Reveal");
    btn.title = "Reveal output location";
    btn.disabled = false;
  }
}

/* bind console buttons (once) */
function bindConsole() {
  $("#console-close").onclick = () => {
    const c = $("#console");
    c.classList.add("closed");
    setTimeout(() => { c.hidden = true; if (S.es) { S.es.close(); S.es = null; } }, 220);
  };
  $("#console-copy").onclick = async () => {
    const log = $("#console-log");
    const txt = $$(".ln", log).map(n => n.textContent).join("\n");
    await navigator.clipboard?.writeText(txt);
    toast("ok", "Log copied");
  };
  $("#console-reveal").onclick = async () => {
    const job = S.jobs.find(j => j.id === S.activeJobId);
    if (!job) return;
    const row = repoRowForKind(job.kind);
    if (job.status === "ok" && row && row.mode === "managed" && (job.outputs || []).length) {
      moveJobToMyFiles(job);
      return;
    }
    const r = await api("/api/reveal", { path: cwdOf(job) });
    if (r.ok) toast("ok", "Opened in your file manager"); else toast("warn", r.errors?.[0] || "Could not reveal");
  };
  $("#console-kill").onclick = () => {
    if (!S.activeJobId) return;
    api(`/api/jobs/${S.activeJobId}/kill`).then(r => r.ok ? toast("warn", "Stopping…") : null);
  };
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#console").hidden && !$("#console").classList.contains("closed")) $("#console-close").click();
    if (e.key === "c" && (e.metaKey || e.ctrlKey) && e.shiftKey) { e.preventDefault(); toggleConsole(); }
  });
}

/* ================================ dashboard ================================ */

/* ---------------- repo prep state (first clone + updates) ------------------ */
const STAGE_NAMES = {
  download: "downloading the newest snapshot",
  extract: "unpacking the files",
  fingerprint: "fingerprinting the files",
  update: "updating the changed files",
  apply: "applying the changes",
};

function repoPrepRow(key) {
  return (S.info.repos || []).find(r => r.key === key) || null;
}

// A repo gates the pages that need it while it is unusable: not deployed yet
// (first clone still going) or a clone/update currently in flight.
function repoReady(key) {
  const row = repoPrepRow(key);
  if (!row) return true;
  if (row.progress) return false;
  if (row.deployed) return true;
  return key === "scm" ? !!S.info.scm.found : !!S.info.extras.found;
}

function prepActive() {
  if (!S.info || !S.info.server.is_packaged) return false;
  if (S.info.server.active) return true;
  return (S.info.repos || []).some(r => !r.deployed || r.progress);
}

// Structural identity: only the card's VISIBILITY is structural ("busy" =
// something on screen, "done" = card must go). Row appearance, stage flips,
// counters and speed are all patched in place — the bars never leave, so the
// page never re-renders (or animates) while a download is in flight.
function prepSignature() {
  if (!S.info) return "boot";
  const repos = S.info.repos || [];
  const active = S.info.server.active ? 1 : 0;
  const cardShown = active
    ? repos.some(r => !r.deployed)
    : repos.some(r => r.progress);
  return cardShown ? "busy" : "done";
}

function fmtRate(bps) {
  if (!bps) return "";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " MB/s";
  return Math.round(bps / 1e3) + " KB/s";
}

function fmtEta(sec) {
  if (!sec) return "";
  if (sec >= 60) return "~" + Math.round(sec / 60) + " min left";
  return "~" + sec + "s left";
}

// "412/564 MB (73%)  ·  6.4 MB/s  ·  ~24s left" — the numbers line per repo
function prepMeta(r) {
  const p = r.progress || {};
  const bits = [];
  if (p.total >= 1000) {
    const doneMB = (p.done || 0) / 1e6, totalMB = p.total / 1e6;
    const pct = Math.min(100, Math.round(100 * (p.done || 0) / p.total));
    bits.push((totalMB >= 10 ? Math.round(doneMB) : doneMB.toFixed(1)) + "/" +
               (totalMB >= 10 ? Math.round(totalMB) : totalMB.toFixed(1)) + " MB (" + pct + "%)");
    if ((p.stage === "download" || p.stage === "apply") && p.speed) bits.push(fmtRate(p.speed));
    if ((p.stage === "download" || p.stage === "apply") && p.eta) bits.push(fmtEta(p.eta));
  } else if (p.total > 0) {
    bits.push(Math.round(100 * (p.done || 0) / p.total) + "%");
  } else if (p.done) {
    // total unknown (chunked download): still show how much has arrived
    bits.push(Math.round((p.done / 1e6) * 10) / 10 + " MB received");
    if ((p.stage === "download" || p.stage === "apply") && p.speed) bits.push(fmtRate(p.speed));
  }
  return bits.join("  ·  ");
}

// S.prows: per-repo handles into the on-screen bar rows (in-place patching).
function ensurePrepRows(rows, container) {
  S.prows = S.prows || {};
  for (const r of rows) {
    if (S.prows[r.key] && S.prows[r.key].isConnected) continue;  // a page re-render replaced the strip — rebuild the row
    const row = el("div", { class: "rp-row" });
    row.append(
      el("div", { class: "rp-label" }),
      el("div", { class: "rp-meta mono" }),
      el("div", { class: "rp-bar" }, el("div", { class: "rp-fill" })));
    container.append(row);
    S.prows[r.key] = row;
  }
  const keys = new Set(rows.map(r => r.key));
  for (const k of Object.keys(S.prows)) if (!keys.has(k)) { S.prows[k].remove(); delete S.prows[k]; }
}

function updatePrepRows() {
  const container = $("#repoprog");
  if (!container) return;
  const rows = (S.info.repos || []).filter(r => r.progress || (S.info.server.active && !r.deployed));
  ensurePrepRows(rows, container);
  for (const r of rows) {
    const row = S.prows[r.key];
    if (!row) continue;
    const p = r.progress || {};
    const det = p.total > 0;
    const pct = det ? Math.min(100, Math.round(100 * (p.done || 0) / p.total)) : 0;
    row.children[0].textContent = r.name + "  —  " + (STAGE_NAMES[p.stage] || p.stage || "working");
    row.children[1].textContent = prepMeta(r);
    const bar = row.children[2], fill = bar.firstElementChild;
    bar.classList.toggle("indet", !det);
    if (det) fill.style.width = pct + "%";
  }
}

let _prepTimer = null;
function stopPrepWatcher() {
  if (_prepTimer) clearTimeout(_prepTimer);
  _prepTimer = null;
}

// While any repo is still being prepared, poll /api/info: counters patch the
// bars in place; only a structural change (stage flip, repo ready) re-renders
// the page — which is what unlocks the waiting run buttons.
function startPrepWatcher() {
  stopPrepWatcher();
  const tick = async () => {
    if (!prepActive()) { _prepTimer = null; return; }
    const before = prepSignature();
    _prepTimer = setTimeout(tick, 2500);
    try {
      await refreshInfo({ keepForms: true, jobs: false });
      const now = prepSignature();
      if (now !== before) {
        if (now === "done") toast("ok", "Your repos are ready — every page is live.");
        go(S.page || "dashboard", null, { push: false, anim: false });
      } else {
        updatePrepRows();
      }
    } catch (e) { /* server briefly busy — the next tick retries */ }
  };
  if (prepActive()) tick();
}

PAGES.dashboard = (root) => {
  const wrap = el("div", {});
  const s = S.info.settings;

    // Live repo-prep status: a bar per busy repo (first clone / update). The
  // global watcher (startPrepWatcher) re-renders this page as the bars move
  // and unlocks the run buttons the moment a repo is ready.
  const busy = (S.info.repos || []).filter(r => r.progress || (S.info.server.active && !r.deployed));
  if (busy.length) {
    wrap.append(el("div", { class: "card prep-card" },
      el("div", { class: "card-head" },
        el("div", { class: "card-ico" }, ico("refresh")),
        el("div", { class: "grow" },
          el("h2", {}, "Preparing your repos"),
          el("p", {}, "First-launch setup runs in the background. The bars below track it live — buttons that need a repo stay disabled until it's ready, then unlock by themselves.")),
      ),
      el("div", { class: "repoprog", id: "repoprog" })));
    updatePrepRows();
  }

  if (connectCardNeeded()) {
    wrap.append(repoSetupCard());
  } else if (!s.onboarded) {
    wrap.append(onboardCard());
  }
  wrap.append(statusGrid());
  wrap.append(el("div", { class: "section-label" }, "Quick actions"));
  wrap.append(quickActions());
  wrap.append(el("div", { class: "section-label" }, "Layout matrix (default variant)"));
  wrap.append(matrixCard("default"));
  wrap.append(el("div", { class: "section-label" }, "Recent jobs"));
  wrap.append(el("div", { id: "recent-jobs" }));
  wrap.append(el("div", { class: "card" }, el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("book")),
    el("div", { class: "grow" }, el("h2", {}, "Documentation"), el("p", {}, "The docs site walks through every step with photos: supplies, printing, cutting, troubleshooting.")),
    el("button", { class: "btn", onclick: () => window.open("https://alan-cha.github.io/silhouette-card-maker/") }, ico("external"), "Open docs"),
  )));
  return wrap;
};

function onboardCard() {
  const steps = [
    ["1", "Fetch card art", "Pick a game (MTG, Pokémon, …) and a decklist. Art lands in game/front."],
    ["2", "Create the PDF", "Cards are laid out on your paper size with registration marks. Print double-sided."],
    ["3", "Calibrate & offset", "Print a calibration sheet, measure drift, apply X/Y/angle offsets to the PDF."],
    ["4", "Cut with a template", "Open the matching .studio3 template in Silhouette Studio and cut."],
  ];
  return el("div", { class: "onboard" },
    el("h2", {}, "Welcome to the Workbench"),
    el("p", { class: "muted", style: "margin-top:6px; font-size:13px; max-width:720px; line-height:1.55" },
      "This console wraps every script in silhouette-card-maker + scm-extras — no terminal required. The workflow has four stages:"),
    el("div", { class: "steps" },
      steps.map(([n, t, d]) => el("div", { class: "step" },
        el("div", { class: "n" }, n), el("div", { class: "t" }, t), el("div", { class: "d" }, d),
      )),
    ),
    el("div", { style: "margin-top:16px" },
      el("button", { class: "btn primary", onclick: async () => { await api("/api/settings", { onboarded: true }); S.info.settings.onboarded = true; go("dashboard"); } }, "Got it — show me the dashboard"),
    ),
  );
}

// The connect card appears only when the user has to act: the repo is not
// found AND the app isn't auto-fetching its own managed copy (packaged
// prep). While a managed clone/update is in flight the "Preparing" card is
// the single source of truth; a custom folder can always be pointed in via
// Settings > Repos afterwards.
function connectCardNeeded() {
  if (!S.info) return true;
  if (S.info.scm.found) return false;
  // hidden only while the app is actively preparing (bootstrap flag set) —
  // a failed/stopped prep must still show the card so a folder can be pointed in
  if (S.info.server.is_packaged && S.info.server.active) return false;
  return true;
}

function repoSetupCard() {
  const wrap = el("div", { class: "card" });
  wrap.append(
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("folder")),
      el("div", { class: "grow" },
        el("h2", {}, "Connect your repos"),
        el("p", {}, S.info.server.is_packaged && prepActive()
          ? "Your own managed copies of both repos are being downloaded — the pages connect themselves the moment each one is ready. Pasting a path below (a folder you already have) takes priority instead."
          : "The Workbench needs to find silhouette-card-maker. It was not found next to this project — paste the folder paths below."),
      ),
    ),
  );
  const row = el("div", { class: "frow" });
  const scmInp = el("input", { class: "input mono", placeholder: "/path/to/silhouette-card-maker" });
  const exInp = el("input", { class: "input mono", placeholder: "/path/to/scm-extras (optional)" });
  row.append(
    el("div", { class: "field w-half" }, el("label", {}, "silhouette-card-maker"), scmInp),
    el("div", { class: "field w-half" }, el("label", {}, "scm-extras (optional)"), exInp),
  );
  wrap.append(row);
  wrap.append(el("div", { style: "margin-top:14px; display:flex; gap:10px; align-items:center" },
    el("button", { class: "btn primary", onclick: async () => {
      const r = await api("/api/settings", { scm_dir: scmInp.value.trim(), extras_dir: exInp.value.trim() });
      await refreshInfo();
      toast(S.info.scm.found ? "ok" : "err", S.info.scm.found ? "Connected! Reloaded the dashboard." : "Still not found — check the path and try again.");
      go("dashboard");
    } }, ico("check"), "Save & reconnect"),
    el("span", { class: "faint small" }, "Paths are stored in this project's settings.json (data folder)."),
  ));
  return wrap;
}

function statusGrid() {
  const i = S.info, s = i.scm, ex = i.extras, sv = i.server;
  const repos = i.repos || [];
  const rr = k => repos.find(r => r.key === k);
  const rS = rr("scm"), rE = rr("extras");
  const dTag = (r, base, detail) => (r && r.mode === "managed" && r.deployed)
    ? `managed copy · ${r.deployed.ref}${detail ? " · " + detail : ""}`
    : (detail ? `${base} — ${detail}` : base);
  const grid = el("div", { class: "status-grid" });
  const card = (icoName, cls, title, desc, dot) => {
    const c = el("div", { class: "statuscard" },
      el("div", { class: `st-ico`, style: cls ? `color:var(${cls}); background:var(${cls}-soft)` : "" }, ico(icoName)),
      el("div", { class: "st-body" },
        el("div", { class: "st-t" }, title, dot ? el("span", { class: `dot ${dot}` }) : null),
        desc ? el("div", { class: "st-d" }, desc) : null,
      ));
    grid.append(c);
  };
  card("terminal", "--ok", `Python ${sv.python}`, (i.server.is_packaged && sv.python_path) ? "private runtime inside the app" : sv.python_path, "ok");
  const preparing = i.server.is_packaged && prepActive();
  card("card", s.found ? "--ok" : "--err", s.found ? `silhouette-card-maker v${s.version || "?"}` : "silhouette-card-maker", s.found ? dTag(rS, s.path) : (preparing ? "preparing — managed copy in progress" : "not connected"), s.found ? "ok" : "");
  card("sparkle", ex.found ? "--info" : "--warn", ex.found ? "scm-extras" : "scm-extras (optional)", ex.found ? dTag(rE, ex.path, `${ex.card_sizes.length} extra sizes`) : (preparing ? "preparing — managed copy in progress" : "not connected — MTG/Sorcery extras unavailable"), ex.found ? "ok" : "");
  card("scissors", "--accent", "Cutting templates",
    `${s.templates.dxf.length + s.templates.borderless_dxf.length} DXF · ${s.templates.studio3.length + s.templates.borderless_studio3.length} studio3${ex.found ? ` · extras: ${ex.templates.dxf.length + ex.templates.borderless_dxf.length} DXF, ${ex.templates.studio3.length + ex.templates.borderless_studio3.length} studio3` : ""}`, "ok");
  card("target", "--info", "Calibration sheets", `${s.calibration.length} PDF${s.calibration.length === 1 ? "" : "s"} in calibration/`, "ok");
  const nPso = Object.keys(S.info.per_size_offsets || {}).length;
  card("copy", (s.saved_offset || nPso) ? "--accent" : "--warn", "Saved offset",
    (s.saved_offset ? `x ${s.saved_offset.x} · y ${s.saved_offset.y} · ${s.saved_offset.angle}°` : "none saved yet")
      + (nPso ? ` · ${nPso} per-size row${nPso === 1 ? "" : "s"}` : ""),
    (s.saved_offset || nPso) ? "ok" : "");
  return grid;
}

function quickActions() {
  const g = el("div", { class: "qa-grid" });
  const qa = (icoName, title, desc, fn) => g.append(el("div", { class: "qa", onclick: fn },
    el("div", { class: "qa-ico" }, ico(icoName)), el("div", { class: "qa-t" }, title), el("div", { class: "qa-d" }, desc)));
  qa("pdf", "Create PDF", "Lay out your card art into a print-ready PDF", () => go("pdf"));
  qa("download", "Fetch MTG art", "Download card images from a decklist (Archidekt, Moxfield, MTGA, …)", () => { S.plugin = "mtg"; go("fetch", { plugin: "mtg" }); });
  qa("sparkle", "Fetch other games", "22 TCGs & LCGs supported — Yu-Gi-Oh!, Pokémon, Lorcana, …", () => { go("fetch"); });
  qa("scissors", "Generate DXF templates", "Create a cutting template for any card × paper size", () => go("templates"));
  qa("target", "Calibration sheets", "Print alignment sheets to measure printer drift", () => go("offset"));
  qa("trash", "Start fresh", "Clear the front / double-sided image folders", () => go("utilities"));
  const due = (S.info.repos || []).filter(r => r.mode === "managed" && r.last_check && r.last_check.checked && r.last_check.checked.ok && !r.last_check.checked.up_to_date);
  if (due.length) qa("refresh", "Repo updates available", due.map(r => `${r.name} → ${r.last_check.checked.target.ref}`).join(" · "), () => go("settings"));
  return g;
}

/* ============================ matrix / sizes pages ========================= */

function matrixCard(variant, compact) {
  const card = el("div", { class: "card" });
  const seg = el("div", { class: "seg" });
  for (const [v, lab] of [["default", "Default"], ["borderless", "Borderless"]]) {
    seg.append(el("button", {
      type: "button", class: variant === v ? "active" : "",
      onclick: e => {
        $$("#matrix-variant button").forEach(b => b.classList.remove("active"));
        e.currentTarget.classList.add("active");
        card.replaceChildren(head(), table(v));
        iconize(card);
      },
    }, lab));
  }
  function head() {
    return el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("layers")),
      el("div", { class: "grow" },
        el("h2", {}, "Cards per page"),
        el("p", {}, "Paper size × card size. Cells show columns × rows (total)."),
      ),
      el("div", { id: "matrix-variant" }, seg),
    );
  }
  function table(v) {
    const t = el("table", { class: "matrix" });
    const papers = S.info.scm.paper_sizes;
    t.append(el("tr", {}, el("th", { class: "rowhead" }, "Card size"),
      ...papers.map(p => el("th", {}, p.name))));
    let maxN = 0;
    const rows = S.info.scm.card_sizes.map(c => {
      const tr = el("tr", {}, el("th", { class: "rowhead" }, c.name));
      for (const p of papers) {
        const l = (S.info.scm.layouts[p.name]?.[c.name] || {})[v];
        const n = l ? l.num_cols * l.num_rows : 0;
        maxN = Math.max(maxN, n);
        if (l) {
          tr.append(el("td", { class: `d${Math.min(3, Math.floor(n / Math.max(1, maxN) * 3))}` },
            `${l.num_cols}×${l.num_rows}`, el("span", { class: "sub" }, `${n} cards`)));
        } else tr.append(el("td", { class: "na" }, "—"));
      }
      return tr;
    });
    t.append(...rows);
    return t;
  }
  card.append(head(), table(variant));
  return card;
}

PAGES.sizes = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Sizes & layouts", "Every card size the repos know about — core + extras — with a scaled silhouette of each card, plus the full paper × card layout matrix."));
  wrap.append(el("div", { class: "section-label" }, "Card sizes"));
  wrap.append(sizeExplorer());
  wrap.append(el("div", { class: "section-label" }, "Paper sizes"));
  wrap.append(paperStrip());
  wrap.append(el("div", { class: "section-label" }, "Specialty layouts"));
  wrap.append(specialtyRow());
  wrap.append(el("div", { class: "section-label" }, "Layout matrix"));
  wrap.append(matrixCard("default"));
  return wrap;
};

function sizeExplorer() {
  const grid = el("div", { class: "sizegrid" });
  for (const c of S.info.scm.card_sizes) {
    const w = mm(c.width), h = mm(c.height);
    const scale = Math.min(64 / w, 84 / h, 1);
    const card = el("button", { class: "sizecard", onclick: () => go("pdf", { card_size: c.name }) },
      el("div", { class: "sc-svg", html: cardSvg(w * scale, h * scale, mm(c.radius) * scale) }),
      el("div", { class: "sc-name" }, c.name),
      el("div", { class: "sc-dims" }, `${w ? w.toFixed(1) : "?"} × ${h ? h.toFixed(1) : "?"} mm`),
      el("div", { class: "sc-tags" },
        c.source === "extras" ? el("span", { class: "tag extras" }, "extras") : null,
        ...(c.aliases || []).slice(0, 3).map(a => el("span", { class: "tag aliases" }, a)),
      ),
    );
    grid.append(card);
  }
  return grid;
}

function cardSvg(w, h, r) {
  const maxW = 64, maxH = 84;
  const scale = Math.min(maxW / (w || 1), maxH / (h || 1));
  const W = (w || 60) * scale, H = (h || 80) * scale, R = Math.min((r || 3) * scale, W / 2, H / 2);
  return `<svg width="${Math.round(W)}" height="${Math.round(H)}" viewBox="0 0 ${Math.round(W)} ${Math.round(H)}" aria-hidden="true">
    <rect x="1" y="1" width="${W - 2}" height="${H - 2}" rx="${Math.max(1, R - 1)}" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1.6"/>
    <line x1="${W * 0.2}" y1="${H * 0.32}" x2="${W * 0.8}" y2="${H * 0.32}" stroke="var(--accent)" stroke-width="1" opacity="0.5"/>
    <line x1="${W * 0.2}" y1="${H * 0.5}" x2="${W * 0.8}" y2="${H * 0.5}" stroke="var(--accent)" stroke-width="1" opacity="0.5"/>
    <line x1="${W * 0.2}" y1="${H * 0.68}" x2="${W * 0.62}" y2="${H * 0.68}" stroke="var(--accent)" stroke-width="1" opacity="0.5"/>
  </svg>`;
}

function paperStrip() {
  const strip = el("div", { class: "paperstrip" });
  for (const p of S.info.scm.paper_sizes) {
    const w = mm(p.width), h = mm(p.height);
    const scale = 96 / Math.max(w, h);
    const W = w * scale, H = h * scale;
    strip.append(el("div", { class: "papercard" },
      el("div", { html: `<svg width="${Math.round(W)}" height="${Math.round(H)}" viewBox="0 0 ${W} ${H}"><rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="3" fill="var(--info-soft)" stroke="var(--info)" stroke-width="1.4"/></svg>` }),
      el("div", { class: "pn" }, p.name),
      el("div", { class: "pd" }, `${w.toFixed(1)} × ${h.toFixed(1)} mm`),
    ));
  }
  return strip;
}

function specialtyRow() {
  const row = el("div", { class: "frow" });
  const items = S.info.scm.specialty;
  if (!items.length) return el("div", { class: "empty" }, "No specialty layouts defined.");
  for (const s of items) {
    row.append(el("div", { class: "field w-full" },
      el("label", {}, s.name),
      el("div", { class: "small muted" }, `${s.paper} · ${s.cols}×${s.rows} grid · card ${s.width} × ${s.height}`),
    ));
  }
  return row;
}

function mm(sizeStr) {
  if (!sizeStr) return 0;
  const m = /([\d.]+)\s*(mm|in)/.exec(String(sizeStr));
  if (!m) return parseFloat(sizeStr) || 0;
  return m[2] === "mm" ? parseFloat(m[1]) : parseFloat(m[1]) * 25.4;
}

/* ================================ fetch page =============================== */

PAGES.fetch = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Fetch card art", "Pick a game, give it a decklist (from the list, a file you browse to on disk, or pasted text) and a format. The plugin downloads the card images into game/front/ (and game/double_sided/ where applicable) — ready for the PDF step."));
  const picker = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("download")),
      el("div", { class: "grow" }, el("h2", {}, "Game"), el("p", {}, "One plugin per game. Formats and options adapt to your pick.")),
    ),
  );
  const grid = el("div", { class: "plugin-grid" });
  const slugs = Object.keys(S.manifest).filter(k => k.startsWith("fetch:")).sort();
  for (const kind of slugs) {
    const slug = kind.slice(6);
    grid.append(el("button", {
      class: `plugin-card ${S.plugin === slug ? "active" : ""}`,
      onclick: () => { S.plugin = slug; go("fetch", { plugin: slug }); },
    },
      el("div", { class: "pc-t" }, S.manifest[kind].game),
      el("div", { class: "pc-f" }, `${(S.manifest[kind].groups.find(g => g.title === "Format")?.options[0].choices || []).length - 1} formats`),
    ));
  }
  picker.append(grid);
  wrap.append(picker);

  const kind = "fetch:" + S.plugin;
  if (!S.info.scm.found) {
    wrap.append(el("div", { class: "banner err" }, el("span", { class: "b-ico" }, ico("alert")), el("span", { class: "grow" }, "SCM repo not connected — the plugins live inside it. Fix it in Settings.")));
    return wrap;
  }
  wrap.append(formCard(kind, { icon: "download" }));
  wrap.__patch = () => patchFetchForm(kind);
  return wrap;
};

/* fetch-specific control behavior: file list UI + paste visibility */
function patchFetchForm(kind) {
  const args = S.forms[kind] || (S.forms[kind] = defaultArgs(kind));
  const card = $(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const files = S.info.scm.decklists || [];
  const src = $$(".field", card).find(f => f.dataset.key === "deck_source");
  const fileF = $$(".field", card).find(f => f.dataset.key === "deck_file");
  const nameF = $$(".field", card).find(f => f.dataset.key === "deck_name");
  const textF = $$(".field", card).find(f => f.dataset.key === "deck_text");
  const urlF = $$(".field", card).find(f => f.dataset.key === "deck_url");
  if (fileF) {
    fileF.innerHTML = "";
    const label = el("label", { class: "fp-label" }, "Decklist file ", el("span", { class: "req" }, "*"));
    // In the app's own window we can open the native OS file chooser; in a
    // browser (dev mode) the button doesn't exist and the folder list is it.
    const canPick = !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_file);
    if (canPick) {
      const browse = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        title: "Pick any file on disk — it's copied into game/decklist/ and appears in the list",
        onclick: async () => {
          browse.disabled = true;
          let picked = null;
          try {
            picked = await window.pywebview.api.pick_file();
          } catch (e) {
            browse.disabled = false;
            toast("warn", "The file picker didn't open — paste the decklist text instead.");
            return;
          }
          browse.disabled = false;
          if (!picked) return; // cancelled in the panel
          let r;
          try {
            r = await api("/api/decklists/import", { path: picked });
          } catch (e) {
            toast("err", e.message);
            return;
          }
          if (r.ok) {
            S.info.scm.decklists = r.decklists;
            args.deck_file = r.name;
            fillList();
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind);
            toast("ok", `Imported “${r.name}” into the decklist folder`);
          } else {
            toast("err", (r.errors || [])[0] || "Importing the file failed.");
          }
        },
      }, ico("folder"), "Browse…");
      label.append(browse);
    }
    fileF.append(label);
    const list = el("div", { class: "filepick" });
    const fillList = () => {
      const fl = S.info.scm.decklists || [];
      list.innerHTML = "";
      if (!fl.length) list.append(el("div", { class: "small faint" },
        "No decklist files in game/decklist/ yet — use “Paste text” to create one" + (canPick ? ", or pick an existing file with Browse…" : "")));
      for (const f of fl) {
        list.append(el("div", {
          class: `fp-item ${args.deck_file === f.name ? "active" : ""}`,
          onclick: (ev) => {
            args.deck_file = f.name;
            $$(".fp-item", list).forEach(n => n.classList.remove("active"));
            ev.currentTarget.classList.add("active");
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind);
          },
        }, ico("file"), f.name, el("span", { class: "sz" }, fmtBytes(f.size))));
      }
    };
    fillList();
    fileF.append(list);
  }
  // if there's nothing to pick from, start in "paste" mode — before the UI syncs
  const seg = src ? $(".seg", src) : null;
  if (!files.length && args.deck_source === "file") {
    args.deck_source = "paste";
    if (seg) $$("button", seg).forEach(b => b.classList.toggle("active", b.textContent.trim() === "Paste text"));
  }
  const sync = () => {
    const mode = args.deck_source;
    if (nameF) nameF.style.display = mode === "paste" ? "" : "none";
    if (textF) textF.style.display = mode === "paste" ? "" : "none";
    if (urlF) urlF.style.display = mode === "url" ? "" : "none";
    if (fileF) fileF.style.display = mode === "file" ? "" : "none";
  };
  // Source-based format auto-selection (mirrors the rule the command builder
  // enforces server-side):
  //  - URL source: the format must be one of this game's URL-based formats,
  //    otherwise the URL would be passed to a file-reading parser (or rejected).
  //  - Existing-file source: picking an .xml decklist selects one of this
  //    game's XML-based formats (MTG: MPCFill XML, Final Fantasy: OctGN XML).
  const autoFormat = () => {
    const spec = S.manifest[kind] || {};
    const allowed = args.deck_source === "url" ? spec.url_formats || []
      : (args.deck_source === "file" && /\.xml$/i.test(args.deck_file || "") ? spec.xml_formats || [] : null);
    if (!allowed || !allowed.length || allowed.includes(args.format)) return;
    args.format = allowed[0];
    const fmtF = $$(".field", card).find(f => f.dataset.key === "format");
    const sel = fmtF && $("select", fmtF);
    if (sel) sel.value = args.format;
    afterFormChange(kind);
  };
  sync();
  autoFormat();
  if (seg) $$("button", seg).forEach(b => b.addEventListener("click", () => setTimeout(() => { sync(); autoFormat(); }, 0)));
}

/* ================================ pdf page ================================ */

PAGES.pdf = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Create PDF", "Lays out the images in your game/ folders into a print-ready PDF with registration marks. Every option from create_pdf.py is available below — the command preview shows exactly what will run."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  wrap.append(formCard("create_pdf", { icon: "pdf" }));
  {  // offset banner — per-size row wins over the global value for this form's paper
    const form = S.forms.create_pdf || (S.forms.create_pdf = defaultArgs("create_pdf"));
    const paper = paperForCreatePdf(form);
    const row = (S.info.per_size_offsets || {})[paper];
    const g = S.info.scm.saved_offset;
    if (row || g) {
      const o = row || g;
      const txt = row
        ? `Per-size offset for “${paper}” is saved: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b> — applied automatically whenever “Apply saved offset” is on.`
        : `Saved printer offset is available: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. Enable “Apply saved offset” below when ready.`;
      wrap.append(el("div", { class: "banner ok", style: "margin-top:16px" }, el("span", { class: "b-ico" }, ico("check")),
        el("span", { class: "grow", html: txt }),
        el("button", { class: "linkish", onclick: () => go("offset") }, "manage offset →")));
    }
  }
  wrap.__patch = () => patchPdfForm("create_pdf");  // must run once the card is in the document
  return wrap;
};

/* Create-PDF behavior: guard the “Front pages only” toggle against images left
   in the double-sided folder. create_pdf.py refuses to run with --only_fronts
   while any double-sided image exists, so when the toggle is flipped on we
   warn that the option won't work and offer to remove the images in one click. */
function patchPdfForm(kind) {
  const card = $(`.form-card[data-kind="${kind}"]`);
  const field = card && $$(".field", card).find(f => f.dataset.key === "only_fronts");
  const input = field && $("input[type=checkbox]", field);
  if (!input) return;
  let scanning = false;

  input.addEventListener("change", async () => {
    if (scanning) return;
    const dir = ((S.forms[kind] || {}).double_sided_dir || "").trim();
    if (!dir || !input.checked) return;
    scanning = true;
    let items = null;
    try {
      const r = await fetch(`/api/file?path=${encodeURIComponent(dir)}&images_only=1`);
      if (r.status === 404) items = [];                 // folder missing → nothing to remove
      else if (r.status === 200) items = (await r.json().catch(() => ({}))).items || [];
      else toast("warn", `Couldn’t check “${dir}” (HTTP ${r.status}).`);
    } catch { items = []; }
    scanning = false;
    if (!items || !items.length) return;

    const n = items.length;
    const ok = await confirmModal({
      title: "Images in the double-sided folder",
      text: `“${dir}” contains ${n} image${n === 1 ? "" : "s"}. While any are there, “Front pages only” (--only_fronts) can’t work — create_pdf.py refuses to run.`,
      paras: [`Remove them from the folder now? This can’t be undone.`],
      list: items.map(i => i.name),
      okLabel: "Yes, remove them",
      danger: true,
      icon: "trash",
      iconCls: "warn",
    });
    if (!ok) {
      toast("warn", `“Front pages only” stays on, but the job will fail while “${dir}” still has images — remove them, or uncheck the option.`, 7000);
      return;
    }
    let j = {};
    try {
      const r = await fetch("/api/fs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ op: "delete_images", path: dir }) });
      j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        toast("err", (j.errors || ["Could not remove the images."]).join("; "), 7000);
        return;
      }
    } catch {
      toast("err", "Could not remove the images.", 7000);
      return;
    }
    toast("ok", `Removed ${j.deleted} image${j.deleted === 1 ? "" : "s"} from “${dir}” — “Front pages only” will work now.`, 6000);
    afterFormChange(kind);   // refresh the preview so the warning clears
  });
}

/* =============================== offset page ============================== */

PAGES.offset = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Offset & calibration", "Printer misalignment is the #1 cause of cards that don't line up. The correction depends on the paper you feed, so you can store one offset per paper size — Create PDF picks the matching row automatically. Generate a calibration sheet, measure the drift, and save the values below."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  prefillOffsetForm();

  // global (shared) offset card — SCM's own single value
  const so = S.info.scm.saved_offset;
  const sc = el("div", { class: "card" });
  sc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("target")),
    el("div", { class: "grow" }, el("h2", {}, "Saved printer offset (global)"), el("p", {}, so ? "The single shared value in data/offset_data.json — what SCM applies when no per-size row matches. Per-paper-size rows live below; saving one of them also updates this file." : "Nothing saved yet. Measure with a calibration sheet, then store the values here or in a per-size row below."))));
  const xI = el("input", { class: "input mono", type: "number", value: so ? so.x : 0 });
  const yI = el("input", { class: "input mono", type: "number", value: so ? so.y : 0 });
  const aI = el("input", { class: "input mono", type: "number", step: 0.1, value: so ? so.angle : 0 });
  sc.append(el("div", { class: "frow" },
    el("div", { class: "field w-quarter" }, el("label", {}, "X (px, right +)"), xI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Y (px, up +)"), yI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Angle (°)"), aI),
    el("div", { class: "field w-quarter" }, el("label", {}, "&nbsp;"), el("div", {},
      el("button", { class: "btn primary", onclick: async () => {
        const r = await api("/api/offset", { x: xI.value, y: yI.value, angle: aI.value });
        if (r.ok) { toast("ok", "Global offset saved — create_pdf can now apply it."); await refreshInfo(); go("offset"); }
      } }, ico("check"), "Save"),
      el("button", { class: "btn btn-ghost", style: "margin-left:6px", onclick: () => { xI.value = 0; yI.value = 0; aI.value = 0; } }, "zero"),
    )),
  ));
  wrap.append(sc);

  wrap.append(offsetsBySizeCard());

  wrap.append(formCard("offset_pdf", { icon: "target" }));

  const cal = el("div", { class: "card" });
  cal.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("zap")),
    el("div", { class: "grow" }, el("h2", {}, "Calibration sheets"), el("p", {}, "One two-page PDF per paper size. Print double-sided (long-edge flip) and compare the front/back dot grids.")),
    el("button", { class: "btn", onclick: () => doRun("calibration", null) }, ico("refresh"), "Regenerate all"),
  ));
  const grid = el("div", { class: "filegrid" });
  for (const c of S.info.scm.calibration) {
    grid.append(el("button", { class: "fileitem", onclick: () => window.open(`/api/file?path=${encodeURIComponent(c.path)}&_t=${Date.now()}`) },
      el("span", { class: "fi-ico" }, ico("file")),
      el("span", { class: "fi-name" }, c.name, el("span", { class: "fi-sub", style: "display:block" }, `${fmtBytes(c.size)} · click to open`))));
  }
  if (!S.info.scm.calibration.length) grid.append(el("div", { class: "empty" }, "No calibration PDFs found."));
  cal.append(grid);
  wrap.append(cal);
  wrap.__patch = () => patchOffsetForm();
  return wrap;
};

/* Per-paper-size offset table: the row for the paper you feed, kept in the
   Workbench's own data/ and staged into SCM's shared offset file on save. */
function offsetsBySizeCard() {
  const pso = S.info.per_size_offsets || {};
  const sizes = S.info.scm.paper_sizes || [];
  const c = el("div", { class: "card" });
  c.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("ruler")),
    el("div", { class: "grow" }, el("h2", {}, "Offsets by paper size"),
      el("p", {}, "Printer drift depends on the paper you feed, so different sizes often need different corrections. Store one row per size — “Create PDF” with “Apply saved offset” picks the matching row automatically, and saving a row also stages it into SCM's shared offset file."))));
  const sel = el("select", { class: "input" });
  if (!sizes.length) sel.append(el("option", { value: "" }, "no paper sizes known"));
  for (const p of sizes) sel.append(el("option", { value: p.name }, `${p.name} — ${p.width || "?"} × ${p.height || "?"}`));
  const xI = el("input", { class: "input mono", type: "number", value: 0 });
  const yI = el("input", { class: "input mono", type: "number", value: 0 });
  const aI = el("input", { class: "input mono", type: "number", step: 0.1, value: 0 });
  c.append(el("div", { class: "frow" },
    el("div", { class: "field w-third" }, el("label", {}, "Paper size"), sel),
    el("div", { class: "field w-quarter" }, el("label", {}, "X (px, right +)"), xI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Y (px, up +)"), yI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Angle (°)"), aI),
    el("div", { class: "field w-quarter" }, el("label", {}, "&nbsp;"),
      el("div", {},
        el("button", { class: "btn primary", onclick: async () => {
          if (!sel.value) return toast("err", "Pick a paper size first.");
          const r = await api("/api/offset", { size: sel.value, x: xI.value, y: yI.value, angle: aI.value });
          if (r.ok) { toast("ok", `Saved for “${sel.value}” — staged into SCM's shared file and applied automatically to that paper.`); await refreshInfo(); go("offset"); }
        } }, ico("check"), "Save for this size"),
        el("button", { class: "btn btn-ghost", style: "margin-left:6px", onclick: () => { xI.value = 0; yI.value = 0; aI.value = 0; } }, "zero"),
      )),
  ));
  const rows = el("div", { class: "pso-rows" });
  const list = Object.entries(pso);
  if (!list.length) rows.append(el("div", { class: "empty" }, ico("target"), "No per-size rows yet — measure the drift on a calibration sheet and save the first row above."));
  for (const [size, o] of list) {
    const p = sizes.find(s => s.name === size);
    rows.append(el("div", { class: "pso-row" },
      el("span", { class: "pso-size" }, size, p ? el("span", { class: "faint" }, ` ${p.width || "?"} × ${p.height || "?"}`) : null),
      el("span", { class: "mono" }, `x ${o.x} · y ${o.y} · ${o.angle}°`),
      el("span", { class: "pso-actions" },
        el("button", { class: "btn sm", onclick: () => { sel.value = size; xI.value = o.x; yI.value = o.y; aI.value = o.angle; } }, "load"),
        el("button", { class: "btn sm btn-ghost", onclick: async () => {
          const r = await api("/api/offset", { size, delete: true });
          if (r.ok) { toast("ok", `Removed the “${size}” row.`); await refreshInfo(); go("offset"); }
        } }, "delete"),
      ),
    ));
  }
  c.append(rows);
  return c;
}

/* The offset_pdf form: which row do the blank fields fall back to? */
function offsetSourceFor(size) {
  if (size) {
    const e = (S.info.per_size_offsets || {})[size];
    if (e) return e;
  }
  return S.info.scm.saved_offset || null;
}

function prefillOffsetForm() {
  const args = S.forms.offset_pdf || (S.forms.offset_pdf = defaultArgs("offset_pdf"));
  const src = offsetSourceFor(args.paper_size);
  if (src && (args.use_saved === undefined ? true : args.use_saved)) {
    if (args.x_offset === "" || args.x_offset === null || args.x_offset === undefined) args.x_offset = src.x;
    if (args.y_offset === "" || args.y_offset === null || args.y_offset === undefined) args.y_offset = src.y;
    if (args.angle === "" || args.angle === null || args.angle === undefined) args.angle = src.angle;
  }
}

function patchOffsetForm() {
  const card = $(`.form-card[data-kind="offset_pdf"]`);
  const args = S.forms.offset_pdf;
  if (!card || !args) return;
  const set = (k, v) => {
    args[k] = v;
    const f = $$(".field", card).find(f => f.dataset.key === k);
    const inp = f && $("input", f);
    if (inp) inp.value = String(v);
  };
  const re = () => {
    if (args.use_saved === false) return;   // the user explicitly opted out of prefill
    const src = offsetSourceFor(args.paper_size);
    if (!src) return;
    set("x_offset", src.x); set("y_offset", src.y); set("angle", src.angle);
    afterFormChange("offset_pdf");
  };
  const fPaper = $$(".field", card).find(f => f.dataset.key === "paper_size");
  const sel = fPaper && $("select", fPaper);
  if (sel) sel.addEventListener("change", () => { re(); });
  const fUse = $$(".field", card).find(f => f.dataset.key === "use_saved");
  const cb = fUse && $("input[type=checkbox]", fUse);
  if (cb) cb.addEventListener("change", () => { re(); });
}

/* Which paper does a create_pdf form run actually print on? */
function paperForCreatePdf(form = {}) {
  const sp = (S.info.scm.specialty || []).find(s => s.name === form.specialty);
  if (sp && sp.paper) return sp.paper;
  return form.paper_size || (S.info.settings.defaults || {}).paper_size || "letter";
}

/* ============================== templates page ============================= */

PAGES.templates = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Cutting templates", "DXF cutting templates for the repo's standard sizes, plus the prebuilt .studio3 files that Silhouette Studio opens. For MTG / Sorcery extras see the Extras page."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  wrap.append(formCard("dxf_single", { icon: "scissors" }));
  wrap.__patch = () => patchDxfForm("dxf_single");  // must run once the card is in the document
  wrap.append(formCard("dxf_batch", { icon: "layers" }));
  wrap.append(el("div", { class: "section-label" }, "Existing templates"));
  wrap.append(templatesGallery("scm"));
  return wrap;
};

function patchDxfForm(kind) {
  const card = $(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const args = S.forms[kind];
  const sync = () => {
    const custom = v => args[v] === "custom";
    for (const key of ["card_size", "card_width", "card_height", "card_radius", "card_name",
      "paper_size", "paper_width", "paper_height", "paper_name"]) {
      const f = $(`.field[data-key="${key}"]`, card);
      if (!f) continue;
      const show = key === "card_size" || key === "paper_size" ? !custom(key.replace("_size", "_mode"))
        : custom(key.replace("_width", "_mode").replace("_height", "_mode").replace("_radius", "_mode").replace("_name", "_mode"));
      if (show) { f.style.display = ""; if (f.querySelector(".frow-field")) {} } else f.style.display = "none";
    }
  };
  // re-bind: listen on the two mode segments
  for (const key of ["card_mode", "paper_mode"]) {
    const f = $(`.field[data-key="${key}"]`, card);
    if (f) $$("button", $(".seg", f)).forEach(b => b.onclick = () => setTimeout(sync, 0));
  }
  sync();
}

function templatesGallery(which) {
  const info = which === "scm" ? S.info.scm : S.info.extras;
  const base = info.path ? info.path + "/cutting_templates" : null;
  const card = el("div", { class: "card" });
  const sections = [
    ["Default DXF", info.templates.dxf, "dxf", "/dxf/"],
    ["Borderless DXF", info.templates.borderless_dxf, "dxf", "/borderless/dxf/"],
    ["Default .studio3", info.templates.studio3, "studio3", "/"],
    ["Borderless .studio3", info.templates.borderless_studio3, "studio3", "/borderless/"],
  ];
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("folder")),
    el("div", { class: "grow" }, el("h2", {}, which === "scm" ? "Repo cutting templates" : "Extras cutting templates"), el("p", {}, "DXF = source files for Silhouette Studio · .studio3 = pre-configured cutting jobs (paper size, registration marks)")),
  ));
  if (!base) card.append(el("div", { class: "empty" }, ico("folder"), "Repo not connected."));
  for (const [title, items, ext, dir] of sections) {
    if (!items?.length || !base) continue;
    card.append(el("div", { class: "section-label" }, title));
    const g = el("div", { class: "filegrid" });
    for (const n of items) {
      g.append(el("button", { class: "fileitem", onclick: () => window.open(`/api/file?path=${encodeURIComponent(base + dir + n)}`) },
        el("span", { class: "fi-ico" }, ico(ext === "dxf" ? "scissors" : "card")),
        el("span", { class: "fi-name" }, n),
      ));
    }
    card.append(g);
  }
  return card;
}

/* ================================= extras page ============================ */

PAGES.extras = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Extras: MTG & Sorcery", "scm-extras adds tailored card sizes — Magic: The Gathering (2.5 mm radius) and Sorcery: Contested Realm (4.5 mm radius) — with their own cutting templates. The Workbench wires SCM_EXTRA_LAYOUTS for you, so you can use these sizes from the Create PDF page without any manual env setup."));
  if (!S.info.extras.found) {
    wrap.append(el("div", { class: "banner warn" }, ico("alert"), el("span", { class: "grow" },
      "scm-extras is not connected. In Settings, point it at your scm-extras folder — or clone it next to this project: ",
      el("code", { class: "mono" }, "git clone https://github.com/Alan-Cha/scm-extras"),
    )));
    return wrap;
  }
  wrap.append(el("div", { class: "banner info" }, ico("info"), el("span", { class: "grow" },
    "Tip: on the Create PDF page, pick ", el("b", {}, "standard_mtg"), " or ", el("b", {}, "standard_sorcery"), " as the card size — the extra layout file is merged automatically, and you cut with the matching ", el("b", {}, ".studio3"), " from this page.")));

  // sizes
  const sc = el("div", { class: "card" });
  sc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("card")),
    el("div", { class: "grow" }, el("h2", {}, "Extra card sizes"), el("p", {}, "Defined in scm-extras/assets/layouts_extra.json and merged into every SCM script."))));
  const grid = el("div", { class: "sizegrid" });
  for (const c of S.info.extras.card_sizes) {
    const w = mm(c.width), h = mm(c.height);
    const scale = Math.min(64 / w, 84 / h, 1);
    grid.append(el("button", { class: "sizecard", onclick: () => go("pdf", { card_size: c.name }) },
      el("div", { class: "sc-svg", html: cardSvg(w * scale, h * scale, mm(c.radius) * scale) }),
      el("div", { class: "sc-name" }, c.name),
      el("div", { class: "sc-dims" }, `${w.toFixed(0)} × ${h.toFixed(0)} mm · r ${c.radius}`),
      el("div", { class: "sc-tags" },
        el("span", { class: "tag extras" }, "extras"),
        ...c.aliases.map(a => el("span", { class: "tag aliases" }, a)),
      ),
      el("div", { class: "small", style: "margin-top:6px" }, el("button", { class: "linkish", onclick: e => { e.stopPropagation(); go("pdf", { card_size: c.name }); } }, "Use in Create PDF →")),
    ));
  }
  sc.append(grid);
  wrap.append(sc);

  wrap.append(el("div", { class: "section-label" }, "Layouts (cards per page)"));
  wrap.append(extrasMatrix());

  wrap.append(formCard("extras_generate", { icon: "scissors" }));
  wrap.append(formCard("extras_tables", { icon: "book", preview: false, run: false }));
  const tcard = $(`.form-card[data-kind="extras_tables"]`, wrap);
  const b = el("button", { class: "btn", onclick: () => doRun("extras_tables", null) }, ico("book"), "Print tables to console");
  tcard?.append(el("div", { class: "runbar" }, el("span", { class: "rb-note" }, "Outputs markdown tables for the extras README."), b));

  wrap.append(el("div", { class: "section-label" }, "Extras cutting templates"));
  wrap.append(templatesGallery("extras"));
  return wrap;
};

function extrasMatrix() {
  const t = el("div", { class: "card" });
  const table = el("table", { class: "matrix" });
  const papers = Object.keys(S.info.extras.layouts || {});
  table.append(el("tr", {}, el("th", { class: "rowhead" }, "Size"), ...papers.map(p => el("th", {}, p))));
  for (const c of S.info.extras.card_sizes) {
    const tr = el("tr", {}, el("th", { class: "rowhead" }, c.name));
    for (const p of papers) {
      const def = (S.info.extras.layouts[p] || {})[c.name]?.default;
      const bl = (S.info.extras.layouts[p] || {})[c.name]?.borderless;
      if (def) tr.append(el("td", { class: "extras-row d1" }, `${def.num_cols}×${def.num_rows}`, el("span", { class: "sub" }, bl ? `bl ${bl.num_cols}×${bl.num_rows}` : "")));
      else tr.append(el("td", { class: "extras-row na" }, "—"));
    }
    table.append(tr);
  }
  t.append(table);
  return t;
}

/* ================================ utilities page ========================== */

PAGES.utilities = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Utilities", "Small tools that don't fit a single workflow step: cleaning the art folders, unit conversion, and a quick dump of every known size."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());

  // clean up
  const cc = el("div", { class: "card" });
  cc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("trash")),
    el("div", { class: "grow" }, el("h2", {}, "Start a new game"), el("p", {}, "Deletes every image in game/front/ and game/double_sided/ (card backs in game/back/ are kept). There is no undo — the files are gone.")),
  ));
  cc.append(el("div", { class: "runbar" },
    el("span", { class: "rb-note" }, "This runs the repo's clean_up.py after you confirm."),
    el("button", { class: "btn danger", onclick: async () => {
      const ok = await confirmModal({ title: "Delete card images?", text: "Every image in game/front/ and game/double_sided/ will be permanently deleted. Card backs are kept.", okLabel: "Yes, clear them", danger: true, icon: "trash", iconCls: "warn" });
      if (ok) doRun("clean_up", null);
    } }, ico("trash"), "Clear front & double-sided")));
  wrap.append(cc);

  // converter
  wrap.append(converterCard());

  // list sizes
  const lc = el("div", { class: "card" });
  lc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("ruler")),
    el("div", { class: "grow" }, el("h2", {}, "List every known size"), el("p", {}, "Runs generate_dxf.py list — prints all card and paper sizes (including extras) to the job console.")),
    el("button", { class: "btn", onclick: () => doRun("dxf_list", null) }, ico("terminal"), "Run"),
  ));
  wrap.append(lc);
  return wrap;
};

function converterCard() {
  const card = el("div", { class: "card" });
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("ruler")),
    el("div", { class: "grow" }, el("h2", {}, "Size converter"), el("p", {}, "The same math the scripts use (size_convert.py): mm, inches, points, and pixels at any PPI."))));
  const ppi = 300;
  const ppiI = el("input", { class: "input mono", type: "number", value: 300, min: 72, max: 1200 });
  const vI = el("input", { class: "input mono", type: "number", step: "any", value: 63 });
  const fromSel = el("select", { class: "input" }, ...[["mm", "mm"], ["in", "in"], ["pt", "pt"], ["px", "px @ PPI"]].map(([v, l]) => el("option", { value: v }, l)));
  const toSel = el("select", { class: "input" }, ...[["mm", "mm"], ["in", "in"], ["pt", "pt"], ["px", "px @ PPI"]].map(([v, l]) => el("option", { value: v }, l)));
  toSel.value = "in";
  const out = el("div", { class: "result-big" }, "—");
  const convert = () => {
    const v = parseFloat(vI.value);
    if (isNaN(v)) { out.textContent = "—"; return; }
    const P = parseFloat(ppiI.value) || 300;
    const toMm = { mm: v, in: v * 25.4, pt: v / 72 * 25.4, px: v / P * 25.4 }[fromSel.value];
    const fromMm = { mm: toMm, in: toMm / 25.4, pt: toMm / 25.4 * 72, px: toMm / 25.4 * P };
    const r = fromMm[toSel.value];
    out.textContent = toSel.value === "in" || toSel.value === "pt" ? r.toFixed(4).replace(/0+$/g).replace(/\.$/, "") : String(Math.round(r * 100) / 100);
  };
  [vI, fromSel, toSel, ppiI].forEach(i => i.addEventListener("input", convert));
  convert();
  card.append(el("div", { class: "frow" },
    el("div", { class: "field w-quarter" }, el("label", {}, "Value"), vI),
    el("div", { class: "field w-quarter" }, el("label", {}, "From"), fromSel),
    el("div", { class: "field w-quarter" }, el("label", {}, "To"), toSel),
    el("div", { class: "field w-quarter" }, el("label", {}, "PPI (for px)"), ppiI),
  ));
  card.append(el("div", { style: "margin-top:14px" }, out));
  return card;
}

/* ================================= settings page ========================== */

/* Re-render a managed-repo row once its long job has finished. */
function watchJobDone(jobId, cb) {
  const t = setInterval(async () => {
    let d;
    try { d = await api(`/api/jobs/${jobId}/log`); } catch { return; }
    if (d.status !== "running") {
      clearInterval(t);
      cb(d);
    }
  }, 3000);
}

/* One row of the “Managed repo copies” card: source picker, live status,
   check / update actions. Re-renders itself in place when the source changes. */
function repoCopyRow(row, container) {
  const box = el("div", { class: "rcre", style: "margin-top:14px; padding-top:12px; border-top:1px solid var(--border-soft)" });
  let selectingPinned = false;
  const modeOf = src => ["main", "latest-release"].includes(src) ? src : "pinned";
  const pickSource = async (v) => {
    if (!v) return;
    let r;
    try {
      r = await api("/api/repos/save", { repo: row.key, source: v });
    } catch (e) {
      return toast("err", e.message || "could not save the source");
    }
    if (!r.ok) return toast("err", (r.errors || ["could not save the source"]).join("; "));
    await refreshInfo({ keepForms: true });
    const fresh = (S.info.repos || []).find(x => x.key === row.key) || row;
    if (container) container.replaceChildren(repoCopyRow(fresh, container));
    else render();
    toast("ok", `Tracking “${r.target ? r.target.ref : v}” for ${row.name}.`);
  };
  const render = async () => {
    box.innerHTML = "";
    const src = row.source || "main";
    const mode = modeOf(src);
    const chip = { managed: ["ok", "managed copy"], external: ["info", "your own clone"], missing: ["warn", "not set up"] }[row.mode] || ["warn", row.mode];
    // two-line header: name + state chip on line one, the (long) path on
    // line two — one row used to wrap the chip over the monospace path
    const head = el("div", { class: "rc-head" },
      el("div", { class: "rc-title" },
        el("span", { class: "rc-name" }, row.name),
        el("span", { class: `rc-chip ${chip[0]}` }, el("span", { class: `dot ${chip[0]}` }), chip[1])
      )
    );
    if (row.path && row.mode === "managed") head.append(el("div", { class: "rc-path" }, "kept privately inside the app's data folder"));
    else if (row.path) head.append(el("div", { class: "rc-path mono" }, row.path));
    box.append(head);
    const seg = el("div", { class: "seg" });
    const segBtns = {};
    for (const [v, lab] of [["main", "Latest (main)"], ["latest-release", "Latest release"], ["pinned", "Pinned"]]) {
      const b = el("button", { type: "button", class: (mode === v || (v === "pinned" && selectingPinned)) ? "active" : "", onclick: () => { if (v === "pinned") { selectingPinned = true; render(); } else pickSource(v); } }, lab);
      segBtns[v] = b;
      seg.append(b);
    }
    // a repo with no published releases can't be tracked by "latest release" —
    // dim that segment instead of letting the save fail with a message
    api("/api/repos/refs", { repo: row.key }).then(r => {
      if (r.ok && !(r.refs.releases || []).length) {
        segBtns["latest-release"].disabled = true;
        segBtns["latest-release"].classList.add("off");
        segBtns["latest-release"].title = "No releases are published for this repo yet";
      }
    }).catch(() => { });
    const showPicker = mode === "pinned" || selectingPinned;
    const pinWrap = el("div", { class: "field", style: "display:" + (showPicker ? "block" : "none") });
    const pinSel = el("select", { class: "input" }, el("option", { value: "" }, "— pick a tag / release —"));
    if (showPicker) {
      box.append(el("div", { style: "margin-top:10px; display:flex; gap:10px; align-items:center; flex-wrap:wrap" },
        el("span", { class: "small faint" }, "track"), seg, pinWrap));
      const r = await api("/api/repos/refs", { repo: row.key });
      if (!r.ok) { toast("err", (r.errors || ["could not list tags — check your connection"]).join("; ")); return; }
      const known = [...r.refs.tags.map(t => t.name), ...r.refs.releases.filter(q => !q.prerelease).map(q => q.tag)];
      for (const name of known) pinSel.append(el("option", { value: name, selected: name === src ? "selected" : null }, name));
      const unknown = !!src && !known.includes(src);
      pinSel.append(el("option", { value: "__custom", selected: unknown ? "selected" : null }, "… or type a tag / branch / SHA"));
      const customI = el("input", { class: "input mono", placeholder: "e.g. v3.0.0 or a branch name", style: "margin-top:6px; display:" + (unknown ? "block" : "none"), value: unknown ? src : "" });
      pinSel.onchange = () => { if (pinSel.value === "__custom") { customI.style.display = "block"; customI.focus(); return; } pickSource(pinSel.value); };
      customI.onkeydown = (e) => { if (e.key === "Enter" && customI.value.trim()) pickSource(customI.value.trim()); };
      pinWrap.append(pinSel, customI);
    } else {
      box.append(el("div", { style: "margin-top:10px; display:flex; gap:10px; align-items:center; flex-wrap:wrap" },
        el("span", { class: "small faint" }, "track"), seg, pinWrap));
    }
    // status line
    const dep = row.deployed;
    const lc = row.last_check && row.last_check.checked ? row.last_check.checked : null;
    let statusText, statusCls;
    if (!dep) { statusText = "No managed copy yet — download one to start tracking updates."; statusCls = "warn"; }
    else if (lc && lc.ok && lc.up_to_date) { statusText = `At ${dep.ref} (${dep.sha.slice(0, 7)}) — up to date.`; statusCls = "ok"; }
    else if (lc && lc.ok && lc.target) { statusText = `New version available: ${lc.target.ref} (${lc.target.sha.slice(0, 7)}) — deployed: ${dep.ref} (${dep.sha.slice(0, 7)}).`; statusCls = "warn"; }
    else { statusText = `Deployed at ${dep.ref} (${dep.sha.slice(0, 7)})` + (dep.date ? `, ${String(dep.date).slice(0, 10)}` : ""); statusCls = "ok"; }
    box.append(el("div", { class: `note ${statusCls}`, style: "margin-top:10px" }, statusText));
    // actions
    const acts = el("div", { style: "margin-top:10px; display:flex; gap:8px; flex-wrap:wrap" });
    if (row.mode === "managed") {
      const checkBtn = el("button", { class: "btn sm" }, ico("search"), "Check for updates");
      checkBtn.onclick = async () => {
        checkBtn.disabled = true;
        const r = await api("/api/repos/check", { repo: row.key, force: true });
        checkBtn.disabled = false;
        if (r.ok) { row.last_check = r.last_check; await refreshInfo({ keepForms: true }); const fresh = (S.info.repos || []).find(x => x.key === row.key); if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container)); else render(); }
        else toast("err", (r.errors || ["check failed"]).join("; "));
      };
      const hasUpdate = lc && lc.ok && !lc.up_to_date;
      const upBtn = el("button", { class: `btn sm ${hasUpdate ? "primary" : ""}` }, ico("refresh"), hasUpdate ? "Update now" : "Update");
      upBtn.onclick = async () => {
        const job = await doRun("repo_update", null, { args: { repo: row.key, force_full: false }, confirm: {
        title: `Update ${row.name}`,
        text: `Moves the managed copy to “${mode === "main" ? "the latest main" : mode === "latest-release" ? "the latest release" : src}”. Forward moves fetch only the changed files; rollbacks and big jumps take a full snapshot. Your images, decklists and local edits are preserved — if upstream also changed a file you edited, your version is kept and flagged.`,
        okLabel: "Update", icon: "refresh" } });
        if (!job) return;
        watchJobDone(job.id, async () => {
          await refreshInfo({ keepForms: true });
          const fresh = (S.info.repos || []).find(x => x.key === row.key);
          if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container));
        });
      };
      acts.append(checkBtn, upBtn);
    } else if (row.mode === "external") {
      const dlBtn = el("button", { class: "btn sm" }, ico("download"), "Also keep a managed copy");
      dlBtn.onclick = async () => {
        const job = await doRun("repo_init", null, { args: { repo: row.key }, confirm: {
        title: `Download a managed copy of ${row.name}`,
        text: "Keeps a second, Workbench-managed copy in the data folder (your own clone stays untouched). Pick the source above first if you want it to track something other than the latest main.",
        okLabel: "Download", icon: "download" } });
        if (!job) return;
        watchJobDone(job.id, async () => {
          await refreshInfo({ keepForms: true });
          const fresh = (S.info.repos || []).find(x => x.key === row.key);
          if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container));
        });
      };
      acts.append(dlBtn, el("span", { class: "small faint" }, "Your own clone stays as it is — the managed copy is the one the Workbench updates for you."));
    } else {
      const dlBtn = el("button", { class: "btn sm primary" }, ico("download"), "Download latest (managed copy)");
      dlBtn.onclick = async () => {
        const job = await doRun("repo_init", null, { args: { repo: row.key }, confirm: {
        title: `Download ${row.name}`,
        text: `Fetches a complete copy into the Workbench's data folder. The first download can be large — silhouette-card-maker is a few hundred MB (it includes the upstream docs site and test material).`,
        okLabel: "Download", icon: "download" } });
        if (!job) return;
        watchJobDone(job.id, async () => {
          await refreshInfo({ keepForms: true });
          const fresh = (S.info.repos || []).find(x => x.key === row.key);
          if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container));
        });
      };
      acts.append(dlBtn, el("span", { class: "small faint" }, "After that, updates are one click and incremental."));
    }
    box.append(acts);
  };
  render();
  return box;
}

PAGES.settings = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Settings", "Everything here is stored in this project's data/settings.json. Repo paths can also be left blank — the Workbench auto-detects sister folders named silhouette-card-maker and scm-extras."));

  const s = S.info.settings;

  // interface (simple / advanced)
  const ic = el("div", { class: "card" });
  ic.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("eye")),
    el("div", { class: "grow" }, el("h2", {}, "Interface"),
      el("p", {}, "How much of the controls to show. Simple trims the Create PDF page to the basics — card & paper size, registration marks, borderless and the front-only switch. The Fetch pages are already as simple as they get, and the other pages are unaffected."))));
  const modeNow = (s.ui_mode || "advanced") === "simple" ? "simple" : "advanced";
  const segM = el("div", { class: "seg" });
  for (const [v, lab] of [["simple", "Simple"], ["advanced", "Advanced"]]) {
    segM.append(el("button", { type: "button", class: modeNow === v ? "active" : "", onclick: () => setUiMode(v) }, lab));
  }
  ic.append(el("div", { style: "margin-top:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap" },
    el("span", { class: "small faint" }, "show"), segM,
    el("span", { class: "small faint" }, modeNow === "simple"
      ? "basic settings only — Advanced brings back the full layout"
      : "the full layout — everything the PDF builder offers")));
  wrap.append(ic);
  // repos
  const rc = el("div", { class: "card" });
  rc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("folder")),
    el("div", { class: "grow" }, el("h2", {}, "Repos"), el("p", {}, "Where the scripts live. Blank = auto-detect next to this project."))));
  const scmI = el("input", { class: "input mono", value: s.scm_dir || "", placeholder: "auto: ../silhouette-card-maker" });
  const exI = el("input", { class: "input mono", value: s.extras_dir || "", placeholder: "auto: ../scm-extras" });
  rc.append(el("div", { class: "frow" },
    el("div", { class: "field w-half" }, el("label", {}, "silhouette-card-maker"), scmI),
    el("div", { class: "field w-half" }, el("label", {}, "scm-extras"), exI),
  ));
  rc.append(el("div", { style: "margin-top:12px; display:flex; gap:9px; align-items:center" },
    el("button", { class: "btn primary", onclick: async () => {
      const r = await api("/api/settings", { scm_dir: scmI.value.trim(), extras_dir: exI.value.trim() });
      await refreshInfo();
      toast(S.info.scm.found ? "ok" : "warn", S.info.scm.found ? "Reconnected — settings reloaded." : "Saved, but the SCM repo still isn't found at that path.");
      go("settings");
    } }, ico("check"), "Save repo paths"),
    el("span", { class: "small faint" }, "You may need to restart the server after changing the Python interpreter."),
  ));
  wrap.append(rc);

  // managed repo copies (download/update the sister repos from inside the Workbench)
  const mc = el("div", { class: "card" });
  mc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("refresh")),
    el("div", { class: "grow" }, el("h2", {}, "Managed repo copies"), el("p", {}, "The Workbench can keep its own copy of each repo in its data folder — fetch the newest version on demand and pick exactly what to track (main, the latest release, or a pinned tag). Your images, decklists and local edits always survive an update."))));
  for (const row of (S.info.repos || [])) {
    const wrapRow = el("div", {});            // each row replaces itself inside its own wrapper
    mc.append(wrapRow);
    wrapRow.append(repoCopyRow(row, wrapRow));
  }
  wrap.append(mc);

  // python & server
  const packaged = !!S.info.server.is_packaged;
  const pc = el("div", { class: "card" });
  pc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("terminal")),
    el("div", { class: "grow" }, el("h2", {}, "Python & server"),
      el("p", {}, packaged
        ? "This app runs on its own private Python (kept in the app's data folder). Job dependencies are installed into it automatically — your system Python is never touched."
        : "Scripts run with the interpreter chosen here. Default: the one that started the Workbench. Install the base repo's requirements.txt into it: pip install -r requirements.txt"))));
  const pyI = el("input", { class: "input mono", value: s.python || (packaged ? "python  (private runtime)" : ""), placeholder: S.info.server.python_path + "  (default)", title: S.info.server.python_path || "", readonly: packaged || null });
  const portI = el("input", { class: "input mono", type: "number", value: s.port || 8037, min: 1024, max: 65535 });
  pc.append(el("div", { class: "frow" },
    el("div", { class: packaged ? "field w-half" : "field w-half" }, el("label", {}, packaged ? "Private Python" : "Python interpreter"), pyI),
    ...(packaged ? [] : [el("div", { class: "field w-quarter" }, el("label", {}, "Port"), portI)]),
    el("div", { class: packaged ? "field w-half" : "field w-quarter" }, el("label", {}, "Theme"),
      el("div", { class: "seg" },
        el("button", { type: "button", class: s.theme === "dark" ? "active" : "", onclick: () => setTheme("dark") }, ico("moon"), " Dark"),
        el("button", { type: "button", class: s.theme === "light" ? "active" : "", onclick: () => setTheme("light") }, ico("sun"), " Light"),
      )),
  ));
  if (!packaged) {
    const autoI = el("input", { type: "checkbox", id: "set-auto-browser", checked: s.auto_open_browser });
    const sw = el("span", { class: "switch" }, autoI, el("span", { class: "track" }), el("span", { class: "knob" }));
    const lab = el("label", {}, "Open the browser when the server starts");
    lab.setAttribute("for", "set-auto-browser");
    pc.append(el("div", { class: "field", style: "margin-top:10px" }, lab, sw));
    pc.append(el("div", { style: "margin-top:12px; display:flex; gap:9px" },
      el("button", { class: "btn primary", onclick: async () => {
        const r = await api("/api/settings", { python: pyI.value.trim(), port: parseInt(portI.value), auto_open_browser: autoI.checked });
        toast("ok", "Saved. Port changes apply on next server start.");
        go("settings");
      } }, ico("check"), "Save python & server"),
    ));
  } else {
    pc.append(el("div", { style: "margin-top:10px" },
      el("div", { class: "small faint" },
        "Serving the UI in its own window — no browser involved. Quitting the app window stops everything.")));
  }
  wrap.append(pc);

  // defaults
  const dc = el("div", { class: "card" });
  dc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("gear")),
    el("div", { class: "grow" }, el("h2", {}, "Create PDF defaults"), el("p", {}, "Pre-selected values for the Create PDF page (still overridable there)." ))));
  const d = s.defaults || {};
  const csSel = el("select", { class: "input" }, ...S.info.scm.card_sizes.map(c => el("option", { value: c.name, selected: (d.card_size || "standard") === c.name ? "selected" : null }, c.name)));
  const psSel = el("select", { class: "input" }, ...S.info.scm.paper_sizes.map(p => el("option", { value: p.name, selected: (d.paper_size || "letter") === p.name ? "selected" : null }, p.name)));
  const ppiR = el("input", { class: "range", type: "range", min: 150, max: 1200, step: 10 });
  ppiR.value = d.ppi || 300;
  const qualR = el("input", { class: "range", type: "range", min: 0, max: 100, step: 1 });
  qualR.value = d.quality || 100;
  const ppiV = el("input", { class: "rangeval", type: "number", step: 1, min: 0 });
  const qualV = el("input", { class: "rangeval", type: "number", step: 1, min: 0 });
  const setFill = r => r.style.setProperty("--fill", ((r.value - r.min) / (r.max - r.min)) * 100 + "%");
  const commit = (r, v) => {
    if (v.value === "") { v.value = r.value; setFill(r); return; } // blank → revert
    const n = Number(v.value);
    if (Number.isFinite(n) && n >= 0) {
      v.value = n; // freeform — the box keeps the exact value…
      r.value = Math.min(r.max, Math.max(r.min, Math.round(n / r.step) * r.step)); // …while the slider snaps
    } else v.value = r.value; // invalid → revert to the slider position
    setFill(r);
  };
  const toNum = (v, fallback) => { const n = Number(v); return Number.isFinite(n) && n >= 0 ? n : fallback; };
  ppiR.oninput = () => { ppiV.value = ppiR.value; setFill(ppiR); };
  qualR.oninput = () => { qualV.value = qualR.value; setFill(qualR); };
  ppiV.onchange = () => commit(ppiR, ppiV);
  qualV.onchange = () => commit(qualR, qualV);
  ppiV.value = ppiR.value; qualV.value = qualR.value; setFill(ppiR); setFill(qualR);
  dc.append(el("div", { class: "frow" },
    el("div", { class: "field w-half" }, el("label", {}, "Card size"), csSel),
    el("div", { class: "field w-half" }, el("label", {}, "Paper size"), psSel),
    el("div", { class: "field w-half" }, el("label", {}, "PPI"), el("span", { class: "rangewrap" }, ppiR, ppiV)),
    el("div", { class: "field w-half" }, el("label", {}, "Quality"), el("span", { class: "rangewrap" }, qualR, qualV)),
  ));
  dc.append(el("div", { style: "margin-top:12px" },
    el("button", { class: "btn primary", onclick: async () => {
      await api("/api/settings", { defaults: { card_size: csSel.value, paper_size: psSel.value, ppi: toNum(ppiV.value, +ppiR.value), quality: toNum(qualV.value, +qualR.value) } });
      toast("ok", "Defaults saved.");
      go("settings");
    } }, ico("check"), "Save defaults"),
  ));
  wrap.append(dc);

  // data & about
  const ac = el("div", { class: "card" });
  ac.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("info")),
    el("div", { class: "grow" }, el("h2", {}, "Data & about"), el("p", {}, `Workbench v${S.info.server.version} · server python ${S.info.server.python} · data dir ${S.info.server.data_dir}`))));
  ac.append(el("div", { style: "display:flex; gap:9px; flex-wrap:wrap" },
    el("button", { class: "btn", onclick: async () => { const r = await api("/api/reveal", { path: S.info.server.data_dir }); r.ok ? toast("ok", "Opening data folder…") : toast("warn", r.errors?.[0]); } }, ico("folder"), "Open data folder"),
    el("button", { class: "btn", onclick: () => window.open("https://github.com/Alan-Cha/silhouette-card-maker") }, ico("external"), "silhouette-card-maker on GitHub"),
    el("button", { class: "btn", onclick: () => window.open("https://github.com/Alan-Cha/scm-extras") }, ico("external"), "scm-extras on GitHub"),
  ));
  wrap.append(ac);
  return wrap;
};

/* --------------------------------- shared --------------------------------- */

function pageHead(title, sub) {
  return el("div", { class: "page-head" }, el("h1", {}, title), el("div", { class: "sub" }, sub));
}

/* ================================= bootstrap =============================== */

async function refreshInfo({ keepForms = false, jobs = true } = {}) {
  S.info = await api("/api/info");
  S.manifest = await api("/api/manifest");
  document.documentElement.dataset.theme = S.info.settings.theme || "dark";
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (S.info.settings.theme || "dark")));
  // pills
  const dS = $("#dot-scm"); dS.classList.toggle("ok", S.info.scm.found);
  const dE = $("#dot-extras"); dE.classList.toggle("ok", S.info.extras.found); dE.classList.toggle("warn", !S.info.extras.found);
  $("#chip-python").textContent = "python " + S.info.server.python;
  if (!keepForms) S.forms = {};
  if (jobs) refreshJobs();
  // if preparation started *after* this page booted (an update job, a
  // re-clone), start watching for it — the bar must appear without reload
  if (prepActive() && !_prepTimer) startPrepWatcher();
}

// Shown when the API is still unreachable after several attempts (the server
// died, or a flaky WSL2 localhost hop) — and kept on screen, with a Retry
// button, until the connection works. Unlike a toast, it can't be missed.
function showBootFailure(e) {
  const raw = (e && e.message) || "";
  const msg = /fetch|network|failed/i.test(raw) ? "no network response from the API" : raw;
  const bar = el("div", { class: "banner err" },
    el("span", { class: "b-ico" }, ico("alert")),
    el("span", { class: "grow" },
      `Can't reach the Workbench API at ${location.origin} (${msg}). Is the server still running? ` +
      "If you opened this tab from a Windows browser (WSL2), use the \u201CWindows host\u201D URL the server printed in its console."),
    el("button", { class: "btn sm", onclick: async () => {
      try {
        bar.remove();
        await refreshInfo();
        bootPage();
        startJobsPoll();
      } catch (e2) {
        showBootFailure(e2);
      }
    } }, "Retry"));
  $("#page").replaceChildren(bar);
}

function startJobsPoll() {
  setInterval(() => { if (S.jobs.some(j => j.status === "running")) refreshJobs(); }, 4000);
}

document.addEventListener("DOMContentLoaded", async () => {
  bindNav();
  bindConsole();
  iconize(document);
  // initial theme before info loads (avoid flash)
  try {
    const r = await fetch("/api/settings"); const s = await r.json();
    document.documentElement.dataset.theme = s.theme || "dark";
  } catch { }
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (document.documentElement.dataset.theme || "dark")));
  // the first API call can fail transiently (server still starting up, or
  // WSL2's per-connection localhost proxy hiccuping) — retry a few times
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await new Promise(r => setTimeout(r, 800 + 700 * attempt));
    try {
      await refreshInfo();
      bootPage();
      startJobsPoll();
      startPrepWatcher();
      return;
    } catch (e) {
      if (attempt === 3) showBootFailure(e);
    }
  }
});
