/* pages/settings — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { PAGES, S, api, el, ico, pageHead, toast, openUrl, $, $$ } from "../core.js";import { revealPath } from "../native-actions.js";import { canPickRepoDirectory, pickRepoDirectory, setSettings } from "../settings-transport.js";import { listRepoRefs, setRepoSource, checkRepo } from "../repos-transport.js";import { getUpdates, checkUpdates, getUpdateNotes, startUpdate as startUpdateRequest } from "../updates-transport.js";

// Update state is server-validated, but keep this boundary defensive before a
// URL reaches the OS browser. A release link must remain on GitHub and have
// the server's release URL shape; credentials, redirects, and extra data are
// never accepted.
function repoPathControl(input, label) {
  if (!canPickRepoDirectory()) return input;
  const browse = el("button", {
    class: "btn",
    type: "button",
    "aria-label": `Browse for ${label}`,
    onclick: async () => {
      browse.disabled = true;
      try {
        const selected = await pickRepoDirectory();
        if (selected !== null) input.value = selected;
      } catch (error) {
        toast("err", error?.message || "Could not open the repository folder picker");
      } finally {
        browse.disabled = false;
      }
    },
  }, ico("folder"), "Browse…");
  return el("div", { class: "repo-path-control" }, input, browse);
}


function serverReleaseUrl(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const u = new URL(value);
    const part = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
    const path = u.pathname.split("/");
    if (u.protocol !== "https:" || u.hostname !== "github.com" || u.port || u.username || u.password || u.search || u.hash ||
        path.length !== 6 || !part.test(path[1]) || !part.test(path[2]) || path[3] !== "releases" || path[4] !== "tag" || !path[5]) return null;
    return u.href;
  } catch {
    return null;
  }
}

/* In-app "What's new": the release notes live on GitHub, but the app's
   window can't open a browser tab in its webview, so the notes are fetched
   through the server (which also renders the markdown) and shown in the
   app's own modal — same chrome as every other confirmation here. */
