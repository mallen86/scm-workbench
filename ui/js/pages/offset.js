/* pages/offset — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, el, fmtBytes, ico, pageHead, toast } from "../core.js";
import { afterFormChange, defaultArgs, doRun, formCard } from "../forms.js";
import { refreshInfo } from "../info.js";
import { go } from "../nav.js";
import { connectCardNeeded, repoSetupCard } from "./dashboard.js";

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


export function offsetsBySizeCard() {
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
export function offsetSourceFor(size) {
  if (size) {
    const e = (S.info.per_size_offsets || {})[size];
    if (e) return e;
  }
  return S.info.scm.saved_offset || null;
}


export function prefillOffsetForm() {
  const args = S.forms.offset_pdf || (S.forms.offset_pdf = defaultArgs("offset_pdf"));
  const src = offsetSourceFor(args.paper_size);
  if (src && (args.use_saved === undefined ? true : args.use_saved)) {
    if (args.x_offset === "" || args.x_offset === null || args.x_offset === undefined) args.x_offset = src.x;
    if (args.y_offset === "" || args.y_offset === null || args.y_offset === undefined) args.y_offset = src.y;
    if (args.angle === "" || args.angle === null || args.angle === undefined) args.angle = src.angle;
  }
}


export function patchOffsetForm() {
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
export function paperForCreatePdf(form = {}) {
  const sp = (S.info.scm.specialty || []).find(s => s.name === form.specialty);
  if (sp && sp.paper) return sp.paper;
  return form.paper_size || (S.info.settings.defaults || {}).paper_size || "letter";
}
