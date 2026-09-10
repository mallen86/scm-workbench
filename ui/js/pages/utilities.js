/* pages/utilities — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, PAGES, confirmModal, el, ico, pageHead } from "../core.js";import { jobs } from "../jobs.js";import { doRun } from "../forms.js";import { connectCardNeeded, repoSetupCard } from "../repo-setup.js";import { mm } from "./sizes.js";
/* ================================ utilities page ========================== */

PAGES.utilities = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Utilities", "Tools for cleaning art folders, converting units, and listing known sizes."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());

  // clean up
  const cc = el("div", { class: "card" });
  cc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("trash")),
    el("div", { class: "grow" }, el("h2", {}, "Start a new game"), el("p", {}, "Deletes images in game/front/ and game/double_sided/. Card backs in game/back/ stay. This cannot be undone.")),
  ));
  cc.append(el("div", { class: "runbar" },
    el("span", { class: "rb-note" }, "This runs clean_up.py from the repo after you confirm."),
    el("button", { class: "btn danger", onclick: async () => {
      const ok = await confirmModal({ title: "Delete card images?", text: "Images in game/front/ and game/double_sided/ will be permanently deleted. Card backs stay.", okLabel: "Yes, clear them", danger: true, icon: "trash", iconCls: "warn" });
      if (ok) doRun("clean_up", null);
    } }, ico("trash"), "Clear front & double-sided")));
  wrap.append(cc);

  // converter
  wrap.append(converterCard());

  // list sizes
  const lc = el("div", { class: "card" });
  lc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("ruler")),
    el("div", { class: "grow" }, el("h2", {}, "List every known size"), el("p", {}, "Runs generate_dxf.py list and prints all card and paper sizes, including extras, in the job console.")),
    el("button", { class: "btn", onclick: () => doRun("dxf_list", null) }, ico("terminal"), "Run"),
  ));
  wrap.append(lc);
  return wrap;
};


export function converterCard() {
  const card = el("div", { class: "card" });
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("ruler")),
    el("div", { class: "grow" }, el("h2", {}, "Size converter"), el("p", {}, "Uses the same math as size_convert.py for mm, inches, points, and pixels at any PPI."))));
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
export function watchJobDone(jobId, cb) {
  const t = setInterval(async () => {
    let d;
    try { d = await jobs.log(jobId); } catch { return; }
    if (d.status !== "running") {
      clearInterval(t);
      cb(d);
    }
  }, 3000);
}

/* One row of the “Managed repo copies” card: source picker, live status,
   check / update actions. Re-renders itself in place when the source changes. */
