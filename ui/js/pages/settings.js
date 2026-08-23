/* pages/settings — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { PAGES, S, api, el, ico, pageHead, toast, esc } from "../core.js";
import { doRun, numSteppers } from "../forms.js";
import { refreshInfo } from "../info.js";
import { go, setTheme, uiMode } from "../nav.js";
import { watchJobDone } from "./utilities.js";

export function repoCopyRow(row, container, simple = false) {
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
    // dim that segment instead of letting the save fail with a message. The
    // whole selector is advanced-mode only: in simple mode the row shows the
    // deployed version plus Check/Update, and the saved source keeps working
    // behind the scenes.
    if (!simple) {
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
        text: simple
          ? "Keeps a second, Workbench-managed copy in the data folder (your own clone stays untouched); it will follow the latest main."
          : "Keeps a second, Workbench-managed copy in the data folder (your own clone stays untouched). Pick the source above first if you want it to track something other than the latest main.",
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
  const simple = uiMode() === "simple";
  wrap.append(pageHead("Settings", simple
    ? "Everything here is stored in this project's data folder, out of the way of your repos."
    : "Everything here is stored in this project's data/settings.json. Repo paths can also be left blank — the Workbench auto-detects sister folders named silhouette-card-maker and scm-extras."));

  const s = S.info.settings;

  // (the Simple / Advanced switch lives in the top bar on every page —
  //  #mode-switch — not as a settings card)
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
  if (!simple) wrap.append(rc);

  // managed repo copies (download/update the sister repos from inside the Workbench)
  const mc = el("div", { class: "card" });
  mc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("refresh")),
    el("div", { class: "grow" }, el("h2", {}, "Managed repo copies"), el("p", {}, simple
      ? "The Workbench keeps its own copy of each repo in its data folder — check for newer versions and install them from here. Your images, decklists and local edits always survive an update."
      : "The Workbench can keep its own copy of each repo in its data folder — fetch the newest version on demand and pick exactly what to track (main, the latest release, or a pinned tag). Your images, decklists and local edits always survive an update."))));
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
        ? "This app runs on its own private Python (kept in the app's data folder). Job dependencies are installed into it automatically — your system Python is never touched."
        : "Scripts run with the interpreter chosen here. Default: the one that started the Workbench. Install the base repo's requirements.txt into it: pip install -r requirements.txt"))));
  const pyI = el("input", { class: "input mono", value: s.python || (packaged && S.info.server.python ? `python ${S.info.server.python}  (private runtime)` : ""), placeholder: S.info.server.python_path + "  (default)", title: S.info.server.python_path || "", readonly: packaged || null });
  const portI = el("input", { class: "input mono", type: "number", value: s.port || 8037, min: 1024, max: 65535 });
  const portW = el("span", { class: "numwrap" }, portI, numSteppers(portI, 1));
  pc.append(el("div", { class: "frow" },
    el("div", { class: packaged ? "field w-half" : "field w-half" }, el("label", {}, packaged ? "Private Python" : "Python interpreter"), pyI),
    ...(packaged ? [] : [el("div", { class: "field w-quarter" }, el("label", {}, "Port"), portW)]),
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
  }
  if (!simple) wrap.append(pc);

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

  // app updates (the packaged app checks GitHub for itself, at start and daily)
  const uc = el("div", { class: "card" });
  uc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("arrow")),
    el("div", { class: "grow" }, el("h2", {}, "App updates"),
      el("p", {}, packaged
        ? "Checks the newest release of this app on GitHub (at start-up, and once a day while it's open). Installing a new version swaps the app folder only — your data folder is never touched."
        : "Running from a source checkout — there's nothing to self-update here; pull the Workbench repo itself instead."))));
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
    const vv = t => "v" + String(t || "").replace(/^v/, "");   // display form of a tag (v0.2.0 → v0.2.0, 0.2.0 → v0.2.0)

    const render = async () => {
      let r;
      try { r = await api("/api/updates"); } catch { return; }
      const st = r.state || {};
      uLast.textContent = "Last checked: " + humanize(st.checked_at);
      if (st.checking) {   // a check is in flight (the daily daemon or another click)
        setBtn("Checking…", null, true);
        uStatus.textContent = "Asking GitHub for the newest release…";
        return;
      }
      const stale = st.checked_at != null && (Date.now() / 1000 - st.checked_at) > 86400;
      const setBtn = (label, onClick, disabled = false, title = "") => {
        uBtn.textContent = "";
        uBtn.append(ico(disabled ? "clock" : "arrow"), el("span", {}, " " + label));
        uBtn.disabled = disabled;
        uBtn.onclick = disabled ? null : onClick;
        uBtn.title = title;
      };
      switch (st.status) {
        case "never":
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "Not checked yet — the first automatic check runs at start-up, and once a day while the app is open.";
          break;
        case "auth-required":
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "The release repo is still private, so this check can't see its releases. Once it's made public, this works with no setup at all — press the button again any time to check.";
          break;
        case "error":
          setBtn("Check again", doCheck);
          uStatus.textContent = "The last check failed: " + (st.reason || "unknown error");
          break;
        case "up-to-date":
          if (!stale) {
            setBtn("Up to date", null, true, "Rechecked at start-up and once a day while the app is open");
            uStatus.innerHTML = `You're on the latest version — <b>${vv(st.latest || r.current)}</b> is the newest release.`;
          } else {
            setBtn("Check for updates", doCheck);
            uStatus.innerHTML = `Last checked ${humanize(st.checked_at)} — you were on the latest (${vv(st.latest || r.current)}). The automatic recheck is due; press to check now.`;
          }
          break;
        case "update-available":
          setBtn(`Download & install ${vv(st.latest)}`, startUpdate);
          uStatus.innerHTML = `A newer version is out — <b>${vv(st.latest)}</b>` +
            (st.published ? ` (released ${esc(new Date(st.published).toLocaleDateString())})` : "") +
            ". The install replaces the app folder and reopens it; your decklists, images and settings stay put." +
            (st.release_url ? ` <a href="${esc(st.release_url)}" target="_blank" rel="noopener">What's new</a>` : "");
          break;
        default:
          setBtn("Check for updates", doCheck);
          uStatus.textContent = "";
      }
    };

    async function doCheck() {
      if (busy) return;
      busy = true;
      const st0 = (await api("/api/updates").catch(() => null))?.state || {};
      const fresh = st0.checked_at != null && (Date.now() / 1000 - st0.checked_at) < 86400
        && ["up-to-date", "update-available"].includes(st0.status);
      if (!fresh) {
        uStatus.textContent = "Asking GitHub for the newest release…";
        uBtn.disabled = true;
      }
      let r;
      try {
        r = await api("/api/updates/check", { force: !fresh });
      } catch (e) {
        uStatus.textContent = "The check didn't get through — try again in a moment.";
      } finally {
        busy = false;
      }
      await render();
      if (r?.state?.status === "up-to-date") toast("ok", `No update — ${vv(r.state.latest)} is the newest.`);
      else if (r?.state?.status === "update-available") toast("ok", `Update available: ${vv(r.state.latest)} — press the button above to install it.`);
      else if (r?.state?.status === "auth-required") toast("warn", "The release repo is still private — this check will work once it's made public.");
    }

    const startUpdate = async () => {
      const r = await api("/api/updates/start", {});
      if (!r.ok) { toast("warn", r.errors?.[0] || "The update could not start."); return; }
      toast("ok", "Update started — the app will close itself and reopen as the new version.");
      uStatus.textContent = "Working: downloading, installing, then reopening the new version. Watch it live in the console (it lands there automatically).";
      go("console");
    };

    uBtn.onclick = null;   // render() owns the button from here
    render();               // first paint; while the card is up it stays current on its own
    clearInterval(S.timers?.appUpdates);   // a re-render must never stack polls
    S.timers.appUpdates = setInterval(render, 5000);   // stay honest while the card is up
  } else {
    uc.append(el("div", { class: "small", style: "margin-top:10px" },
      `Version v${S.info.server.version}. In a packaged app this card would check GitHub for itself and offer to install the newest release in place.`));
  }
  wrap.append(uc);

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
