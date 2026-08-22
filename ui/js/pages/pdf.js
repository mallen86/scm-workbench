/* pages/pdf — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, confirmModal, el, ico, pageHead, toast } from "../core.js";
import { afterFormChange, defaultArgs, formCard } from "../forms.js";
import { go, uiMode } from "../nav.js";
import { connectCardNeeded, repoSetupCard } from "./dashboard.js";
import { paperForCreatePdf } from "./offset.js";

/* ================================ pdf page ================================ */

PAGES.pdf = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Create PDF", "Lays out the images in your game/ folders into a print-ready PDF with registration marks. Every option from create_pdf.py is available below — the command preview shows exactly what will run."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  wrap.append(formCard("create_pdf", { icon: "pdf" }));
  {  // offset banner — per-size row wins over the global value for this form's paper
    const form = S.forms.create_pdf || (S.forms.create_pdf = defaultArgs("create_pdf"));
    const paper = paperForCreatePdf(form);
    const row = (S.info.per_size_offsets || {})[paper];
    const g = S.info.scm.saved_offset;
    if (row || g) {
      const o = row || g;
      const txt = row
        ? `Per-size offset for “${paper}” is saved: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b> — applied automatically whenever “Apply saved offset” is on.`
        : `Saved printer offset is available: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. Enable “Apply saved offset” below when ready.`;
      wrap.append(el("div", { class: "banner ok", style: "margin-top:16px" }, el("span", { class: "b-ico" }, ico("check")),
        el("span", { class: "grow", html: txt }),
        // the Offset & calibration page is hidden in simple mode — no link to it
        ...(uiMode() === "advanced" ? [el("button", { class: "linkish", onclick: () => go("offset") }, "manage offset →")] : [])));
    }
  }
  wrap.__patch = () => patchPdfForm("create_pdf");  // must run once the card is in the document
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
