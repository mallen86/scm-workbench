/* pages/pdf — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, confirmModal, el, ico, pageHead, toast } from "../core.js";
import { afterFormChange, defaultArgs, formCard } from "../forms.js";
import { go, uiMode } from "../nav.js";
import { connectCardNeeded, repoSetupCard } from "./dashboard.js";
import { paperForCreatePdf } from "./offset.js";
import { jobStrip } from "../jobstrip.js";

/* ================================ pdf page ================================ */

PAGES.pdf = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Create PDF", "Lays out the images in your game/ folders into a print-ready PDF with registration marks. Every option from create_pdf.py is available below — the command preview shows exactly what will run."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  // Simple mode: the form is one flat section — no group headers, no
  // collapsible wrappers, and no card title of its own (the page head
  // above carries the name). Advanced mode keeps the full grouped layout.
  const simple = uiMode() === "simple";
  wrap.append(formCard("create_pdf", { icon: "pdf", flat: simple, head: !simple }));
  {  // offset banner — per-size row wins over the global value for this form's paper
    const form = S.forms.create_pdf || (S.forms.create_pdf = defaultArgs("create_pdf"));
    const paper = paperForCreatePdf(form);
    const row = (S.info.per_size_offsets || {})[paper];
    const g = S.info.scm.saved_offset;
    // advanced only — in simple mode this sits below the (flat) form the
    // toggle lives in, so the note would point “below” at nothing
    if (uiMode() === "advanced" && (row || g)) {
      const o = row || g;
      const txt = row
        ? `Per-size offset for “${paper}” is saved: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b> — applied automatically whenever “Apply saved offset” is on.`
        : `Saved printer offset is available: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. Enable “Apply saved offset” above when ready.`;
      wrap.append(el("div", { class: "banner ok", style: "margin-top:16px" }, el("span", { class: "b-ico" }, ico("check")),
        el("span", { class: "grow", html: txt }),
        // the Offset & calibration page is hidden in simple mode — no link to it
        ...(uiMode() === "advanced" ? [el("button", { class: "linkish", onclick: () => go("offset") }, "manage offset →")] : [])));
    }
  }
  // simple mode: the console is hidden, so the page shows its own compact
  // status for the job — a progress bar while it runs, then a button that
  // opens the finished PDF in the system's default viewer.
  wrap.append(jobStrip("create_pdf", {
    icon: "pdf",
    runningLabel: "Creating your PDF",
    onOk: (done, body) => {
      const out = (done.outputs || [])[0];
      body.append(el("div", { class: "js-msg ok" },
        ico("check"), el("span", {}, "Your PDF is ready.")));
      if (!out) return;
      body.append(el("div", { class: "js-actions" },
        el("button", { class: "btn primary", title: "Opens in your default PDF app",
          onclick: async (e) => {
            const b = e.currentTarget;
            b.disabled = true;
            // api() already parses the JSON body (and throws on real server
            // errors) - calling .json() on its result is what made every click
            // look like a failure even while “open” was doing its job.
            try {
              const r = await api(`/api/file?path=${encodeURIComponent(out)}&open=1`);
              if (r?.ok) toast("ok", "Opening the PDF — big files can take a moment to appear in the viewer.", 6000);
              else toast("warn", r?.errors?.[0] || "Couldn't open the PDF.");
            } catch (err) {
              toast("warn", err?.message || "Couldn't open the PDF.");
            } finally {
              b.disabled = false;
            }
          } }, ico("file"), "Open PDF")));
    },
  }));
  // both must run once the card is in the document
  wrap.__patch = () => { patchPdfForm("create_pdf"); patchOffsetToggle("create_pdf"); };
  return wrap;
};

/* Create-PDF behavior: guard the “Front pages only” toggle against images left
   in the double-sided folder. create_pdf.py refuses to run with --only_fronts
   while any double-sided image exists, so when the toggle is flipped on we
   warn that the option won't work and offer to remove the images in one click. */


