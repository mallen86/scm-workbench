/* pages/sizes — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, el, ico, iconize, pageHead } from "../core.js";import { go } from "../nav.js";PAGES.sizes = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Sizes & layouts", "Every card size known to the repos, including extras, with scaled silhouettes and the full paper by card layout matrix."));
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


export function sizeExplorer() {
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


export function cardSvg(w, h, r) {
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


export function paperStrip() {
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


export function specialtyRow() {
  const row = el("div", { class: "frow" });
  const items = S.info.scm.specialty;
  if (!items.length) return el("div", { class: "empty" }, "No specialty layouts are defined.");
  for (const s of items) {
    row.append(el("div", { class: "field w-full" },
      el("label", {}, s.name),
      el("div", { class: "small muted" }, `${s.paper} · ${s.cols}×${s.rows} grid · card ${s.width} × ${s.height}`),
    ));
  }
  return row;
}


export function mm(sizeStr) {
  if (!sizeStr) return 0;
  const m = /([\d.]+)\s*(mm|in)/.exec(String(sizeStr));
  if (!m) return parseFloat(sizeStr) || 0;
  return m[2] === "mm" ? parseFloat(m[1]) : parseFloat(m[1]) * 25.4;
}


/* The cards-per-page matrix, with its Default/Borderless switch. It lives on
   this page now (the dashboard that used to share it is gone). */
export function matrixCard(variant) {
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
        el("p", {}, "Paper size by card size. Cells show columns by rows and the total."),
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
