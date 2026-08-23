/* pages/dashboard — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, el, ico, iconize, toast } from "../core.js";
import { refreshInfo } from "../info.js";
import { go } from "../nav.js";
import { prepActive, updatePrepRows } from "../prep.js";

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


export function onboardCard() {
  const steps = [
    ["1", "Calibrate & offset", "Print a calibration sheet and measure the drift — the saved X/Y/angle is then applied when the PDF is created. Optional: only needed if you're printing backs or double-sided cards."],
    ["2", "Fetch card art", "Pick a game (MTG, Pokémon, …) and a decklist. Art lands in game/front."],
    ["3", "Create the PDF", "Cards are laid out on your paper size with registration marks. Print double-sided."],
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
export function connectCardNeeded() {
  if (!S.info) return true;
  if (S.info.scm.found) return false;
  // hidden only while the app is actively preparing (bootstrap flag set) —
  // a failed/stopped prep must still show the card so a folder can be pointed in
  if (S.info.server.is_packaged && S.info.server.active) return false;
  return true;
}


export function repoSetupCard() {
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


export function statusGrid() {
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


export function quickActions() {
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

export function matrixCard(variant, compact) {
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
