/* pages/dashboard — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, el, ico, iconize, toast, openUrl } from "../core.js";import { refreshInfo } from "../info.js";import { go } from "../nav.js";import { prepActive, updatePrepRows } from "../prep.js";import { setSettings } from "../settings-transport.js";
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
          el("p", {}, "First launch setup runs in the background. Track progress below. Actions that need a repo unlock when it is ready.")),
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
  // the documentation card gets its own divider and sits above Recent jobs —
  // tucking it below the jobs list made it read as part of that (empty) box
  wrap.append(el("div", { class: "section-label" }, "Documentation"));
  wrap.append(el("div", { class: "card" }, el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("book")),
    el("div", { class: "grow" }, el("h2", {}, "Documentation"), el("p", {}, "The docs site covers each step with photos, from supplies and printing to cutting and troubleshooting.")),
    el("button", { class: "btn", onclick: () => openUrl("https://alan-cha.github.io/silhouette-card-maker/", "the silhouette-card-maker docs") }, ico("external"), "Open docs"),
  )));
  wrap.append(el("div", { class: "section-label" }, "Recent jobs"));
  wrap.append(el("div", { id: "recent-jobs" }));
  return wrap;
};


export function onboardCard() {
  const steps = [
    ["1", "Calibrate & offset", "Print a calibration sheet, measure the drift, and save the X/Y/angle correction. Create PDF applies it when enabled."],
    ["2", "Fetch card art", "Choose a game and decklist. Card art goes to game/front."],
    ["3", "Create the PDF", "Lay out cards on your paper with registration marks. Print both sides when needed."],
    ["4", "Cut with a template", "Open the matching .studio3 template in Silhouette Studio and cut your cards."],
  ];
  return el("div", { class: "onboard" },
    el("h2", {}, "Welcome to the Workbench"),
    el("p", { class: "muted", style: "margin-top:6px; font-size:13px; max-width:720px; line-height:1.55" },
      "This console runs scripts from silhouette-card-maker and scm-extras, so no terminal is needed. The workflow has four stages:"),
    el("div", { class: "steps" },
      steps.map(([n, t, d]) => el("div", { class: "step" },
        el("div", { class: "n" }, n), el("div", { class: "t" }, t), el("div", { class: "d" }, d),
      )),
    ),
    el("div", { style: "margin-top:16px" },
      el("button", { class: "btn primary", onclick: async () => { await setSettings({ onboarded: true }); S.info.settings.onboarded = true; go("dashboard"); } }, "Got it. Show me the dashboard"),
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
          ? "Managed copies of both repos are downloading. Pages connect as each repo is ready. A path you enter takes priority."
          : "Workbench could not find silhouette-card-maker next to this project. Enter repo paths below."),
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
      const r = await setSettings({ scm_dir: scmInp.value.trim(), extras_dir: exInp.value.trim() });
      await refreshInfo();
      toast(S.info.scm.found ? "ok" : "err", S.info.scm.found ? "Connected. Reloaded the dashboard." : "Still not found. Check the path and try again.");
      go("dashboard");
    } }, ico("check"), "Save & reconnect"),
    el("span", { class: "faint small" }, "Paths are saved in this project's data/settings.json."),
  ));
  return wrap;
}


export function statusGrid() {
  const i = S.info, s = i.scm, ex = i.extras, sv = i.server;
  const repos = i.repos || [];
  const rr = k => repos.find(r => r.key === k);
  const rS = rr("scm"), rE = rr("extras");
  const dTag = (r, base, detail) => (r && r.mode === "managed" && r.deployed)
    ? `managed copy: ${r.deployed.ref}${detail ? " | " + detail : ""}`
    : (detail ? `${base}: ${detail}` : base);
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
  card("terminal", "--ok", `Python ${sv.python}`, (i.server.is_packaged && sv.python_path) ? "private runtime in the data folder" : sv.python_path, "ok");
  if (i.server.is_windows && i.server.is_packaged) {
    const wf = i.window || {};
    const inBrowser = wf.mode === "browser";
    // the specific failure beats the generic one: dig the real error line out
    // of the recorded traceback when there is one
    const diag = wf.detail
      ? String(wf.detail).split("\n").map(s => s.trim())
          .filter(l => /error|exception|fail|0x[0-9a-f]{8}/i.test(l)).pop()
      : null;
    card("window", inBrowser ? "--warn" : "--ok", "App window",
      inBrowser
        ? "the native window failed to start on this machine. The UI is running in your browser" +
          ((diag || wf.reason) ? ` (${String(diag || wf.reason).slice(0, 160)})` : "")
        : "native window in use with WinForms and WebView2",
      inBrowser ? "" : "ok");
  }
  const preparing = i.server.is_packaged && prepActive();
  card("card", s.found ? "--ok" : "--err", s.found ? `silhouette-card-maker v${s.version || "?"}` : "silhouette-card-maker", s.found ? dTag(rS, s.path) : (preparing ? "preparing managed copy" : "not connected"), s.found ? "ok" : "");
  card("sparkle", ex.found ? "--info" : "--warn", ex.found ? "scm-extras" : "scm-extras (optional)", ex.found ? dTag(rE, ex.path, `${ex.card_sizes.length} extra sizes`) : (preparing ? "preparing managed copy" : "not connected; MTG and Sorcery extras unavailable"), ex.found ? "ok" : "");
  card("scissors", "--accent", "Cutting templates",
    `${s.templates.dxf.length + s.templates.borderless_dxf.length} DXF | ${s.templates.studio3.length + s.templates.borderless_studio3.length} studio3${ex.found ? ` | extras: ${ex.templates.dxf.length + ex.templates.borderless_dxf.length} DXF, ${ex.templates.studio3.length + ex.templates.borderless_studio3.length} studio3` : ""}`, "ok");
  card("target", "--info", "Calibration sheets", `${s.calibration.length} PDF${s.calibration.length === 1 ? "" : "s"} in calibration/`, "ok");
  const nPso = Object.keys(S.info.per_size_offsets || {}).length;
  card("copy", (s.saved_offset || nPso) ? "--accent" : "--warn", "Saved offset",
    (s.saved_offset ? `x ${s.saved_offset.x} | y ${s.saved_offset.y} | ${s.saved_offset.angle}°` : "none saved yet")
      + (nPso ? ` | ${nPso} paper specific row${nPso === 1 ? "" : "s"}` : ""),
    (s.saved_offset || nPso) ? "ok" : "");
  return grid;
}


export function quickActions() {
  const g = el("div", { class: "qa-grid" });
  const qa = (icoName, title, desc, fn) => g.append(el("div", { class: "qa", onclick: fn },
    el("div", { class: "qa-ico" }, ico(icoName)), el("div", { class: "qa-t" }, title), el("div", { class: "qa-d" }, desc)));
  qa("pdf", "Create PDF", "Lay out card art in a PDF that is ready to print", () => go("pdf"));
  qa("download", "Fetch MTG art", "Download card images from a decklist", () => { S.plugin = "mtg"; go("fetch", { plugin: "mtg" }); });
  qa("sparkle", "Fetch other games", "22 TCGs and LCGs supported, including Yu-Gi-Oh!, Pokémon, and Lorcana.", () => { go("fetch"); });
  qa("scissors", "Generate DXF templates", "Create a cutting template for any card and paper size", () => go("templates"));
  qa("target", "Calibration sheets", "Print alignment sheets to measure printer drift", () => go("offset"));
  qa("trash", "Start fresh", "Clear the front and double sided image folders", () => go("utilities"));
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
