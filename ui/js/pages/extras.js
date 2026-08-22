/* pages/extras — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, PAGES, S, el, ico, pageHead } from "../core.js";
import { doRun, formCard } from "../forms.js";
import { go } from "../nav.js";
import { cardSvg, mm } from "./sizes.js";
import { templatesGallery } from "./templates.js";

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


export function extrasMatrix() {
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
