/* pages/pdf — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, confirmModal, el, ico, pageHead, toast } from "../core.js";import { openFile, revealPath } from "../native-actions.js";import { deleteImages } from "../fs-transport.js";import { canImportBackImage, importBackImage } from "../back-image-transport.js";import { afterFormChange, defaultArgs, formCard } from "../forms.js";import { go, uiMode } from "../nav.js";import { connectCardNeeded, repoSetupCard } from "../repo-setup.js";import { paperForCreatePdf } from "./offset.js";import { jobStrip } from "../jobstrip.js";import { listFiles, resolveTemplate } from "../artifacts.js";

/* ================================ pdf page ================================ */

PAGES.pdf = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Create PDF", "Lays out images from game/ folders in a PDF that is ready to print, with registration marks. The options below match create_pdf.py, and the preview shows the command that will run."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  // Simple mode: the form is one flat section — no group headers, no
  // collapsible wrappers, and no card title of its own (the page head
  // above carries the name). Advanced mode keeps the full grouped layout.
  const simple = uiMode() === "simple";
  wrap.append(formCard("create_pdf", { icon: "pdf", flat: simple, head: !simple }));
  wrap.append(backImageCard());
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
        ? `A paper specific offset for “${paper}” is saved: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. It is applied when “Apply saved offset” is on.`
        : `Saved printer offset: x <b>${o.x}</b>, y <b>${o.y}</b>, angle <b>${o.angle}°</b>. Enable “Apply saved offset” to use it.`;
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
    progressTotal: async job => {
      const f = S.jobArgs?.[job.id] || S.forms.create_pdf || {};
      const listing = await listFiles(f.front_dir || "game/front", true);
      // A truncated bounded listing cannot provide an honest denominator.
      return listing.truncated ? 0 : (listing.found ?? listing.items.length);
    },
    onOk: (done, body) => {
      const out = (done.outputs || [])[0];
      body.append(el("div", { class: "js-msg ok" },
        ico("check"), el("span", {}, "Your PDF is ready.")));
      // one shared action row: the PDF button and the cutting-template button
      // sit side by side (the template resolves async and joins the same row)
      let actions = null;
      const ensureActions = () => {
        if (!actions) { actions = el("div", { class: "js-actions" }); body.append(actions); }
        return actions;
      };
      if (out) {
        ensureActions().append(el("button", { class: "btn primary", title: "Open in your default PDF app",
          onclick: async (e) => {
            const b = e.currentTarget;
            b.disabled = true;
            // api() already parses the JSON body (and throws on real server
            // errors) - calling .json() on its result is what made every click
            // look like a failure even while “open” was doing its job.
            try {
              const r = await openFile(out);
              if (r?.ok) toast("ok", "Opening the PDF. Large files may take a moment to appear.", 6000);
              else toast("warn", r?.errors?.[0] || "Couldn't open the PDF.");
            } catch (err) {
              toast("warn", err?.message || "Couldn't open the PDF.");
            } finally {
              b.disabled = false;
            }
          } }, ico("file"), "Open PDF"));
      }
      // the cutting template that matches this exact PDF (paper + card size,
      // borderless family when the form is borderless) - resolved server-side
      // so the answer is the same in both modes
      const f = S.jobArgs?.[done.id] || S.forms.create_pdf || {};
      if (f.card_size && f.paper_size) {
        resolveTemplate(f.paper_size, f.card_size, !!f.borderless)
          .then(t => {
            if (!body.isConnected) return;
            if (t?.ok) {
              ensureActions().append(el("button", { class: "btn", title: `Open ${t.name} from ${t.repo} in its default app`,
                onclick: async (e) => {
                  const b = e.currentTarget;
                  b.disabled = true;
                  try {
                    const r = await openFile(t.path);
                    if (r?.ok) toast("ok", `Opening ${t.name}. It should appear in your cutting app shortly.`);
                    else toast("warn", r?.errors?.[0] || "Couldn't open the cutting template.");
                  } catch (err) {
                    toast("warn", err?.message || "Couldn't open the cutting template.");
                  } finally {
                    b.disabled = false;
                  }
                } }, ico("scissors"), "Open cutting template"));
            } else {
              body.append(el("div", { class: "js-hint" }, t?.errors?.[0] || "No matching cutting template was found."));
            }
          })
          .catch(err => {
            if (body.isConnected) body.append(el("div", { class: "js-hint" }, err?.message || "Couldn’t check for a matching cutting template."));
          });
      }
    },
  }));
  // both must run once the card is in the document
  wrap.__patch = () => { patchPdfForm("create_pdf"); patchOffsetToggle("create_pdf"); };
  return wrap;
};

