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

function confirmModal({ title, text, icon = "alert", iconCls = "warn", okLabel = "OK", okClass = "btn", danger = false }) {
  return new Promise(resolve => {
    const root = $("#modal-root");
    const m = $(".modal", root);
    m.innerHTML = "";
    m.append(
      el("div", { class: `m-ico ${iconCls}` }, ico(icon)),
      el("h3", {}, title),
      el("p", {}, text),
    );
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

function go(page, prefill, { push = true } = {}) {
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
  pageEl.firstElementChild && pageEl.firstElementChild.classList.add("page-anim");
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
  const btn = el("button", { class: "cb-copy", onclick: () => { if (d.cmd) navigator.clipboard?.writeText(d.cmd).then(() => toast("ok", "Copied to clipboard")); } }, ico("copy"), "Copy");
  head.append(btn);
  box.append(head);
  if (!d.cmd) {
    box.append(el("pre", { class: "dim" }, "— incomplete —"));
  } else {
    box.append(el("pre", {}, d.cmd));
    if (d.cwd) box.append(el("pre", { class: "cb-cmt" }, el("span", { class: "p-cmt" }, `# cwd: ${d.cwd}`)));
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
      const sw = el("span", { class: "switch" },
        el("input", { type: "checkbox", checked: !!args[o.key] }),
        el("span", { class: "track" }),
        el("span", { class: "knob" }),
      );
      $("input", sw).addEventListener("change", e => { args[o.key] = e.target.checked; afterFormChange(kind); o.onChange && o.onChange(e.target.checked); });
      // a <label> row: clicking the text OR the switch toggles the checkbox
      wrap.append(el("label", { class: "switchrow" }, sw,
        el("span", { class: "sl" }, el("div", { class: "t" }, o.label), o.help ? el("div", { class: "d" }, o.help) : null)));
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
  if (o.help && o.type !== "toggle") wrap.append(el("span", { class: "help" }, o.help));
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
    if (g.collapsible) {
      const any = (g.options || []).some(o => o.show ? o.show(args) : true);
      const adv = el("div", { class: "adv" });
      adv.append(
        el("button", { class: "adv-head", type: "button", onclick: () => adv.classList.toggle("open") },
          el("span", { class: "arr" }, ico("arrow")), g.title),
        el("div", { class: "adv-body" }, groupInner(g, kind, args)),
      );
      card.append(adv);
    } else {
      card.append(el("div", { class: "section-label", "data-label": true }, g.title), groupInner(g, kind, args));
    }
  }

  if (opts.preview !== false) {
    const box = el("div", { class: "cmdbox", "data-kind": kind });
    card.append(box);
    setTimeout(() => updatePreview(kind), 0); // must run once the card is in the document
  }

  if (opts.run !== false) {
    const runBtn = el("button", { class: "btn primary", id: `run-${kind}` }, ico("play"), "Run " + spec.title.replace(/^Fetch .*card art$/, "card art fetch"));
    runBtn.onclick = () => doRun(kind, runBtn);
    const note = el("span", { class: "rb-note" }, "Runs in the background — watch the job console below.");
    card.append(el("div", { class: "runbar" }, note, runBtn));
  }
  return card;
}

function groupInner(g, kind, args) {
  const row = el("div", { class: "frow" });
  for (const o of g.options || []) {
    const node = renderOption(o, args, kind);
    if (node) row.append(node);
  }
  return row;
}

/* ================================ job control ============================== */

async function doRun(kind, btn, opts = {}) {
  if (!S.info || S.manifest[kind]) {
    for (const need of S.manifest[kind].needs || []) {
      if (need === "scm" && !S.info.scm.found) return toast("err", "SCM repo not found — open Settings and point it at your silhouette-card-maker folder.");
      if (need === "extras" && !S.info.extras.found) return toast("err", "scm-extras repo not found — open Settings and point it at your scm-extras folder.");
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
      body: JSON.stringify({ kind, args: S.forms[kind] }),
    });
    const j = await r.json();
    if (!j.ok) {
      toast("err", j.errors?.join("; ") || "Failed to start job");
    } else {
      for (const w of j.warnings || []) toast("warn", w, 5200);
      toast("ok", `${j.job.title} — job started`);
      refreshJobs();
      openConsole(j.job.id);
      if (kind === "calibration" || kind === "dxf_batch" || kind === "dxf_single" || kind === "extras_generate" || kind === "clean_up") {
        setTimeout(() => refreshInfo(), 2500);
      }
      if (kind.startsWith("fetch:")) {
        // keep the user's form state (pasted decklists etc.) alive
        setTimeout(() => refreshInfo({ keepForms: true }), 2500);
      }
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = ""; btn.append(ico("play"), "Run"); }
  }
}

async function refreshJobs() {
  S.jobs = (await api("/api/jobs")).jobs;
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
      el("div", { class: "jr-cmd" }, j.cmd || ""),
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
    el("span", { class: "mono", title: job.cmd }, truncate(job.cmd, 90)),
    el("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
      const r = await api("/api/reveal", { path: cwdOf(job) });
      if (r.ok) toast("ok", "Opened folder in your file manager"); else toast("warn", r.errors?.[0] || "Could not reveal folder");
    } }, ico("folder"), "Reveal folder"),
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

PAGES.dashboard = (root) => {
  const wrap = el("div", {});
  const s = S.info.settings;

  if (!S.info.scm.found) {
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

function repoSetupCard() {
  const wrap = el("div", { class: "card" });
  wrap.append(
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("folder")),
      el("div", { class: "grow" }, el("h2", {}, "Connect your repos"), el("p", {}, "The Workbench needs to find silhouette-card-maker. It was not found next to this project — paste the folder paths below.")),
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
  card("terminal", "--ok", `Python ${sv.python}`, sv.python_path, "ok");
  card("card", s.found ? "--ok" : "--err", s.found ? `silhouette-card-maker v${s.version || "?"}` : "silhouette-card-maker", s.found ? s.path : "not connected", s.found ? "ok" : "");
  card("sparkle", ex.found ? "--info" : "--warn", ex.found ? "scm-extras" : "scm-extras (optional)", ex.found ? `${ex.path} — ${ex.card_sizes.length} extra sizes` : "not connected — MTG/Sorcery extras unavailable", ex.found ? "ok" : "");
  card("scissors", "--accent", "Cutting templates",
    `${s.templates.dxf.length + s.templates.borderless_dxf.length} DXF · ${s.templates.studio3.length + s.templates.borderless_studio3.length} studio3${ex.found ? ` · extras: ${ex.templates.dxf.length + ex.templates.borderless_dxf.length} DXF, ${ex.templates.studio3.length + ex.templates.borderless_studio3.length} studio3` : ""}`, "ok");
  card("target", "--info", "Calibration sheets", `${s.calibration.length} PDF${s.calibration.length === 1 ? "" : "s"} in calibration/`, "ok");
  card("copy", s.saved_offset ? "--accent" : "--warn", "Saved offset",
    s.saved_offset ? `x ${s.saved_offset.x} · y ${s.saved_offset.y} · ${s.saved_offset.angle}°` : "none saved yet", s.saved_offset ? "ok" : "");
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
  wrap.append(pageHead("Fetch card art", "Pick a game, give it a decklist (existing file or pasted text) and a format. The plugin downloads the card images into game/front/ (and game/double_sided/ where applicable) — ready for the PDF step."));
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
      el("div", { class: "pc-t" }, S.manifest[kind].title.replace(/^Fetch /, "")),
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
    fileF.append(el("label", {}, "Decklist file ", el("span", { class: "req" }, "*")));
    const list = el("div", { class: "filepick" });
    if (!files.length) list.append(el("div", { class: "small faint" }, "No decklist files in game/decklist/ yet — use “Paste text” to create one."));
    for (const f of files) {
      list.append(el("div", {
        class: `fp-item ${args.deck_file === f.name ? "active" : ""}`,
        onclick: (ev) => {
          args.deck_file = f.name;
          $$(".fp-item", list).forEach(n => n.classList.remove("active"));
          ev.currentTarget.classList.add("active");
          afterFormChange(kind);
        },
      }, ico("file"), f.name, el("span", { class: "sz" }, fmtBytes(f.size))));
    }
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
  // URL source: make sure the format is one of this game's URL-based formats,
  // otherwise the URL would be passed to a file-reading parser (or rejected).
  const autoFormat = () => {
    if (args.deck_source !== "url") return;
    const urlFormats = (S.manifest[kind] || {}).url_formats || [];
    if (!urlFormats.length || urlFormats.includes(args.format)) return;
    args.format = urlFormats[0];
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
  if (!S.info.scm.found) wrap.append(repoSetupCard());
  wrap.append(formCard("create_pdf", { icon: "pdf" }));
  if (S.info.scm.saved_offset) {
    const o = S.info.scm.saved_offset;
    wrap.append(el("div", { class: "banner ok", style: "margin-top:16px" }, el("span", { class: "b-ico" }, ico("check")),
      el("span", { class: "grow" }, `Saved printer offset is available: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. Enable “Apply saved offset” below when ready.`),
      el("button", { class: "linkish", onclick: () => go("offset") }, "manage offset →")));
  }
  return wrap;
};

/* =============================== offset page ============================== */

PAGES.offset = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Offset & calibration", "Printer misalignment is the #1 cause of cards that don't line up. Generate a calibration sheet, measure the drift, save an offset, and apply it to your PDF before cutting."));
  if (!S.info.scm.found) wrap.append(repoSetupCard());
  prefillOffsetForm();

  // saved offset card
  const so = S.info.scm.saved_offset;
  const sc = el("div", { class: "card" });
  sc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("target")),
    el("div", { class: "grow" }, el("h2", {}, "Saved printer offset"), el("p", {}, so ? "Currently stored in the repo at data/offset_data.json — used by create_pdf --load_offset and offset_pdf." : "Nothing saved yet. Measure with a calibration sheet, then store the values here."))));
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
        if (r.ok) { toast("ok", "Offset saved — create_pdf can now apply it."); await refreshInfo(); go("offset"); }
      } }, ico("check"), "Save"),
      el("button", { class: "btn btn-ghost", style: "margin-left:6px", onclick: () => { xI.value = 0; yI.value = 0; aI.value = 0; } }, "zero"),
    )),
  ));
  wrap.append(sc);

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
  return wrap;
};