export function patchPdfForm(kind) {
  const card = $(`.form-card[data-kind="${kind}"]`);
  const field = card && $$(".field", card).find(f => f.dataset.key === "only_fronts");
  const input = field && $("input[type=checkbox]", field);
  if (!input) return;
  let scanning = false;

  input.addEventListener("change", async () => {
    if (scanning) return;
    const dir = ((S.forms[kind] || {}).double_sided_dir || "").trim();
    if (!dir || !input.checked) return;
    scanning = true;
    let items = null;
    try {
      const r = await fetch(`/api/file?path=${encodeURIComponent(dir)}&images_only=1`);
      if (r.status === 404) items = [];                 // folder missing → nothing to remove
      else if (r.status === 200) items = (await r.json().catch(() => ({}))).items || [];
      else toast("warn", `Couldn’t check “${dir}” (HTTP ${r.status}).`);
    } catch { items = []; }
    scanning = false;
    if (!items || !items.length) return;

    const n = items.length;
    const ok = await confirmModal({
      title: "Images in the double-sided folder",
      text: `“${dir}” contains ${n} image${n === 1 ? "" : "s"}. While any are there, “Front pages only” (--only_fronts) can’t work — create_pdf.py refuses to run.`,
      paras: [`Remove them from the folder now? This can’t be undone.`],
      list: items.map(i => i.name),
      okLabel: "Yes, remove them",
      danger: true,
      icon: "trash",
      iconCls: "warn",
    });
    if (!ok) {
      toast("warn", `“Front pages only” stays on, but the job will fail while “${dir}” still has images — remove them, or uncheck the option.`, 7000);
      return;
    }
    let j = {};
    try {
      const r = await fetch("/api/fs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ op: "delete_images", path: dir }) });
      j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        toast("err", (j.errors || ["Could not remove the images."]).join("; "), 7000);
        return;
      }
    } catch {
      toast("err", "Could not remove the images.", 7000);
      return;
    }
    toast("ok", `Removed ${j.deleted} image${j.deleted === 1 ? "" : "s"} from “${dir}” — “Front pages only” will work now.`, 6000);
    afterFormChange(kind);   // refresh the preview so the warning clears
  });
}

/* “Apply saved offset” only makes sense when something is saved for the paper
   this form prints — a per-size row for that paper, or the global value.
   With neither, the switch is disabled and its help line points to the
   Offset & calibration page; if the paper changes to one with no row the
   switch is turned back off, so a run can never ask for an offset that
   doesn't exist. Works in both the flat (simple) and grouped (advanced)
   form. */
export function patchOffsetToggle(kind) {
  const card = $(`.form-card[data-kind="${kind}"]`);
  const field = card && $$(".field", card).find(f => f.dataset.key === "load_offset");
  const input = field && $("input[type=checkbox]", field);
  if (!input) return;

  const refresh = () => {
    const a = S.forms[kind] || (S.forms[kind] = defaultArgs(kind));
    const paper = paperForCreatePdf(a);
    const row = (S.info.per_size_offsets || {})[paper];
    const g = S.info.scm?.saved_offset;
    let note = field.querySelector(":scope > .help");
    if (!note) { note = el("span", { class: "help" }); field.append(note); }
    if (!row && !g) {
      if (input.checked) {
        a.load_offset = false;
        input.checked = false;
        afterFormChange(kind);   // the preview follows the switch
      }
      input.disabled = true;
      note.textContent = `No offset is saved for “${paper}” yet — record one on the Offset & calibration page and this switch enables itself.`;
    } else {
      input.disabled = false;
      note.textContent = row
        ? `Applies the “${paper}” row (x ${row.x}, y ${row.y}, angle ${row.angle}°) recorded on the Offset & calibration page.`
        : `No per-size row for “${paper}” — the saved global offset (x ${g.x}, y ${g.y}, angle ${g.angle}°) will be applied.`;
    }
  };
  refresh();
  card.addEventListener("change", refresh);
}
