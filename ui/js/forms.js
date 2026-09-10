/* forms — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, S, confirmModal, el, ico, toast } from "./core.js";import { jobs } from "./jobs.js";import { preview } from "./preview.js";import { repoReady } from "./prep.js";import { uiMode } from "./nav.js";
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


export function afterFormChange(kind, args) {
  // The S.forms slot can be wiped out from under a live form at any time
  // (refreshInfo on mode switches / repo updates / boot retries). The form
  // card's args object is the source of truth for what the user is editing -
  // if the slot no longer points at it, re-own it, or the next preview would
  // serialize a missing/stale form and the page would sit "not updating".
  if (args && S.forms[kind] !== args) S.forms[kind] = args;
  clearTimer(kind);
  S.timers[kind] = setTimeout(() => updatePreview(kind), 250);
}


export function clearTimer(kind) { if (S.timers[kind]) { clearTimeout(S.timers[kind]); S.timers[kind] = null; } }


export function updatePreview(kind) {
  const box = document.querySelector(`.cmdbox[data-kind="${kind}"]`);
  if (!box) return;
  // Requests are sequenced: a slow answer to an older form state must never
  // repaint the box over the newer one (this is how the preview could sit
  // "stale" for seconds while the user kept editing).
  const seq = (S.previewSeq = S.previewSeq || {})[kind] = ((S.previewSeq || {})[kind] || 0) + 1;
  const isCurrent = () => (S.previewSeq || {})[kind] === seq;
  // The box must never be able to sit frozen in a stale state: on a failed
  // round-trip (server mid-restart, a dropped connection) we retry briefly and
  // then say so, instead of swallowing the error and keeping whatever the box
  // showed before.
  const run = (attempt = 0) => {
    preview(kind, S.forms[kind])
      .then(d => {
        if (!isCurrent()) return;
        renderPreview(box, d);
      })
      .catch(err => {
        if (!isCurrent()) return;
        if (box.isConnected && attempt < 5) {
          if (attempt === 0) showPreviewPending(box, "Waiting for the server to answer…");
          setTimeout(() => run(attempt + 1), 2000);
        } else if (box.isConnected) {
          showPreviewPending(box, `Couldn't build the preview (${(err && err.message) || "error"}). It will refresh when you change a field.`);
        }
      });
  };
  run();
}


function showPreviewPending(box, msg) {
  box.innerHTML = "";
  box.append(el("div", { class: "cb-head" },
    el("span", { class: "t" }, ico("terminal"), "Command preview")));
  box.append(el("pre", { class: "dim" }, msg));
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
    box.append(el("pre", { class: "dim" }, "Incomplete"));
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
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind, args); });
      wrap.append(label, i);
      break;
    }
    case "textarea": {
      const t = el("textarea", { class: "input", rows: 5 });
      t.value = strVal(args[o.key]);
      t.addEventListener("input", () => { args[o.key] = t.value; afterFormChange(kind, args); });
      wrap.append(label, t);
      break;
    }
    case "number": {
      const i = el("input", { class: "input mono", type: "number", step: o.step || 1, value: strVal(args[o.key]) });
      const w = el("span", { class: "numwrap" }, i, numSteppers(i, o.step || 1));
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind, args); });
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
      i.addEventListener("input", () => { sync(i.value); afterFormChange(kind, args); });
      val.addEventListener("change", () => {
        if (val.value !== "") {
          const n = Number(val.value);
          if (Number.isFinite(n) && n >= 0) { sync(n); afterFormChange(kind, args); return; }
        }
        sync(args[o.key]); // blank / invalid → back to last good value
        afterFormChange(kind, args);
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
      sel.addEventListener("change", () => { args[o.key] = sel.value; afterFormChange(kind, args); });
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
            afterFormChange(kind, args);
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
      $("input", sw).addEventListener("change", e => { args[o.key] = e.target.checked; afterFormChange(kind, args); o.onChange && o.onChange(e.target.checked); });
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
            el("button", { type: "button", onclick: e => { (args[o.key] = args[o.key].filter(x => x !== c)).length; e.stopPropagation(); redraw(); afterFormChange(kind, args); } }, "×")));
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
          afterFormChange(kind, args);
        } else if (e.key === "Backspace" && !inp.value && (args[o.key] || []).length) {
          args[o.key].pop(); redraw(); afterFormChange(kind, args);
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
            afterFormChange(kind, args);
          },
        }, lab));
      }
      wrap.append(label, box);
      break;
    }
    default: {
      const i = el("input", { class: "input", value: strVal(args[o.key]) });
      i.addEventListener("input", () => { args[o.key] = i.value; afterFormChange(kind, args); });
      wrap.append(label, i);
    }
  }
  if (o.type === "path" && (o.key === "output_path" || o.key === "output_pdf_path")) {
    const row = repoRowForKind(kind);
    if (row && row.mode === "managed") wrap.append(el("span", { class: "help" },
      "Kept in the app's working area. When the run finishes, use the console's “Move to my files…” to save it elsewhere."));
  }
  if (o.help) wrap.append(el("span", { class: "help" }, o.help));
  return wrap;
}


export function strVal(v) { return v === null || v === undefined ? "" : String(v); }


export function stepNum(input, d) {
  const v = parseFloat(input.value);
  // round to the step's decimal places, or repeated 0.1 steps leave 0.30000000000000004
  const frac = Math.abs(d) < 1 ? String(Math.abs(d)).split(".")[1] || "" : "";
  input.value = +((isNaN(v) ? 0 : v) + d).toFixed(frac.length);
  input.dispatchEvent(new Event("input"));
}


/* the clean −/+ stepper pair, as a span to place inside a .numwrap next to a
   number input (the form's number options use it; so do the hand-rolled
   X/Y/Angle boxes on the offset cards and the Settings port box) */