function showWhatsNew(tag, releaseUrl) {
  const safeReleaseUrl = serverReleaseUrl(releaseUrl);
  return (async () => {
    const root = $("#modal-root");
    const m = $(".modal", root);
    m.innerHTML = "";
    m.append(el("div", { class: "m-ico info" }, ico("info")));
    m.append(el("h3", {}, "What's new in ", String(tag ?? "")));
    m.append(el("div", { class: "m-loading" }, "Fetching the release notes…"));
    root.hidden = false;
    const close = () => { root.hidden = true; };
    try {
      const r = await getUpdateNotes(tag);
      if (!r.ok) throw new Error(r.error || "the release notes couldn't be fetched");
      m.querySelectorAll(".m-ico, h3, .m-loading").forEach(n => n.remove());
      m.append(el("h3", {}, "What's new in ", String(r.tag ?? "")));
      if (r.published) m.append(el("p", { class: "m-when" }, "Released ", String(r.published)));
      const body = el("div", { class: "notes" });
      // The server's markdown renderer is the sole trusted HTML boundary.
      body.innerHTML = r.body || "<p>(no notes on this release)</p>";
      m.append(body);
      const actions = el("div", { class: "m-actions" },
        el("button", { class: "btn", onclick: close }, "Close"),
      );
      if (safeReleaseUrl) {
        // a raw <a> here would carry target="_blank" — the app's webview can't
        // spawn that (window.open is denied), and even href="#" bounces the
        // whole app away on a mis-click. The OS browser is the
        // honest target, and the app already has a sanctioned door for it.
        actions.append(el("button", { class: "btn", onclick: () => { close(); openUrl(safeReleaseUrl, "the release page"); } }, "Open on GitHub"));
      }
      m.append(actions);
      document.addEventListener("keydown", function onKey(e) {
        if (e.key !== "Escape") return;
        close();
        document.removeEventListener("keydown", onKey);
      });
    } catch (e) {
      m.querySelector(".m-loading").remove();
      m.append(el("p", {}, "The notes couldn't be loaded: " + e.message));
      m.append(el("div", { class: "m-actions" }, el("button", { class: "btn", onclick: close }, "Close")));
    }
  })();
}
import { doRun, numSteppers } from "../forms.js";import { refreshInfo } from "../info.js";import { go, uiMode } from "../nav.js";import { openConsole, attachStream, renderConsoleTabs, toggleConsole } from "../console.js";import { refreshUpdateNotice, startUpdateStrip } from "../updater-ui.js";import { watchJobDone } from "./utilities.js";export function repoCopyRow(row, container, simple = false) {
  const box = el("div", { class: "rcre", style: "margin-top:14px; padding-top:12px; border-top:1px solid var(--border-soft)" });
  let selectingPinned = false;
  let repoInitPending = false;
  const modeOf = src => ["main", "latest-release"].includes(src) ? src : "pinned";
  const pickSource = async (v) => {
    if (!v) return;
    try {
      const r = await setRepoSource(row.key, v);
      if (!r.ok) return toast("err", (r.errors || ["could not save the source"]).join("; "));
      await refreshInfo({ keepForms: true });
      const fresh = (S.info.repos || []).find(x => x.key === row.key) || row;
      if (container) container.replaceChildren(repoCopyRow(fresh, container));
      else rerender();
      const warnings = Array.isArray(r.warnings) ? r.warnings.filter(Boolean) : (r.warnings ? [r.warnings] : []);
      if (warnings.length) {
        for (const warning of warnings) toast("warn", warning, 5200);
      } else {
        toast("ok", `Tracking “${r.target ? r.target.ref : v}” for ${row.name}.`);
      }
    } catch (e) {
      toast("err", e.message || "could not save the source");
    }
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
    if (row.path && row.mode === "managed") head.append(el("div", { class: "rc-path" }, "kept in the app's data folder"));
    else if (row.path) head.append(el("div", { class: "rc-path mono" }, row.path));
    box.append(head);
    const seg = el("div", { class: "seg" });
    const segBtns = {};
    for (const [v, lab] of [["main", "Latest (main)"], ["latest-release", "Latest release"], ["pinned", "Pinned"]]) {
      const b = el("button", { type: "button", class: (mode === v || (v === "pinned" && selectingPinned)) ? "active" : "", onclick: () => { if (v === "pinned") { selectingPinned = true; rerender(); } else pickSource(v); } }, lab);
      segBtns[v] = b;
      seg.append(b);
    }
    // a repo with no published releases can't be tracked by "latest release" —
    // dim that segment instead of letting the save fail with a message. The
    // whole selector is advanced-mode only: in simple mode the row shows the
    // deployed version plus Check/Update, and the saved source keeps working
    // behind the scenes.
    if (!simple) {
      listRepoRefs(row.key).then(r => {
        if (r.ok && !(r.refs.releases || []).length) {
          segBtns["latest-release"].disabled = true;
          segBtns["latest-release"].classList.add("off");
          segBtns["latest-release"].title = "This repo has no published releases yet";
        }
      }).catch(() => { });
      const showPicker = mode === "pinned" || selectingPinned;
      const pinWrap = el("div", { class: "field", style: "display:" + (showPicker ? "block" : "none") });
      const pinSel = el("select", { class: "input" }, el("option", { value: "" }, "— pick a tag / release —"));
      if (showPicker) {
        box.append(el("div", { style: "margin-top:10px; display:flex; gap:10px; align-items:center; flex-wrap:wrap" },
          el("span", { class: "small faint" }, "track"), seg, pinWrap));
        const r = await listRepoRefs(row.key);
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
    }
    // status line
    const dep = row.deployed;
    const lc = row.last_check && row.last_check.checked ? row.last_check.checked : null;
    let statusText, statusCls;
    if (!dep) { statusText = "No managed copy yet. Download one to track updates."; statusCls = "warn"; }
    else if (lc && lc.ok && lc.up_to_date) { statusText = `At ${dep.ref} (${dep.sha.slice(0, 7)}). Up to date.`; statusCls = "ok"; }
    else if (lc && lc.ok && lc.target) { statusText = `New version: ${lc.target.ref} (${lc.target.sha.slice(0, 7)}). Deployed: ${dep.ref} (${dep.sha.slice(0, 7)}).`; statusCls = "warn"; }
    else { statusText = `Deployed at ${dep.ref} (${dep.sha.slice(0, 7)})` + (dep.date ? `, ${String(dep.date).slice(0, 10)}` : ""); statusCls = "ok"; }
    box.append(el("div", { class: `note ${statusCls}`, style: "margin-top:10px" }, statusText));
    // actions
    const acts = el("div", { style: "margin-top:10px; display:flex; gap:8px; flex-wrap:wrap" });
    if (row.mode === "managed") {
      const checkBtn = el("button", { class: "btn sm" }, ico("search"), "Check for updates");
      checkBtn.onclick = async () => {
        checkBtn.disabled = true;
        try {
          const r = await checkRepo(row.key, true);
          if (r.ok) { row.last_check = r.last_check; await refreshInfo({ keepForms: true }); const fresh = (S.info.repos || []).find(x => x.key === row.key); if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container)); else rerender(); }
          else toast("err", (r.errors || ["check failed"]).join("; "));
        } catch (error) {
          toast("err", error?.message || "check failed");
        } finally {
          checkBtn.disabled = false;
        }
      };
      const hasUpdate = lc && lc.ok && !lc.up_to_date;
      const upBtn = el("button", { class: `btn sm ${hasUpdate ? "primary" : ""}` }, ico("refresh"), hasUpdate ? "Update now" : "Update");
      upBtn.onclick = async () => {
        const job = await doRun("repo_update", null, { args: { repo: row.key, force_full: false }, confirm: {
        title: `Update ${row.name}`,
        text: `Moves the managed copy to “${mode === "main" ? "the latest main" : mode === "latest-release" ? "the latest release" : src}”. Forward moves fetch changed files only. Rollbacks and large jumps use a full snapshot. Images, decklists, and local edits are preserved. If upstream changed an edited file too, your version is kept and flagged.`,
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
      const initRunning = (S.jobs || []).some(j => j.kind === "repo_init" && j.status === "running");
      const dlBtn = el("button", { class: "btn sm", disabled: initRunning,
        title: initRunning ? "A managed-copy download is already running" : null }, ico("download"), "Also keep a managed copy");
      dlBtn.onclick = async () => {
        if (repoInitPending || S.repoInitPending || dlBtn.disabled ||
            (S.jobs || []).some(j => j.kind === "repo_init" && j.status === "running")) {
          dlBtn.disabled = true; return;
        }
        repoInitPending = true; S.repoInitPending = true; dlBtn.disabled = true;
        const job = await doRun("repo_init", null, { args: { repo: row.key }, confirm: {
        title: `Download a managed copy of ${row.name}`,
        text: simple
          ? "Keeps a second repo copy managed by Workbench in the data folder. Your clone stays untouched, and the copy follows the latest main."
          : "Keeps a second repo copy managed by Workbench in the data folder. Your clone stays untouched. Choose a source above to track something other than the latest main.",
        okLabel: "Download", icon: "download" } });
        if (!job) { repoInitPending = false; S.repoInitPending = false; dlBtn.disabled = false; return; }
        watchJobDone(job.id, async () => {
          repoInitPending = false; S.repoInitPending = false;
          await refreshInfo({ keepForms: true });
          const fresh = (S.info.repos || []).find(x => x.key === row.key);
          if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container));
        });
      };
      acts.append(dlBtn, el("span", { class: "small faint" }, "Your clone stays unchanged. Workbench updates the managed copy."));
    } else {
      const initRunning = (S.jobs || []).some(j => j.kind === "repo_init" && j.status === "running");
      const dlBtn = el("button", { class: "btn sm primary", disabled: initRunning,
        title: initRunning ? "A managed-copy download is already running" : null }, ico("download"), "Download latest (managed copy)");
      dlBtn.onclick = async () => {
        if (repoInitPending || S.repoInitPending || dlBtn.disabled ||
            (S.jobs || []).some(j => j.kind === "repo_init" && j.status === "running")) {
          dlBtn.disabled = true; return;
        }
        repoInitPending = true; S.repoInitPending = true; dlBtn.disabled = true;
        const job = await doRun("repo_init", null, { args: { repo: row.key }, confirm: {
        title: `Download ${row.name}`,
        text: `Fetches a complete copy into the Workbench data folder. The first download may be large because silhouette-card-maker includes documentation and test files.`,
        okLabel: "Download", icon: "download" } });
        if (!job) { repoInitPending = false; S.repoInitPending = false; dlBtn.disabled = false; return; }
        watchJobDone(job.id, async () => {
          repoInitPending = false; S.repoInitPending = false;
          await refreshInfo({ keepForms: true });
          const fresh = (S.info.repos || []).find(x => x.key === row.key);
          if (fresh && container) container.replaceChildren(repoCopyRow(fresh, container));
        });
      };
      acts.append(dlBtn, el("span", { class: "small faint" }, "Afterward, updates are one click and incremental."));
    }
    box.append(acts);
  };
  const rerender = () => render().catch(error => {
    toast("err", error?.message || "could not load repository information");
  });
  rerender();
  return box;
}


PAGES.settings = (root) => {
  const wrap = el("div", {});
  const simple = uiMode() === "simple";
  wrap.append(pageHead("Settings", simple
    ? "Everything here is stored in this project's data folder, away from your repos."
    : "Everything here is stored in this project's data/settings.json. Leave repo paths blank to find sibling folders named silhouette-card-maker and scm-extras automatically."));

  const s = S.info.settings;

  // (the Simple / Advanced switch lives in the sidebar on every page —
  //  #mode-switch — not as a settings card)
  // create-PDF defaults — first card in both modes; it is the most-used
  // of all the options on this page

  // defaults
  const dc = el("div", { class: "card" });
  dc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("gear")),
    el("div", { class: "grow" }, el("h2", {}, "Create PDF defaults"), el("p", {}, "Values preselected on Create PDF. You can change them there." ))));
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
      await setSettings({ defaults: { card_size: csSel.value, paper_size: psSel.value, ppi: toNum(ppiV.value, +ppiR.value), quality: toNum(qualV.value, +qualR.value) } });
      toast("ok", "Defaults saved.");
      go("settings");
    } }, ico("check"), "Save defaults"),
  ));
  wrap.append(dc);

  // repos
  const rc = el("div", { class: "card" });
  rc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("folder")),
    el("div", { class: "grow" }, el("h2", {}, "Repos"), el("p", {}, "Repo paths. Leave blank to find them next to this project automatically."))));
  const scmI = el("input", { class: "input mono", value: s.scm_dir || "", placeholder: "auto: ../silhouette-card-maker" });
  const exI = el("input", { class: "input mono", value: s.extras_dir || "", placeholder: "auto: ../scm-extras" });
  rc.append(el("div", { class: "frow" },
    el("div", { class: "field w-half" }, el("label", {}, "silhouette-card-maker"), repoPathControl(scmI, "silhouette-card-maker")),
    el("div", { class: "field w-half" }, el("label", {}, "scm-extras"), repoPathControl(exI, "scm-extras")),
  ));
  rc.append(el("div", { style: "margin-top:12px; display:flex; gap:9px; align-items:center" },
    el("button", { class: "btn primary", onclick: async () => {
      const r = await setSettings({ scm_dir: scmI.value.trim(), extras_dir: exI.value.trim() });
      await refreshInfo();
      toast(S.info.scm.found ? "ok" : "warn", S.info.scm.found ? "Reconnected. The new paths are in use." : "Saved, but the SCM repo is still not found at that path.");
      go("settings");
    } }, ico("check"), "Save repo paths"),
  ));
  if (!simple) wrap.append(rc);

  // managed repo copies (download/update the sister repos from inside the Workbench)
  const mc = el("div", { class: "card" });
  mc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("refresh")),
    el("div", { class: "grow" }, el("h2", {}, "Managed repo copies"), el("p", {}, simple
      ? "Workbench keeps its own repo copies in the data folder. Check for updates and install them here. Images, decklists, and local edits survive updates."
      : "Workbench can keep its own repo copies in the data folder. Fetch the newest version on demand and choose what to track: main, the latest release, or a pinned tag. Images, decklists, and local edits survive updates."))));
  for (const row of (S.info.repos || [])) {
    const wrapRow = el("div", {});            // each row replaces itself inside its own wrapper
    mc.append(wrapRow);
    wrapRow.append(repoCopyRow(row, wrapRow, simple));
  }
  wrap.append(mc);

  // python & server
  const packaged = !!S.info.server.is_packaged;
  const pc = el("div", { class: "card" });
  pc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("terminal")),
    el("div", { class: "grow" }, el("h2", {}, "Python & server"),
      el("p", {}, packaged
        ? "This app uses a private Python runtime in its data folder. Job dependencies install there automatically. Your system Python is not changed."
        : "Scripts use the selected interpreter. By default, this is the interpreter that started Workbench. Install the base repo's requirements.txt into it: pip install -r requirements.txt"))));
  const pyI = el("input", { class: "input mono", value: s.python || (packaged && S.info.server.python ? `python ${S.info.server.python}  (private runtime)` : ""), placeholder: S.info.server.python_path + "  (default)", title: S.info.server.python_path || "", readonly: packaged || null });
  const portI = el("input", { class: "input mono", type: "number", value: s.port || 8037, min: 1024, max: 65535 });
  const portW = el("span", { class: "numwrap" }, portI, numSteppers(portI, 1));
  pc.append(el("div", { class: "frow" },
    el("div", { class: packaged ? "field w-full" : "field w-half" }, el("label", {}, packaged ? "Private Python" : "Python interpreter"), pyI),
    ...(packaged ? [] : [el("div", { class: "field w-quarter" }, el("label", {}, "Port"), portW)]),
  ));
  if (!packaged) {
    const autoI = el("input", { type: "checkbox", id: "set-auto-browser", checked: s.auto_open_browser });
    const sw = el("span", { class: "switch" }, autoI, el("span", { class: "track" }), el("span", { class: "knob" }));
    const lab = el("label", {}, "Open the browser when the server starts");
    lab.setAttribute("for", "set-auto-browser");
    pc.append(el("div", { class: "field", style: "margin-top:10px" }, lab, sw));
    pc.append(el("div", { style: "margin-top:12px; display:flex; gap:9px" },
      el("button", { class: "btn primary", onclick: async () => {
        await setSettings({ python: pyI.value.trim(), port: parseInt(portI.value), auto_open_browser: autoI.checked });
        await refreshInfo();
        toast("ok", "Saved. The selected Python interpreter will run new jobs. Port changes apply next time the server starts.");
        go("settings");
      } }, ico("check"), "Save python & server"),
    ));
  }
  if (!simple) wrap.append(pc);


  // app updates (the packaged app checks GitHub for itself, at start and daily)
  const uc = el("div", { class: "card" });
  uc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("arrow")),
    el("div", { class: "grow" }, el("h2", {}, "App updates"),
      el("p", {}, packaged
        ? "Checks GitHub for the newest app release at startup and once daily while open. Installing a version replaces only the app folder. Your data folder is untouched."
        : "Running from a source checkout. Pull the Workbench repo to update it."))));
  if (packaged) {
    const uRow = el("div", { class: "frow", style: "align-items:center; gap:14px" });
    const uBtn = el("button", { class: "btn primary" });
    const uLast = el("span", { class: "small faint" });
    uRow.append(uBtn, uLast);
    const uStatus = el("div", { class: "small", style: "margin-top:10px; line-height:1.55" });
    uc.append(uRow, uStatus);

    const humanize = ts => {
      if (!ts) return "never";
      const d = new Date(ts * 1000), now = new Date();
      const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (d.toDateString() === now.toDateString()) return `today at ${hm}`;
      const days = (now - d) / 864e5;
      if (days < 2) return `yesterday at ${hm}`;
      if (days < 7) return `${Math.floor(days)} days ago`;
      return d.toLocaleDateString();
    };

    let busy = false;
    let checkPending = false;
    const vv = t => "v" + String(t || "").replace(/^v/, "");   // display form of a tag (v0.2.0 → v0.2.0, 0.2.0 → v0.2.0)

    const render = async () => {
      let r;
      try { r = await getUpdates(); }
      catch (error) {
        uLast.textContent = "Last checked: unavailable";
        uBtn.textContent = checkPending ? "Checking…" : "Update check unavailable";
        uBtn.disabled = true;
        uBtn.onclick = null;
        uStatus.textContent = checkPending
          ? "Asking GitHub for the newest release…"
          : "The update service is unavailable: " + (error?.message || "try again later");
        return;
      }
      const st = r.state || {};
      uLast.textContent = "Last checked: " + humanize(st.checked_at);
      const setBtn = (label, onClick, disabled = false, title = "") => {
        uBtn.textContent = "";
        uBtn.append(ico(disabled ? "clock" : "arrow"), el("span", {}, " " + label));
        uBtn.disabled = disabled;
        uBtn.onclick = disabled ? null : onClick;
        uBtn.title = title;
      };
      // if (st.checking) is represented by this combined server/local guard.
      if (checkPending || st.checking) {   // a check is in flight (the daily daemon or another click)
        setBtn("Checking…", null, true);
        uStatus.textContent = "Asking GitHub for the newest release…";
        return;
      }
      switch (st.status) {
        case "never":
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "Not checked yet. The first automatic check runs at startup and once daily while the app is open.";
          break;
        case "auth-required":
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "The release repo is private, so this check cannot see releases. Once it is public, no setup is needed. Press again to check.";
          break;
        case "error":
          setBtn("Check again", doCheck);
          uStatus.textContent = "The last check failed: " + (st.reason || "unknown error");
          break;
        case "up-to-date": {
          const latest = vv(st.latest || r.current);
          setBtn("Check for updates", doCheck);
          uStatus.replaceChildren("You are on the latest version. ", el("b", {}, latest),
            " is the newest release. Press to check GitHub again.");
          break;
        }
        case "update-available": {
          const latest = vv(st.latest);
          const releaseUrl = serverReleaseUrl(st.release_url);
          setBtn(`Download & install ${latest}`, startUpdate);
          const released = st.published ? ` (released ${new Date(st.published).toLocaleDateString()})` : "";
          const whatsNew = releaseUrl ? el("a", { class: "linkish" }, "What's new") : null;
          uStatus.replaceChildren(
            "A newer version is available: ", el("b", {}, latest), released,
            ". The install replaces the app folder and reopens it. Your decklists, images, and settings stay put.",
            whatsNew ? " " : "", whatsNew,
          );
          if (whatsNew) {
            whatsNew.onclick = (e) => { e.preventDefault(); showWhatsNew(st.latest, releaseUrl); };
            // no href on purpose: this is a button styled as a link. A real
            // href (even "#") makes the browser navigate the whole app on a
            // mis-click, and the webview can't spawn the target="_blank" a
            // genuine one would want - the click does everything in-app.
          }
          break;
        }
        default:
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "";
      }
    };

    async function doCheck() {
      if (busy) return;
      busy = true;
      checkPending = true;
      uBtn.disabled = true;
      uStatus.textContent = "Asking GitHub for the newest release…";
      let r;
      try {
        // A button click is an explicit refresh, not the scheduled daily
        // check. Always bypass the persisted freshness cache.
        r = await checkUpdates(true);
      } catch (e) {
        uStatus.textContent = "The check did not get through. Try again in a moment.";
      } finally {
        busy = false;
        checkPending = false;
      }
      await render();
      // Keep the sidebar notice in step with what this card now shows.
      refreshUpdateNotice();
      if (r?.state?.status === "up-to-date") toast("ok", `No update. ${vv(r.state.latest)} is the newest.`);
      else if (r?.state?.status === "update-available") toast("ok", `Update available: ${vv(r.state.latest)}. Press the button above to install it.`);
      else if (r?.state?.status === "auth-required") toast("warn", "The release repo is private. This check will work once it is public.");
    }

    const startUpdate = async () => {
      let r;
      try { r = await startUpdateRequest(); }
      catch (error) { toast("err", error?.message || "The update could not start."); return; }
      if (!r.ok) { toast("warn", r.errors?.[0] || "The update could not start."); return; }
      uStatus.textContent = "Working. Progress appears in the strip at bottom left. The app closes and reopens as the new version when it is done.";
      const job = r.job || {};
      // The console (advanced mode) keeps its live transcript view; the strip
      // follows either way, so simple mode — where the console can't be
      // reached from this button — sees the same progress.
      if (uiMode() === "advanced") {
        // the console keeps its live transcript; the strip (below) follows
        // in both modes, so simple mode sees the same progress without a
        // console to host it
        if (job.id && S.activeJobId !== job.id) {
          S.activeJobId = job.id;
          renderConsoleTabs();
          attachStream(job.id, true);
        }
        toggleConsole();
      }
      startUpdateStrip(job.id);
    };

    uBtn.onclick = null;   // render() owns the button from here
    render();               // first paint; while the card is up it stays current on its own
    clearInterval(S.timers?.appUpdates);   // a re-render must never stack polls
    S.timers.appUpdates = setInterval(render, 5000);   // stay honest while the card is up
  } else {
    uc.append(el("div", { class: "small", style: "margin-top:10px" },
      `Version v${S.info.server.version}. Running from source. Self updates are available only in packaged builds.`));
  }
  wrap.append(uc);

  // data & about
  const ac = el("div", { class: "card" });
  ac.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, el("img", { src: "/logo.svg", alt: "SCM Workbench", style: "width:26px; height:26px; display:block" })),
    el("div", { class: "grow" }, el("h2", {}, "Data & about"), el("p", {}, `Workbench v${S.info.server.version} · server python ${S.info.server.python} · data dir ${S.info.server.data_dir}`))));
  ac.append(el("div", { style: "display:flex; gap:9px; flex-wrap:wrap" },
    el("button", { class: "btn", onclick: async () => {
      try {
        const r = await revealPath(S.info.server.data_dir);
        r.ok ? toast("ok", "Opening data folder…") : toast("warn", r.errors?.[0]);
      } catch (error) {
        toast("warn", error?.message || "Could not reveal data folder");
      }
    } }, ico("folder"), "Open data folder"),
    el("button", { class: "btn", onclick: () => openUrl("https://github.com/Alan-Cha/silhouette-card-maker", "silhouette-card-maker on GitHub") }, ico("external"), "silhouette-card-maker on GitHub"),
    el("button", { class: "btn", onclick: () => openUrl("https://github.com/Alan-Cha/scm-extras", "scm-extras on GitHub") }, ico("external"), "scm-extras on GitHub"),
    el("button", { class: "btn", onclick: () => openUrl("https://github.com/mallen86/scm-workbench", "scm-workbench on GitHub") }, ico("external"), "scm-workbench on GitHub"),
  ));
  wrap.append(ac);
  return wrap;
};
