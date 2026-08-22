/* forms — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { openConsole, refreshJobs } from "./console.js";
import { $, $$, S, confirmModal, el, ico, toast } from "./core.js";
import { refreshInfo } from "./info.js";
import { repoReady } from "./prep.js";
import { uiMode } from "./nav.js";

export const escRe = x => String(x || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");


/* A kind is "simple-flagged" when any of its options carries `simple: true`
   in the manifest: in simple mode its flat rendering (formCard's `flat:`
   option) shows only those options; kinds without any flag (Fetch, Offset,
   …) have no simple version and render unchanged. Hidden options keep their
   defaults in S.forms, so the command preview is identical in both modes. */
export function kindHasSimple(spec) {
  return !!(spec && (spec.groups || []).some(g => (g.options || []).some(o => o.simple)));
}


export function optVisible(o, spec) {
  if (uiMode() !== "simple" || !kindHasSimple(spec)) return true;
  return !!o.simple;
}


export function repoRowForKind(kind) {
  const key = kind.startsWith("extras_") ? "extras" : "scm";
  return (S.info?.repos || []).find(r => r.key === key) || null;
}


export function displayCmd(cmd, kind) {
  if (!cmd) return "";
  let out = cmd;
  const py = S.info?.server?.python_path;
  if (py) out = out.replace(new RegExp("^[\"']?" + escRe(py) + "[\"']?\\s+"), "python ");
  const row = repoRowForKind(kind);
  if (row && row.path) out = out.split(row.path + "/").join("");
  // the Workbench's own scripts live outside the repo — show them by name,
  // not as a path into the app's private files (the app path can contain
  // spaces, so the sweep class must match them too)
  out = out.replace(/["']?[\w./: -]*clear_images\.py["']?/, "clear_images.py (workbench)");
  return out;
}


/* ============================== form system =============================== */
/* Forms are rendered from the server manifest; values live in S.forms[kind]. */

export function defaultArgs(kind) {
  const spec = S.manifest[kind];
  if (!spec) return {};
  const args = {};
  for (const g of spec.groups || [])
    for (const o of g.options)
      args[o.key] = o.type === "chips" || o.type === "choice_chips" ? (Array.isArray(o.default) ? [...o.default] : [])
        : (o.default !== undefined ? o.default : (o.type === "number" || o.type === "range" ? (o.default ?? "") : ""));
  return args;
}


export function formArgs(kind) {
  if (!S.forms[kind]) S.forms[kind] = defaultArgs(kind);
  return S.forms[kind];
}


export function afterFormChange(kind) {
  clearTimer(kind);
  S.timers[kind] = setTimeout(() => updatePreview(kind), 250);
}


export function clearTimer(kind) { if (S.timers[kind]) { clearTimeout(S.timers[kind]); S.timers[kind] = null; } }


export function updatePreview(kind) {
  const box = document.querySelector(`.cmdbox[data-kind="${kind}"]`);
  if (!box) return;
  fetch(`/api/preview?kind=${encodeURIComponent(kind)}&args=${encodeURIComponent(JSON.stringify(S.forms[kind]))}`)
    .then(r => r.json())
    .then(d => renderPreview(box, d))
    .catch(() => {});
}


export function renderPreview(box, d) {
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
  const blocked = !!(d.errors?.length || d.no_front_images);
  (S.previewBlock ||= {})[kind] = blocked;
  if (!blocked && d.cmd) box.append(el("div", { class: "note ok" }, "✓ ready to run"));
  const runBtn = document.getElementById("run-" + kind);
  if (runBtn && !runBtn.classList.contains("wait")) runBtn.disabled = blocked;
}


/* option renderers — one per manifest type */

export function renderOption(o, args, kind) {
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


export function strVal(v) { return v === null || v === undefined ? "" : String(v); }


export function stepNum(input, d) {
  const v = parseFloat(input.value);
  input.value = (isNaN(v) ? 0 : v) + d;
  input.dispatchEvent(new Event("input"));
}


/* generic form card for a manifest kind */

export function formCard(kind, opts = {}) {
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

  if (opts.flat) {
    // Flat layout: no group headers, no collapsible wrappers, no card title
    // of its own. In simple mode only the options flagged `simple` in the
    // manifest make it in (the everyday ones); everything else keeps its
    // default behind the scenes. A kind-level `simple_rows` list, when
    // given, is the layout of that section — one form row per entry, in
    // the listed order (e.g. the two dropdowns alone up top, the three
    // toggles together below); simple options not named in any row fall
    // into a final row, manifest order.
    const os = [];
    for (const g of spec.groups || [])
      for (const o of g.options || [])
        if (optVisible(o, spec)) os.push(o);
    const rows = uiMode() === "simple" ? (spec.simple_rows || []) : [];
    const place = keys => {
      const row = el("div", { class: "frow" });
      for (const k of keys) {
        const o = os.find(x => x.key === k);
        if (!o) continue;
        const node = renderOption(o, args, kind);
        if (node) row.append(node);
      }
      if (row.childElementCount) card.append(row);
    };
    if (rows.length) {
      const listed = new Set(rows.flat());
      rows.forEach(place);
      place(os.filter(o => !listed.has(o.key)).map(o => o.key));
    } else {
      place(os.map(o => o.key));
    }
  } else {
    for (const g of spec.groups || []) {
      const os = g.options || [];
      if (g.collapsible) {
        const any = os.some(o => o.show ? o.show(args) : true);
        if (!any) continue;
        const adv = el("div", { class: "adv" });
        adv.append(
          el("button", { class: "adv-head", type: "button", onclick: () => adv.classList.toggle("open") },
            el("span", { class: "arr" }, ico("arrow")), g.title),
          el("div", { class: "adv-body" }, groupInner(os, kind, args)),
        );
        card.append(adv);
      } else {
        card.append(el("div", { class: "section-label", "data-label": true }, g.title), groupInner(os, kind, args));
      }
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


export function groupInner(opts, kind, args) {
  const row = el("div", { class: "frow" });
  for (const o of opts) {
    const node = renderOption(o, args, kind);
    if (node) row.append(node);
  }
  return row;
}

/* ---- Simple / Advanced interface mode ------------------------------------
   Two sizes of Workbench in one app:
   • advanced (default) — every page, every control, exactly as documented.
   • simple — "don't overwhelm me": the side nav collapses to the essentials
     (Fetch card art, Create PDF, Settings) and the Create PDF form becomes
     one flat section (formCard(..., { flat: true, head: false }): no group
     headers, no collapsible wrappers, no card title — the page head above
     keeps the name) holding just the everyday options flagged `simple` in
     the manifest. The other forms (Fetch, Offset, …) have no simple version
     and render unchanged in both modes. */


/* ================================ job control ============================== */

export async function doRun(kind, btn, opts = {}) {
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
    // don't silently re-enable a button the latest preview has since blocked
    // (e.g. the front directory is still empty)
    if (btn && !S.previewBlock?.[kind]) { btn.disabled = false; btn.innerHTML = ""; btn.append(ico("play"), btn.dataset.label || "Run"); }
  }
}