export function numSteppers(input, step = 1) {
  return el("span", { class: "steppers" },
    el("button", { type: "button", onclick: () => stepNum(input, -step) }, "−"),
    el("button", { type: "button", onclick: () => stepNum(input, step) }, "+"),
  );
}


/* generic form card for a manifest kind */

export function formCard(kind, opts = {}) {
  const spec = S.manifest[kind];
  if (!spec) return el("div", { class: "empty" }, "unknown job kind");
  const args = formArgs(kind);

  // If a saved choice no longer exists in the option's current choices list
  // (the repo was re-synced and the decklist/format lists changed, or the user
  // is on an older manifest than their saved form), reset it to the default.
  // One stale value in the form state otherwise poisons every preview after
  // it — the server silently re-defaults it and the box just reads
  // “— incomplete —” no matter which other option you then touch.
  for (const g of spec.groups || []) {
    for (const o of g.options || []) {
      if (!o.choices) continue;
      const vals = o.choices.map(c => String(c[0]));
      const cur = args[o.key];
      if (o.type === "chips" || o.type === "choice_chips") {
        if (Array.isArray(cur)) {
          const kept = cur.filter(v => vals.includes(String(v)));
          if (kept.length !== cur.length) args[o.key] = kept;
        }
      } else if (cur !== undefined && cur !== null && String(cur).trim() !== "") {
        if (!vals.includes(String(cur).trim())) {
          args[o.key] = o.default !== undefined ? o.default : "";
          afterFormChange(kind, args);
        }
      }
    }
  }

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

  const appendCollapsibleGroup = g => {
    const os = (g.options || []).filter(o => !o.simple_only);
    if (!os.some(o => o.show ? o.show(args) : true)) return;
    const adv = el("div", { class: "adv" });
    adv.append(
      el("button", { class: "adv-head", type: "button", onclick: () => adv.classList.toggle("open") },
        el("span", { class: "arr" }, ico("arrow")), g.title),
      el("div", { class: "adv-body" }, groupInner(os, kind, args)),
    );
    card.append(adv);
  };

  if (opts.flat) {
    // Flat layout: no ordinary group headers or card title of its own. In
    // simple mode only the options flagged `simple` in the manifest make it
    // in (the everyday ones); everything else keeps its
    // default behind the scenes. Each group becomes one form row; a
    // kind-level `simple_rows` list (create_pdf, fetch:*) names which
    // groups hold which option rows, in order. A group that names its own
    // `simple_rows` (MTG's preferences group does: 7 toggles = 3+3+1
    // per row) renders them as sub-rows inside the group's row. The fetch
    // form is fully flat — every group, every option — unlike create_pdf,
    // whose flat section shows only its `simple`-flagged options.
    const rows = uiMode() === "simple" ? (spec.simple_rows || []) : [];
    // Render one group's options into one flat row. A group that names its
    // own `simple_rows` (MTG's preferences group) gets a sub-frow for each
    // of those rows — 3-per-row, the standard rhythm — inside its row.
    const placeGroup = (g, keys) => {
      const row = el("div", { class: "frow" });
      // simple-mode visibility: create_pdf's flat section shows only its
      // `simple`-flagged options; the fetch form is fully flat, so every
      // option is visible there.
      const visible = kind === "create_pdf" ? o => optVisible(o, spec) : () => true;
      const draw = (o, k, target) => {
        const node = renderOption(o, args, kind);
        if (node) target.append(node);
      };
      const fill = r => {
        // flat rows split evenly, whatever the manifest widths say: the four-
        // toggle row needs 25% apiece to fit, and a two-dropdown row reads
        // better at half width than two one-thirds with dead space at the end
        const n = r.childElementCount;
        if (n > 1) {
          for (const f of r.children) {
            f.classList.remove("w-half", "w-third", "w-quarter", "w-full");
            f.style.flex = `1 1 calc(${100 / n}% - ${14 * (n - 1) / n}px)`;
          }
        }
      };
      if (g.simple_rows?.length) {
        for (const rkeys of g.simple_rows) {
          const sub = el("div", { class: "frow" });
          for (const k of rkeys) {
            const o = (g.options || []).find(x => x.key === k);
            if (o && visible(o)) draw(o, k, sub);
          }
          fill(sub);
          if (sub.childElementCount) row.append(sub);
        }
      } else {
        for (const k of keys) {
          const o = (g.options || []).find(x => x.key === k);
          if (o && visible(o)) draw(o, k, row);
        }
        fill(row);
      }
      if (!row.childElementCount) return;
      card.append(row);
    };
    if (kind === "create_pdf" && rows.length) {
      // PDF's everyday options span several advanced groups, so its
      // kind-level row plan deliberately flattens across those groups. This
      // also gives simple-only controls (such as MPCFill Crop) their one
      // intended home without exposing the advanced collapsible sections.
      const simpleGroup = { options: (spec.groups || []).flatMap(g => g.options || []) };
      for (const keys of rows) placeGroup(simpleGroup, keys);
    } else {
      // one frow per ordinary fetch group, in manifest order. Keep explicitly
      // collapsible preference groups collapsed in simple mode too: simplifying
      // the main form must not promote infrequently-used settings into it.
      for (const g of spec.groups || []) {
        if (g.collapsible) appendCollapsibleGroup(g);
        else placeGroup(g, g.options?.map(o => o.key) || []);
      }
    }
  } else {
    for (const g of spec.groups || []) {
      const os = (g.options || []).filter(o => !o.simple_only);   // simple-only options never appear in the advanced form
      if (g.collapsible) appendCollapsibleGroup(g);
      else card.append(el("div", { class: "section-label", "data-label": true }, g.title), groupInner(os, kind, args));
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
    const note = el("span", { class: "rb-note" }, "Runs in the background. Watch the job console below.");
    card.append(el("div", { class: "runbar" }, note, runBtn));
    const missing = (S.manifest[kind] ? S.manifest[kind].needs || [] : []).filter(k => !repoReady(k));
    if (missing.length) {
      runBtn.disabled = true;
      runBtn.classList.add("wait");
      runBtn.innerHTML = "";
      runBtn.append(ico("refresh"), "Waiting for " + missing.join(" + ") + "…");
      card.append(el("div", { class: "prep-note" },
        "This page needs “" + missing.join("”, “") + "”. The button unlocks when preparation finishes."));
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
          ? "silhouette-card-maker is still being prepared. The button unlocks when it is done."
          : "SCM repo not found. Open Settings and choose your silhouette-card-maker folder.");
      }
      if (need === "extras" && !S.info.extras.found) {
        return toast("err", (S.info.server.is_packaged && !repoReady("extras"))
          ? "scm-extras is still being prepared. The button unlocks when it is done."
          : "scm-extras repo not found. Open Settings and choose your scm-extras folder.");
      }
    }
  }
  if (opts.confirm) {
    const ok = await confirmModal(opts.confirm);
    if (!ok) return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = ""; btn.append(el("span", { class: "spinner" }), " Starting…"); }
  let startFailed = false;
  const runArgs = opts.args !== undefined ? opts.args : S.forms[kind];
  try {
    let j;
    try {
      j = await jobs.start(kind, runArgs);
    } catch (error) {
      startFailed = true;
      toast("err", error?.message || "Failed to start job");
      return null;
    }
    if (!j.ok) {
      toast("err", j.errors?.join("; ") || "Failed to start job");
    } else {
      const warnings = j.job?.warnings || j.warnings || [];
      for (const w of warnings) toast("warn", w, 5200);
      toast("ok", `${j.job.title}. Job started.`);
      // register the job locally right away: the console tab (advanced mode)
      // and the page's status strip (simple mode) must not wait for the next
      // background list refresh to learn something is running.
      const j0 = { id: j.job.id, ts: Date.now() / 1000, kind, title: j.job.title,
                   status: "running", exit_code: null, cmd: j.job.cmd,
                   warnings, outputs: [] };
      // Keep the latest run and the exact submitted settings across page
      // navigation. The PDF completion actions and progress total must describe
      // the job that ran, not whatever happens to be in the live form later.
      S.startedJobIds[kind] = j0.id;
      S.jobArgs[j0.id] = JSON.parse(JSON.stringify(runArgs || {}));
      const i = (S.jobs || []).findIndex(x => x.id === j0.id);
      if (i >= 0) S.jobs[i] = { ...S.jobs[i], ...j0 };
      else S.jobs = [j0, ...(S.jobs || [])];
      const { refreshJobs, openConsole } = await import("./console.js");
      refreshJobs();
      if (uiMode() !== "simple") openConsole(j.job.id);   // in simple mode the page's status strip takes over
      if (kind === "calibration" || kind === "dxf_batch" || kind === "dxf_single" || kind === "extras_generate" || kind === "clean_up" || kind === "repo_update" || kind === "repo_init") {
        // clean_up changes image inventory, not form choices. Preserve the live
        // fetch form object so its completion preview cannot serialize
        // `undefined` and remain stuck at “Waiting for the server”.
        const refreshOptions = kind === "clean_up" ? { keepForms: true } : {};
        setTimeout(() => import("./info.js").then(({ refreshInfo }) => refreshInfo(refreshOptions)), 2500);
      }
      if (kind.startsWith("fetch:")) {
        // keep the user's form state (pasted decklists etc.) alive
        setTimeout(() => import("./info.js").then(({ refreshInfo }) => refreshInfo({ keepForms: true })), 2500);
      }
      return j.job;
    }
    return null;
  } finally {
    // don't silently re-enable a button the latest preview has since blocked
    // (e.g. the front directory is still empty)
    if (btn && (startFailed || !S.previewBlock?.[kind])) { btn.disabled = false; btn.innerHTML = ""; btn.append(ico("play"), btn.dataset.label || "Run"); }
  }
}