function prefillOffsetForm() {
  const args = S.forms.offset_pdf || (S.forms.offset_pdf = defaultArgs("offset_pdf"));
  const so = S.info.scm.saved_offset;
  if (so && (args.use_saved === undefined ? true : args.use_saved)) {
    if (args.x_offset === "" || args.x_offset === null) args.x_offset = so.x;
    if (args.y_offset === "" || args.y_offset === null) args.y_offset = so.y;
    if (args.angle === "" || args.angle === null) args.angle = so.angle;
  }
}

/* ============================== templates page ============================= */

PAGES.templates = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Cutting templates", "DXF cutting templates for the repo's standard sizes, plus the prebuilt .studio3 files that Silhouette Studio opens. For MTG / Sorcery extras see the Extras page."));
  if (!S.info.scm.found) wrap.append(repoSetupCard());
  wrap.append(formCard("dxf_single", { icon: "scissors" }));
  patchDxfForm("dxf_single");
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
  if (!S.info.scm.found) wrap.append(repoSetupCard());

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

PAGES.settings = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Settings", "Everything here is stored in this project's data/settings.json. Repo paths can also be left blank — the Workbench auto-detects sister folders named silhouette-card-maker and scm-extras."));

  const s = S.info.settings;

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

  // python & server
  const pc = el("div", { class: "card" });
  pc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("terminal")),
    el("div", { class: "grow" }, el("h2", {}, "Python & server"), el("p", {}, "Scripts run with the interpreter chosen here. Default: the one that started the Workbench. Install the base repo's requirements.txt into it: pip install -r requirements.txt"))));
  const pyI = el("input", { class: "input mono", value: s.python || "", placeholder: S.info.server.python_path + "  (default)" });
  const portI = el("input", { class: "input mono", type: "number", value: s.port || 8037, min: 1024, max: 65535 });
  pc.append(el("div", { class: "frow" },
    el("div", { class: "field w-half" }, el("label", {}, "Python interpreter"), pyI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Port"), portI),
    el("div", { class: "field w-quarter" }, el("label", {}, "Theme"),
      el("div", { class: "seg" },
        el("button", { type: "button", class: s.theme === "dark" ? "active" : "", onclick: () => setTheme("dark") }, ico("moon"), " Dark"),
        el("button", { type: "button", class: s.theme === "light" ? "active" : "", onclick: () => setTheme("light") }, ico("sun"), " Light"),
      )),
  ));
  const autoI = el("input", { type: "checkbox", id: "set-auto-browser", checked: s.auto_open_browser });
  pc.append(el("div", { style: "margin-top:10px" },
    el("label", { class: "switchrow" },
      el("span", { class: "switch" },
        autoI,
        el("span", { class: "track" }), el("span", { class: "knob" })),
      el("span", { class: "sl" }, el("div", { class: "t" }, "Open the browser when the server starts"))),
  ));
  pc.append(el("div", { style: "margin-top:12px; display:flex; gap:9px" },
    el("button", { class: "btn primary", onclick: async () => {
      const r = await api("/api/settings", { python: pyI.value.trim(), port: parseInt(portI.value), auto_open_browser: autoI.checked });
      toast("ok", "Saved. Port changes apply on next server start.");
      go("settings");
    } }, ico("check"), "Save python & server"),
  ));
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

async function refreshInfo({ keepForms = false } = {}) {
  S.info = await api("/api/info");
  S.manifest = await api("/api/manifest");
  document.documentElement.dataset.theme = S.info.settings.theme || "dark";
  $$("#theme-switch .ts-btn").forEach(b => b.classList.toggle("active", b.dataset.theme === (S.info.settings.theme || "dark")));
  // pills
  const dS = $("#dot-scm"); dS.classList.toggle("ok", S.info.scm.found);
  const dE = $("#dot-extras"); dE.classList.toggle("ok", S.info.extras.found); dE.classList.toggle("warn", !S.info.extras.found);
  $("#chip-python").textContent = "python " + S.info.server.python;
  if (!keepForms) S.forms = {};
  refreshJobs();
  renderConsoleTabs();
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
      return;
    } catch (e) {
      if (attempt === 3) showBootFailure(e);
    }
  }
});