function backImageCard() {
  const card = el("div", { class: "card back-image-card" });
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("image")),
    el("div", { class: "grow" }, el("h2", {}, "Card back image"),
      el("p", {}, "Import one image for game/back. Existing recognized back images are replaced; placeholders and other files stay."))));
  const status = el("p", { class: "small" });
  const refreshStatus = () => {
    const images = S.info?.scm?.back_images || [];
    status.textContent = images.length === 0
      ? "No recognized card back image is installed."
      : images.length === 1
        ? `Current card back: ${images[0].name}`
        : `There are ${images.length} recognized card back images. Import one to keep exactly one.`;
    status.className = `small ${images.length > 1 ? "warn" : ""}`;
  };
  refreshStatus();
  const row = el("div", { class: "runbar" });
  const native = canImportBackImage();
  let pathInput = null;
  if (!native) {
    pathInput = el("input", { class: "input grow", type: "text", placeholder: "Path to an image file", "aria-label": "Image path" });
    row.append(pathInput);
  }
  const browse = el("button", { class: "btn primary", type: "button", title: native ? "Choose a card back image" : "Import the image at the path above", onclick: async () => {
    browse.disabled = true;
    try {
      const existing = S.info?.scm?.back_images || [];
      if (existing.length) {
        const replace = await confirmModal({
          title: "Replace the card back image?",
          text: `Choosing a new image replaces ${existing.length === 1 ? `“${existing[0].name}”` : `the ${existing.length} recognized images currently in game/back`}. Placeholders and non-image files stay.`,
          okLabel: "Choose replacement",
          icon: "image",
        });
        if (!replace) return;
      }
      const result = await importBackImage(native ? null : pathInput.value.trim());
      if (result === null) return;
      if (!result?.ok) {
        toast("err", result?.errors?.[0] || "Importing the card back failed.");
        return;
      }
      S.info.scm.back_images = result.back_images || [];
      refreshStatus();
      afterFormChange("create_pdf", S.forms.create_pdf);
      toast("ok", `Imported “${result.name}”. Exactly one recognized card back is now installed.`);
    } catch (error) {
      toast("warn", error?.message || "The card back image could not be imported.");
    } finally {
      browse.disabled = false;
    }
  } }, ico("folder"), native ? "Choose image" : "Import image");
  row.append(browse);
  const reveal = el("button", { class: "btn btn-ghost", type: "button", title: "Reveal game/back in the file manager", onclick: async () => {
    try {
      const result = await revealPath("game/back");
      if (!result?.ok) toast("warn", result?.errors?.[0] || "Could not reveal the card back folder.");
    } catch (error) {
      toast("warn", error?.message || "Could not reveal the card back folder.");
    }
  } }, ico("folder"), "Reveal folder");
  row.append(reveal);
  card.append(status, row);
  return card;
}

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
    try {
      const listing = await listFiles(dir, true);
      if (listing.truncated) {
        // Never offer a destructive action when the bounded listing may have
        // omitted images. Leave the switch on so its current state remains
        // honest, and explain why the warning cannot be cleared here.
        toast("warn", `Could not safely check “${dir}”. The image list was truncated. “Front pages only” remains on. Remove images manually or uncheck it before running.`, 7000);
        return;
      }
      const items = listing.items;
      if (!items.length) return;

      const n = items.length;
      const ok = await confirmModal({
        title: "Images in the double-sided folder",
        text: `“${dir}” contains ${n} image${n === 1 ? "" : "s"}. While images remain, “Front pages only” (--only_fronts) cannot work. create_pdf.py will refuse to run.`,
        paras: [`Remove them from this folder now? This cannot be undone.`],
        list: items.map(i => i.name),
        okLabel: "Yes, remove them",
        danger: true,
        icon: "trash",
        iconCls: "warn",
      });
      if (!ok) {
        toast("warn", `“Front pages only” remains on, but the job will fail while “${dir}” contains images. Remove them or uncheck the option.`, 7000);
        return;
      }
      let j = {};
      try {
        j = await deleteImages(dir);
        if (!j?.ok) {
          toast("err", (j?.errors || ["Could not remove the images."]).join("; "), 7000);
          return;
        }
      } catch {
        toast("err", "Could not remove the images.", 7000);
        return;
      }
      toast("ok", `Removed ${j.deleted} image${j.deleted === 1 ? "" : "s"} from “${dir}”. “Front pages only” will work now.`, 6000);
      afterFormChange(kind, S.forms[kind]);   // refresh the preview so the warning clears
    } catch (err) {
      toast("warn", err?.message || `Couldn’t check “${dir}”.`, 7000);
    } finally {
      scanning = false;
    }
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
        afterFormChange(kind, a);   // the preview follows the switch
      }
      input.disabled = true;
      note.textContent = `No offset is saved for “${paper}” yet. Record one on the Offset & calibration page to enable this switch.`;
    } else {
      input.disabled = false;
      note.textContent = row
        ? `Applies the “${paper}” row (x ${row.x}, y ${row.y}, angle ${row.angle}°) from the Offset & calibration page.`
        : `No paper specific row exists for “${paper}”. The saved global offset (x ${g.x}, y ${g.y}, angle ${g.angle}°) will be applied.`;
    }
  };
  refresh();
  card.addEventListener("change", refresh);
}
